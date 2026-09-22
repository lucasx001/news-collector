import json
import io
import unittest
from contextlib import redirect_stdout
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
                         '{"selected_ids":[true]}', 'bad']:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.selector.client.chat.return_value = response
                curate_stats(self.stats, self.selector)

    def test_empty_selection_is_allowed(self):
        self.selector.client.chat.return_value = '{"selected_ids":[]}'
        self.assertEqual(curate_stats(self.stats, self.selector), [])

    def test_failure_logs_identify_response_problem_and_preserve_input(self):
        cases = [
            ('', '未提取到非空JSON'),
            ('bad', 'JSON解析失败'),
            ('[]', 'JSON顶层必须为对象'),
            ('{}', '缺少selected_ids字段'),
            ('{"selected_ids":null}', 'selected_ids必须为数组'),
            ('{"selected_ids":["0",true,{}]}', 'ID必须为整数'),
            ('{"selected_ids":[99]}', 'ID不在本批候选中'),
        ]
        original = json.dumps(self.stats)
        for response, reason in cases:
            with self.subTest(response=response):
                self.selector.client.chat.return_value = response
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(ValueError):
                    curate_stats(self.stats, self.selector)
                log = output.getvalue()
                self.assertIn(reason, log)
                self.assertIn('[简报精选][请求1]', log)
                self.assertIn('AI原始响应摘要=', log)
                self.assertIn('提取JSON摘要=', log)
                self.assertEqual(json.dumps(self.stats), original)

    def test_response_preview_is_bounded_and_escapes_newlines(self):
        self.selector.client.chat.return_value = 'bad\n' + 'x' * 10000
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(ValueError):
            curate_stats(self.stats, self.selector)
        previews = [line for line in output.getvalue().splitlines() if '摘要=' in line]
        self.assertEqual(len(previews), 2)
        for line in previews:
            self.assertIn('bad\\n', line)
            self.assertIn('已截断', line)
            self.assertLess(len(line), 1600)

    def test_railway_response_uses_corrected_final_json(self):
        first = [24, 28, 46, 12, 158, 161, 162, 176, 159, 184, 174, 186,
                 107, 143, 160, 163, 194, 196, 175, 178, 128, 151, 47,
                 107, 11, 14, 34, 6, 18, 5, 44, 39, 20, 57, 140, 146]
        final = list(dict.fromkeys(first))
        self.selector.client.chat.return_value = (
            json.dumps({"selected_ids": first}) +
            '\n\nWait, I need to strictly output only JSON with selected_ids. '
            'Let me finalize carefully, ensuring IDs are valid and deduplicated, '
            'ranked by investment info value.\n\n' +
            json.dumps({"selected_ids": final}))
        stats = [{"word": "主题", "titles": [{"title": str(i)} for i in range(200)]}]
        output = io.StringIO()
        with redirect_stdout(output):
            result = curate_stats(stats, self.selector)
        self.assertEqual([t['title'] for t in result[0]['titles']], ['24', '28', '46', '12', '158'])
        self.assertIn('检测到2份精选JSON', output.getvalue())

    def test_last_answer_replaces_draft_even_when_ranking_changes(self):
        self.selector.client.chat.return_value = (
            '```json\n{"selected_ids":[0,1]}\n```\nCorrection:\n'
            '```json\n{"selected_ids":[5,4]}\n```')
        result = curate_stats(self.stats, self.selector)
        self.assertEqual([t['title'] for t in result[0]['titles']], ['新闻5', '新闻4'])

    def test_duplicate_ids_do_not_consume_topic_quota(self):
        self.selector.client.chat.return_value = '{"selected_ids":[2,2,1,0]}'
        output = io.StringIO()
        with redirect_stdout(output):
            result = curate_stats(self.stats, self.selector, per_topic=2)
        self.assertEqual([t['title'] for t in result[0]['titles']], ['新闻2', '新闻1'])
        self.assertIn('重复ID已按首次出现顺序去重', output.getvalue())

    def test_invalid_final_answer_is_not_replaced_with_valid_draft(self):
        for final in ['{"selected_ids":[99]}', '{"selected_ids":[true]}',
                      '{"selected_ids":null}']:
            with self.subTest(final=final), self.assertRaises(ValueError):
                self.selector.client.chat.return_value = '{"selected_ids":[0]}\n' + final
                curate_stats(self.stats, self.selector)

    def test_explicit_empty_final_answer_overrides_draft(self):
        self.selector.client.chat.return_value = '{"selected_ids":[0]}\n{"selected_ids":[]}'
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

    def test_unlimited_total_preserves_all_topics_across_batches(self):
        stats = [{"word": str(topic), "titles": [{"title": f"{topic}-{i}"}
                 for i in range(100)]} for topic in range(4)]
        def choose(messages, **kwargs):
            self.assertIn("所有主题合计不设条数上限", messages[0]["content"])
            batch = json.loads(messages[-1]["content"])
            return json.dumps({"selected_ids": [item["id"] for item in batch]})
        self.selector.client.chat.side_effect = choose
        result = curate_stats(stats, self.selector)
        self.assertEqual([group["count"] for group in result], [5, 5, 5, 5])
        self.assertEqual(sum(group["count"] for group in result), 20)
        self.assertEqual(self.selector.client.chat.call_count, 3)
