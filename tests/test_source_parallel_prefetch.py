"""Offline admission and settlement contracts for directed prefetch."""
import sys
from pathlib import Path
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config, Paper, RunCancelled
from retrieval import RetrievalRouter
from source import SearchBatch, SourceRun
from source_parallel import DirectedPrefetch
from tools import CostMeter


STEP = {'provider': 'openalex', 'query': 'retrieval', 'filters': {}}


class Meter:
    def __init__(self):
        self.events = []

    def reserve(self, action, units, detail=None):
        handle = len([item for item in self.events if item[0] == 'reserve'])
        self.events.append(('reserve', handle, action, units, detail or {}))
        return handle

    def settle(self, handle, units):
        self.events.append(('settle', handle, units))

    def summary(self):
        reserved = sum(event[3] for event in self.events if event[0] == 'reserve')
        settled = sum(event[2] for event in self.events if event[0] == 'settle')
        return {'reserved': reserved, 'settled': settled}


class Source:
    def __init__(self, result=None, error=None, entered=None, release=None):
        self.result = result or SearchBatch('retrieval', ['openalex'], [],
                                            [SourceRun('openalex', 'empty', 0)])
        self.error = error
        self.error_by_provider = {}
        self.entered = entered
        self.release = release
        self.calls = 0

    def search_batch(self, query, limit, providers=None, filters=None):
        provider = providers[0]
        self.calls += 1
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(2):
            raise RuntimeError('test release was not signalled')
        if self.error:
            raise self.error
        if self.error_by_provider.get(provider):
            raise self.error_by_provider[provider]
        return self.result


def step(index=0):
    return dict(STEP, query=f'retrieval-{index}')


class DirectedPrefetchTests(unittest.TestCase):
    def test_context_creation_does_not_start_a_request(self):
        source = Source()
        meter = Meter()
        with DirectedPrefetch(source, [step()], 10, settle=meter.settle):
            self.assertEqual(source.calls, 0)

    def test_reservation_precedes_worker_submission(self):
        source = Source()
        meter = Meter()
        entered = threading.Event()
        source.entered = entered
        with DirectedPrefetch(source, [step()], 10, settle=meter.settle) as prefetch:
            self.assertEqual(source.calls, 0)
            handle = meter.reserve('search', 1.0, {'provider': 'openalex'})
            prefetch.submit(0, step(), handle)
            self.assertTrue(entered.wait(1))
            self.assertEqual([event[0] for event in meter.events], ['reserve'])
            prefetch.take(0)
            prefetch.settle(0, 0.0)
        self.assertEqual(meter.events[-1], ('settle', 0, 0.0))

    def test_success_is_settled_once_by_the_owner(self):
        source = Source()
        meter = Meter()
        with DirectedPrefetch(source, [step()], 10, settle=meter.settle) as prefetch:
            prefetch.submit(0, step(), meter.reserve('search', 1.0))
            prefetch.take(0)
            prefetch.settle(0, 1.0)
        self.assertEqual([event[0] for event in meter.events], ['reserve', 'settle'])
        self.assertEqual(meter.events[-1][2], 1.0)

    def test_classified_cancel_after_start_is_refunded(self):
        source = Source(error=RunCancelled())
        meter = Meter()
        with self.assertRaises(RunCancelled):
            with DirectedPrefetch(source, [step()], 10, settle=meter.settle) as prefetch:
                prefetch.submit(0, step(), meter.reserve('search', 1.0))
                prefetch.take(0)
        self.assertEqual(meter.events[-1], ('settle', 0, 0.0))

    def test_unknown_worker_failure_is_conservatively_settled(self):
        source = Source(error=ValueError('unclassified worker failure'))
        meter = Meter()
        with self.assertRaises(ValueError):
            with DirectedPrefetch(source, [step()], 10, settle=meter.settle) as prefetch:
                prefetch.submit(0, step(), meter.reserve('search', 1.0))
                prefetch.take(0)
        self.assertEqual(meter.events[-1], ('settle', 0, 1.0))

    def test_cancelled_inflight_take_returns_without_waiting_for_worker(self):
        entered = threading.Event()
        release = threading.Event()
        source = Source(entered=entered, release=release)
        source.budget = Budget(10)
        meter = Meter()
        with DirectedPrefetch(source, [step()], 10, settle=meter.settle) as prefetch:
            prefetch.submit(0, step(), meter.reserve('search', 1.0))
            self.assertTrue(entered.wait(1))
            source.budget.cancel_token.request()
            with self.assertRaises(RunCancelled):
                prefetch.take(0)
            release.set()
        self.assertEqual(meter.events[-1], ('settle', 0, 1.0))

    def test_base_exception_settles_all_started_workers_and_preserves_interrupt(self):
        entered = threading.Barrier(2)

        class BothStartedSource(Source):
            def search_batch(self, query, limit, providers=None, filters=None):
                entered.wait(1)
                raise KeyboardInterrupt()

        source = BothStartedSource()
        meter = Meter()
        with self.assertRaises(KeyboardInterrupt):
            with DirectedPrefetch(source, [step(0), step(1)], 10, settle=meter.settle) as prefetch:
                prefetch.submit(0, step(0), meter.reserve('search', 1.0))
                prefetch.submit(1, step(1), meter.reserve('search', 1.0))
                prefetch.take(0)
        self.assertEqual([event[0] for event in meter.events].count('reserve'), 2)
        self.assertEqual([event[0] for event in meter.events].count('settle'), 2)
        self.assertEqual(sum(event[2] for event in meter.events if event[0] == 'settle'), 2.0)

    def test_unconsumed_inflight_request_is_settled_after_context_shutdown(self):
        entered = threading.Event()
        release = threading.Event()
        source = Source(entered=entered, release=release)
        meter = Meter()
        with DirectedPrefetch(source, [step()], 10, settle=meter.settle) as prefetch:
            prefetch.submit(0, step(), meter.reserve('search', 1.0))
            self.assertTrue(entered.wait(1))
            release.set()
        self.assertEqual(meter.events[-1][0], 'settle')
        self.assertEqual(meter.events[-1][1:], (0, 1.0))


