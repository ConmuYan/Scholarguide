"""Per-provider health must update from actual search_batch runs."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Budget, Config, GuideError
from source import AcademicSource


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {key: '' for key in ('OPENALEX_API_KEY', 'S2_API_KEY', 'SEMANTIC_SCHOLAR_API_KEY',
                                    'DEEPSEEK_SEARCH_KEY', 'DEEPSEEK_SEARCH_API_KEY',
                                    'LUNE_API_KEY', 'LUNE_TOKEN')}
        env['SG_MCP_TOKEN_FILE'] = str(Path(tmp.name) / 'missing-token.json')
        patcher = patch.dict('os.environ', env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_persisted_cooldown_single_source_fails_without_dispatch(self):
        import time
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from source_store import SourceStore
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.db'
            store = SourceStore(path)
            store.cooldown('dblp', time.time() + 60)
            store.close()
            store = SourceStore(path)
            try:
                budget = Budget(3)
                source = AcademicSource(Config(source='dblp', cache=False, source_store=store), budget)
                with patch.object(source, '_search_one') as dispatch, patch('source.urlopen') as network:
                    for call in (lambda: source.search_batch('fixture'), lambda: source.search('fixture')):
                        with self.assertRaises(GuideError) as caught:
                            call()
                        self.assertEqual(caught.exception.code, 'unavailable')
                        self.assertIn('cooldown', str(caught.exception))
                    dispatch.assert_not_called()
                    network.assert_not_called()
                self.assertEqual(budget.search_http_calls, 0)
            finally:
                store.close()

    def test_local_cooldown_fails_and_missing_key_keeps_legacy_behavior(self):
        import time
        from unittest.mock import patch
        source = AcademicSource(Config(source='deepseek', cache=False, deepseek_search_key='',
                                       api_key='', base_url='https://fixture.invalid'), Budget(3))
        with patch.object(source, '_search_one') as dispatch:
            self.assertEqual(source.search('fixture'), [])
            self.assertEqual(source.last_batch.runs[0].status, 'skipped')
            source.cooldown['deepseek'] = time.monotonic() + 60
            with self.assertRaises(GuideError) as caught:
                source.search('fixture')
            self.assertEqual(caught.exception.code, 'unavailable')
            dispatch.assert_not_called()
        self.assertEqual(source.budget.search_http_calls, 0)

    def test_persisted_cooldown_multi_source_keeps_partial_results(self):
        import time
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from common import Paper
        from source_store import SourceStore
        with TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 'source.db')
            try:
                store.cooldown('dblp', time.time() + 60)
                source = AcademicSource(Config(source='dblp,openalex', cache=False, source_store=store), Budget(3))
                paper = Paper('fixture', 'Fixture title', 'Abstract', 'https://fixture.invalid/paper')
                with patch.object(source, '_search_one', return_value=[paper]) as dispatch, \
                     patch('source.urlopen') as network:
                    batch = source.search_batch('fixture')
                self.assertEqual(batch.papers, [paper])
                self.assertEqual([run.status for run in batch.runs], ['skipped', 'ok'])
                self.assertEqual(dispatch.call_args.args[0], 'openalex')
                self.assertEqual(dispatch.call_count, 1)
                network.assert_not_called()
                self.assertEqual(source.budget.search_http_calls, 0)
            finally:
                store.close()

    def test_pause_write_failure_emits_safe_event_preserving_transport_error(self):
        import sqlite3
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from execution_events import bind_events
        from source import SourceTransportError
        from source_store import SourceStore
        events = []
        with TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 'source.db')
            try:
                source = AcademicSource(Config(source='dblp', cache=False, source_store=store), Budget(3))
                original = SourceTransportError('fixture.invalid', TimeoutError('private-remote-text'))
                with patch.object(source, '_request_bytes', side_effect=original) as request, \
                     patch.object(store, 'cooldown', side_effect=sqlite3.OperationalError('private-db-text')), \
                     bind_events(lambda action, payload: events.append((action, payload))):
                    with self.assertRaises(SourceTransportError) as caught:
                        source._fetch('https://fixture.invalid/private-query', provider='dblp')
                self.assertIs(caught.exception, original)
                self.assertEqual(request.call_count, 1)
                failures = [payload for action, payload in events if action == 'source_cooldown_failed']
                self.assertEqual(len(failures), 1)
                self.assertEqual({k: v for k, v in failures[0].items() if k != 'at'},
                                 {'source': 'dblp', 'status': 'unknown', 'reason': 'source_store_unavailable'})
                for secret in ('private-remote-text', 'private-db-text', 'private-query'):
                    self.assertNotIn(secret, str(events))
                self.assertGreater(source.cooldown['dblp'], 0)
            finally:
                store.close()

    def test_pause_event_never_contains_unrecognized_provider_or_failure_text(self):
        from unittest.mock import Mock
        from execution_events import bind_events
        events = []
        source = AcademicSource(Config(source='dblp', cache=False), Budget(3))
        source.store = Mock()
        source.store.cooldown.side_effect = OSError('private-db-text')
        with bind_events(lambda action, payload: events.append((action, payload))):
            source._pause('private-provider', 2, 'private-remote-text')
        self.assertEqual(events[0][0], 'source_cooldown_failed')
        self.assertEqual(events[0][1]['source'], 'unknown')
        for text in ('private-provider', 'private-db-text', 'private-remote-text'):
            self.assertNotIn(text, str(events))

    def test_cooldown_check_respects_cancel_and_expired_budget(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from common import BudgetExceeded, RunCancelled
        from source_store import SourceStore
        with TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 'source.db')
            try:
                source = AcademicSource(Config(source='dblp', cache=False, source_store=store), Budget(3))
                with patch.object(store, 'cooldown') as cooldown, patch.object(source, '_search_one') as dispatch:
                    with patch.object(source.budget, 'check_cancelled', side_effect=RunCancelled()):
                        with self.assertRaises(RunCancelled):
                            source.search_batch('fixture')
                    with patch.object(source.budget, 'remaining', side_effect=BudgetExceeded('expired')):
                        with self.assertRaises(BudgetExceeded):
                            source.search_batch('fixture')
                    cooldown.assert_not_called()
                    dispatch.assert_not_called()
                self.assertEqual(source.budget.search_http_calls, 0)
            finally:
                store.close()

    def test_failed_provider_run_updates_health_and_code(self):
        source = AcademicSource(Config(source='dblp'), Budget(10))
        source._search_one = lambda *args, **kwargs: (_ for _ in ()).throw(
            GuideError('DBLP returned an HTML challenge (bot protection)'))
        with self.assertRaises(GuideError):
            source.search_batch('RAG', providers=['dblp'])
        health = source.health_snapshot()
        self.assertEqual(health['dblp']['last_code'], 'bot_challenge')
        self.assertGreaterEqual(health['dblp']['failures'], 1)

    def test_local_quota_failure_retains_actionable_code(self):
        from lune_quota import LuneQuotaStore
        from tempfile import TemporaryDirectory
        from source import classify_source_error
        with TemporaryDirectory() as tmp:
            with LuneQuotaStore(Path(tmp) / 'quota.sqlite3') as ledger:
                with self.assertRaises(GuideError) as caught:
                    ledger.reserve('tools/call')
        self.assertEqual(classify_source_error(caught.exception), 'quota_no_snapshot')
        source = AcademicSource(Config(source='openalex', cache=False), Budget(10))
        def reject(*args, **kwargs):
            raise caught.exception
        source._search_one = reject
        with self.assertRaises(GuideError) as result:
            source.search_batch('fixture', providers=['openalex'])
        self.assertEqual(getattr(result.exception, 'code', None), 'quota_no_snapshot')
        self.assertEqual(source.health_snapshot()['openalex']['last_code'], 'quota_no_snapshot')

    def test_success_cooldown_and_audit_failures_do_not_erase_result(self):
        from unittest.mock import patch
        from execution_events import bind_events
        from source_store import SourceStore
        import sqlite3
        from tempfile import TemporaryDirectory
        events = []
        with TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 'source.db')
            try:
                source = AcademicSource(Config(source='dblp', cache=False, source_store=store), Budget(3))
                with patch.object(source, '_request_bytes', return_value=b'{}') as request, \
                     patch.object(store, 'cooldown', side_effect=sqlite3.OperationalError('private-db-text')), \
                     bind_events(lambda action, payload: events.append((action, payload))):
                    self.assertEqual(source._fetch('https://fixture.invalid', provider='dblp'), b'{}')
                    self.assertEqual(request.call_count, 1)
                source._search_one = lambda *args: []
                with patch.object(store, 'record_run', side_effect=sqlite3.OperationalError('private-db-text')), \
                     bind_events(lambda action, payload: events.append((action, payload))):
                    batch = source.search_batch('private-query', providers=['dblp'])
                self.assertEqual(batch.runs[0].status, 'empty')
                self.assertTrue(any(a == 'source_audit_failed' for a, _ in events))
                self.assertNotIn('private-db-text', str(events))
            finally:
                store.close()

    def test_failure_audit_uses_independent_bounded_budget_and_signals_failure(self):
        import sqlite3
        import time
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from execution_events import bind_events
        from source import SourceRun
        from source_store import SourceStore
        events = []
        with TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 'source.db')
            blocker = sqlite3.connect(str(store.path), isolation_level=None)
            try:
                budget = Budget(.01)
                source = AcademicSource(Config(source='dblp', cache=False, source_store=store), budget)
                time.sleep(.02)
                run = SourceRun('dblp', 'error', error='private-remote-text', code='timeout')
                source.record_run(run)
                self.assertEqual(store.health_snapshot()['dblp']['last_code'], 'timeout')
                blocker.execute('BEGIN EXCLUSIVE')
                start = time.monotonic()
                with bind_events(lambda action, payload: events.append((action, payload))):
                    source.record_run(run)
                self.assertLess(time.monotonic() - start, 1)
                self.assertTrue(any(a == 'source_audit_failed' for a, _ in events))
                self.assertNotIn('private-remote-text', str(events))
            finally:
                if blocker.in_transaction:
                    blocker.execute('ROLLBACK')
                blocker.close()
                store.close()

    def test_failed_search_event_omits_query_filters_and_remote_error(self):
        from execution_events import bind_events
        events = []
        source = AcademicSource(Config(source='dblp', cache=False), Budget(2))
        def fail(*args):
            raise GuideError('private-remote-text')
        source._search_one = fail
        with bind_events(lambda action, payload: events.append((action, payload))):
            with self.assertRaises(GuideError):
                source.search_batch('private-query', providers=['dblp'], filters={'venue': 'private-filter'})
        failures = [payload for action, payload in events if action == 'tool_error']
        self.assertTrue(failures)
        for secret in ('private-query', 'private-filter', 'private-remote-text'):
            self.assertNotIn(secret, str(failures))

    def test_failure_audit_deadline_does_not_replace_provider_error(self):
        import time
        from unittest.mock import patch
        from tempfile import TemporaryDirectory
        from source_store import SourceStore
        with TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 'source.db')
            try:
                source = AcademicSource(Config(source='dblp', cache=False, source_store=store), Budget(.1))
                source._search_one = lambda *args: (_ for _ in ()).throw(GuideError('provider fixture error'))
                def slow_audit(run):
                    time.sleep(.15)
                    raise OSError('local fixture failure')
                with patch.object(store, 'record_run', side_effect=slow_audit):
                    with self.assertRaises(GuideError) as caught:
                        source.search_batch('fixture', providers=['dblp'])
                self.assertEqual(str(caught.exception), 'provider fixture error')
            finally:
                store.close()

    def test_success_clears_previous_failure_count(self):
        source = AcademicSource(Config(source='openalex', cache=False), Budget(10))
        source._search_one = lambda *args, **kwargs: []
        batch = source.search_batch('RAG', providers=['openalex'])
        self.assertEqual(batch.runs[0].status, 'empty')
        health = source.health_snapshot()
        self.assertEqual(health['openalex']['failures'], 0)
        self.assertEqual(health['openalex']['last_status'], 'empty')
