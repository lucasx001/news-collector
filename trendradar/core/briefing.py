"""Twice-daily, cross-date briefings with durable per-recipient delivery receipts.

The queue is independent of daily SQLite files. Only successful deliveries consume
it. Identity includes the source: reports from different sources are preserved.
"""

import copy
import hashlib
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from trendradar.ai import AIAnalyzer, AIAnalysisResult
from trendradar.ai.filter import AIFilter
from trendradar.ai.curation import curate_stats
from trendradar.crawler.fetcher import DataFetcher
from trendradar.core.config import parse_multi_account_config, get_account_at_index
from trendradar.notification.dispatcher import NotificationDispatcher
from trendradar.storage.briefing import BriefingStateStore


CHANNEL_KEYS = {
    "feishu": ("FEISHU_WEBHOOK_URL",),
    "dingtalk": ("DINGTALK_WEBHOOK_URL",),
    "wework": ("WEWORK_WEBHOOK_URL",),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "ntfy": ("NTFY_TOPIC", "NTFY_TOKEN"),
    "bark": ("BARK_URL",),
    "slack": ("SLACK_WEBHOOK_URL",),
    "generic_webhook": ("GENERIC_WEBHOOK_URL",),
    "email": ("EMAIL_TO",),
}


def digest_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def identities(source, title, url):
    """Either the same URL or normalized title identifies a repeat within a source."""
    values = ["title:" + " ".join(title.split()).casefold()]
    if url:
        values.append("url:" + url.strip())
    return [digest_hash(source + "\0" + value) for value in values]


def notification_targets(config):
    """Split existing channels into individual recipients; never persist secrets."""
    targets = []
    maximum = config.get("MAX_ACCOUNTS_PER_CHANNEL", 3)
    for channel, keys in CHANNEL_KEYS.items():
        accounts = parse_multi_account_config(config.get(keys[0], ""))
        if not accounts:
            continue
        if channel == "email" and not (config.get("EMAIL_FROM") and config.get("EMAIL_PASSWORD")):
            continue
        if channel == "ntfy" and not config.get("NTFY_SERVER_URL"):
            continue
        paired = parse_multi_account_config(config.get(keys[1], "")) if len(keys) > 1 else []
        if channel == "telegram" and len(paired) != len(accounts):
            raise ValueError("Telegram token 和 chat_id 数量不一致")
        if channel == "ntfy" and paired and len(paired) != len(accounts):
            raise ValueError("ntfy topic 和 token 数量不一致")
        for i, account in enumerate(accounts[:maximum]):
            if not account:
                continue
            isolated = copy.deepcopy(config)
            for other_keys in CHANNEL_KEYS.values():
                for key in other_keys:
                    isolated[key] = ""
            isolated[keys[0]] = account
            if len(keys) > 1:
                isolated[keys[1]] = get_account_at_index(paired, i, "")
            # Scope the receipt to the destination. Credentials never enter state.
            destination = account
            if channel == "telegram":
                destination = account.split(":", 1)[0] + ":" + isolated[keys[1]]
            elif channel == "ntfy":
                destination = config["NTFY_SERVER_URL"] + "/" + account
            target_id = digest_hash(channel + ":" + destination)
            targets.append((target_id, channel, isolated))
    return targets