class RouterPrefetchSource:
    """Thread-safe source double that opts into the router prefetch path."""
    class Store:
        def enabled_sources(self):
            return ['openalex', 'semantic_scholar']

    def __init__(self, meter):
        self.store = self.Store()
        self.providers = ['openalex', 'semantic_scholar']
        self.meter = meter
        self.calls = []
        self.lock = threading.Lock()
        self.error_by_provider = {}
        self.barrier = None

    def available(self, name):
        return True

    def search_batch(self, query, limit=10, providers=None, filters=None):
        provider = providers[0]
        paper = Paper(
            f'prefetch-{provider}', f'Prefetch {provider}',
            'A grounded abstract for the prefetch accounting integration test.',
            f'https://example.org/{provider}', year=2025, venue='Test Venue',
            sources=[provider], source=provider, abstract_source=provider)
        with self.lock:
            reserved = sum(1 for event in self.meter.events if event[0] == 'reserve')
            self.calls.append({'provider': provider, 'reserved_at_start': reserved})
        if self.barrier is not None:
            self.barrier.wait(1)
        if self.error_by_provider.get(provider):
            raise self.error_by_provider[provider]
        return SearchBatch(query, providers, [paper],
                           [SourceRun(provider, 'ok', 1)])


class RetrievalPrefetchIntegrationTests(unittest.TestCase):
    def test_router_admits_both_workers_before_any_source_call_and_settles_all(self):
        meter = Meter()
        source = RouterPrefetchSource(meter)
        router = RetrievalRouter(Config(source='multi', research_mode=True, cache=False),
                                 source, today=2026)
        result = router.retrieve({'topic': 'retrieval', 'target_count': 1},
                                 'retrieval', limit=2, meter=meter)
        self.assertEqual(len(source.calls), 2)
        self.assertTrue(all(call['reserved_at_start'] == 2 for call in source.calls))
        self.assertEqual([call['reserved_at_start'] for call in source.calls],
                         sorted(call['reserved_at_start'] for call in source.calls))
        self.assertEqual([event[0] for event in meter.events].count('reserve'), 2)
        self.assertEqual([event[0] for event in meter.events].count('settle'), 2)
        self.assertEqual(meter.summary()['reserved'], 2.0)
        self.assertEqual(meter.summary()['settled'], 2.0)
        self.assertEqual(result.cost, 2.0)


    def test_router_admission_honors_preexisting_cancel(self):
        meter = Meter()
        source = RouterPrefetchSource(meter)
        source.budget = Budget(10)
        source.budget.cancel_token.request()
        router = RetrievalRouter(Config(source='multi', research_mode=True, cache=False),
                                 source, today=2026)
        router.retrieve({'topic': 'retrieval', 'target_count': 1}, 'retrieval',
                        limit=2, meter=meter)
        self.assertEqual(source.calls, [])
        self.assertEqual([event[0] for event in meter.events], [])

    def test_router_admission_honors_expired_budget(self):
        meter = Meter()
        source = RouterPrefetchSource(meter)
        source.budget = Budget(0)
        router = RetrievalRouter(Config(source='multi', research_mode=True, cache=False),
                                 source, today=2026)
        router.retrieve({'topic': 'retrieval', 'target_count': 1}, 'retrieval',
                        limit=2, meter=meter)
        self.assertEqual(source.calls, [])
        self.assertEqual([event[0] for event in meter.events], [])

    def test_unknown_worker_failure_cost_is_conservative(self):
        meter = Meter()
        source = RouterPrefetchSource(meter)
        source.error_by_provider['openalex'] = ValueError('unknown worker failure')
        router = RetrievalRouter(Config(source='multi', research_mode=True, cache=False),
                                 source, today=2026)
        result = router.retrieve({'topic': 'retrieval', 'target_count': 1},
                                 'retrieval', limit=2, meter=meter)
        self.assertEqual(result.cost, 2.0)
        self.assertEqual(meter.summary(), {'reserved': 2.0, 'settled': 2.0})

    def test_classified_cancellation_refunds_only_failed_worker(self):
        meter = Meter()
        source = RouterPrefetchSource(meter)
        source.barrier = threading.Barrier(2)
        source.error_by_provider['openalex'] = RunCancelled()
        router = RetrievalRouter(Config(source='multi', research_mode=True, cache=False),
                                 source, today=2026)
        result = router.retrieve({'topic': 'retrieval', 'target_count': 1},
                                 'retrieval', limit=2, meter=meter)
        self.assertEqual(result.cost, 1.0)
        self.assertEqual(meter.summary(), {'reserved': 2.0, 'settled': 1.0})


