"""Synthetic Lune dispatch tests; two SQLite connections and no network."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Budget, BudgetExceeded, Config, RunCancelled
from source import AcademicSource
from source_store import SourceStore


class LuneDispatchCooldownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'sources.sqlite'
        self.store, self.other = SourceStore(path), SourceStore(path)
        self.addCleanup(self.store.close)
        self.addCleanup(self.other.close)

    def test_search_cooldown_after_rate_wait_blocks_transport(self):
        client = AcademicSource(Config(source='lune', source_store=self.store, cache=False), Budget(.35))
        called = []
        class Provider:
            _lock = threading.RLock()
            _client = None
            def search(self, *args, before_page, **kwargs):
                before_page()
                called.append(True)
                return []
        original = client._wait_for
        def wait(name):
            original(name)
            self.other.cooldown('lune', time.time() + 10, errorcode='rate_limited')
        with patch('mcp_lune.install', return_value=Provider()), patch.object(client, '_wait_for', side_effect=wait):
            with self.assertRaises(BudgetExceeded):
                client._search_one('lune', 'fixture', 1)
        self.assertEqual(called, [])

    def test_search_local_cooldown_wait_is_cancellable(self):
        budget = Budget(5)
        client = AcademicSource(Config(source='lune', source_store=self.store, cache=False), budget)
        waiting, calls, errors = threading.Event(), [], []
        class Provider:
            _lock = threading.RLock()
            _client = None
            def search(self, *args, before_page, **kwargs):
                before_page()
                calls.append(True)
                return []
        original_wait = budget.wait
        def wait(delay):
            if delay > 1: waiting.set()
            return original_wait(delay)
        def run():
            try:
                client._search_one('lune', 'fixture', 1)
            except BaseException as exc:
                errors.append(exc)
        client.cooldown['lune'] = time.monotonic() + 2
        with patch('mcp_lune.install', return_value=Provider()), patch.object(budget, 'wait', side_effect=wait):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(waiting.wait(2))
                budget.cancel_token.request()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(errors[0], RunCancelled)
                self.assertEqual(calls, [])
            finally:
                budget.cancel_token.request()
                thread.join(2)

    def test_extension_rechecked_and_two_stores_dispatch_spaced(self):
        import source_store
        entered, extend = threading.Event(), threading.Event()
        dispatches, errors = [], []
        class Provider:
            _lock = threading.RLock()
            _client = None
            def search(self, *args, before_page, **kwargs):
                before_page()
                dispatches.append(time.monotonic())
                return []
        provider = Provider()
        clients = [AcademicSource(Config(source='lune', source_store=store, cache=False), Budget(3))
                   for store in (self.store, self.other)]
        original_rate = clients[0]._wait_for
        original_wait = clients[0].budget.wait
        first = True
        def rate(name):
            nonlocal first
            original_rate(name)
            if first:
                first = False
                self.other.cooldown('lune', time.time() + .18, errorcode='rate_limited')
        def wait(delay):
            if delay > .1 and not entered.is_set():
                entered.set()
                if not extend.wait(2): raise AssertionError('barrier timeout')
            return original_wait(delay)
        def run(client):
            try:
                client._search_one('lune', 'fixture', 1)
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(client,)) for client in clients]
        with patch('mcp_lune.install', return_value=provider), patch.dict(source_store._RATE, {'lune': .12}), \
             patch.object(clients[0], '_wait_for', side_effect=rate), \
             patch.object(clients[0].budget, 'wait', side_effect=wait):
            threads[0].start()
            try:
                self.assertTrue(entered.wait(2))
                self.other.cooldown('lune', time.time() + .35, errorcode='rate_limited')
                deadline = self.other.cooldown('lune')
                threads[1].start()
                extend.set()
                for thread in threads: thread.join(3)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(len(dispatches), 2)
                self.assertGreaterEqual(dispatches[1] - dispatches[0], .08)
                self.assertGreaterEqual(time.time() - time.monotonic() + dispatches[0], deadline - .03)
            finally:
                extend.set()
                for thread in threads:
                    if thread.ident is not None: thread.join(3)

    def test_invoke_cooldown_during_mcp_lock_wait_blocks_transport(self):
        import source_store
        called = []
        class Client:
            def call_tool(self, *args):
                called.append(True)
                return {'content': []}
        class Provider:
            def client(self, **kwargs): return Client()
        budget = Budget(.5)
        attempted = threading.Event()
        original = self.store.wait_for_rate
        def wait(name, task_budget):
            original(name, task_budget)
            attempted.set()
        errors = []
        def run():
            try:
                self.store.invoke('lune', 'search', {'query': 'fixture'}, Config(source='lune'), budget=budget)
            except BaseException as exc:
                errors.append(exc)
        with patch.object(self.store, '_ensure_ready', side_effect=lambda n, c: c), \
             patch.object(self.store, 'wait_for_rate', side_effect=wait), patch('mcp_lune.install', return_value=Provider()):
            with source_store._MCP_LOCK:
                thread = threading.Thread(target=run)
                thread.start()
                self.assertTrue(attempted.wait(2))
                self.other.cooldown('lune', time.time() + 10, errorcode='rate_limited')
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors, 'expected budget failure')
        self.assertIsInstance(errors[0], BudgetExceeded)
        self.assertEqual(called, [])


if __name__ == '__main__':
    unittest.main()
