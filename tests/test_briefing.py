import copy
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from trendradar.context import AppContext
from trendradar.core.loader import load_config
from trendradar.core.briefing import BriefingRunner, notification_targets, CHANNEL_KEYS
from trendradar.storage.briefing import BriefingStateStore
from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.base import NewsData, NewsItem
from trendradar.ai.filter import AIFilter
from trendradar.ai import AIAnalysisResult
from trendradar.report.formatter import format_title_for_platform
from trendradar.report.helpers import preferred_news_url


ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


class NewsLinkTests(unittest.TestCase):
    def test_cls_prefers_web_detail_over_app_share_url(self):
        title_data = {
            "title": "财联社测试新闻",
            "source_id": "cls-telegraph",
            "source_name": "财联社电报",
            "url": "https://www.cls.cn/detail/123456",
            "mobile_url": "https://api3.cls.cn/share/subject/123456?os=web&sv=859",
            "ranks": [1],
            "rank_threshold": 5,
            "time_display": "08:00",
            "count": 1,
        }

        self.assertEqual(preferred_news_url(title_data), title_data["url"])
        formatted = format_title_for_platform("wework", title_data)
        self.assertIn(title_data["url"], formatted)
        self.assertNotIn(title_data["mobile_url"], formatted)

    def test_other_sources_keep_mobile_url_priority(self):
        title_data = {
            "title": "普通来源测试新闻",
            "source_id": "thepaper",
            "source_name": "澎湃新闻",
            "url": "https://example.com/desktop",
            "mobile_url": "https://example.com/mobile",
        }

        self.assertEqual(preferred_news_url(title_data), title_data["mobile_url"])


class BriefingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {}, clear=True):
            cls.base_config = load_config(str(ROOT / "config/config.yaml"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.previous = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.previous)
        self.config = copy.deepcopy(self.base_config)
        self.config["PLATFORMS"] = [{"id": "a", "name": "来源A"}, {"id": "b", "name": "来源B"}]
        self.config["AI_ANALYSIS"]["ENABLED"] = False
        for keys in CHANNEL_KEYS.values():
            for key in keys:
                self.config[key] = ""
        self.config["FEISHU_WEBHOOK_URL"] = "https://test.invalid/one;https://test.invalid/two"
        self.now = datetime(2026, 9, 18, 21, tzinfo=TZ)
        self.backend = LocalStorageBackend(data_dir=self.temp.name)
        self.addCleanup(self.backend.cleanup)
        self.manager = SimpleNamespace(get_backend=lambda: self.backend, _resolve_backend_type=lambda: "local")
        self.ctx = AppContext(self.config)
        self.ctx.get_time = lambda: self.now
        self.ctx._storage_manager = self.manager
        self.results = {}
        self.analyzer = SimpleNamespace(ctx=self.ctx, storage_manager=self.manager, proxy_url=None,
                                        _crawl_data=lambda: (self.results, {"a": "来源A", "b": "来源B"}, []))
        self.sent = []
        self.failed_destinations = set()
        self.dispatch = patch("trendradar.core.briefing.NotificationDispatcher.dispatch_all", autospec=True,
                              side_effect=self.send).start()
        self.addCleanup(patch.stopall)
        self.original_build = BriefingRunner._build_stats
        self.build = patch.object(BriefingRunner, "_build_stats", autospec=True, side_effect=self.build_stats).start()

    def send(self, dispatcher, **kwargs):
        destination = dispatcher.config["FEISHU_WEBHOOK_URL"]
        self.sent.append((destination, copy.deepcopy(kwargs)))
        return {"feishu": destination not in self.failed_destinations}

    @staticmethod
    def build_stats(runner, items):
        return [{"word": "科技", "count": len(items), "titles": [runner._title(i) for i in items]}]

    def run_at(self, stamp, results=None):
        self.now = datetime.fromisoformat(stamp).replace(tzinfo=TZ)
        self.results = results or {}
        BriefingRunner(self.analyzer).run()

    def item(self, title="芯片新闻", url="https://example.com/1", source="a"):
        return {source: {title: {"url": url, "ranks": [1]}}}

    def state(self):
        return BriefingStateStore(self.backend).load()

    def titles(self, call):
        return [t["title"] for s in call[1]["report_data"]["stats"] for t in s["titles"]]

    def test_cross_midnight_silence_and_same_source_dedup(self):
        self.run_at("2026-09-18T21:00", self.item())
        self.run_at("2026-09-19T02:00", self.item("第二条", "https://example.com/2"))
        self.assertFalse(self.build.called)
        self.assertFalse(self.sent)
        self.run_at("2026-09-19T08:00", self.item())
        self.assertEqual(self.titles(self.sent[0]), ["芯片新闻", "第二条"])
        self.assertEqual(len(self.sent), 2)
        self.assertIn("09-18 20:00", self.sent[0][1]["report_type"])
        self.run_at("2026-09-19T08:30", self.item())
        self.run_at("2026-09-19T14:00", self.item())
        self.run_at("2026-09-19T20:00", self.item())
        self.assertEqual(len(self.sent), 2)
        html = Path("output/html/briefings/2026-09-19_08-00.html").read_text(encoding="utf-8")
        self.assertIn("早间简报", html)

    def test_different_sources_preserved_and_day_news_only_in_evening(self):
        self.run_at("2026-09-18T21:00", self.item())
        self.run_at("2026-09-19T08:00", self.item(source="b"))
        self.assertEqual(self.titles(self.sent[0]), ["芯片新闻", "芯片新闻"])
        self.run_at("2026-09-19T08:30", self.item("白天新闻", "https://example.com/day"))
        self.assertEqual(len(self.sent), 2)
        self.run_at("2026-09-19T20:00")
        self.assertEqual(self.titles(self.sent[2]), ["白天新闻"])

    def test_partial_failure_retries_only_failed_recipient_after_restart(self):
        self.failed_destinations = {"https://test.invalid/two"}
        self.run_at("2026-09-18T21:00", self.item())
        self.run_at("2026-09-19T08:00")
        self.assertIsNotNone(self.state()["flight"])
        self.failed_destinations.clear()
        self.run_at("2026-09-19T08:30")
        self.assertEqual([c[0] for c in self.sent], ["https://test.invalid/one", "https://test.invalid/two", "https://test.invalid/two"])
        self.assertEqual(self.build.call_count, 1)
        self.assertIsNone(self.state()["flight"])

    def test_failure_outside_window_preserves_queue(self):
        self.failed_destinations = {"https://test.invalid/one", "https://test.invalid/two"}
        self.run_at("2026-09-18T21:00", self.item())
        self.run_at("2026-09-19T08:00")
        self.run_at("2026-09-19T09:00")
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.state()["pending"])
        self.assertFalse(self.state()["seen"])

    def test_ai_failure_does_not_consume_news(self):
        self.build.side_effect = RuntimeError("AI unavailable")
        self.run_at("2026-09-18T21:00", self.item())
        with self.assertRaisesRegex(RuntimeError, "AI unavailable"):
            self.run_at("2026-09-19T08:00")
        self.assertTrue(self.state()["pending"])
        self.assertFalse(self.state()["seen"])
        self.assertIsNone(self.state()["lease"])
        self.assertFalse(self.sent)

    def test_bootstrap_reads_yesterday_database(self):
        self.backend.save_news_data(NewsData(date="2026-09-18", crawl_time="23:00", items={
            "a": [NewsItem(title="昨晚新闻", source_id="a", url="https://example.com/night", rank=1)]
        }, id_to_name={"a": "来源A"}))
        self.run_at("2026-09-19T08:00")
        self.assertEqual(self.titles(self.sent[0]), ["昨晚新闻"])

    def test_active_lease_skips_crawl_and_send(self):
        self.run_at("2026-09-18T21:00", self.item())
        store = BriefingStateStore(self.backend)
        state = store.load()
        state["lease"] = {"owner": "another", "until": "2026-09-19T08:10:00+08:00"}
        store.save(state)
        self.analyzer._crawl_data = Mock(side_effect=AssertionError("must not crawl"))
        self.run_at("2026-09-19T08:00")
        self.assertFalse(self.sent)

    def test_bad_state_fails_closed(self):
        path = BriefingStateStore(self.backend).path
        path.parent.mkdir(parents=True)
        path.write_text('{"version": 99}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_at("2026-09-19T08:00")
        self.assertFalse(self.sent)

    def test_targets_split_pairs_without_leaking_secrets(self):
        config = {"TELEGRAM_BOT_TOKEN": "bot1:secret;bot2:secret", "TELEGRAM_CHAT_ID": "1;2"}
        targets = notification_targets(config)
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[1][2]["TELEGRAM_CHAT_ID"], "2")
        self.assertNotIn("secret", targets[0][0])

    def test_real_classifier_and_analysis_use_only_queue_snapshot(self):
        self.build.side_effect = self.original_build
        self.config["AI_ANALYSIS"]["ENABLED"] = True
        self.config["AI"]["API_KEY"] = "test-only"
        with patch("trendradar.core.briefing.AIFilter") as selector_class, patch("trendradar.core.briefing.AIAnalyzer") as analysis_class:
            selector = selector_class.return_value
            selector.client.validate_config.return_value = (True, "")
            selector.load_interests_content.return_value = "科技"
            selector.compute_interests_hash.return_value = "interests-hash"
            selector.extract_tags.return_value = [{"tag": "科技"}]
            selector.batch_size = 200
            selector.classify_batch.return_value = [{"news_item_id": 0, "tag_id": 1, "relevance_score": 0.9}]
            analysis_class.return_value.analyze.return_value = AIAnalysisResult(success=True, core_trends="芯片产业变化")
            self.run_at("2026-09-18T21:00", self.item())
            selector_class.assert_not_called()
            analysis_class.assert_not_called()
            self.run_at("2026-09-19T08:00")
            self.assertEqual(self.titles(self.sent[0]), ["芯片新闻"])
            self.assertTrue(selector.classify_batch.call_args.kwargs["strict"])
            self.assertIn("芯片新闻", analysis_class.return_value.analyze.call_args.kwargs["stats"][0]["titles"][0]["title"])
            self.assertEqual(self.sent[0][1]["ai_analysis"].core_trends, "芯片产业变化")

    def test_news_saved_before_queue_crash_is_recovered(self):
        self.run_at("2026-09-18T21:00")
        self.backend.save_news_data(NewsData(date="2026-09-18", crawl_time="23:00", items={
            "a": [NewsItem(title="待恢复新闻", source_id="a", url="https://example.com/recover", rank=1)]
        }, id_to_name={"a": "来源A"}))
        self.run_at("2026-09-19T08:00")
        self.assertEqual(self.titles(self.sent[0]), ["待恢复新闻"])

    def test_same_url_with_changed_title_is_not_resent(self):
        self.run_at("2026-09-18T21:00", self.item())
        self.run_at("2026-09-19T08:00")
        self.run_at("2026-09-19T14:00", self.item("改写的芯片标题"))
        self.run_at("2026-09-19T20:00")
        self.assertEqual(len(self.sent), 2)

    def test_slow_analysis_does_not_send_after_window(self):
        def slow_build(runner, items):
            self.now = self.now.replace(hour=9, minute=1)
            return self.build_stats(runner, items)
        self.build.side_effect = slow_build
        self.run_at("2026-09-18T21:00", self.item())
        self.run_at("2026-09-19T08:30")
        self.assertFalse(self.sent)
        self.assertIsNotNone(self.state()["flight"])

    def test_main_routes_briefing_without_old_pipeline(self):
        from trendradar.__main__ import NewsAnalyzer
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = Mock(config=self.config)
        analyzer._initialize_and_check_config = Mock(return_value=True)
        analyzer._crawl_data = Mock(side_effect=AssertionError("old pipeline"))
        with patch("trendradar.core.briefing.BriefingRunner") as runner:
            NewsAnalyzer.run(analyzer)
            runner.assert_called_once_with(analyzer)
            runner.return_value.run.assert_called_once()
            analyzer.ctx.cleanup.assert_called_once()

    def test_remote_fallback_is_rejected(self):
        self.manager._resolve_backend_type = lambda: "remote"
        with self.assertRaisesRegex(RuntimeError, "回退"):
            BriefingRunner(self.analyzer)


class RemoteStateTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.backend = SimpleNamespace(backend_name="remote", bucket_name="test", s3_client=self.client)

    def test_remote_restart_and_conditional_save(self):
        state = {"version": 1, "since": "2026-09-18T20:00:00+08:00", "pending": {}, "seen": ["receipt"]}
        self.client.get_object.return_value = {"ETag": '"v1"', "Body": io.BytesIO(json.dumps(state).encode())}
        self.client.put_object.return_value = {"ETag": '"v2"'}
        store = BriefingStateStore(self.backend)
        self.assertEqual(store.load()["seen"], ["receipt"])
        store.save(state)
        self.assertEqual(self.client.put_object.call_args.kwargs["IfMatch"], '"v1"')
        self.assertEqual(store.etag, '"v2"')

    def test_permission_failure_never_looks_like_empty_state(self):
        self.client.get_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
        with self.assertRaises(ClientError):
            BriefingStateStore(self.backend).load()

    def test_missing_key_initializes_with_create_condition(self):
        self.client.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        self.client.put_object.return_value = {"ETag": '"new"'}
        store = BriefingStateStore(self.backend)
        self.assertIsNone(store.load())
        store.save({"version": 1})
        self.assertEqual(self.client.put_object.call_args.kwargs["IfNoneMatch"], "*")

    def test_conflicting_writer_is_not_overwritten(self):
        self.client.put_object.side_effect = ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        store = BriefingStateStore(self.backend)
        store.etag = '"old"'
        with self.assertRaises(ClientError):
            store.save({"version": 1})
        self.assertEqual(store.etag, '"old"')