class LegacyAccountingTests(unittest.TestCase):
    def test_unknown_legacy_source_error_closes_reservation(self):
        class BrokenSource:
            providers = ['openalex']
            def search_batch(self, *args, **kwargs):
                raise ValueError('unclassified legacy failure')
        meter = CostMeter()
        router = RetrievalRouter(Config(source='openalex', research_mode=False, cache=False),
                                 BrokenSource(), today=2026)
        result = router.retrieve({'topic': 'retrieval'}, 'retrieval', limit=2, meter=meter)
        self.assertEqual(meter.summary()['reserved'], 1.0)
        self.assertEqual(meter.summary()['settled'], 1.0)
        self.assertEqual(result.cost, 1.0)
        self.assertTrue(any('unknown execution failure' in error for error in result.errors))

    def test_base_exception_legacy_source_closes_reservation_and_preserves_interrupt(self):
        class BrokenSource:
            providers = ['openalex']
            def search_batch(self, *args, **kwargs):
                raise KeyboardInterrupt()
        meter = CostMeter()
        router = RetrievalRouter(Config(source='openalex', research_mode=False, cache=False),
                                 BrokenSource(), today=2026)
        with self.assertRaises(KeyboardInterrupt):
            router.retrieve({'topic': 'retrieval'}, 'retrieval', limit=2, meter=meter)
        self.assertEqual(meter.summary()['reserved'], 1.0)
        self.assertEqual(meter.summary()['settled'], 1.0)


if __name__ == '__main__':
    unittest.main()
