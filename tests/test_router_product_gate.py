"""Unresolved paid outcomes remain a gate across workflow and session actions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Config, Paper
from demo import ChatDemoLLM, DemoLLM, DemoSource
from retrieval import RetrievalPlan, RetrievalResult
from session import ChatSession
from state import StateStore
from workflow import run


class MultiSource(DemoSource):
    providers = ['lune', 'openalex']
    def search_batch(self, *args, **kwargs):
        raise AssertionError('fixture router should own this call')


class BlockedRouter:
    def __init__(self): self.calls = 0
    def retrieve(self, *args, **kwargs):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError('unexpected additional search')
        paper = Paper('lune:first', 'Partial candidate', 'Unassessed evidence.',
                      'https://fixture.invalid/paper', sources=['lune'])
        plan = RetrievalPlan(['lune', 'openalex'], ['fixture'], ['fixture'])
        return RetrievalResult([paper], plan, tool_cost_unknown=True,
                               paid_followups_blocked=True,
                               remote_outcomes=[{'providers': ['lune'],
                                                 'remote_outcome': 'unknown', 'retry_safe': False}])


class RouterGateTests(unittest.TestCase):
    def test_workflow_stops_after_unresolved_paid_outcome(self):
        router = BlockedRouter()
        with patch('workflow.RetrievalRouter', return_value=router):
            result = run('Recommend five RAG papers.', Config(source='multi'), 'A',
                         source=MultiSource(), llm=DemoLLM())
        self.assertEqual(router.calls, 1)
        self.assertEqual(result['papers'], [])
        self.assertEqual([p['id'] for p in result['candidates']], ['lune:first'])
        self.assertIsNone(result['cost']['tools'])
        self.assertNotEqual(result['status'], 'complete')

    def test_chat_stops_after_unresolved_paid_outcome_and_keeps_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / 'state.sqlite')
            try:
                chat = ChatSession(Config(source='multi'), store, source=MultiSource(), llm=ChatDemoLLM())
                router = BlockedRouter()
                chat.router = router
                result = chat.start(message='Recommend five RAG papers.')
                self.assertEqual(router.calls, 1)
                self.assertEqual(result['papers'], [])
                self.assertEqual([p['paper_id'] for p in result['candidates']], ['lune:first'])
                self.assertIsNone(result['cost']['tools'])
                self.assertNotEqual(result['status'], 'complete')
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
