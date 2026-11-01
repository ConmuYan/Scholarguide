"""Auditor regressions: shared in-flight caps and accurate transport failures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import multiprocessing
import os
import sqlite3
from pathlib import Path
from queue import Empty
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

from common import Budget, BudgetExceeded, Config, GuideError, RunCancelled
from execution_events import bind_events
from source import AcademicSource, SourceTransportError, classify_source_error
import source_store
from source_store import SourceStore


def _hold_process_slot(path, entered, release, errors):
    store = SourceStore(path)
    try:
        with store.request_slot('openalex', Budget(10)):
            entered.put('entered')
            if not release.wait(6):
                raise RuntimeError('test did not release the request')
    except Exception as exc:
        errors.put(type(exc).__name__)
    finally:
        store.close()


class SourceInflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'sources.sqlite3'
        self.store = SourceStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_search_setup_and_audit_cancel_on_locked_store(self):
        blocker = sqlite3.connect(str(self.path), isolation_level=None)
        blocker.execute('BEGIN EXCLUSIVE')
        budget = Budget(3)
        client = AcademicSource(Config(source='dblp', source_store=self.store, cache=False), budget)
        try:
            with patch('source.urlopen') as transport, ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.search_batch, 'fixture')
                time.sleep(.1)
                budget.cancel_token.request()
                with self.assertRaises(RunCancelled):
                    future.result(timeout=1)
                transport.assert_not_called()
        finally:
            blocker.execute('ROLLBACK'); blocker.close()

    def test_scoped_budgets_are_thread_local_nested_and_cleanup_can_override(self):
        outer, inner, other = Budget(3), Budget(3), Budget(3)
        outer.cancel_token.request()
        barrier = threading.Barrier(2)
        def isolated():
            with self.store.budget_scope(other):
                barrier.wait(timeout=2)
                return self.store._row('dblp')['name']
        with self.store.budget_scope(outer), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(isolated)
            barrier.wait(timeout=2)
            with self.assertRaises(RunCancelled):
                self.store._row('dblp')
            with self.store.budget_scope(inner):
                self.assertEqual(self.store._row('dblp')['name'], 'dblp')
                with self.store._db(budget=Budget(1)) as db:
                    self.assertEqual(db.execute('SELECT 1').fetchone()[0], 1)
            with self.assertRaises(RunCancelled):
                self.store._row('dblp')
            self.assertEqual(future.result(timeout=2), 'dblp')
        self.assertEqual(self.store._row('dblp')['name'], 'dblp')

    def test_nested_db_savepoints_and_outer_rollback(self):
        for budget in (None, Budget(2)):
            with self.subTest(budget=budget):
                with self.assertRaisesRegex(ValueError, 'outer'):
                    with self.store._db(budget=budget) as db:
                        with self.store._db(budget=budget) as inner:
                            inner.execute("UPDATE sources SET priority=77 WHERE name='dblp'")
                        raise ValueError('outer')
                self.assertNotEqual(self.store._row('dblp')['priority'], 77)
                with self.store._db(budget=budget) as db:
                    try:
                        with self.store._db(budget=budget) as inner:
                            inner.execute("UPDATE sources SET priority=88 WHERE name='dblp'")
                            raise ValueError('inner')
                    except ValueError as exc:
                        self.assertEqual(str(exc), 'inner')
                    self.assertNotEqual(db.execute("SELECT priority FROM sources WHERE name='dblp'").fetchone()[0], 88)

    def test_busy_timeout_cleanup_preserves_original_error(self):
        connection = self.store._connection
        class Proxy:
            def execute(self, sql, *args):
                if sql == 'PRAGMA busy_timeout=10000':
                    raise sqlite3.OperationalError('cleanup failed')
                return connection.execute(sql, *args)
            def __enter__(self):
                return connection.__enter__()
            def __exit__(self, *args):
                return connection.__exit__(*args)
            def __getattr__(self, name):
                return getattr(connection, name)
        self.store._connection = Proxy()
        try:
            error = ValueError('original')
            with self.assertRaises(ValueError) as caught:
                with self.store._db(budget=Budget(1)):
                    raise error
            self.assertIs(caught.exception, error)
            with self.assertRaises(GuideError) as unavailable:
                self.store._row('dblp')
            self.assertEqual(unavailable.exception.code, 'source_store_unusable')
        finally:
            self.store._connection = connection

    def test_timeout_restore_failure_preserves_cancel_and_rollback(self):
        real = self.store._connection
        class Proxy:
            def execute(self, sql, *args):
                if sql == 'PRAGMA busy_timeout=10000':
                    raise sqlite3.OperationalError('cleanup failed')
                return real.execute(sql, *args)
            def __enter__(self): return real.__enter__()
            def __exit__(self, *args): return real.__exit__(*args)
            def __getattr__(self, name): return getattr(real, name)
        self.store._connection = Proxy()
        error = RunCancelled()
        with self.assertRaises(RunCancelled) as caught:
            with self.store._db(budget=Budget(1)) as db:
                db.execute("UPDATE sources SET priority=94 WHERE name='dblp'")
                raise error
        self.assertIs(caught.exception, error)
        with self.assertRaises(GuideError) as unavailable:
            self.store._row('dblp')
        self.assertEqual(unavailable.exception.code, 'source_store_unusable')
        with closing(sqlite3.connect(self.path)) as check:
            self.assertNotEqual(check.execute("SELECT priority FROM sources WHERE name='dblp'").fetchone()[0], 94)

    def test_nested_savepoint_failures_poison_outer_transaction(self):
        for failure_sql, body_fails in [('ROLLBACK TO', True), ('RELEASE', True), ('RELEASE', False)]:
            with self.subTest(failure_sql=failure_sql, body_fails=body_fails):
                store = SourceStore(self.path)
                real = store._connection
                class Proxy:
                    def execute(self, sql, *args):
                        if sql.startswith(failure_sql):
                            raise sqlite3.OperationalError('injected savepoint failure')
                        return real.execute(sql, *args)
                    def __enter__(self): return real.__enter__()
                    def __exit__(self, *args): return real.__exit__(*args)
                    def __getattr__(self, name): return getattr(real, name)
                store._connection = Proxy()
                try:
                    with self.assertRaises(GuideError):
                        with store._db() as db:
                            try:
                                with store._db() as inner:
                                    inner.execute("UPDATE sources SET priority=91 WHERE name='dblp'")
                                    if body_fails:
                                        raise ValueError('original')
                            except (ValueError, sqlite3.OperationalError) as exc:
                                if body_fails:
                                    self.assertEqual(str(exc), 'original')
                    with closing(sqlite3.connect(self.path)) as check:
                        self.assertNotEqual(check.execute("SELECT priority FROM sources WHERE name='dblp'").fetchone()[0], 91)
                finally:
                    store.close()

    def test_uncaught_nested_original_survives_savepoint_and_outer_rollback_failure(self):
        real = self.store._connection
        class Proxy:
            def execute(self, sql, *args):
                if sql.startswith('ROLLBACK TO'):
                    raise sqlite3.OperationalError('savepoint failed')
                return real.execute(sql, *args)
            def __enter__(self): return real.__enter__()
            def __exit__(self, typ, value, tb):
                if typ is not None:
                    raise sqlite3.OperationalError('outer rollback failed')
                return real.__exit__(typ, value, tb)
            def __getattr__(self, name): return getattr(real, name)
        self.store._connection = Proxy()
        error = RunCancelled()
        with self.assertRaises(RunCancelled) as caught:
            with self.store._db(budget=Budget(2)):
                with self.store._db() as inner:
                    inner.execute("UPDATE sources SET priority=93 WHERE name='dblp'")
                    raise error
        self.assertIs(caught.exception, error)
        self.assertTrue(real.in_transaction)
        with self.assertRaises(GuideError) as unavailable:
            self.store._row('dblp')
        self.assertEqual(unavailable.exception.code, 'source_store_unusable')
        real.rollback()
        with closing(sqlite3.connect(self.path)) as check:
            self.assertNotEqual(check.execute("SELECT priority FROM sources WHERE name='dblp'").fetchone()[0], 93)

    def test_outer_rollback_failure_preserves_body_and_poison_connection(self):
        real = self.store._connection
        class Proxy:
            def __enter__(self): return real.__enter__()
            def __exit__(self, typ, value, tb):
                if typ is not None:
                    raise sqlite3.OperationalError('injected rollback failure')
                return real.__exit__(typ, value, tb)
            def __getattr__(self, name): return getattr(real, name)
        self.store._connection = Proxy()
        error = ValueError('original')
        with self.assertRaises(ValueError) as caught:
            with self.store._db() as db:
                db.execute("UPDATE sources SET priority=92 WHERE name='dblp'")
                raise error
        self.assertIs(caught.exception, error)
        with self.assertRaises(GuideError) as unusable:
            self.store._row('dblp')
        self.assertEqual(unusable.exception.code, 'source_store_unusable')
        self.assertTrue(real.in_transaction)
        real.rollback()
        with closing(sqlite3.connect(self.path)) as check:
            self.assertNotEqual(check.execute("SELECT priority FROM sources WHERE name='dblp'").fetchone()[0], 92)

    def test_timeout_restore_failure_committed_and_connection_closed_to_reuse(self):
        real = self.store._connection
        class Proxy:
            def execute(self, sql, *args):
                if sql == 'PRAGMA busy_timeout=10000':
                    raise sqlite3.OperationalError('database is busy')
                return real.execute(sql, *args)
            def __enter__(self): return real.__enter__()
            def __exit__(self, *args): return real.__exit__(*args)
            def __getattr__(self, name): return getattr(real, name)
        self.store._connection = Proxy()
        with self.assertRaises(GuideError) as caught:
            self.store.reserve('lune', now=100, budget=Budget(2))
        self.assertEqual(caught.exception.code, 'source_store_cleanup_failed')
        self.assertEqual(caught.exception.transaction_outcome, 'committed')
        with closing(sqlite3.connect(self.path)) as check:
            self.assertEqual(check.execute("SELECT rate_version FROM sources WHERE name='lune'").fetchone()[0], 1)
        with self.assertRaises(GuideError) as unavailable:
            self.store._row('lune')
        self.assertEqual(unavailable.exception.code, 'source_store_unusable')

    def test_scoped_write_busy_deadline_rolls_back(self):
        blocker = sqlite3.connect(str(self.path), isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        try:
            with self.store.budget_scope(Budget(.12)):
                with self.assertRaises(BudgetExceeded):
                    self.store.record_run({'name': 'dblp', 'status': 'ok'})
            self.assertLess(time.monotonic() - started, 1)
        finally:
            blocker.execute('ROLLBACK'); blocker.close()
        self.assertEqual(self.store.health_snapshot()['dblp']['status'], 'unknown')

    def test_eight_http_tasks_share_cap_through_response_consumption(self):
        stores = [SourceStore(self.path) for _ in range(8)]
        entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
        state = {'active': 0, 'peak': 0, 'calls': 0}
        class Response:
            status = 200
            def __enter__(self):
                with lock:
                    state['active'] += 1
                    state['calls'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                    if state['active'] == 2:
                        entered.set()
                return self
            def read(self, _limit):
                if not release.wait(4):
                    raise RuntimeError('test did not release the body')
                return b'actual buffered response'
            def __exit__(self, *_):
                with lock:
                    state['active'] -= 1
        clients = [AcademicSource(Config(source='openalex', source_store=store, cache=False), Budget(8))
                   for store in stores]
        try:
            with patch('source.urlopen', side_effect=lambda *a, **kw: Response()), \
                 patch('source_store._RATE', {'openalex': 0}), \
                 ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(client._fetch, 'https://example.test/works', provider='openalex')
                           for client in clients]
                try:
                    self.assertTrue(entered.wait(3), state)
                    # Simulate arbitrarily slow response-body reads, beyond the
                    # original lease deadline, without a wall-clock sleep.
                    with self.store._db() as db:
                        db.execute('UPDATE source_inflight SET expires_at=0 WHERE name=?', ('openalex',))
                    time.sleep(.15)
                    self.assertEqual(state['calls'], 2)
                    self.assertEqual(self.store.concurrency('openalex'), {'limit': 2, 'in_flight': 2})
                finally:
                    release.set()
                self.assertTrue(all(future.result(timeout=5) == b'actual buffered response' for future in futures))
            self.assertEqual(state['calls'], 8)
            self.assertEqual(state['peak'], 2)
            self.assertEqual(state['active'], 0)
            self.assertEqual(self.store.concurrency('openalex')['in_flight'], 0)
        finally:
            release.set()
            for store in stores:
                store.close()

    def test_separate_processes_share_the_same_provider_slots(self):
        context = multiprocessing.get_context('spawn')
        entered, errors, release = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=_hold_process_slot,
                     args=(str(self.path), entered, release, errors)) for _ in range(4)]
        try:
            for process in processes:
                process.start()
            self.assertEqual(entered.get(timeout=5), 'entered')
            self.assertEqual(entered.get(timeout=5), 'entered')
            with self.assertRaises(Empty):
                entered.get(timeout=.2)
            self.assertEqual(self.store.concurrency('openalex')['in_flight'], 2)
        finally:
            release.set()
            for process in processes:
                process.join(8)
                if process.is_alive():
                    process.terminate()
                    process.join(2)
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        with self.assertRaises(Empty):
            errors.get(timeout=.1)
        self.assertEqual(self.store.concurrency('openalex')['in_flight'], 0)
        entered.close(); errors.close()
        entered.join_thread(); errors.join_thread()

    def test_cancelled_rate_wait_releases_only_its_last_reservation(self):
        previous = time.time() + .8
        with self.store._db() as db:
            db.execute('UPDATE sources SET next_request_at=? WHERE name=?', (previous, 'lune'))
        budget, done = Budget(3), threading.Event()
        errors = []
        def wait():
            try:
                self.store.wait_for_rate('lune', budget)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        worker = threading.Thread(target=wait)
        worker.start()
        try:
            for _ in range(100):
                with self.store._db() as db:
                    value = db.execute('SELECT next_request_at FROM sources WHERE name=?', ('lune',)).fetchone()[0]
                if value > previous:
                    break
                time.sleep(.01)
            else:
                self.fail('rate reservation was not made')
            budget.cancel_token.request()
            self.assertTrue(done.wait(1))
            self.assertIsInstance(errors[0], RunCancelled)
            with self.store._db() as db:
                after = db.execute('SELECT next_request_at FROM sources WHERE name=?', ('lune',)).fetchone()[0]
            self.assertEqual(after, previous)
        finally:
            budget.cancel_token.request(); worker.join(4)

    def test_committed_busy_admission_retry_is_idempotent(self):
        import sqlite3
        original = self.store._acquire_request_slot
        first = True
        def uncertain(*args, **kwargs):
            nonlocal first
            admitted = original(*args, **kwargs)
            if first:
                first = False
                self.assertTrue(admitted)
                raise sqlite3.OperationalError('database is busy')
            return admitted
        with patch.object(self.store, '_acquire_request_slot', side_effect=uncertain):
            with self.store.request_slot('lune', Budget(2)):
                self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 0)

    def test_unknown_rate_commit_is_not_retried_or_dispatched(self):
        import sqlite3
        from contextlib import contextmanager
        original_db = self.store._db
        injected = False
        @contextmanager
        def uncertain(budget=None):
            nonlocal injected
            with original_db(budget=budget) as db:
                yield db
            if budget is not None and not injected:
                injected = True
                raise sqlite3.OperationalError('database is busy')
        with patch.object(self.store, '_db', side_effect=uncertain):
            with self.assertRaises(GuideError) as caught:
                self.store.wait_for_rate('lune', Budget(3))
        self.assertEqual(caught.exception.code, 'rate_reservation_unknown')
        with original_db() as db:
            row = db.execute('SELECT rate_version FROM sources WHERE name=?', ('lune',)).fetchone()
        self.assertEqual(row[0], 1)

    def test_rate_cancel_does_not_undo_same_timestamp_later_reservation(self):
        first = time.time() + .2
        with self.store._db() as db:
            db.execute('UPDATE sources SET next_request_at=? WHERE name=?', (first, 'lune'))
        budget, issued = Budget(5), []
        def competing_wait(_delay):
            _, ticket = self.store.reserve('lune', interval=0, budget=Budget(5), _ticket=True)
            issued.append(ticket[2])
            raise RunCancelled()
        with patch.object(budget, 'wait', side_effect=competing_wait):
            with self.assertRaises(RunCancelled):
                self.store.wait_for_rate('lune', budget)
        with self.store._db() as db:
            after = db.execute('SELECT next_request_at FROM sources WHERE name=?', ('lune',)).fetchone()[0]
        self.assertEqual(after, issued[0])

    def test_rate_cancel_does_not_undo_later_owners_reservation(self):
        first = time.time() + .2
        with self.store._db() as db:
            db.execute('UPDATE sources SET next_request_at=? WHERE name=?', (first, 'lune'))
        budget = Budget(5)
        def competing_wait(_delay):
            self.store.reserve('lune', budget=Budget(5))
            raise RunCancelled()
        with patch.object(budget, 'wait', side_effect=competing_wait):
            with self.assertRaises(RunCancelled):
                self.store.wait_for_rate('lune', budget)
        with self.store._db() as db:
            after = db.execute('SELECT next_request_at FROM sources WHERE name=?', ('lune',)).fetchone()[0]
        self.assertAlmostEqual(after, first + 2.0, places=5)

    def test_unaffordable_cooldown_does_not_advance_rate_reservation(self):
        with self.store._db() as db:
            db.execute('UPDATE sources SET cooldown_until=?, next_request_at=0 WHERE name=?',
                       (time.time() + 3600, 'lune'))
        with self.assertRaises(BudgetExceeded):
            self.store.reserve('lune', budget=Budget(1))
        with self.store._db() as db:
            value = db.execute('SELECT next_request_at FROM sources WHERE name=?', ('lune',)).fetchone()[0]
        self.assertEqual(value, 0)

    def test_rate_reservation_lock_respects_cancellation(self):
        import sqlite3
        blocker = sqlite3.connect(str(self.path), isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')
        budget, started = Budget(3), threading.Event()
        def run():
            started.set()
            self.store.reserve('lune', budget=budget)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run)
                self.assertTrue(started.wait(1))
                time.sleep(.1)
                budget.cancel_token.request()
                with self.assertRaises(RunCancelled):
                    future.result(timeout=1)
        finally:
            blocker.execute('ROLLBACK'); blocker.close()

    def test_sqlite_write_lock_wait_respects_cancel_without_request(self):
        import sqlite3
        blocker = sqlite3.connect(str(self.path), isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')
        budget, started = Budget(3), threading.Event()
        def run():
            started.set()
            with self.store.request_slot('lune', budget):
                self.fail('cancelled task acquired source slot')
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run)
                self.assertTrue(started.wait(1))
                time.sleep(.1)
                budget.cancel_token.request()
                with self.assertRaises(RunCancelled):
                    future.result(timeout=1)
        finally:
            blocker.execute('ROLLBACK'); blocker.close()

    def test_locked_release_after_success_reports_unknown_and_keeps_lease(self):
        import sqlite3
        blocker = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            with self.assertRaises(GuideError) as caught:
                with self.store.request_slot('lune', Budget(3)):
                    blocker.execute('BEGIN IMMEDIATE')
            self.assertEqual(caught.exception.code, 'source_slot_release_unknown')
        finally:
            blocker.execute('ROLLBACK'); blocker.close()
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)

    def test_locked_release_preserves_cancellation_and_leaves_lease(self):
        import sqlite3
        blocker = sqlite3.connect(str(self.path), isolation_level=None)
        start = time.monotonic()
        try:
            with self.assertRaises(RunCancelled):
                with self.store.request_slot('lune', Budget(3)):
                    blocker.execute('BEGIN IMMEDIATE')
                    raise RunCancelled()
            self.assertLess(time.monotonic() - start, 1.5)
        finally:
            blocker.execute('ROLLBACK'); blocker.close()
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)

    def test_waiting_task_can_cancel_without_releasing_another_owner(self):
        budget, waiting = Budget(3), threading.Event()
        def wait():
            with bind_events(lambda action, payload: waiting.set() if action == 'source_wait' else None):
                with self.store.request_slot('lune', budget):
                    self.fail('cancelled waiter acquired a slot')
        with self.store.request_slot('lune', Budget(5)):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(wait)
                self.assertTrue(waiting.wait(1))
                budget.cancel_token.request()
                with self.assertRaises(RunCancelled):
                    future.result(timeout=1)
            self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 0)

    def test_queue_deadline_and_failure_release_slots(self):
        def wait_for_slot():
            with self.store.request_slot('lune', Budget(.08)):
                self.fail('queue deadline did not stop admission')
        with self.store.request_slot('lune', Budget(3)):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises(BudgetExceeded):
                    pool.submit(wait_for_slot).result(timeout=2)
            self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
        for failure in (GuideError('fixture failed'), RunCancelled()):
            with self.assertRaises(type(failure)):
                with self.store.request_slot('lune', Budget(1)):
                    raise failure
            self.assertEqual(self.store.concurrency('lune')['in_flight'], 0)

    def test_concurrent_migration_preserves_legacy_rows(self):
        import sqlite3
        path = Path(self.tmp.name) / 'legacy.sqlite3'
        db = sqlite3.connect(str(path))
        try:
            db.execute('CREATE TABLE source_inflight (token TEXT PRIMARY KEY, name TEXT NOT NULL, expires_at REAL NOT NULL)')
            db.execute('INSERT INTO source_inflight VALUES (?, ?, ?)', ('old', 'lune', 0))
            db.commit()
        finally:
            db.close()
        barrier = threading.Barrier(6)
        def initialize():
            barrier.wait(timeout=5)
            store = SourceStore(path)
            try:
                return store.concurrency('lune')['in_flight']
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(lambda _: initialize(), range(6))), [1] * 6)
        store = SourceStore(path)
        try:
            with self.assertRaises(BudgetExceeded):
                with store.request_slot('lune', Budget(.1)):
                    self.fail('migrated legacy row was reclaimed')
        finally:
            store.close()

    def test_finished_release_failure_is_reclaimed_by_another_store(self):
        import sqlite3
        blocker = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            with self.assertRaises(GuideError) as caught:
                with self.store.request_slot('lune', Budget(2)):
                    blocker.execute('BEGIN IMMEDIATE')
            self.assertEqual(caught.exception.code, 'source_slot_release_unknown')
        finally:
            blocker.execute('ROLLBACK')
            blocker.close()
        other = SourceStore(self.path)
        try:
            with other.request_slot('lune', Budget(2)):
                self.assertEqual(other.concurrency('lune')['in_flight'], 1)
            self.assertEqual(other.concurrency('lune')['in_flight'], 0)
        finally:
            other.close()

    def test_dead_process_reclaimed_without_waiting_for_expiry(self):
        context = multiprocessing.get_context('spawn')
        entered, errors, release = context.Queue(), context.Queue(), context.Event()
        process = context.Process(target=_hold_process_slot,
                                  args=(str(self.path), entered, release, errors))
        try:
            process.start()
            self.assertEqual(entered.get(timeout=5), 'entered')
            process.terminate()
            process.join(5)
            self.assertFalse(process.is_alive())
            with self.store.request_slot('openalex', Budget(2)):
                self.assertEqual(self.store.concurrency('openalex')['in_flight'], 1)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            entered.close(); errors.close()

    def test_legacy_recovery_requires_explicit_ack_and_never_deletes_owned_slots(self):
        with self.store._db() as db:
            db.execute('INSERT INTO source_inflight(token,name,expires_at) VALUES(?,?,?)',
                       ('legacy', 'lune', 0))
            db.execute('INSERT INTO source_inflight(token,name,expires_at,owner_pid,owner_instance) VALUES(?,?,?,?,?)',
                       ('owned', 'lune', 0, 12345, 'unverified-owner'))
        self.assertEqual(self.store.legacy_inflight_status('lune')['legacy'], 1)
        with self.assertRaises(GuideError):
            self.store.reconcile_legacy_inflight('lune', {'acknowledged': True})
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 2)
        result = self.store.reconcile_legacy_inflight('lune',
                    {'acknowledged': True, 'legacy_clients_stopped': True})
        self.assertEqual(result['removed'], 1)
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)

    def test_legacy_and_uncertain_owners_are_never_reclaimed(self):
        with self.store._db() as db:
            db.execute('INSERT INTO source_inflight(token,name,expires_at) VALUES(?,?,?)',
                       ('legacy', 'lune', 0))
        with self.assertRaises(BudgetExceeded):
            with self.store.request_slot('lune', Budget(.1)):
                self.fail('legacy row was reclaimed')
        with self.store._db() as db:
            db.execute("UPDATE source_inflight SET owner_pid=?,owner_instance='unknown'", (123456789,))
        for alive in (None, True):  # Unknown or a potentially reused PID.
            with self.subTest(alive=alive), patch('lune_quota._process_alive', return_value=alive):
                with self.assertRaises(BudgetExceeded):
                    with self.store.request_slot('lune', Budget(.1)):
                        self.fail('unverifiable or live owner was reclaimed')
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)

    def test_failed_insert_rollback_preserves_finished_evidence_and_cleans_absent_token(self):
        slot = self.store.request_slot('lune', Budget(2))
        old_token = slot.__enter__()
        with patch.object(self.store, '_db', side_effect=sqlite3.OperationalError('fixture failure')):
            with self.assertRaises(GuideError):
                slot.__exit__(None, None, None)
        self.assertEqual(source_store._SLOT_STATES[old_token], 'finished')
        with self.store._db() as db:
            db.execute("CREATE TRIGGER reject_slot BEFORE INSERT ON source_inflight "
                       "BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.request_slot('lune', Budget(2)):
                self.fail('trigger must reject insertion')
        self.assertEqual(source_store._SLOT_STATES[old_token], 'finished')
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
        with self.store._db() as db:
            db.execute('DROP TRIGGER reject_slot')
        with self.store.request_slot('lune', Budget(2)):
            self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
        self.assertNotIn(old_token, source_store._SLOT_STATES)
        # The failed insertion left a finished registry entry with no row.
        with source_store._SLOT_LOCK:
            self.assertFalse(any(value == self.store._slot_database and
                                 source_store._SLOT_STATES[key] == 'finished'
                                 for key, value in source_store._SLOT_DATABASES.items()))

    def test_unknown_commit_outcome_keeps_evidence_until_row_checked(self):
        original_db = self.store._db
        @contextmanager
        def committed_then_error(*args, **kwargs):
            with original_db(*args, **kwargs) as db:
                yield db
            raise sqlite3.OperationalError('synthetic uncertain commit')
        with patch.object(self.store, '_db', side_effect=committed_then_error):
            with self.assertRaises(sqlite3.OperationalError):
                with self.store.request_slot('lune', Budget(2)):
                    self.fail('uncertain admission must not run')
        with self.store._db() as db:
            token = db.execute('SELECT token FROM source_inflight WHERE name=?', ('lune',)).fetchone()[0]
        self.assertEqual(source_store._SLOT_STATES[token], 'finished')
        with self.store.request_slot('lune', Budget(2)):
            self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
        self.assertNotIn(token, source_store._SLOT_STATES)

    def test_release_baseexception_does_not_mask_body_and_interrupt_propagates(self):
        for body_failure in (GuideError('original'), RunCancelled()):
            with self.subTest(body=type(body_failure).__name__):
                slot = self.store.request_slot('lune', Budget(2))
                slot.__enter__()
                with patch.object(self.store, '_db', side_effect=KeyboardInterrupt()):
                    with self.assertRaises(type(body_failure)) as caught:
                        try:
                            raise body_failure
                        finally:
                            slot.__exit__(*__import__('sys').exc_info())
                self.assertIs(caught.exception, body_failure)
                with self.store.request_slot('lune', Budget(2)):
                    pass
        slot = self.store.request_slot('lune', Budget(2))
        slot.__enter__()
        with patch.object(self.store, '_db', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                slot.__exit__(None, None, None)
        with self.store.request_slot('lune', Budget(2)):
            pass

    def test_release_requires_matching_owner(self):
        events = []
        with bind_events(lambda action, payload: events.append((action, payload))):
            with self.assertRaises(GuideError) as caught:
                with self.store.request_slot('lune', Budget(2)) as token:
                    with self.store._db() as db:
                        db.execute("UPDATE source_inflight SET owner_instance='different' WHERE token=?", (token,))
        self.assertEqual(caught.exception.code, 'source_slot_release_unknown')
        self.assertEqual(source_store._SLOT_STATES[token], 'finished')
        self.assertTrue(any(a == 'source_release_deferred' for a, _ in events))
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)

    def test_owner_mismatch_preserves_cancel_and_recovery_evidence(self):
        events = []
        error = RunCancelled()
        with bind_events(lambda action, payload: events.append((action, payload))):
            with self.assertRaises(RunCancelled) as caught:
                with self.store.request_slot('lune', Budget(2)) as token:
                    with self.store._db() as db:
                        db.execute("UPDATE source_inflight SET owner_instance='different' WHERE token=?", (token,))
                    raise error
        self.assertIs(caught.exception, error)
        self.assertEqual(source_store._SLOT_STATES[token], 'finished')
        self.assertTrue(any(a == 'source_release_deferred' for a, _ in events))
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)

    def test_inherited_context_and_connection_rejected_on_identity_change(self):
        slot = self.store.request_slot('lune', Budget(2))
        token = slot.__enter__()
        with patch.object(source_store, '_SLOT_INSTANCE', 'child-instance'):
            with self.assertRaises(GuideError) as caught:
                self.store.concurrency('lune')
            self.assertEqual(caught.exception.code, 'source_store_forked')
            with self.assertRaises(GuideError):
                self.store.close()
            slot.__exit__(None, None, None)
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
        with self.store._db() as db:
            db.execute('DELETE FROM source_inflight WHERE token=?', (token,))
        with source_store._SLOT_LOCK:
            source_store._SLOT_STATES.pop(token, None)
            source_store._SLOT_DATABASES.pop(token, None)

    def test_actual_fork_cannot_release_parent_or_use_inherited_connection(self):
        if not hasattr(os, 'fork'):
            # Verify the real POSIX path in WSL rather than recording an
            # unverified platform skip in the delivery acceptance suite.
            import subprocess
            result = subprocess.run(['wsl', '--', 'python3', '-m', 'unittest',
                'tests.test_source_inflight.SourceInflightTests.test_actual_fork_cannot_release_parent_or_use_inherited_connection'],
                capture_output=True, timeout=40)
            self.assertEqual(result.returncode, 0, repr(result.stderr[-2000:]))
            return
        slot = self.store.request_slot('lune', Budget(3))
        slot.__enter__()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                try:
                    self.store.concurrency('lune')
                    result = b'inherited connection accepted'
                except GuideError as exc:
                    result = b'ok' if exc.code == 'source_store_forked' else b'wrong error'
                slot.__exit__(None, None, None)
                fresh = SourceStore(self.path)
                try:
                    if fresh.concurrency('lune')['in_flight'] != 1:
                        result = b'parent lease deleted'
                finally:
                    fresh.close()
                os.write(write_fd, result)
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        try:
            result = os.read(read_fd, 128)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(status, 0)
            self.assertEqual(result, b'ok')
            self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
        finally:
            os.close(read_fd)
            slot.__exit__(None, None, None)

    def test_expired_active_slot_never_admits_second_request(self):
        def wait_for_slot():
            with self.store.request_slot('lune', Budget(.1)):
                self.fail('expired active request exceeded concurrency cap')
        with self.store.request_slot('lune', Budget(3)):
            with self.store._db() as db:
                db.execute('UPDATE source_inflight SET expires_at=0 WHERE name=?', ('lune',))
            self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises(BudgetExceeded):
                    pool.submit(wait_for_slot).result(timeout=2)

    def test_different_sources_are_independent(self):
        with self.store.request_slot('lune', Budget(2)):
            with self.store.request_slot('openalex', Budget(2)):
                self.assertEqual(self.store.concurrency('lune')['in_flight'], 1)
                self.assertEqual(self.store.concurrency('openalex')['in_flight'], 1)
        self.assertEqual(self.store.concurrency('lune')['in_flight'], 0)


class SourceFailureClassificationTests(unittest.TestCase):
    def test_dns_connection_timeout_and_other_transport_are_distinct_in_health_and_events(self):
        failures = [
            (URLError(socket.gaierror('DNS resolution failed')), 'dns_error'),
            (URLError('DNS resolution failed'), 'dns_error'),
            (URLError(ConnectionRefusedError('connection refused')), 'connection_error'),
            (ConnectionResetError('connection reset'), 'connection_error'),
            (URLError(TimeoutError('timed out')), 'timeout'),
            (TimeoutError('read timeout'), 'timeout'),
            (URLError(OSError('TLS negotiation failed')), 'transport'),
        ]
        for failure, expected in failures:
            with self.subTest(failure=type(failure).__name__, code=expected):
                store = SourceStore(':memory:')
                client = AcademicSource(Config(source='dblp', source_store=store, cache=False), Budget(3))
                events = []
                try:
                    with patch.object(client, '_wait_for'), patch('source.urlopen', side_effect=failure) as transport, \
                         bind_events(lambda action, payload: events.append((action, payload))):
                        with self.assertRaises(GuideError) as caught:
                            client.search_batch('RAG', providers=['dblp'])
                    self.assertEqual(transport.call_count, 1)
                    self.assertEqual(classify_source_error(str(caught.exception)), expected)
                    self.assertEqual(store.health_snapshot()['dblp']['last_code'], expected)
                    from diagnosis import diagnose
                    diagnosis = diagnose(request={'topic':'RAG','target_count':1},
                        gaps=[{'requirement':'paper_count','required':1,'found':0,'missing':1}],
                        trace=[{'action':'search','error':str(caught.exception),
                                'router':{'batches':[client.last_batch.summary()]}}])
                    self.assertTrue(diagnosis['source_errors'])
                    self.assertEqual({row['code'] for row in diagnosis['source_errors']}, {expected})
                    self.assertEqual(store.concurrency('dblp')['in_flight'], 0)
                    errors = [payload for action, payload in events if action == 'tool_error']
                    self.assertTrue(errors)
                    self.assertTrue(all(payload['error_code'] == expected for payload in errors))
                    if expected != 'timeout':
                        self.assertNotIn('timed out', str(caught.exception))
                finally:
                    store.close()

    def test_failed_cooldown_and_audit_do_not_replace_transport_failure(self):
        store = SourceStore(':memory:')
        client = AcademicSource(Config(source='dblp', source_store=store, cache=False), Budget(3))
        try:
            original_cooldown = store.cooldown
            def cooldown(name, until=None, **kwargs):
                if until is not None:
                    raise sqlite3.OperationalError('database is locked')
                return original_cooldown(name)
            with patch.object(store, 'cooldown', side_effect=cooldown), \
                 patch.object(store, 'record_run', side_effect=sqlite3.OperationalError('database is locked')), \
                 patch.object(client, '_wait_for'), \
                 patch('source.urlopen', side_effect=URLError(socket.gaierror('remote private detail'))):
                with self.assertRaises(GuideError) as caught:
                    client.search_batch('fixture')
            self.assertEqual(caught.exception.code, 'dns_error')
            self.assertNotIn('remote private detail', str(caught.exception))
        finally:
            store.close()

    def test_original_exception_type_is_preserved_without_echoing_provider_details(self):
        error = SourceTransportError('api.example.test', URLError(socket.gaierror('private provider detail')))
        self.assertEqual(error.code, 'dns_error')
        self.assertEqual(error.original_type, 'gaierror')
        self.assertEqual(classify_source_error(error), 'dns_error')
        self.assertNotIn('private provider detail', str(error))


if __name__ == '__main__':
    unittest.main()
