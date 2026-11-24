"""Delivered papers carry an explicit rank and null for unknown metadata."""
import unittest

from scholarguide.common import Paper
from scholarguide.demo import demo_run
from scholarguide.report import UNKNOWN_AS_NULL, delivered_papers, markdown


class DeliveryContractTests(unittest.TestCase):
    def test_rank_is_delivered_order_and_unknown_values_are_null(self):
        papers = [{'id': 'a', 'title': 'A', 'doi': '', 'venue': None, 'authors': 'not-a-list'},
                  {'id': 'b', 'title': 'B'},
                  {'id': 'c', 'title': 'C', 'citation_count': 0}]
        delivered = delivered_papers(papers)
        self.assertEqual([row['rank'] for row in delivered], [1, 2, 3])
        self.assertIsNone(delivered[0]['doi'])
        self.assertIsNone(delivered[0]['venue'])
        self.assertEqual(delivered[0]['authors'], [])
        self.assertIsNone(delivered[1]['doi'])
        self.assertIsNone(delivered[1]['year'])
        self.assertEqual(delivered[2]['citation_count'], 0)  # A real zero is not unknown.

    def test_rank_survives_reapplication_and_skips_non_objects(self):
        once = delivered_papers([{'title': 'A'}, 'broken', {'title': 'B', 'rank': 99}])
        self.assertEqual([row['rank'] for row in once], [1, 2])
        self.assertEqual([row['rank'] for row in delivered_papers(once)], [1, 2])

    def test_api_and_chat_public_shapes_keep_the_delivered_contract(self):
        from scholarguide.api_v1 import public_result
        from scholarguide.webapp import ChatService
        raw = {'status': 'complete', 'papers': [
            {'id': 'a', 'title': 'A', 'rank': 2, 'doi': None, 'venue': None},
            {'id': 'b', 'title': 'B', 'rank': 7, 'doi': '10.1/x', 'venue': 'ICML'}]}
        api = public_result(raw)['papers']
        self.assertEqual([paper['rank'] for paper in api], [2, 7])  # never renumbered
        self.assertIsNone(api[0]['doi'])
        web = ChatService._public(raw)['papers']
        self.assertEqual([paper['rank'] for paper in web], [2, 7])
        self.assertIsNone(web[0]['doi'])
        self.assertIsNone(web[0]['venue'])

    def test_demo_result_and_markdown_use_the_same_shape(self):
        result = demo_run('satisfied', 'A')
        self.assertTrue(result['papers'])
        for index, paper in enumerate(result['papers'], 1):
            self.assertEqual(paper['rank'], index)
            for key in UNKNOWN_AS_NULL:
                self.assertIn(key, paper)
        report = markdown(result)
        self.assertIn('## 1. ', report)

    def test_paper_only_export_unchanged(self):
        # Paper.to_dict keeps its internal shape; only delivery normalizes it.
        paper = Paper(id='arxiv:1', title='T', abstract='a', url='')
        self.assertNotIn('rank', paper.to_dict())


if __name__ == '__main__':
    unittest.main()
