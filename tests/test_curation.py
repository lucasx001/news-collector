import json
import unittest
from unittest.mock import Mock

from trendradar.ai.curation import curate_stats
from trendradar.ai.filter import AIFilter


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.selector = Mock()
        self.selector._extract_json = lambda response: AIFilter._extract_json(self.selector, response)
        self.selector._structured_chat_options.return_value = {}
        self.stats = [{"word": f"主题{topic}", "count": 3, "titles": [
            {"title": f"新闻{topic * 3 + i}", "source_name": "财联社深度"}
            for i in range(3)]} for topic in range(2)]

    def test_ranked_selection_enforces_both_limits(self):
        self.selector.client.chat.return_value = '{"selected_ids":[5,4,3,2,1,0]}'
        result = curate_stats(self.stats, self.selector, per_topic=2, total=3)
        self.assertEqual([g["count"] for g in result], [2, 1])
        self.assertEqual([t["title"] for g in result for t in g["titles"]],
                         ["新闻5", "新闻4", "新闻2"])
        self.assertEqual(len(self.stats[0]["titles"]), 3)

    def test_invalid_responses_fail_closed(self):
        for response in ['{}', '[]', '{"selected_ids":[99]}',
                         '{"selected_ids":[0,0]}', '{"selected_ids":[true]}', 'bad']:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.selector.client.chat.return_value = response
                curate_stats(self.stats, self.selector)

    def test_empty_selection_is_allowed(self):
        self.selector.client.chat.return_value = '{"selected_ids":[]}'
        self.assertEqual(curate_stats(self.stats, self.selector), [])

    def test_later_batches_compete_in_final_selection(self):
        stats = [{"word": "科技", "titles": [{"title": str(i)} for i in range(401)]}]
        def choose(messages, **kwargs):
            batch = json.loads(messages[-1]["content"])
            return json.dumps({"selected_ids": [batch[-1]["id"]]})
        self.selector.client.chat.side_effect = choose
        result = curate_stats(stats, self.selector, total=1)
        self.assertEqual(result[0]["titles"][0]["title"], "400")
        self.assertEqual(self.selector.client.chat.call_count, 4)

    def test_invalid_limits(self):
        with self.assertRaises(ValueError):
            curate_stats(self.stats, self.selector, per_topic=0)
