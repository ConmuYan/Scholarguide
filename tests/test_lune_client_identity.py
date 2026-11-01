"""Isolation of the shared Lune client across concurrent source operations."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Budget, BudgetExceeded, Config, RunCancelled
from lune_quota import LuneQuotaStore, default_quota_path
from mcp_client import McpClient, ServerConfig
from mcp_lune import LuneProvider
from source import AcademicSource
from source_store import SourceStore


class LuneClientIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(root / 'quota.sqlite')})
        environment.start()
        self.addCleanup(environment.stop)
        self.first = SourceStore(root / 'first.sqlite')
        self.second = SourceStore(root / 'second.sqlite')
        self.addCleanup(self.first.close)
        self.addCleanup(self.second.close)
        self.first.upsert({'name': 'lune', 'endpoint': 'https://localhost:1111/mcp',
                           'api_key': 'search-synthetic-key'})
        self.config_path = root / 'servers.json'
        self.config_path.write_text(json.dumps({'servers': {'lune': {
            'url': 'https://localhost:2222/mcp', 'allow': ['search_papers']}}}), encoding='utf-8')
        self.sent = []
        self.search_entered = threading.Event()
        self.release_search = threading.Event()
        self.skill_entered = threading.Event()
        self.client_read = threading.Event()
        self.barrier = threading.Barrier(2)

        def transport(request, timeout):
            message = json.loads(request.data)
            self.sent.append((message['params']['arguments']['query'], request.full_url,
                              request.get_header('Authorization')))
            if message['params']['arguments']['query'] == 'search':
                self.search_entered.set()
                if not self.release_search.wait(5):
                    raise RuntimeError('search was not released')
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                'result': {'structuredContent': {'papers': []}}}).encode())

        self.transport = transport
        shared = McpClient(ServerConfig('lune', 'https://localhost:2222/mcp',
            headers={'Authorization': 'Bearer default-synthetic-key'}, allow=('search_papers',)),
            transport=transport)
        self.provider = LuneProvider(config_path=self.config_path, client=shared,
                                     ledger_path=root / 'ledger.json')
        self.quota = LuneQuotaStore(default_quota_path())
        self.addCleanup(self.quota.close)
        self.quota.confirm(10, time.time() + 60)

    def test_skill_never_copies_search_override(self):
        original_init = McpClient.__init__
        original_client = self.provider.client
        errors = []

        def init(client, config, **kwargs):
            kwargs.setdefault('transport', self.transport)
            original_init(client, config, **kwargs)

        def client(**kwargs):
            result = original_client(**kwargs)
            if threading.current_thread().name == 'skill':
                self.client_read.set()
            return result

        def search():
            try:
                cfg = self.first.configure(Config(source='lune', source_store=self.first))
                self.barrier.wait(3)
                AcademicSource(cfg, Budget(5))._search_one('lune', 'search', 1)
            except BaseException as exc:
                errors.append(exc)

        def skill():
            try:
                self.barrier.wait(3)
                if not self.search_entered.wait(3):
                    raise AssertionError('search did not reach transport')
                self.skill_entered.set()
                self.second._call_lune('search_papers', {'query': 'skill'},
                                       Config(source='lune'), budget=Budget(5))
            except BaseException as exc:
                errors.append(exc)

        with patch('mcp_lune.install', return_value=self.provider), \
             patch.object(McpClient, '__init__', init), \
             patch.object(self.provider, 'client', side_effect=client), \
             patch.object(self.second, '_ensure_ready', return_value=Config(source='lune')):
            threads = [threading.Thread(target=search, name='search'),
                       threading.Thread(target=skill, name='skill')]
            for thread in threads:
                thread.start()
            try:
                self.assertTrue(self.skill_entered.wait(4))
                # Before the fix the skill reads the override while search is paused.
                self.client_read.wait(.5)
            finally:
                self.release_search.set()
                for thread in threads:
                    thread.join(6)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertCountEqual(self.sent, [
            ('search', 'https://localhost:1111/mcp', 'Bearer search-synthetic-key'),
            ('skill', 'https://localhost:2222/mcp', 'Bearer default-synthetic-key')])

    def test_reentrant_skill_cannot_inherit_search_override(self):
        original_init = McpClient.__init__
        def transport(request, timeout):
            message = json.loads(request.data)
            query = message['params']['arguments']['query']
            self.sent.append((query, request.full_url, request.get_header('Authorization')))
            if query == 'search':
                self.second._call_lune('search_papers', {'query': 'skill'},
                                       Config(source='lune'), budget=Budget(3))
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                'result': {'structuredContent': {'papers': []}}}).encode())
        def init(client, config, **kwargs):
            kwargs.setdefault('transport', transport)
            original_init(client, config, **kwargs)
        with patch('mcp_lune.install', return_value=self.provider), \
             patch.object(McpClient, '__init__', init), \
             patch.object(self.second, '_ensure_ready', return_value=Config(source='lune')):
            cfg = self.first.configure(Config(source='lune', source_store=self.first))
            AcademicSource(cfg, Budget(5))._search_one('lune', 'search', 1)
        self.assertCountEqual(self.sent, [
            ('search', 'https://localhost:1111/mcp', 'Bearer search-synthetic-key'),
            ('skill', 'https://localhost:2222/mcp', 'Bearer default-synthetic-key')])

    def test_provider_lock_is_reentrant(self):
        with patch('mcp_lune.install', return_value=self.provider), \
             patch.object(self.second, '_ensure_ready', return_value=Config(source='lune')), \
             self.provider._lock:
            result = self.second._call_lune('search_papers', {'query': 'skill'},
                                            Config(source='lune'), budget=Budget(3))
        self.assertEqual(result['result'], {'papers': []})
        self.assertEqual(len(self.sent), 1)

    def test_budget_expires_while_waiting_for_provider_lock(self):
        errors = []
        def run():
            try:
                self.second._call_lune('search_papers', {'query': 'skill'},
                                       Config(source='lune'), budget=Budget(.3))
            except BaseException as exc:
                errors.append(exc)
        with patch('mcp_lune.install', return_value=self.provider), \
             patch.object(self.second, '_ensure_ready', return_value=Config(source='lune')), \
             self.provider._lock:
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(2)
            still_waiting = thread.is_alive()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(still_waiting)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], BudgetExceeded)
        self.assertEqual(self.sent, [])

    def test_cancel_while_waiting_for_provider_lock(self):
        budget = Budget(5)
        entered = threading.Event()
        errors = []
        def run():
            try:
                entered.set()
                self.second._call_lune('search_papers', {'query': 'skill'},
                                       Config(source='lune'), budget=budget)
            except BaseException as exc:
                errors.append(exc)
        with patch('mcp_lune.install', return_value=self.provider), \
             patch.object(self.second, '_ensure_ready', return_value=Config(source='lune')), \
             self.provider._lock:
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                time.sleep(.2)
                budget.cancel_token.request()
                thread.join(2)
                still_waiting = thread.is_alive()
            finally:
                budget.cancel_token.request()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(still_waiting)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RunCancelled)
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
