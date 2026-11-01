"""Offline integration tests for account-shared MCP admission."""
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError

from common import Budget, GuideError
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, ServerConfig


class MockTransport:
    def __init__(self, *, first_unauthorized=False):
        self.calls = []
        self.lock = threading.Lock()
        self.first_unauthorized = first_unauthorized

    def __call__(self, request, timeout):
        message = json.loads(request.data)
        with self.lock:
            self.calls.append(message['method'])
            index = len(self.calls)
        if self.first_unauthorized and index == 1:
            raise HTTPError(request.full_url, 401, 'fixture', {}, io.BytesIO(b''))
        if message['method'] == 'tools/list':
            result = {'tools': [{'name': 'search_papers', 'inputSchema': {
                'type': 'object', 'properties': {'query': {'type': 'string'},
                                                  'limit': {'type': 'integer'}},
                'required': ['query'], 'additionalProperties': False}}]}
        elif message['method'] == 'prompts/get':
            result = {'messages': []}
        else:
            result = {'structuredContent': {'papers': []}}
        return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                      'result': result}).encode())


class LuneQuotaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'quota.sqlite3'
        self.quota = LuneQuotaStore(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.quota.close)
        self.transport = MockTransport()
        self.config = ServerConfig('lune', 'https://example.invalid/mcp',
                                   allow=('search_papers',),
                                   allow_prompts=('verify_draft',))

    def client(self):
        return McpClient(self.config, transport=self.transport, quota_store=self.quota)

    def test_missing_confirmation_blocks_before_transport(self):
        client = self.client()
        with self.assertRaises(GuideError):
            client.call_tool('search_papers', {'query': 'fixture'}, budget=Budget(5))
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.quota.status()['snapshot_present'])

    def test_discovery_and_tool_use_same_quota(self):
        self.quota.confirm(2, time.time() + 60)
        client = self.client()
        client.list_tools()
        client.call_tool('search_papers', {'query': 'fixture', 'limit': 1})
        with self.assertRaises(GuideError):
            client.get_prompt('verify_draft', {'draft': 'fixture'})
        self.assertEqual(self.transport.calls, ['tools/list', 'tools/call'])
        status = self.quota.status()
        self.assertEqual(status['remaining_from_snapshot'], 0)
        self.assertTrue(status['external_usage_unknown'])

    def test_401_retry_requires_an_additional_reservation(self):
        self.quota.confirm(1, time.time() + 60)
        self.transport.first_unauthorized = True
        client = self.client()
        client.set_auth_provider(lambda force: 'fake-token-for-test')
        with self.assertRaises(GuideError):
            client.call_tool('search_papers', {'query': 'fixture'})
        self.assertEqual(self.transport.calls, ['tools/call'])
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 0)

    def test_raw_json_rpc_cannot_avoid_account_quota(self):
        self.quota.confirm(1, time.time() + 60)
        client = self.client()
        client.request('tools/call', {'name': 'search_papers',
                                      'arguments': {'query': 'fixture'}})
        with self.assertRaises(GuideError):
            client.request('tools/call', {'name': 'search_papers',
                                          'arguments': {'query': 'fixture'}})
        self.assertEqual(self.transport.calls, ['tools/call'])

    def test_lune_host_alias_still_requires_shared_quota(self):
        config = ServerConfig('alias', 'https://mcp.luneresearch.com/mcp',
                              allow=('search_papers',),
                              local_tool_schemas={'search_papers': {
                                  'type': 'object',
                                  'properties': {'query': {'type': 'string'}},
                                  'required': ['query'], 'additionalProperties': False}})
        client = McpClient(config, transport=self.transport, quota_store=self.quota)
        with self.assertRaises(GuideError):
            client.call_tool('search_papers', {'query': 'fixture'})
        self.assertEqual(self.transport.calls, [])

    def test_source_skill_propagates_caller_cancellation(self):
        from common import RunCancelled
        from source_store import SourceStore
        from unittest.mock import patch
        store = SourceStore(Path(self.tmp.name) / 'sources.sqlite3')
        budget = Budget(5)
        budget.cancel_token.request()
        try:
            with patch.object(store, '_call_lune_reserved', side_effect=AssertionError('must not dispatch')):
                with self.assertRaises(RunCancelled):
                    store.invoke('lune', 'search_papers', {'query': 'fixture'}, None, budget=budget)
        finally:
            store.close()

    def test_source_mcp_queue_observes_cancellation_while_waiting(self):
        from common import RunCancelled
        from source_store import SourceStore, _MCP_LOCK
        budget = Budget(3)
        observed = []
        ready = threading.Event()
        _MCP_LOCK.acquire()
        def worker():
            ready.set()
            try:
                with SourceStore._mcp_lock(budget):
                    observed.append('entered')
            except RunCancelled:
                observed.append('cancelled')
        thread = threading.Thread(target=worker)
        try:
            thread.start(); self.assertTrue(ready.wait(1))
            budget.cancel_token.request()
            thread.join(0.8)
            self.assertFalse(thread.is_alive(), 'cancelled queue remained blocked')
            self.assertEqual(observed, ['cancelled'])
        finally:
            _MCP_LOCK.release()
            thread.join(4)

    def test_same_origin_redirect_is_not_a_second_unmetered_attempt(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        calls = []
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                calls.append('POST')
                self.send_response(302); self.send_header('Location', '/redirected'); self.end_headers()
            def do_GET(self):
                calls.append('GET')
                self.send_response(200); self.end_headers()
        server = HTTPServer(('127.0.0.1', 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.quota.confirm(2, time.time() + 60)
        client = McpClient(ServerConfig('lune', f'http://127.0.0.1:{server.server_port}/',
                                        allow=('search_papers',)), quota_store=self.quota)
        try:
            with self.assertRaises(Exception):
                client.call_tool('search_papers', {'query': 'fixture'})
            self.assertEqual(calls, ['POST'])
            self.assertEqual(self.quota.status()['remaining_from_snapshot'], 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_budget_exhausted_during_reservation_blocks_transport(self):
        self.quota.confirm(2, time.time() + 60)
        original = self.quota.reserve
        def delayed(method, **kwargs):
            time.sleep(0.08)
            return original(method, **kwargs)
        self.quota.reserve = delayed
        with self.assertRaises(GuideError):
            self.client().call_tool('search_papers', {'query': 'fixture'}, budget=Budget(0.04))
        self.assertEqual(self.transport.calls, [])
        # Admission notices expiry before opening a quota transaction.
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 2)

    def test_locked_ledger_fails_promptly_without_network(self):
        import sqlite3
        self.quota.confirm(2, time.time() + 60)
        blocker = sqlite3.connect(str(self.path), timeout=1, isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            start = time.monotonic()
            with self.assertRaises(GuideError):
                self.client().call_tool('search_papers', {'query': 'fixture'}, budget=Budget(1))
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual(self.transport.calls, [])
        finally:
            blocker.execute('ROLLBACK')
            blocker.close()

    def test_two_clients_share_the_last_request(self):
        self.quota.confirm(1, time.time() + 60)
        other_store = LuneQuotaStore(self.path)
        self.addCleanup(other_store.close)
        clients = [self.client(), McpClient(self.config, transport=self.transport,
                                           quota_store=other_store)]
        barrier = threading.Barrier(2)
        outcomes = []
        def worker(client):
            barrier.wait(5)
            try:
                client.call_tool('search_papers', {'query': 'fixture'})
                outcomes.append('ok')
            except GuideError:
                outcomes.append('rejected')
        workers = [threading.Thread(target=worker, args=(client,)) for client in clients]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sorted(outcomes), ['ok', 'rejected'])
        self.assertEqual(len(self.transport.calls), 1)


if __name__ == '__main__':
    unittest.main()
