"""Unknown paid-provider charges must stay unknown across product boundaries."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_tools import ToolRegistry
from common import Budget, Config, GuideError, Paper, RunCancelled
from costing import cost_breakdown, tool_cost
from source import AcademicSource, SearchBatch, SourceRun
from source_store import SourceStore
from tools import search


class UnknownCostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        env = patch.dict(os.environ, {'SG_SKIP_PROJECT_ENV': '1',
                                      'SG_MCP_TOKEN_FILE': str(root / 'missing-token'),
                                      'SG_SOURCE_STORE': str(root / 'sources.sqlite'),
                                      'SG_LUNE_QUOTA_DB': str(root / 'quota.sqlite'),
                                      'LUNE_API_KEY': '', 'LUNE_TOKEN': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_lune_success_and_failure_are_not_recorded_as_free(self):
        source = AcademicSource(Config(source='lune', cache=False), Budget(3))
        paper = Paper(id='lune:fixture', title='Fixture', abstract='', url='')
        with patch.object(source, 'available', return_value=True), \
             patch.object(source, '_search_one', return_value=[paper]):
            batch = source.search_batch('fixture', providers=['lune'])
        self.assertIsNone(batch.runs[0].cost_cny)
        self.assertIsNone(batch.summary()['runs'][0]['cost_cny'])
        with patch.object(source, 'available', return_value=True), \
             patch.object(source, '_search_one', side_effect=GuideError('synthetic error')):
            with self.assertRaises(GuideError):
                source.search_batch('fixture', providers=['lune'])
        self.assertIsNone(source.last_batch.runs[0].cost_cny)

    def test_legacy_lune_zero_does_not_make_total_known(self):
        trace = [{'router': {'batches': [{'runs': [
            {'name': 'lune', 'status': 'ok', 'cost_cny': 0},
            {'name': 'deepseek', 'status': 'ok', 'cost_cny': .005}]}]}}]
        cost = cost_breakdown(Config(input_price=2, output_price=2, currency='CNY'),
                              {'input_tokens': 100, 'output_tokens': 100}, trace)
        self.assertIsNone(tool_cost(trace))
        self.assertIsNone(cost['tools'])
        self.assertIsNone(cost['total'])
        self.assertFalse(cost['known'])

    def test_legacy_deepseek_empty_and_failed_zero_remain_unknown(self):
        for status in ('empty', 'error'):
            with self.subTest(status=status):
                trace = [{'router': {'batches': [{'runs': [
                    {'name': 'deepseek', 'status': status, 'cost_cny': 0}]}]}}]
                self.assertIsNone(tool_cost(trace))

    def test_cache_and_skipped_lune_do_not_create_unknown_new_cost(self):
        trace = [{'router': {'batches': [{'runs': [
            {'name': 'lune', 'status': 'cache', 'cost_cny': None},
            {'name': 'lune', 'status': 'skipped', 'cost_cny': None}]}]}}]
        self.assertEqual(tool_cost(trace), 0)

    def test_registry_and_direct_search_propagate_unknown(self):
        class Source:
            providers = ['lune']
            last_batch = None
            def search_batch(self, query, limit, providers=None, filters=None):
                self.last_batch = SearchBatch(query, ['lune'], [],
                                              [SourceRun('lune', 'empty', cost_cny=None)])
                return self.last_batch
            def search(self, query, limit):
                return self.search_batch(query, limit).papers
        source = Source()
        observation = ToolRegistry.from_source(source).search('fixture', 1, ['lune'])
        self.assertIsNone(observation.cost_cny)
        self.assertIsNone(search(source, 'fixture', 1).cost_cny)

    def test_failed_paid_router_call_stays_unknown(self):
        from retrieval import RetrievalRouter
        class Source:
            providers = ['lune']
            budget = Budget(3)
            def search_batch(self, *args, **kwargs):
                error = GuideError('synthetic failure')
                error.remote_outcome = 'unknown'
                error.retry_safe = False
                raise error
        result = RetrievalRouter(Config(source='lune'), Source()).retrieve(
            {'topic': 'fixture', 'target_count': 1}, 'fixture')
        self.assertTrue(result.observation()['tool_cost_unknown'])
        self.assertIsNone(tool_cost([{'router': result.observation()}]))

    def test_workflow_and_chat_preserve_unknown_success_cost(self):
        from api_v1 import public_result
        from demo import ChatDemoLLM, DemoLLM, DemoSource
        from session import ChatSession
        from state import StateStore
        from workflow import run
        class UnknownSource(DemoSource):
            providers = ['lune']
            def search(self, query, limit=10):
                papers = super().search(query, limit)
                self.last_batch = SearchBatch(query, ['lune'], papers,
                                              [SourceRun('lune', 'ok', len(papers), cost_cny=None)])
                return papers
        result = run('Recommend five RAG papers.', Config(source='lune'), 'S',
                     source=UnknownSource(), llm=DemoLLM())
        self.assertIsNone(result['cost']['tools'])
        self.assertIsNone(result['cost']['total'])
        self.assertIsNone(public_result(result)['cost']['tools'])
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / 'chat.sqlite')
            try:
                chat = ChatSession(Config(source='lune'), store, source=UnknownSource(), llm=ChatDemoLLM())
                answer = chat.start(message='Recommend five RAG papers.')
                self.assertIsNone(answer['cost']['tools'])
                self.assertIsNone(answer['cost']['total'])
            finally:
                store.close()

    def test_cancelled_paid_search_does_not_report_zero(self):
        from demo import DemoLLM, DemoSource
        from workflow import run
        budget = Budget(5)
        class CancelledSource(DemoSource):
            providers = ['lune']
            def search(self, query, limit=10):
                budget.cancel_token.request()
                error = RunCancelled()
                error.remote_outcome, error.retry_safe = 'unknown', False
                raise error
        result = run('Recommend five RAG papers.', Config(source='lune'), 'S',
                     source=CancelledSource(), llm=DemoLLM(), budget=budget)
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertIsNone(result['cost']['tools'])
        self.assertIsNone(result['cost']['total'])
        from demo import ChatDemoLLM
        from session import ChatSession
        from state import StateStore
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / 'chat.sqlite')
            try:
                chat = ChatSession(Config(source='lune'), store,
                                   source=CancelledSource(), llm=ChatDemoLLM())
                # The fake raises the same cancelled token used by this turn.
                budget.cancel_token = chat.budget.cancel_token
                answer = chat.start(message='Recommend five RAG papers.')
                self.assertEqual(answer['stop_reason'], 'user_cancelled')
                self.assertIsNone(answer['cost']['tools'])
                self.assertIsNone(answer['cost']['total'])
            finally:
                store.close()

    def test_source_audit_records_unknown_without_schema_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 'source.sqlite')
            try:
                store.record_run(SourceRun('lune', 'ok'))
                with store._db() as db:
                    value = db.execute('SELECT cost_cny FROM attempts ORDER BY id DESC').fetchone()[0]
                self.assertEqual(value, -1)
                store.record_run(SourceRun('lune', 'ok', cost_cny=None))
                with store._db() as db:
                    value = db.execute('SELECT cost_cny FROM attempts ORDER BY id DESC').fetchone()[0]
                self.assertEqual(value, -1)
            finally:
                store.close()

    def test_nonzero_cny_tools_are_not_added_to_usd_model_cost(self):
        trace = [{'cost_cny': .005}]
        cost = cost_breakdown(Config(input_price=2, output_price=2, currency='USD'),
                              {'input_tokens': 100, 'output_tokens': 100}, trace)
        self.assertEqual(cost['tools'], .005)
        self.assertIsNone(cost['total'])
        self.assertFalse(cost['known'])


if __name__ == '__main__':
    unittest.main()
