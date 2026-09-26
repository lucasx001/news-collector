import unittest
from unittest.mock import Mock, patch

from trendradar.crawler.fetcher import DataFetcher


class DataFetcher36KrTests(unittest.TestCase):
    def test_official_feed_keeps_36kr_source_in_normal_crawl(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><title>36氪</title>
        <item><title>测试快讯</title><link>https://36kr.com/newsflashes/123</link></item>
        </channel></rss>""".encode("utf-8")
        response = Mock(content=feed)
        response.raise_for_status.return_value = None

        with patch("trendradar.crawler.fetcher.requests.get", return_value=response) as get:
            results, names, failed = DataFetcher().crawl_websites(
                [("36kr-quick", "36氪快讯")]
            )

        self.assertEqual(failed, [])
        self.assertEqual(names["36kr-quick"], "36氪快讯")
        self.assertEqual(
            results["36kr-quick"]["测试快讯"]["url"],
            "https://36kr.com/newsflashes/123",
        )
        self.assertEqual(results["36kr-quick"]["测试快讯"]["ranks"], [1])
        self.assertEqual(get.call_args.args[0], DataFetcher.KR36_QUICK_FEED_URL)

    def test_empty_or_blocked_feed_is_reported_as_failed_source(self):
        response = Mock(content=b"<html>blocked</html>")
        response.raise_for_status.return_value = None

        with patch("trendradar.crawler.fetcher.requests.get", return_value=response):
            content, source_id, name = DataFetcher().fetch_data(
                ("36kr-quick", "36氪快讯"), max_retries=0
            )

        self.assertIsNone(content)
        self.assertEqual(source_id, "36kr-quick")
        self.assertEqual(name, "36氪快讯")


if __name__ == "__main__":
    unittest.main()