class StrictFilterTests(unittest.TestCase):
    def test_tag_extraction_uses_json_mode_and_retries_empty_response(self):
        selector = AIFilter.__new__(AIFilter)
        selector.client = Mock()
        selector.client.model = "deepseek/deepseek-flash"
        selector.extract_system = ""
        selector.extract_user = "{interests_content}"
        selector.debug = False
        selector.client.chat.side_effect = [
            "",
            '{"tags":[{"tag":"宏观经济","description":"政策与数据"}]}',
        ]
        self.assertEqual(
            selector.extract_tags("关注政策与宏观经济"),
            [{"tag": "宏观经济", "description": "政策与数据"}],
        )
        first_kwargs = selector.client.chat.call_args_list[0].kwargs
        self.assertEqual(first_kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(first_kwargs["extra_body"], {"thinking": {"type": "disabled"}})

    def test_tag_parser_accepts_json_variants_and_surrounding_text(self):
        selector = AIFilter.__new__(AIFilter)
        self.assertEqual(
            selector._parse_tags_response(
                '先说明一下：{"labels":[{"name":"宏观经济","summary":"政策与数据"}]}'
            ),
            [{"tag": "宏观经济", "description": "政策与数据"}],
        )
        self.assertEqual(
            selector._parse_tags_response('[{"tag":"科技","description":"AI 与芯片"}]'),
            [{"tag": "科技", "description": "AI 与芯片"}],
        )

    def test_invalid_response_raises_and_empty_array_is_valid(self):
        selector = AIFilter.__new__(AIFilter)
        selector.client = Mock()
        selector.classify_system = "classify"
        selector.classify_user = "{news_list}"
        selector.debug = False
        titles = [{"id": 0, "title": "新闻"}]
        tags = [{"id": 1, "tag": "科技"}]
        selector.client.chat.return_value = "not json"
        with self.assertRaises(ValueError):
            selector.classify_batch(titles, tags, strict=True)
        selector.client.chat.return_value = "[]"
        self.assertEqual(selector.classify_batch(titles, tags, strict=True), [])
        selector.client.chat.return_value = '{"matches":[{"id":0,"tag_id":1,"score":0.9}]}'
        self.assertEqual(
            selector.classify_batch(titles, tags, strict=True),
            [{"news_item_id": 0, "tag_id": 1, "relevance_score": 0.9}],
        )


if __name__ == "__main__":
    unittest.main()
