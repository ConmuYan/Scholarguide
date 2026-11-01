"""Tool registry and structured observation tests (no network)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_tools import ToolObservation, ToolRegistry
from common import Budget, Config, GuideError, Paper
from source import AcademicSource, SearchBatch, SourceRun, classify_source_error


class FakeBatchSource:
    mode = 'live'
    providers = ['arxiv', 'openalex']

    def __init__(self):
        self.calls = []

    def available(self, name):
        return True

    def search_batch(self, query, limit=10, providers=None, filters=None):
        self.calls.append((query, providers, filters))
        papers = [Paper('p1', 'RAG', 'abstract', 'https://example.org/1',
                        venue='CVPR', year=2025, open_access_url='https://example.org/1.pdf')]
        return SearchBatch(query, list(providers or self.providers), papers,
                           [SourceRun('arxiv', 'ok', 1)])


class RegistryTests(unittest.TestCase):
    def test_concurrent_older_success_cannot_erase_newer_failure(self):
        import threading
        from dataclasses import replace
        spec = replace(ToolRegistry.from_source(FakeBatchSource()).specs['arxiv'],
                       min_interval_s=0, failure_threshold=1, cooldown_s=60)
        entered, release = threading.Event(), threading.Event()
        def handler(query):
            if query == 'old':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test barrier timed out')
                return 'ok'
            raise GuideError('synthetic failure')
        registry = ToolRegistry([spec], handlers={'arxiv': handler})
        results = []
        thread = threading.Thread(target=lambda: results.append(
            registry.call('arxiv', handler, _input={'query': 'old'})))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            failed = registry.call('arxiv', handler, _input={'query': 'new'})
            self.assertFalse(failed.ok)
        finally:
            release.set(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0].ok)
        self.assertEqual(registry.health()['arxiv']['failures'], 1)
        self.assertTrue(registry.health()['arxiv']['circuit_open'])

    def test_concurrent_handler_admissions_are_spaced_and_large_interval_honored(self):
        import threading
        import time
        from dataclasses import replace
        spec = replace(ToolRegistry.from_source(FakeBatchSource()).specs['arxiv'],
                       min_interval_s=2.1, timeout_s=5)
        starts = []
        barrier = threading.Barrier(3)
        def handler(query):
            starts.append(time.monotonic())
        registry = ToolRegistry([spec], handlers={'arxiv': handler})
        def invoke():
            barrier.wait(timeout=3)
            registry.call('arxiv', handler, _input={'query': 'fixture'})
        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads: thread.start()
        barrier.wait(timeout=3)
        for thread in threads: thread.join(6)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], 2.08)

    def test_waiters_do_not_share_one_rate_slot_or_bypass_with_circuit_key(self):
        import threading
        import time
        from dataclasses import replace
        spec = replace(ToolRegistry.from_source(FakeBatchSource()).specs['arxiv'],
                       min_interval_s=.1)
        starts = []
        barrier = threading.Barrier(3)
        def handler(query):
            starts.append(time.monotonic())
        registry = ToolRegistry([spec], handlers={'arxiv': handler})
        registry.call('arxiv', handler, _input={'query': 'prime'})
        def invoke(key):
            barrier.wait(timeout=2)
            registry.call('arxiv', handler, _circuit_key=key, _input={'query': 'fixture'})
        threads = [threading.Thread(target=invoke, args=(str(i),)) for i in range(2)]
        for thread in threads: thread.start()
        barrier.wait(timeout=2)
        for thread in threads: thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(b - a >= .09 for a, b in zip(starts, starts[1:])))

    def test_waiting_handler_observes_cancel_and_budget_without_failure(self):
        import threading
        from dataclasses import replace
        from common import BudgetExceeded, RunCancelled
        spec = replace(ToolRegistry.from_source(FakeBatchSource()).specs['arxiv'],
                       min_interval_s=3, failure_threshold=1)
        entered = threading.Event()
        class ObservedBudget(Budget):
            def wait(self, seconds):
                entered.set()
                return super().wait(seconds)
        class Source:
            budget = ObservedBudget(10)
        source = Source()
        calls = []
        def handler(query):
            calls.append(query)
        registry = ToolRegistry([spec], source, handlers={'arxiv': handler})
        registry.call('arxiv', handler, _input={'query': 'first'})
        errors = []
        def invoke():
            try:
                registry.call('arxiv', handler, _input={'query': 'waiting'})
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=invoke); thread.start()
        try:
            self.assertTrue(entered.wait(2))
            source.budget.cancel_token.request()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(errors[0], RunCancelled)
            self.assertEqual(calls, ['first'])
            self.assertEqual(registry.health()['arxiv']['failures'], 0)
        finally:
            source.budget.cancel_token.request(); thread.join(2)
        source.budget = Budget(.03)
        with self.assertRaises(BudgetExceeded):
            registry.call('arxiv', handler, _input={'query': 'budget'})
        self.assertEqual(registry.health()['arxiv']['failures'], 0)

    def test_safe_retry_also_obeys_handler_rate(self):
        import time
        from dataclasses import replace
        spec = replace(ToolRegistry.from_source(FakeBatchSource()).specs['arxiv'],
                       min_interval_s=1.1, max_retries=2, failure_threshold=3)
        starts = []
        def handler(query):
            starts.append(time.monotonic())
            if len(starts) == 1:
                error = GuideError('synthetic retry')
                error.retry_safe = True
                raise error
        registry = ToolRegistry([spec], handlers={'arxiv': handler})
        result = registry.call('arxiv', handler, _input={'query': 'fixture'})
        self.assertTrue(result.ok)
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], 1.08)

    def test_waiter_rechecks_circuit_before_dispatch(self):
        import threading
        from dataclasses import replace
        spec = replace(ToolRegistry.from_source(FakeBatchSource()).specs['arxiv'],
                       min_interval_s=.2, failure_threshold=1, cooldown_s=60)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def handler(query):
            calls.append(query)
            if query == 'fail':
                entered.set()
                if not release.wait(2):
                    raise AssertionError('test barrier timed out')
                raise GuideError('synthetic failure')
        registry = ToolRegistry([spec], handlers={'arxiv': handler})
        results = []
        first = threading.Thread(target=lambda: registry.call('arxiv', handler, _input={'query': 'fail'}))
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            second = threading.Thread(target=lambda: results.append(
                registry.call('arxiv', handler, _input={'query': 'waiting'})))
            second.start()
            release.set()
            first.join(2); second.join(2)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(results[0].status, 'circuit_open')
            self.assertEqual(calls, ['fail'])
        finally:
            release.set(); first.join(2)

    def test_reentrant_event_callback_does_not_hold_admission_lock(self):
        from dataclasses import replace
        from execution_events import bind_events
        spec = replace(ToolRegistry.from_source(FakeBatchSource()).specs['arxiv'],
                       min_interval_s=.02)
        calls = []
        def handler(query):
            calls.append(query)
        registry = ToolRegistry([spec], handlers={'arxiv': handler})
        nested = []
        def sink(action, payload):
            if action == 'tool_start' and not nested:
                nested.append(None)
                nested[0] = registry.call('arxiv', handler, _input={'query': 'nested'})
        with bind_events(sink):
            result = registry.call('arxiv', handler, _input={'query': 'outer'})
        self.assertTrue(result.ok)
        self.assertTrue(nested[0].ok)
        self.assertEqual(calls, ['nested', 'outer'])

    def test_registry_has_schema_timeout_cost_and_provenance(self):
        registry = ToolRegistry.from_source(AcademicSource(Config(), Budget(10)))
        specs = {item['name']: item for item in registry.describe()}
        self.assertEqual(len(specs), 9)
        self.assertIn("lune", specs)
        for name in ('arxiv', 'openalex', 'semantic_scholar', 'dblp',
                     'openreview', 'acl_anthology', 'deepseek'):
            self.assertIn(name, specs)
            self.assertIn('query', specs[name]['input_schema']['properties'])
            self.assertGreater(specs[name]['timeout_s'], 0)
            self.assertTrue(specs[name]['provenance'])
            self.assertTrue(specs[name]['circuit_breaker'])
            self.assertIn('isolated process', specs[name]['execution_boundary'])
            self.assertIn('unknown', specs[name]['remote_usage_on_abort'])
        self.assertIn('CNY', specs['deepseek']['cost_model'])
        self.assertIn('unknown', specs['lune']['cost_model'])

    def test_timeout_and_guide_errors_become_observations(self):
        specs = ToolRegistry.from_source(FakeBatchSource()).specs
        def timeout_handler(**values):
            raise TimeoutError('slow')
        def limited_handler(**values):
            raise GuideError('HTTP 429')
        timeout_registry = ToolRegistry(list(specs.values()), handlers={'arxiv': timeout_handler})
        timeout = timeout_registry.call('arxiv', timeout_handler, _input={'query': 'fixture'})
        self.assertFalse(timeout.ok)
        self.assertFalse(timeout.retryable)
        limited_registry = ToolRegistry(list(specs.values()), handlers={'arxiv': limited_handler})
        limited = limited_registry.call('arxiv', limited_handler, _input={'query': 'fixture'})
        self.assertFalse(limited.ok)
        self.assertFalse(limited.retryable)
        unknown = timeout_registry.call('nope', lambda: 1)
        self.assertEqual(unknown.status, 'unknown_tool')

    def test_search_observation_carries_cost_and_provenance(self):
        source = FakeBatchSource()
        registry = ToolRegistry.from_source(source)
        obs = registry.search('RAG', 5, providers=['arxiv'], filters={'venue': 'CVPR'})
        self.assertTrue(obs.ok)
        self.assertEqual(obs.cost_units, 1.0)
        self.assertEqual(obs.data.papers[0].venue, 'CVPR')
        self.assertIn('filters', obs.provenance)
        self.assertEqual(source.calls[0][2], {'venue': 'CVPR'})

    def test_timeout_retry_and_circuit_breaker_are_enforced(self):
        from dataclasses import replace
        import time
        specs = ToolRegistry.from_source(FakeBatchSource()).specs
        spec = replace(specs['arxiv'], timeout_s=0.03, max_retries=2,
                       failure_threshold=10, cooldown_s=60, min_interval_s=0)
        calls = []
        def handler(query):
            calls.append(query)
            if query == 'slow':
                time.sleep(.2)
            elif query == 'flaky' and calls.count('flaky') == 1:
                error = GuideError('HTTP 429')
                error.retry_safe = True
                raise error
            elif query == 'unsafe':
                raise GuideError('HTTP 503 temporarily unavailable')
            return 'ok'
        registry = ToolRegistry([spec], handlers={'arxiv': handler})
        timed = registry.call('arxiv', handler, _input={'query': 'slow'})
        self.assertEqual(timed.status, 'timeout')
        self.assertFalse(timed.retryable)
        self.assertTrue(timed.worker_waited)
        self.assertGreaterEqual(timed.elapsed_ms, 150)
        retried = registry.call('arxiv', handler, _input={'query': 'flaky'})
        self.assertTrue(retried.ok)
        self.assertEqual(calls.count('flaky'), 2)
        registry.specs['arxiv'] = replace(spec, failure_threshold=1)
        unsafe = registry.call('arxiv', handler, _input={'query': 'unsafe'})
        self.assertFalse(unsafe.retryable)
        self.assertEqual(calls.count('unsafe'), 1)
        opened = registry.call('arxiv', handler, _input={'query': 'never'})
        self.assertEqual(opened.status, 'circuit_open')

    def test_source_error_classification_is_stable(self):
        self.assertEqual(classify_source_error('HTTP 429 rate limit'), 'rate_limited')
        self.assertEqual(classify_source_error('DBLP returned an HTML challenge'), 'bot_challenge')
        self.assertEqual(classify_source_error('request timed out'), 'timeout')
        self.assertEqual(classify_source_error('could not resolve venue "CVPR"'), 'venue_resolve')

    def test_aggregate_search_timeout_keeps_worker_waited_signal(self):
        from dataclasses import replace
        import time
        registry = ToolRegistry.from_source(AcademicSource(Config(), Budget(10)))
        registry.specs['search'] = replace(registry.specs['search'], timeout_s=0.01,
                                           max_retries=1)
        registry._search_impl = lambda *args, **kwargs: time.sleep(.1)
        obs = registry.search('RAG', 5)
        self.assertFalse(obs.ok)
        self.assertEqual(obs.status, 'timeout')
        self.assertTrue(obs.worker_waited)
        self.assertIn('worker did not cancel', obs.error)

    def test_provider_capabilities_and_health_are_exposed(self):
        registry = ToolRegistry.from_source(AcademicSource(Config(), Budget(10)))
        caps = registry.capabilities()
        self.assertTrue(caps['openalex']['supports_venue'])
        self.assertTrue(caps['arxiv']['supports_year'])
        self.assertFalse(caps['deepseek']['supports_venue'])
        health = registry.health()
        self.assertIn('arxiv', health)
        self.assertFalse(health['arxiv']['circuit_open'])
        self.assertIn('timeout_s', health['arxiv'])

    def test_missing_deepseek_key_is_unavailable(self):
        source = AcademicSource(Config(source='deepseek'), Budget(10))
        registry = ToolRegistry.from_source(source)
        self.assertFalse(registry.available('deepseek'))

    def test_directed_provider_failures_do_not_block_healthy_fallback(self):
        class Degraded(FakeBatchSource):
            providers = ['openalex', 'semantic_scholar', 'dblp']
            def search_batch(self, query, limit=10, providers=None, filters=None):
                self.calls.append(providers[0])
                if providers[0] in ('openalex', 'semantic_scholar'):
                    raise GuideError('HTTP 429' if providers[0] == 'openalex' else 'HTTP 503')
                return SearchBatch(query, providers, [], [SourceRun(providers[0], 'empty')])
        source = Degraded()
        registry = ToolRegistry.from_source(source)
        for provider in ('openalex', 'semantic_scholar'):
            self.assertFalse(registry.search('RAG', 5, providers=[provider]).ok)
        self.assertTrue(registry.search('RAG', 5, providers=['dblp']).ok)
        self.assertEqual(source.calls, ['openalex', 'semantic_scholar', 'dblp'])
        self.assertNotIn('search', registry._open_until)

    def test_cooperative_extension_timeout_never_detaches_or_implicitly_retries(self):
        from dataclasses import replace
        import threading
        import time
        spec = ToolRegistry.from_source(FakeBatchSource()).specs['arxiv']
        spec = replace(spec, timeout_s=.01, max_retries=3, min_interval_s=0)
        calls = []
        before = {thread.ident for thread in threading.enumerate()}
        def slow(**values):
            calls.append('start')
            time.sleep(.03)
            calls.append('done')
        registry = ToolRegistry([spec], handlers={'arxiv': slow})
        observation = registry.call('arxiv', slow, _input={'query': 'fixture'})
        self.assertEqual(observation.status, 'timeout')
        self.assertEqual(calls, ['start', 'done'])
        self.assertEqual({thread.ident for thread in threading.enumerate()}, before)


if __name__ == '__main__':
    unittest.main()
