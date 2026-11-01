"""A late cooperative tool result is retained without authorizing a replay."""
import time
import unittest
from unittest.mock import patch

from agent_tools import ToolRegistry, ToolSpec
from common import Budget, Config, Paper, RunCancelled
from retrieval import RetrievalPlan, RetrievalRouter
from source import SearchBatch, SourceRun, retained_partial_batch


class LateSource:
    providers = ['lune']
    def __init__(self):
        self.budget = Budget(3)
        self.calls = 0
    def search_batch(self, query, limit, providers=None, filters=None):
        self.calls += 1
        time.sleep(.04)
        paper = Paper('lune:late', 'Late candidate', 'Evidence text.',
                      'https://fixture.invalid/late', sources=['lune'])
        return SearchBatch(query, list(providers or self.providers), [paper],
                           [SourceRun('lune', 'ok', 1, cost_cny=None)])


class RegistryLateResultTests(unittest.TestCase):
    def test_late_search_retains_batch_with_nonretryable_classification(self):
        source = LateSource()
        registry = ToolRegistry.from_source(source, request_timeout=.01)
        observation = registry.search('fixture', 1, ['lune'])
        self.assertFalse(observation.ok)
        self.assertEqual(observation.status, 'timeout')
        self.assertEqual(observation.remote_outcome, 'completed')
        self.assertIs(observation.retry_safe, False)
        self.assertIsNotNone(observation.data)
        self.assertEqual(observation.data.stop_reason, 'tool_timeout')
        self.assertEqual([paper.id for paper in observation.data.papers], ['lune:late'])
        self.assertEqual(source.calls, 1)
        class Failure(Exception): pass
        error = Failure()
        error.partial_batch = observation.data
        self.assertIsNotNone(retained_partial_batch(error, 'fixture', ['lune'], 1))

    def test_cancelled_search_keeps_confirmed_result_without_success_observation(self):
        class Source(LateSource):
            def search_batch(self, query, limit, providers=None, filters=None):
                self.calls += 1
                paper = Paper('lune:done', 'Completed', 'Evidence', 'https://fixture.invalid/p')
                self.budget.cancel_token.request()
                return SearchBatch(query, ['lune'], [paper], [SourceRun('lune', 'ok', 1, cost_cny=None)])
        source = Source()
        registry = ToolRegistry.from_source(source, request_timeout=1)
        with self.assertRaises(RunCancelled) as caught:
            registry.search('fixture', 1, ['lune'])
        self.assertEqual(caught.exception.remote_outcome, 'completed')
        self.assertIs(caught.exception.retry_safe, False)
        batch = retained_partial_batch(caught.exception, 'fixture', ['lune'], 1)
        self.assertEqual([paper.id for paper in batch.papers], ['lune:done'])
        self.assertEqual(batch.stop_reason, 'user_cancelled')
        self.assertEqual(source.calls, 1)

    def test_cancel_race_preserves_prior_partial_and_remote_outcome(self):
        from common import GuideError
        class Source:
            providers = ['openalex', 'lune']
            def __init__(self): self.budget = Budget(3)
            def search_batch(self, query, limit, providers=None, filters=None):
                paper = Paper('openalex:one', 'Prior', 'Evidence', 'https://fixture.invalid/p')
                batch = SearchBatch(query, ['openalex', 'lune'], [paper], [
                    SourceRun('openalex', 'ok', 1, remote_outcome='completed', retry_safe=False),
                    SourceRun('lune', 'partial', remote_outcome='unknown', retry_safe=False, cost_cny=None),
                ], stop_reason='source_error')
                error = GuideError('Synthetic source failure')
                error.remote_outcome, error.retry_safe, error.partial_batch = 'unknown', False, batch
                self.budget.cancel_token.request()
                raise error
        source = Source()
        registry = ToolRegistry.from_source(source, request_timeout=1)
        with self.assertRaises(RunCancelled) as caught:
            registry.search('fixture', 1, ['openalex', 'lune'])
        self.assertEqual(caught.exception.remote_outcome, 'unknown')
        self.assertIs(caught.exception.retry_safe, False)
        batch = retained_partial_batch(caught.exception, 'fixture', ['openalex', 'lune'], 1)
        self.assertIsNotNone(batch)
        self.assertEqual([paper.id for paper in batch.papers], ['openalex:one'])
        self.assertEqual(batch.runs[-1].remote_outcome, 'unknown')

    def test_cancelled_host_handler_keeps_result_without_claiming_remote_success(self):
        budget = Budget(3)
        class Source:
            pass
        source = Source()
        source.budget = budget
        def handler(value):
            budget.cancel_token.request()
            return {'value': value}
        spec = ToolSpec('fixture', 'local', 'fixture',
                        {'type': 'object', 'properties': {'value': {'type': 'string'}},
                         'required': ['value'], 'additionalProperties': False},
                        1, 1, 'unknown', ())
        registry = ToolRegistry([spec], source, handlers={'fixture': handler})
        with self.assertRaises(RunCancelled) as caught:
            registry.call('fixture', handler, _input={'value': 'fixture'})
        self.assertEqual(caught.exception.partial_data, {'value': 'fixture'})
        self.assertEqual(caught.exception.remote_outcome, 'unknown')
        self.assertIs(caught.exception.retry_safe, False)

    def test_router_keeps_late_candidates_without_followup_paid_request(self):
        source = LateSource()
        registry = ToolRegistry.from_source(source, request_timeout=.01)
        router = RetrievalRouter(Config(source='lune'), source, tools=registry)
        plan = RetrievalPlan(['lune'], ['fixture', 'next'], ['fixture'], single_source=True)
        with patch.object(router, 'plan', return_value=plan):
            result = router.retrieve({'topic': 'fixture', 'target_count': 1}, 'fixture', limit=1)
        self.assertEqual(source.calls, 1)
        self.assertEqual([paper.id for paper in result.papers], ['lune:late'])
        self.assertTrue(result.paid_followups_blocked)
        self.assertTrue(result.tool_cost_unknown)
        self.assertEqual(result.batches[0].stop_reason, 'tool_timeout')


if __name__ == '__main__':
    unittest.main()
