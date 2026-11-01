"""Offline fixture feedback is narrow, explicit and never invents exclusions."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from demo import ChatDemoLLM
from webapp import ChatService


class DemoFeedbackTests(unittest.TestCase):
    def update(self, message):
        data = {'task': {'topic': 'RAG', 'excluded_paper_ids': ['previous-id']},
                'message': message,
                'last_recommendations': [{'paper_id': 'arxiv:1234.5678', 'index': 1},
                                         {'paper_id': 'arxiv:9876.5432', 'index': 2}]}
        return ChatDemoLLM().generate('update', '', data, {}, lambda value: None)['patch']

    def test_only_explicit_supported_actions_reference_delivered_papers(self):
        for message in ('请排除第一篇，并提供替代论文。',
                        '排除论文 arxiv:1234.5678（第一篇），尽量保留其余推荐。'):
            with self.subTest(message=message):
                self.assertEqual(self.update(message)['excluded_paper_ids'],
                                 ['previous-id', 'arxiv:1234.5678'])
        for message in ('不要排除第一篇', '考虑是否排除第一篇',
                        '排除论文 arxiv:9999.9999（未交付的论文）'):
            with self.subTest(message=message):
                self.assertNotIn('excluded_paper_ids', self.update(message))

    def test_demo_chat_applies_explicit_exclusion_without_inventing_a_replacement(self):
        with TemporaryDirectory() as directory:
            service = ChatService(output=Path(directory))
            try:
                created = service.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
                session_id = created['session_id']
                excluded = created['result']['papers'][0]['id']
                result = service.send(session_id, {'message': '请排除第一篇，并提供替代论文。'})['result']
                self.assertIn(excluded, result['request']['excluded_paper_ids'])
                self.assertNotIn(excluded, [paper['id'] for paper in result['papers']])
                self.assertTrue(all(paper['id'].startswith('arxiv:') for paper in result['papers']))
                if len(result['papers']) < result['request']['target_count']:
                    self.assertNotEqual(result['status'], 'complete')
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
