import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_delivery import isolated_environment
from common import Budget, BudgetExceeded, Config, GuideError, Paper, RunCancelled
from source import AcademicSource, SearchBatch, SourceRun, retained_partial_batch
from source_store import SourceStore


class PriorCandidatesCancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = isolated_environment(self.root, inherited=dict(os.environ))
        env['PYTHONPATH'] = os.environ.get('PYTHONPATH', '')
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.store = SourceStore(self.root / 'sources.sqlite3', quota_path=self.root / 'ledger.sqlite3')
        self.addCleanup(self.store.close)
        self.budget = Budget(10)
        config = Config(source='openalex,lune,deepseek', cache=True, cache_dir=self.root / 'cache')
        config.source_store = self.store
        self.client = AcademicSource(config, self.budget)
        self.paper = Paper('openalex:W1', 'Candidate', 'Abstract', 'https://example.invalid/1',
                           year=2025, sources=['openalex'])
        self.old = Paper('openalex:W2', 'Old', 'Abstract', 'https://example.invalid/2',
                         year=2020, sources=['openalex'])

    def run_case(self, failure, cancel_after_failure=False):
        seen = []
        def search(name, *args):
            seen.append(name)
            if name == 'openalex':
                return [self.paper, self.old]
            if cancel_after_failure:
                self.budget.cancel_token.request()
            raise failure
        with patch.object(self.client, 'available', return_value=True), \
             patch.object(self.client, 'record_run') as audit, \
             patch.object(self.client, '_search_one', side_effect=search):
            expected = RunCancelled if cancel_after_failure else type(failure)
            with self.assertRaises(expected) as caught:
                self.client.search_batch('fixture', providers=['openalex', 'lune', 'deepseek'],
                                         filters={'year_from': 2024})
        self.assertEqual(seen, ['openalex', 'lune'])
        self.assertEqual(audit.call_count, 1)
        self.assertEqual(self.store._connection.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        self.assertFalse((self.root / 'cache').exists())
        error = caught.exception
        self.assertEqual(error.remote_outcome, failure.remote_outcome)
        self.assertIs(error.retry_safe, failure.retry_safe)
        batch = retained_partial_batch(error, 'fixture', ['openalex', 'lune', 'deepseek'], 10)
        self.assertIsNotNone(batch)
        self.assertEqual([p.id for p in batch.papers], ['openalex:W1'])
        self.assertEqual(batch.runs[0].name, 'openalex')
        self.assertEqual(batch.runs[0].remote_outcome, 'completed')
        self.assertEqual(batch.runs[-1].name, 'lune')
        self.assertEqual(batch.runs[-1].remote_outcome, failure.remote_outcome)
        self.assertIs(batch.runs[-1].retry_safe, failure.retry_safe)
        self.assertIsNone(retained_partial_batch(error, 'wrong', ['openalex', 'lune', 'deepseek'], 10))
        self.assertIsNone(retained_partial_batch(error, 'fixture', ['lune', 'deepseek'], 10))
        return error, batch

    def test_not_started_cancel_and_budget(self):
        for kind in (RunCancelled, BudgetExceeded):
            with self.subTest(kind=kind):
                error = kind()
                error.remote_outcome, error.retry_safe = 'not_started', True
                caught, batch = self.run_case(error)
                self.assertIs(caught, error)
                self.assertEqual(batch.runs[-1].status, 'not_started')

    def test_failure_then_cancellation_retains_original_outcome(self):
        for outcome, retry in [('unknown', False), ('unknown', True), ('error', False), ('not_started', True)]:
            with self.subTest(outcome=outcome):
                self.budget = Budget(10)
                self.client.budget = self.budget
                error = GuideError('synthetic failure')
                error.remote_outcome, error.retry_safe = outcome, retry
                caught, batch = self.run_case(error, cancel_after_failure=True)
                self.assertIs(caught.__context__, error)
                self.assertEqual(batch.stop_reason, 'user_cancelled')

    def test_local_completed_cancel_and_unknown_tail(self):
        error = RunCancelled()
        success = SourceRun('openalex', 'ok', 1, remote_outcome='completed', retry_safe=False)
        error.partial_batch = SearchBatch('fixture', ['openalex', 'lune'], [self.paper],
                                          [success], stop_reason='user_cancelled')
        batch = retained_partial_batch(error, 'fixture', ['openalex', 'lune'], 10)
        self.assertIsNotNone(batch)
        self.assertIsNot(batch, error.partial_batch)
        self.assertIsNone(retained_partial_batch(error, 'fixture', ['lune'], 10))
        unknown = SourceRun('lune', 'partial', remote_outcome='unknown', retry_safe=False)
        error.partial_batch.runs.append(unknown)
        batch = retained_partial_batch(error, 'fixture', ['openalex', 'lune'], 10)
        self.assertEqual(batch.runs[-1].remote_outcome, 'unknown')
        unknown.retry_safe = True
        self.assertIsNone(retained_partial_batch(error, 'fixture', ['openalex', 'lune'], 10))

    def test_timeout_is_recomputed_after_final_authorization(self):
        import io
        import time
        from contextlib import nullcontext
        from unittest.mock import patch
        from common import Budget, Config
        from source import AcademicSource
        budget = Budget(.7)
        source = AcademicSource(Config(source='deepseek', deepseek_search_key='fixture', cache=False), budget)
        original = source._deepseek_target_key
        captured = []
        def delayed(target):
            time.sleep(.18)
            return original(target)
        def send(request, timeout):
            captured.append((timeout, budget.remaining()))
            return io.BytesIO(b'{}')
        with patch.object(source, '_source_slot', return_value=nullcontext()), \
             patch.object(source, '_wait_for'), patch.object(source, '_wait_for_cooldown', return_value=False), \
             patch.object(source, '_deepseek_target_key', side_effect=delayed), patch('source.urlopen', side_effect=send):
            source._request_bytes('https://api.deepseek.com/v1/tools/search', 'deepseek',
                                  {'Authorization': 'Bearer fixture'}, b'{}', 'POST', 1)
        self.assertEqual(len(captured), 1)
        self.assertLessEqual(captured[0][0], captured[0][1] + .03)

    def test_normal_multisource_is_not_blocked(self):
        seen = []
        def search(name, *args):
            seen.append(name)
            return [self.paper] if name == 'openalex' else []
        with patch.object(self.client, 'available', return_value=True), \
             patch.object(self.client, 'record_run'), \
             patch.object(self.client, '_search_one', side_effect=search):
            batch = self.client.search_batch('fixture', providers=['openalex', 'lune', 'deepseek'])
        self.assertEqual(seen, ['openalex', 'lune', 'deepseek'])
        self.assertEqual([p.id for p in batch.papers], ['openalex:W1'])


if __name__ == '__main__':
    unittest.main()