class BriefingRunner:
    def __init__(self, analyzer):
        self.analyzer = analyzer
        self.ctx = analyzer.ctx
        self.config = self.ctx.config
        self.backend = analyzer.storage_manager.get_backend()
        if (analyzer.storage_manager._resolve_backend_type() == "remote"
                and self.backend.backend_name != "remote"):
            raise RuntimeError("远程存储不可用，简报不允许回退到本地以免重复发送")
        self.store = BriefingStateStore(self.backend)
        self.scheduler = self.ctx.create_scheduler()
        if not self.scheduler.enabled:
            raise ValueError("briefing 模式需要启用 schedule")
        if self.ctx.rss_enabled:
            raise ValueError("当前 briefing 模式仅处理平台新闻，请关闭 rss.enabled")
        self.owner = uuid.uuid4().hex
        self.state = None

    def _save(self, release=False):
        self.state["lease"] = None if release else {
            "owner": self.owner,
            "until": (self.ctx.get_time() + timedelta(minutes=15)).isoformat(),
        }
        self.store.save(self.state)

    def _cutoffs(self, now):
        """Use actual timeline starts; no second copy of the delivery schedule."""
        cutoffs = []
        timeline = self.scheduler.timeline
        for offset in (-2, -1, 0):
            day = now + timedelta(days=offset)
            plan = timeline["day_plans"][timeline["week_map"][day.isoweekday()]]
            for key in plan["periods"]:
                period = timeline["periods"][key]
                if not period.get("push", timeline["default"].get("push", False)):
                    continue
                hour, minute = map(int, period["start"].split(":"))
                cutoff = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if cutoff <= now:
                    cutoffs.append(cutoff)
        if not cutoffs:
            raise ValueError("briefing 时间线必须包含推送时段")
        return sorted(set(cutoffs))

    def _ingest(self, results, names, when):
        pending = self.state["pending"]
        seen = set(self.state["seen"])
        aliases = {alias: key for key, item in pending.items() for alias in item["identities"]}
        for source, titles in results.items():
            if source not in self.ctx.platform_ids:
                continue
            for title, info in titles.items():
                if not title.strip():
                    continue
                ids = identities(source, title, info.get("url", ""))
                if seen.intersection(ids):
                    # Remember title/URL aliases even after the original item was sent.
                    seen.update(ids)
                    continue
                existing = next((aliases[k] for k in ids if k in aliases), None)
                if existing:
                    item = pending[existing]
                    item["identities"] = sorted(set(item["identities"]) | set(ids))
                    aliases.update({alias: existing for alias in ids})
                    continue
                if when <= datetime.fromisoformat(self.state["since"]):
                    seen.update(ids)
                    continue
                key = ids[-1]
                pending[key] = {
                    "identities": ids, "source_id": source,
                    "source_name": names.get(source, source), "title": title,
                    "url": info.get("url", ""), "mobileUrl": info.get("mobileUrl", ""),
                    "ranks": info.get("ranks", []), "first_seen": when.isoformat(),
                }
                aliases.update({alias: key for alias in ids})
        self.state["seen"] = sorted(seen)

    def _bootstrap(self, now):
        """Recover news committed to SQLite before an interrupted queue update."""
        for day in (now - timedelta(days=1), now):
            data = self.backend.get_today_all_data(day.strftime("%Y-%m-%d"))
            if not data:
                continue
            for source, items in data.items.items():
                for item in items:
                    stamp = item.first_time or item.crawl_time
                    if " " in stamp or "T" in stamp:
                        observed = datetime.fromisoformat(stamp)
                        if observed.tzinfo is None:
                            if hasattr(now.tzinfo, "localize"):
                                observed = now.tzinfo.localize(observed)
                            else:
                                observed = observed.replace(tzinfo=now.tzinfo)
                    else:
                        stamp = stamp.replace("时", ":").replace("分", "").replace("-", ":")
                        hour, minute = map(int, stamp.split(":")[:2])
                        observed = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    self._ingest({source: {item.title: {
                        "url": item.url, "mobileUrl": item.mobile_url,
                        "ranks": item.ranks or [item.rank],
                    }}}, data.id_to_name, observed)

    def run(self):
        now = self.ctx.get_time()
        schedule = self.scheduler.resolve()
        cutoffs = self._cutoffs(now)
        self.state = self.store.load()
        fresh = self.state is None
        if fresh:
            since = cutoffs[-2] if schedule.push and len(cutoffs) > 1 else cutoffs[-1]
            self.state = {"version": 1, "since": since.isoformat(), "seen": [],
                          "pending": {}, "flight": None, "last_slot": None}
        lease = self.state.get("lease")
        if lease and datetime.fromisoformat(lease["until"]) > now:
            print("[简报] 另一轮任务仍持有状态锁，跳过")
            return
        self._save()  # Claim before crawling/AI/sending; remote CAS rejects stale writers.
        try:
            self._bootstrap(now)
            if schedule.collect:
                results, names, failed = self.analyzer._crawl_data()
                # Use the run start time, so a crawl started at 08:00 belongs to the morning.
                self._ingest(results, names, now)
                self._save()
            else:
                failed = []
            if not schedule.push or not self.config["ENABLE_NOTIFICATION"]:
                print("[简报] 静默采集完成，不调用 AI、不生成报告、不推送")
                return
            cutoff = cutoffs[-1]
            slot = cutoff.isoformat()
            if self.state["last_slot"] == slot:
                print("[简报] 当前时段已完成推送")
                return
            targets = notification_targets(self.config)
            if not targets:
                print("[简报] 未配置通知渠道，保留待推送新闻")
                return
            if self.state["flight"] is None:
                standalone = self._standalone_snapshot()
                keys = [key for key, item in self.state["pending"].items()
                        if datetime.fromisoformat(item["first_seen"]) <= cutoff]
                if not keys and not standalone:
                    print("[简报] 本时段没有新增新闻")
                    return
                start = self.state["since"]
                label = f"{schedule.period_name}（{datetime.fromisoformat(start):%m-%d %H:%M} 至 {cutoff:%m-%d %H:%M}）"
                self.state["flight"] = {
                    "keys": keys, "cutoff": slot, "label": label,
                    "stats": None, "analysis": None, "delivered": [],
                    "failed_ids": failed,
                    "standalone": standalone,
                }
                self._save()
            flight = self.state["flight"]
            if flight["stats"] is None:
                flight["stats"] = self._build_stats([self.state["pending"][k] for k in flight["keys"]]) if flight["keys"] else []
                self._save()
            curated = self.config.get("BRIEFING", {})
            if (curated.get("curation_enabled", False) and not flight.get("curated")
                    and not flight["delivered"]):
                selector = AIFilter(self.config["AI"], self.ctx.ai_filter_config, self.ctx.get_time)
                flight["stats"] = curate_stats(
                    flight["stats"], selector,
                    per_topic=curated.get("max_per_topic", 2),
                    total=curated.get("max_total", 12), heartbeat=self._save,
                )
                flight["analysis"] = None
                flight["curated"] = True
                self._save()
            if flight["stats"] or flight.get("standalone"):
                self._deliver(flight, targets, schedule)
                if not all(target[0] in flight["delivered"] for target in targets):
                    print("[简报] 部分推送未成功，保留快照，下次窗口内重试失败的接收方")
                    return
            # Includes explicitly filtered-out items, so the same noise isn't reclassified.
            seen = set(self.state["seen"])
            for key in flight["keys"]:
                seen.update(self.state["pending"].pop(key)["identities"])
            self.state["seen"] = sorted(seen)
            self.state["since"] = flight["cutoff"]
            self.state["last_slot"] = slot
            self.state["flight"] = None
            self._save()
        finally:
            self._save(release=True)

    def _standalone_snapshot(self):
        """Fetch current boards only at delivery time and freeze them for retries."""
        display = self.config.get("DISPLAY", {})
        if not display.get("REGIONS", {}).get("STANDALONE", False):
            return None
        settings = display.get("STANDALONE", {})
        sources = settings.get("PLATFORMS", [])
        if not sources:
            return None
        names = {"cls-hot": "财联社热门", "wallstreetcn-hot": "华尔街见闻热门",
                 "thepaper": "澎湃新闻"}
        names.update({p["id"]: p.get("name", p["id"]) for p in self.config["PLATFORMS"]})
        results, source_names, failed = DataFetcher(self.analyzer.proxy_url).crawl_websites(
            [(source, names.get(source, source)) for source in dict.fromkeys(sources)],
            request_interval=self.config.get("REQUEST_INTERVAL", 2000),
        )
        if failed:
            raise RuntimeError(f"独立热榜获取失败，保留简报等待重试: {', '.join(failed)}")
        boards = []
        limit = settings.get("MAX_ITEMS", 0)
        stamp = self.ctx.get_time().strftime("%m-%d %H:%M")
        for source in dict.fromkeys(sources):
            items = [{"title": title, "url": info.get("url", ""),
                      "mobileUrl": info.get("mobileUrl", ""),
                      "ranks": info.get("ranks", []), "count": 1,
                      "first_time": stamp, "last_time": stamp}
                     for title, info in results.get(source, {}).items()]
            if limit > 0:
                items = items[:limit]
            if items:
                boards.append({"id": source, "name": source_names.get(source, source), "items": items})
        print(f"[独立热榜] 已获取 {len(boards)} 个平台，共 {sum(len(b['items']) for b in boards)} 条（不经AI精选）")
        return {"platforms": boards, "rss_feeds": []} if boards else None

    def _build_stats(self, items):
        if self.ctx.filter_method == "ai":
            selector = AIFilter(self.config["AI"], self.ctx.ai_filter_config, self.ctx.get_time)
            valid, error = selector.client.validate_config()
            if not valid:
                raise ValueError(error)
            interests = selector.load_interests_content()
            if not interests:
                raise ValueError("简报兴趣文件为空")
            cache_key = selector.compute_interests_hash(interests)
            cache = self.state.get("tags", {})
            if cache.get("hash") != cache_key:
                tags = selector.extract_tags(interests)
                if not tags:
                    raise ValueError("简报 AI 标签提取失败，保留新闻等待重试")
                cache = {"hash": cache_key, "values": [dict(t, id=i + 1) for i, t in enumerate(tags)]}
                self.state["tags"] = cache
                self._save()
            tags = cache["values"]
            groups = {t["id"]: {"word": t["tag"], "titles": [], "count": 0} for t in tags}
            size = max(1, selector.batch_size)
            for offset in range(0, len(items), size):
                batch = [{"id": i, "title": item["title"], "source": item["source_name"]}
                         for i, item in enumerate(items[offset:offset + size], offset)]
                matches = selector.classify_batch(batch, tags, interests, strict=True)
                for match in matches:
                    if match["relevance_score"] >= self.ctx.ai_filter_config.get("MIN_SCORE", 0.7):
                        groups[match["tag_id"]]["titles"].append(self._title(items[match["news_item_id"]]))
                self._save()  # Renew lease between bounded model calls.
                if offset + size < len(items):
                    time.sleep(self.ctx.ai_filter_config.get("BATCH_INTERVAL", 2))
            stats = [group for group in groups.values() if group["titles"]]
        else:
            # Keep the existing keyword grammar and first-match grouping behavior.
            words, filters, global_filters = self.ctx.load_frequency_words()
            results, names = {}, {}
            for item in items:
                results.setdefault(item["source_id"], {})[item["title"]] = item
                names[item["source_id"]] = item["source_name"]
            stats, _ = self.ctx.count_frequency(results, words, filters, names,
                                                mode="daily", global_filters=global_filters)
        for group in stats:
            group["count"] = len(group["titles"])
        return stats

    def _title(self, item):
        return {**item, "time_display": datetime.fromisoformat(item["first_seen"]).strftime("%m-%d %H:%M"),
                "first_time": item["first_seen"], "last_time": item["first_seen"],
                "rank_threshold": self.ctx.rank_threshold, "count": 1, "is_new": False}

    def _deliver(self, flight, targets, schedule):
        stats = flight["stats"]
        if (stats and flight["analysis"] is None and schedule.analyze
                and self.config.get("AI_ANALYSIS", {}).get("ENABLED", False)):
            analysis_config = dict(self.config["AI_ANALYSIS"], MODE="follow_report")
            result = AIAnalyzer(self.config["AI"], analysis_config, self.ctx.get_time).analyze(
                stats=stats, rss_stats=None, report_mode="daily", report_type=flight["label"],
                platforms=list(dict.fromkeys(t["source_name"] for s in stats for t in s["titles"])),
                keywords=[s["word"] for s in stats],
            )
            if not result.success:
                raise RuntimeError(f"简报 AI 分析失败，保留快照: {result.error}")
            flight["analysis"] = asdict(result)
            self._save()
        ai = AIAnalysisResult(**flight["analysis"]) if flight["analysis"] else None
        report_data = self.ctx.prepare_report(stats, flight["failed_ids"], {}, {}, "daily")
        report_data["briefing_title"] = flight["label"]
        total = sum(s["count"] for s in stats)
        html_content = self.ctx.render_html(report_data, total, "daily", ai_analysis=ai,
                                            standalone_data=flight.get("standalone"))
        html_dir = Path("output/html/briefings")
        html_dir.mkdir(parents=True, exist_ok=True)
        filename = datetime.fromisoformat(flight["cutoff"]).strftime("%Y-%m-%d_%H-%M.html")
        html_path = html_dir / filename
        html_path.write_text(html_content, encoding="utf-8")
        Path("output/index.html").write_text(html_content, encoding="utf-8")
        if self.backend.backend_name == "remote":
            self.backend.s3_client.put_object(
                Bucket=self.backend.bucket_name, Key=f"html/briefings/{filename}",
                Body=html_content.encode("utf-8"), ContentType="text/html; charset=utf-8",
            )
        for target_id, channel, isolated in targets:
            if target_id in flight["delivered"]:
                continue
            self._save()
            # Stop retries outside the configured window, including long model calls.
            current = self.scheduler.resolve()
            if not current.push or current.period_key != schedule.period_key:
                print("[简报] 推送窗口已结束，保留报告至下一窗口")
                return
            dispatcher = NotificationDispatcher(isolated, self.ctx.get_time, self.ctx.split_content)
            results = dispatcher.dispatch_all(
                report_data=report_data, report_type=flight["label"], mode="daily",
                html_file_path=str(html_path), ai_analysis=ai, proxy_url=self.analyzer.proxy_url,
                standalone_data=flight.get("standalone"),
            )
            if results.get(channel):
                flight["delivered"].append(target_id)
                self._save()
