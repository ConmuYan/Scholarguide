"""Partial, unassessed Lune pages survive cancellation without becoming recommendations."""
import tempfile
import unittest
from pathlib import Path

from api_v1 import public_result
from common import Budget, Config, Paper, RunCancelled
from demo import ChatDemoLLM, DemoLLM, DemoSource
from session import ChatSession
from source import SearchBatch, SourceRun
from state import StateStore
from workflow import run


class PartialSource(DemoSource):
    providers = ['lune']
    def __init__(self, budget):
        super().__init__()
        self.budget = budget
        self.calls = 0
    def search(self, query, limit=10):
        self.calls += 1
        paper = Paper('lune:first', 'First partial paper', 'Unassessed abstract.',
                      'https://fixture.invalid/paper', sources=['lune'])
        batch = SearchBatch(query, ['lune'], [paper],
                            [SourceRun('lune', 'partial', 1, cost_cny=None,
                                       remote_outcome='unknown', retry_safe=False)],
                            stop_reason='user_cancelled')
        error = RunCancelled()
        error.remote_outcome, error.retry_safe = 'unknown', False
        error.partial_batch = batch
        self.budget.cancel_token.request()
        raise error


class PartialProductTests(unittest.TestCase):
    def test_search_keeps_candidates_but_not_recommendations(self):
        budget = Budget(5)
        source = PartialSource(budget)
        result = run('Recommend five RAG papers.', Config(source='lune'), 'S',
                     source=source, llm=DemoLLM(), budget=budget)
        self.assertEqual(source.calls, 1)
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertEqual(result['papers'], [])
        self.assertEqual([p['id'] for p in result['candidates']], ['lune:first'])
        self.assertIsNone(result['cost']['tools'])
        public = public_result(result)
        self.assertEqual(public['papers'], [])
        self.assertEqual([p['paper_id'] for p in public['candidates']], ['lune:first'])
        self.assertNotIn('evidence', public['candidates'][0])

    def test_partial_batch_is_bound_to_query_provider_and_outcome(self):
        from common import GuideError
        from source import retained_partial_batch
        source = PartialSource(Budget(5))
        with self.assertRaises(RunCancelled) as caught:
            source.search('fixture', 1)
        error = caught.exception
        self.assertIsNotNone(retained_partial_batch(error, 'fixture', ['lune'], 1))
        self.assertIsNone(retained_partial_batch(error, 'other', ['lune'], 1))
        self.assertIsNone(retained_partial_batch(error, 'fixture', ['openalex'], 1))
        error.partial_batch.runs[-1].remote_outcome = 'not_started'
        self.assertIsNone(retained_partial_batch(error, 'fixture', ['lune'], 1))
        fake = GuideError('fixture')
        fake.partial_batch = {'papers': [error.partial_batch.papers[0]]}
        self.assertIsNone(retained_partial_batch(fake, 'fixture', ['lune'], 1))

    def test_partial_batch_obeys_year_filter(self):
        from common import GuideError
        from unittest.mock import patch
        from source import AcademicSource
        source = AcademicSource(Config(source='lune', cache=False), Budget(3))
        error = GuideError('synthetic failure')
        error.remote_outcome, error.retry_safe, error.partial_search = 'unknown', False, True
        error.partial_papers = [Paper('lune:old', 'Old paper', 'Abstract',
                                      'https://fixture.invalid/old', year=2020)]
        with patch.object(source, 'available', return_value=True), \
             patch.object(source, '_search_one', side_effect=error):
            with self.assertRaises(GuideError) as caught:
                source.search_batch('fixture', filters={'year_from': 2025})
        self.assertEqual(caught.exception.partial_batch.papers, [])
        self.assertEqual(caught.exception.partial_batch.stop_reason, 'source_error')

    def test_chat_retains_partial_candidates_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite'
            store = StateStore(path)
            try:
                source = PartialSource(Budget(5))
                chat = ChatSession(Config(source='lune'), store, source=source, llm=ChatDemoLLM())
                source.budget = chat.budget
                result = chat.start(message='Recommend five RAG papers.')
                session_id = chat.session_id
                self.assertEqual(source.calls, 1)
                self.assertEqual(result['stop_reason'], 'user_cancelled')
                self.assertEqual(result['papers'], [])
                self.assertEqual([p['paper_id'] for p in result['candidates']], ['lune:first'])
                self.assertIsNone(result['cost']['tools'])
                self.assertEqual([p['paper_id'] for p in public_result(result)['candidates']], ['lune:first'])
            finally:
                store.close()
            reopened = StateStore(path)
            try:
                self.assertEqual([p['paper_id'] for p in reopened.candidate_papers(session_id)], ['lune:first'])
            finally:
                reopened.close()


if __name__ == '__main__':
    unittest.main()
