"""Tests for the local Lune quota ledger.

Everything here is local and offline: temporary SQLite files only, no network,
no credentials, no real ledger.  The suite covers fail-closed behaviour,
conservative accounting, re-confirmation safety, input validation, and
contention between threads and between spawned processes.
"""
from __future__ import annotations

import contextlib
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import unittest

from common import GuideError
from lune_quota import (
    ACCOUNT_DAILY_LIMIT,
    ALLOWED_METHODS,
    CODE_CORRUPT,
    CODE_EXHAUSTED,
    CODE_EXPIRED,
    CODE_INVALID_INPUT,
    CODE_INVALID_METHOD,
    CODE_INVALID_OUTCOME,
    CODE_NOT_FOUND,
    CODE_NO_SNAPSHOT,
    CODE_PENDING_RESERVED,
    LuneQuotaStore,
)

T0 = 1_000_000.0


def _spawn_reserve_worker(path, queue, now):
    """Top-level worker so that Windows ``spawn`` can pickle it."""
    try:
        from common import GuideError as _GuideError
        from lune_quota import LuneQuotaStore as _Store

        store = _Store(path)
        try:
            queue.put(('ok', store.reserve('search_papers', now=now)))
        except _GuideError as exc:
            queue.put(('error', getattr(exc, 'code', 'unknown')))
        finally:
            store.close()
    except Exception as exc:  # pragma: no cover - defensive
        queue.put(('crash', type(exc).__name__))


class LuneQuotaTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'ledger.sqlite')
        self.store = LuneQuotaStore(self.path)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)

    def _event_count(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return conn.execute('SELECT COUNT(*) FROM quota_events').fetchone()[0]

    def _rows(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(
                'SELECT id, created_at, method, status FROM quota_events'
            ).fetchall()

    # -- fail closed ---------------------------------------------------------
    def test_reserve_without_snapshot_fails_closed(self):
        with self.assertRaises(GuideError) as ctx:
            self.store.reserve('search_papers', now=T0)
        self.assertEqual(CODE_NO_SNAPSHOT, ctx.exception.code)
        self.assertEqual(0, self._event_count())

    def test_expired_snapshot_fails_closed_and_needs_new_confirmation(self):
        self.store.confirm(10, T0 + 100, now=T0)
        with self.assertRaises(GuideError) as ctx:
            self.store.reserve('search_papers', now=T0 + 200)
        self.assertEqual(CODE_EXPIRED, ctx.exception.code)
        self.assertEqual(0, self._event_count())
        # a fresh confirmation (authorised by the host) makes it usable again
        self.store.confirm(10, T0 + 1000, now=T0 + 200)
        self.assertTrue(self.store.reserve('search_papers', now=T0 + 200))

    def test_exhausted_snapshot_fails_closed(self):
        self.store.confirm(2, T0 + 100, now=T0)
        self.store.reserve('search_papers', now=T0)
        self.store.reserve('search_papers', now=T0)
        with self.assertRaises(GuideError) as ctx:
            self.store.reserve('search_papers', now=T0)
        self.assertEqual(CODE_EXHAUSTED, ctx.exception.code)
        self.assertEqual(2, self._event_count())

    def test_corrupt_snapshot_fails_closed(self):
        self.store.confirm(5, T0 + 100, now=T0)
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute('UPDATE quota_snapshot SET remaining = 999 WHERE id = 1')
            conn.commit()
        with self.assertRaises(GuideError) as ctx:
            self.store.reserve('search_papers', now=T0)
        self.assertEqual(CODE_CORRUPT, ctx.exception.code)

    def test_broken_ledger_file_fails_closed(self):
        broken = os.path.join(self._tmp.name, 'broken.sqlite')
        with open(broken, 'wb') as handle:
            handle.write(b'this is definitely not a sqlite database' * 64)
        with self.assertRaises(GuideError) as caught:
            LuneQuotaStore(broken)
        self.assertIn(caught.exception.code, (CODE_CORRUPT, 'quota_ledger_unavailable'))

    def test_legacy_schema_preserves_rows_and_accepts_session_methods(self):
        path = os.path.join(self._tmp.name, 'legacy.sqlite')
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.executescript('''
                CREATE TABLE quota_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE quota_snapshot (id INTEGER PRIMARY KEY, window_id INTEGER NOT NULL,
                    remaining INTEGER NOT NULL, expires_at REAL NOT NULL, confirmed_at REAL NOT NULL);
                CREATE TABLE quota_events (id TEXT PRIMARY KEY, window_id INTEGER NOT NULL,
                    created_at REAL NOT NULL, method TEXT NOT NULL, status TEXT NOT NULL);
                INSERT INTO quota_meta VALUES ('schema_version', '1');
                INSERT INTO quota_snapshot VALUES (1, 7, 4, 1000100, 1000000);
                INSERT INTO quota_events VALUES ('old', 7, 1000000, 'ping', 'unknown');
            ''')
            conn.commit()
        with LuneQuotaStore(path) as store:
            self.assertEqual(store.status(now=T0)['remaining_from_snapshot'], 3)
            for method in ('notifications/initialized', 'session/delete'):
                reservation = store.reserve(method, now=T0)
                store.finish(reservation, 'unknown')
            self.assertEqual(store.status(now=T0)['remaining_from_snapshot'], 1)
        with contextlib.closing(sqlite3.connect(path)) as conn:
            rows = conn.execute('SELECT id, method, status FROM quota_events ORDER BY created_at, id').fetchall()
        self.assertIn(('old', 'ping', 'unknown'), rows)
        self.assertEqual({row[1] for row in rows}, {'ping', 'notifications/initialized', 'session/delete'})

    # -- accounting ----------------------------------------------------------
    def test_normal_consumption_is_recorded(self):
        self.store.confirm(3, T0 + 100, now=T0)
        reservation = self.store.reserve('search_papers', now=T0)
        self.assertTrue(reservation)
        status = self.store.status(now=T0)
        self.assertEqual(1, status['local_reserved'])
        self.assertEqual(2, status['remaining_from_snapshot'])
        self.assertTrue(status['external_usage_unknown'])
        self.store.finish(reservation, 'ok')
        status = self.store.status(now=T0)
        self.assertEqual(0, status['local_reserved'])
        self.assertEqual(2, status['remaining_from_snapshot'])  # spent stays spent

    def test_failed_and_unknown_outcomes_are_not_refunded(self):
        self.store.confirm(2, T0 + 100, now=T0)
        first = self.store.reserve('search_papers', now=T0)
        second = self.store.reserve('get_paper_citations', now=T0)
        self.store.finish(first, 'error')
        self.store.finish(second, 'unknown')
        status = self.store.status(now=T0)
        self.assertEqual(0, status['remaining_from_snapshot'])
        self.assertEqual(0, status['local_reserved'])
        with self.assertRaises(GuideError) as ctx:
            self.store.reserve('search_papers', now=T0)
        self.assertEqual(CODE_EXHAUSTED, ctx.exception.code)

    def test_duplicate_finish_is_idempotent(self):
        self.store.confirm(3, T0 + 100, now=T0)
        reservation = self.store.reserve('search_papers', now=T0)
        self.store.finish(reservation, 'ok')
        self.store.finish(reservation, 'ok')
        self.store.finish(reservation, 'error')  # no resurrection, no refund
        status = self.store.status(now=T0)
        self.assertEqual(0, status['local_reserved'])
        self.assertEqual(2, status['remaining_from_snapshot'])
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual('ok', rows[0][3])

    def test_crash_leftover_reservation_counts_as_consumed(self):
        self.store.confirm(3, T0 + 100, now=T0)
        self.store.reserve('search_papers', now=T0)  # never finished
        status = self.store.status(now=T0)
        self.assertEqual(1, status['local_reserved'])
        self.assertEqual(2, status['remaining_from_snapshot'])

    def test_finish_of_unknown_id_is_rejected(self):
        self.store.confirm(3, T0 + 100, now=T0)
        with self.assertRaises(GuideError) as ctx:
            self.store.finish('does-not-exist', 'ok')
        self.assertEqual(CODE_NOT_FOUND, ctx.exception.code)

    # -- re-confirmation safety ---------------------------------------------
    def test_reconfirm_is_refused_while_reservations_are_pending(self):
        self.store.confirm(5, T0 + 100, now=T0)
        reservation = self.store.reserve('search_papers', now=T0)
        with self.assertRaises(GuideError) as ctx:
            self.store.confirm(300, T0 + 1000, now=T0 + 1)
        self.assertEqual(CODE_PENDING_RESERVED, ctx.exception.code)
        # the pending reservation is still intact
        self.assertEqual(1, self.store.status(now=T0 + 1)['local_reserved'])
        self.store.finish(reservation, 'ok')
        self.store.confirm(4, T0 + 100, now=T0 + 2)
        status = self.store.status(now=T0 + 2)
        self.assertEqual(4, status['remaining_from_snapshot'])
        self.assertEqual(0, status['local_reserved'])

    def test_reconfirm_starts_a_fresh_window(self):
        self.store.confirm(2, T0 + 100, now=T0)
        first = self.store.reserve('search_papers', now=T0)
        second = self.store.reserve('search_papers', now=T0)
        self.store.finish(first, 'ok')
        self.store.finish(second, 'ok')
        self.assertEqual(0, self.store.status(now=T0)['remaining_from_snapshot'])
        self.store.confirm(2, T0 + 200, now=T0 + 101)
        self.assertEqual(2, self.store.status(now=T0 + 101)['remaining_from_snapshot'])

    # -- input validation ----------------------------------------------------
    def test_invalid_confirm_input_is_rejected_and_writes_nothing(self):
        for bad in (-1, ACCOUNT_DAILY_LIMIT + 1, 1.5, '300', True, None):
            with self.assertRaises(GuideError) as ctx:
                self.store.confirm(bad, T0 + 100, now=T0)
            self.assertEqual(CODE_INVALID_INPUT, ctx.exception.code)
        for bad in (T0 - 1, T0, float('inf'), float('nan'), 'soon', None):
            with self.assertRaises(GuideError) as ctx:
                self.store.confirm(5, bad, now=T0)
            self.assertEqual(CODE_INVALID_INPUT, ctx.exception.code)
        self.assertEqual(0, self._event_count())
        status = self.store.status(now=T0)
        self.assertFalse(status['snapshot_present'])

    def test_invalid_method_is_rejected_and_writes_nothing(self):
        self.store.confirm(5, T0 + 100, now=T0)
        for bad in ('search', 'drop_table', 'search_papers?query=secret', '', None, 42):
            with self.assertRaises(GuideError) as ctx:
                self.store.reserve(bad, now=T0)
            self.assertEqual(CODE_INVALID_METHOD, ctx.exception.code)
        self.assertEqual(0, self._event_count())

    def test_invalid_outcome_is_rejected_and_writes_nothing(self):
        self.store.confirm(5, T0 + 100, now=T0)
        reservation = self.store.reserve('search_papers', now=T0)
        with self.assertRaises(GuideError) as ctx:
            self.store.finish(reservation, 'maybe')
        self.assertEqual(CODE_INVALID_OUTCOME, ctx.exception.code)
        self.assertEqual('reserved', self._rows()[0][3])

    def test_ledger_contains_no_secrets_or_parameters(self):
        self.store.confirm(5, T0 + 100, now=T0)
        self.store.reserve('search_papers', now=T0)
        self.store.reserve('list_conferences', now=T0)
        blobs = []
        for name in os.listdir(os.path.dirname(self.path)):
            if name.startswith('ledger.sqlite'):
                with open(os.path.join(os.path.dirname(self.path), name), 'rb') as handle:
                    blobs.append(handle.read().lower())
        for secret in (b'lune_api_key', b'bearer ', b'password', b'secret', b'sk-'):
            for blob in blobs:
                self.assertNotIn(secret, blob)
        for row in self._rows():
            self.assertIn(row[2], ALLOWED_METHODS)
            self.assertEqual(4, len(row))

    def test_stale_reconfirmation_cannot_restore_spent_allowance(self):
        self.store.confirm(2, T0 + 100, now=T0)
        reservation = self.store.reserve('tools/call', now=T0 + 1)
        self.store.finish(reservation, 'ok')
        with self.assertRaises(GuideError):
            self.store.confirm(2, T0 + 100, now=T0 + 2)
        self.assertEqual(self.store.status(now=T0 + 2)['remaining_from_snapshot'], 1)

    def test_reconcile_rejects_reservation_owned_by_live_process(self):
        self.store.confirm(2, T0 + 100, now=T0)
        reservation = self.store.reserve('tools/call', now=T0 + 1)
        with self.assertRaises(GuideError):
            self.store.reconcile_unknown(reservation)
        self.assertEqual(self.store.pending()[0]['id'], reservation)

    def test_reconcile_dead_owner_marks_unknown_without_refund(self):
        from unittest.mock import patch
        self.store.confirm(2, T0 + 100, now=T0)
        reservation = self.store.reserve('tools/call', now=T0 + 1)
        with patch('lune_quota._process_alive', return_value=False):
            self.store.reconcile_unknown(reservation)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.status(now=T0 + 2)['remaining_from_snapshot'], 1)

    def test_pending_crash_reservation_can_be_inspected_and_closed_as_unknown(self):
        self.store.confirm(2, T0 + 100, now=T0)
        reservation = self.store.reserve('tools/call', now=T0 + 1)
        pending = self.store.pending()
        self.assertEqual([entry['id'] for entry in pending], [reservation])
        self.store.finish(reservation, 'unknown')
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.status(now=T0 + 2)['remaining_from_snapshot'], 1)

    def test_local_available_does_not_double_count_pending_reservations(self):
        self.store.confirm(3, T0 + 100, now=T0)
        self.store.reserve('search_papers', now=T0)
        status = self.store.status(now=T0)
        self.assertEqual(status['remaining_from_snapshot'], 2)
        self.assertEqual(status['local_available'], 2)

    def test_snapshot_expiry_is_rechecked_after_acquiring_write_lock(self):
        from unittest.mock import patch
        self.store.confirm(1, T0 + 1, now=T0)
        with patch('lune_quota.time.time', side_effect=[T0, T0 + 2]):
            with self.assertRaises(GuideError) as caught:
                self.store.reserve('tools/call')
        self.assertEqual(caught.exception.code, CODE_EXPIRED)
        self.assertEqual(self._event_count(), 0)

    def test_expired_snapshot_has_no_locally_available_requests(self):
        self.store.confirm(3, T0 + 100, now=T0)
        status = self.store.status(now=T0 + 100)
        self.assertTrue(status['expired'])
        self.assertEqual(status['local_available'], 0)

    def test_discovery_and_prompt_methods_consume_same_shared_allowance(self):
        self.store.confirm(2, T0 + 100, now=T0)
        self.store.reserve('tools/list', now=T0)
        self.store.reserve('prompts/get', now=T0)
        with self.assertRaises(GuideError) as caught:
            self.store.reserve('tools/call', now=T0)
        self.assertEqual(caught.exception.code, CODE_EXHAUSTED)

    def test_status_never_claims_the_account_balance(self):
        self.store.confirm(300, T0 + 100, now=T0)
        status = self.store.status(now=T0)
        self.assertTrue(status['external_usage_unknown'])
        self.assertFalse(status['account_remaining_known'])
        self.assertEqual(300, status['remaining_from_snapshot'])
        self.assertEqual(T0 + 100, status['expires_at'])

    # -- contention ----------------------------------------------------------
    def test_eight_threads_contend_for_the_last_units(self):
        self.store.confirm(5, T0 + 100, now=T0)
        successes = []
        failures = []
        lock = threading.Lock()

        def worker(index):
            store = LuneQuotaStore(self.path)
            try:
                reservation = store.reserve('search_papers', now=T0)
                with lock:
                    successes.append(reservation)
            except GuideError as exc:
                with lock:
                    failures.append(getattr(exc, 'code', 'unknown'))
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(5, len(successes))
        self.assertEqual(3, len(failures))
        self.assertEqual({CODE_EXHAUSTED}, set(failures))
        self.assertEqual(5, self._event_count())
        self.assertEqual(0, self.store.status(now=T0)['remaining_from_snapshot'])

    def test_two_spawned_processes_contend_for_the_last_unit(self):
        context = multiprocessing.get_context('spawn')
        self.store.confirm(1, T0 + 100, now=T0)
        self.store.close()
        queue = context.Queue()
        workers = [
            context.Process(target=_spawn_reserve_worker, args=(self.path, queue, T0)),
            context.Process(target=_spawn_reserve_worker, args=(self.path, queue, T0)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(120)
        results = [queue.get(timeout=30) for _ in workers]
        for worker in workers:
            self.assertEqual(0, worker.exitcode)
        outcomes = sorted(kind for kind, _ in results)
        self.assertEqual(['error', 'ok'], outcomes)
        error_code = [payload for kind, payload in results if kind == 'error'][0]
        self.assertEqual(CODE_EXHAUSTED, error_code)
        self.assertEqual(1, self._event_count())

    def test_two_spawned_processes_share_one_allowance_without_overshooting(self):
        context = multiprocessing.get_context('spawn')
        self.store.confirm(2, T0 + 100, now=T0)
        self.store.close()
        queue = context.Queue()
        workers = [
            context.Process(target=_spawn_reserve_worker, args=(self.path, queue, T0)),
            context.Process(target=_spawn_reserve_worker, args=(self.path, queue, T0)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(120)
        results = [queue.get(timeout=30) for _ in workers]
        self.assertEqual(2, sum(1 for kind, _ in results if kind == 'ok'))
        self.assertEqual(2, self._event_count())


if __name__ == '__main__':
    unittest.main()
