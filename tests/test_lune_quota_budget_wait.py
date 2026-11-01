import io
import json
import sqlite3
import threading
import time
import unittest

from common import Budget, BudgetExceeded, GuideError, RunCancelled
from tests.lune_quota_fixture import confirmed_quota


class BudgetWaitTests(unittest.TestCase):
    def setUp(self):
        self.store = confirmed_quota(self)

    def held_lock(self):
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            with self.store._lock:
                acquired.set()
                release.wait(3)

        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(acquired.wait(1))
        self.addCleanup(worker.join, 4)
        self.addCleanup(release.set)
        return release

    def test_stateful_dispatch_waiting_on_quota_observes_deadline(self):
        from mcp_client import McpClient, ServerConfig
        release = self.held_lock()
        calls = []
        config = ServerConfig('offline', 'https://mcp.luneresearch.com', protocol='2025-03-26',
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}})
        client = McpClient(config, stateful=True, quota_store=self.store,
                           transport=lambda *a, **kw: calls.append('sent'))
        with self.assertRaises(BudgetExceeded) as caught:
            client.call_tool('t', {}, budget=Budget(.08))
        self.assertEqual(caught.exception.remote_outcome, 'not_started')
        self.assertEqual(calls, [])
        release.set()
        self.assertEqual([], self.store.pending())

    def test_cancelled_finalization_retains_received_tool_result_and_quota(self):
        from mcp_client import McpClient, McpResult, ServerConfig
        budget = Budget(2)
        def transport(request, timeout):
            message = json.loads(request.data)
            budget.cancel_token.request()
            body = {'jsonrpc': '2.0', 'id': message['id'],
                    'result': {'content': [{'type': 'text', 'text': 'synthetic'}]}}
            return io.BytesIO(json.dumps(body).encode())
        config = ServerConfig('offline', 'https://mcp.luneresearch.com',
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}})
        client = McpClient(config, quota_store=self.store, transport=transport)
        with self.assertRaises(RunCancelled) as caught:
            client.call_tool('t', {}, budget=budget)
        self.assertEqual(caught.exception.remote_outcome, 'response_received')
        self.assertTrue(caught.exception.quota_finalization_failed)
        self.assertIsInstance(caught.exception.mcp_result, McpResult)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.status()['remaining_from_snapshot'], 299)

    def test_reserve_lock_deadline_and_release(self):
        release = self.held_lock()
        start = time.monotonic()
        with self.assertRaises(BudgetExceeded):
            self.store.reserve('search_papers', budget=Budget(.08))
        self.assertLess(time.monotonic() - start, .5)
        release.set()
        self.assertEqual([], self.store.pending())
        self.assertTrue(self.store.reserve('search_papers', budget=Budget(1)))

    def test_precancelled_reserve_and_finish_keep_accounting(self):
        reservation = self.store.reserve('search_papers')
        budget = Budget(1)
        budget.cancel_token.request()
        release = self.held_lock()
        with self.assertRaises(RunCancelled):
            self.store.reserve('search_papers', budget=budget)
        with self.assertRaises(RunCancelled):
            self.store.finish(reservation, 'response_received', budget=budget)
        release.set()
        self.assertEqual([reservation], [row['id'] for row in self.store.pending()])
        self.assertEqual(299, self.store.status()['remaining_from_snapshot'])

    def test_finish_expired_while_lock_held_keeps_reservation(self):
        reservation = self.store.reserve('search_papers')
        release = self.held_lock()
        start = time.monotonic()
        with self.assertRaises(BudgetExceeded):
            self.store.finish(reservation, 'unknown', budget=Budget(.08))
        self.assertLess(time.monotonic() - start, .5)
        release.set()
        self.assertEqual([reservation], [row['id'] for row in self.store.pending()])
        self.assertEqual(299, self.store.status()['remaining_from_snapshot'])

    def test_cancellation_during_lock_wait(self):
        release = self.held_lock()
        budget = Budget(2)
        timer = threading.Timer(.05, budget.cancel_token.request)
        timer.start()
        self.addCleanup(timer.join)
        start = time.monotonic()
        with self.assertRaises(RunCancelled):
            self.store.reserve('search_papers', budget=budget)
        self.assertLess(time.monotonic() - start, .5)
        release.set()
        self.assertEqual([], self.store.pending())

    def test_sqlite_busy_timeout_restored(self):
        self.store._conn.execute('PRAGMA busy_timeout = 180')
        other = sqlite3.connect(self.store._path, isolation_level=None)
        self.addCleanup(other.close)
        other.execute('BEGIN IMMEDIATE')
        try:
            start = time.monotonic()
            with self.assertRaises((GuideError, BudgetExceeded)):
                self.store.reserve('search_papers', budget=Budget(.06))
            self.assertLess(time.monotonic() - start, .2)
        finally:
            other.execute('ROLLBACK')
        self.assertEqual(180, self.store._conn.execute('PRAGMA busy_timeout').fetchone()[0])
        self.assertEqual([], self.store.pending())
        self.assertTrue(self.store.reserve('search_papers'))

    def test_cancel_inside_transaction_rolls_back(self):
        class CancelAfterBegin:
            def __init__(self):
                self.calls = 0
            def check_cancelled(self):
                self.calls += 1
                if self.calls >= 4:
                    raise RunCancelled()
            def remaining(self):
                return 1
        with self.assertRaises(RunCancelled):
            self.store.reserve('search_papers', budget=CancelAfterBegin())
        self.assertFalse(self.store._conn.in_transaction)
        self.assertEqual([], self.store.pending())

    def test_commit_failure_does_not_remove_existing_reservation(self):
        reservation = self.store.reserve('search_papers')
        real = self.store._conn

        class FailCommit:
            def execute(self, sql, *args):
                if sql == 'COMMIT':
                    raise sqlite3.OperationalError('injected commit failure')
                return real.execute(sql, *args)

        self.store._conn = FailCommit()
        try:
            with self.assertRaises(GuideError):
                self.store.finish(reservation, 'response_received', budget=Budget(1))
        finally:
            self.store._conn = real
        self.assertFalse(real.in_transaction)
        self.assertEqual([reservation], [row['id'] for row in self.store.pending()])
        self.assertEqual(299, self.store.status()['remaining_from_snapshot'])


    def test_commit_result_unknown_does_not_refund(self):
        reservation = self.store.reserve('search_papers')
        real = self.store._conn

        class CommitThenFail:
            def execute(self, sql, *args):
                result = real.execute(sql, *args)
                if sql == 'COMMIT':
                    raise sqlite3.OperationalError('commit result unavailable')
                return result

        self.store._conn = CommitThenFail()
        try:
            with self.assertRaises(GuideError):
                self.store.finish(reservation, 'response_received', budget=Budget(1))
        finally:
            self.store._conn = real
        self.assertEqual(299, self.store.status()['remaining_from_snapshot'])
        self.assertEqual([], self.store.pending())
        self.assertEqual('response_received', real.execute(
            'SELECT status FROM quota_events WHERE id = ?', (reservation,)).fetchone()[0])


if __name__ == '__main__':
    unittest.main()
