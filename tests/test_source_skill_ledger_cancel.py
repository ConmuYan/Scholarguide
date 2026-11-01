"""Offline coverage for interruption of the Lune JSON ledger after response."""
import io
import json
import os
import tempfile
import threading
import time
from http.client import HTTPConnection
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Budget, BudgetExceeded, Config, RunCancelled
from execution_events import bind_events
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, ServerConfig
from mcp_lune import LuneProvider
from source_store import RetainedSourceResult, SourceStore


class LedgerCancellationTests(unittest.TestCase):
    def test_finish_interruption_retains_trusted_redacted_response(self):
        for error in (RunCancelled(), BudgetExceeded('fixture budget')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                quota_path = root / 'quota.sqlite'
                with LuneQuotaStore(quota_path) as quota:
                    quota.confirm(2, time.time() + 60)
                calls, events = [], []
                def transport(request, timeout):
                    calls.append(1)
                    message = json.loads(request.data)
                    result = {'structuredContent': {'papers': [{'id': 'one', 'title': 'synthetic-key'}],
                              'api_key': 'synthetic-key'}, 'audit_status': 'recorded',
                              'retry_safe': True, 'remote_outcome': 'not_started'}
                    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                                  'result': result}).encode())
                client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                                   transport=transport)
                provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
                store = SourceStore(root / 'sources.sqlite')
                config = Config(source='lune', source_store=store, lune_key='synthetic-key', cache=False)
                try:
                    with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(quota_path),
                                                'SG_MCP_TOKEN_FILE': str(root / 'missing-token')}), \
                         patch('mcp_lune.install', return_value=provider), \
                         patch.object(store, '_ensure_ready', return_value=config), \
                         patch.object(provider, '_finish', side_effect=error), \
                         bind_events(lambda action, payload: events.append((action, payload))):
                        with self.assertRaises(type(error)) as caught:
                            store.invoke('lune', 'search', {'query': 'fixture'}, config, budget=Budget(5))
                    self.assertIs(caught.exception, error)
                    self.assertEqual(error.remote_outcome, 'completed')
                    self.assertIs(error.retry_safe, False)
                    retained = error.partial_source_result
                    self.assertIsInstance(retained, RetainedSourceResult)
                    self.assertEqual(retained['audit_status'], 'failed')
                    self.assertEqual(retained['result']['papers'][0]['id'], 'one')
                    self.assertNotIn('synthetic-key', repr(retained))
                    self.assertEqual(calls, [1])
                    self.assertTrue(any(action == 'source_remote_completed' and payload.get('status') == 'completed'
                                        and payload.get('retry_safe') is False for action, payload in events))
                    self.assertTrue(any(action == 'source_audit_failed' for action, _ in events))
                    with LuneQuotaStore(quota_path) as quota:
                        self.assertEqual(quota.status()['remaining_from_snapshot'], 1)
                finally:
                    store.close()
    def test_existing_exception_wins_over_finish_interruption(self):
        for original in (RunCancelled(), BudgetExceeded('original')):
            with self.subTest(original=type(original).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                provider = LuneProvider(ledger_path=root / 'ledger.json')
                store = SourceStore(root / 'sources.sqlite')
                config = Config(source='lune', source_store=store, cache=False)
                try:
                    with patch('mcp_lune.install', return_value=provider), \
                         patch.object(store, '_ensure_ready', return_value=config), \
                         patch.object(provider, 'client') as client, \
                         patch.object(provider, '_finish', side_effect=BudgetExceeded('secondary')):
                        client.return_value.call_tool.side_effect = original
                        with self.assertRaises(type(original)) as caught:
                            store.invoke('lune', 'search', {'query': 'fixture'}, config, budget=Budget(5))
                    self.assertIs(caught.exception, original)
                    self.assertFalse(hasattr(original, 'partial_source_result'))
                finally:
                    store.close()

    def test_redaction_failure_does_not_replace_finish_error(self):
        for error in (RunCancelled(), BudgetExceeded('original')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                quota_path = root / 'quota.sqlite'
                with LuneQuotaStore(quota_path) as quota:
                    quota.confirm(2, time.time() + 60)
                def transport(request, timeout):
                    message = json.loads(request.data)
                    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                                  'result': {'structuredContent': {'papers': []}}}).encode())
                provider = LuneProvider(client=McpClient(ServerConfig('lune', 'https://fixture.invalid',
                    allow=('search_papers',)), transport=transport), ledger_path=root / 'ledger.json')
                store = SourceStore(root / 'sources.sqlite')
                config = Config(source='lune', source_store=store, cache=False)
                try:
                    with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(quota_path)}), \
                         patch('mcp_lune.install', return_value=provider), \
                         patch.object(store, '_ensure_ready', return_value=config), \
                         patch.object(store, '_redact', side_effect=BudgetExceeded('secondary')), \
                         patch.object(provider, '_finish', side_effect=error):
                        with self.assertRaises(type(error)) as caught:
                            store.invoke('lune', 'search', {'query': 'fixture'}, config, budget=Budget(5))
                    self.assertIs(caught.exception, error)
                    self.assertEqual(error.remote_outcome, 'completed')
                    self.assertFalse(hasattr(error, 'partial_source_result'))
                finally:
                    store.close()

    def test_invalid_response_is_not_retained_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota_path = root / 'quota.sqlite'
            with LuneQuotaStore(quota_path) as quota:
                quota.confirm(2, time.time() + 60)
            def transport(request, timeout):
                message = json.loads(request.data)
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {}}).encode())
            provider = LuneProvider(client=McpClient(ServerConfig('lune', 'https://fixture.invalid',
                allow=('search_papers',)), transport=transport), ledger_path=root / 'ledger.json')
            store = SourceStore(root / 'sources.sqlite')
            error = BudgetExceeded('fixture')
            config = Config(source='lune', source_store=store, cache=False)
            try:
                with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(quota_path)}), \
                     patch('mcp_lune.install', return_value=provider), \
                     patch.object(store, '_ensure_ready', return_value=config), \
                     patch.object(provider, '_finish', side_effect=error):
                    with self.assertRaises(BudgetExceeded) as caught:
                        store.invoke('lune', 'search', {'query': 'fixture'}, config, budget=Budget(5))
                self.assertIs(caught.exception, error)
                self.assertEqual(error.remote_outcome, 'unknown')
                self.assertFalse(hasattr(error, 'partial_source_result'))
            finally:
                store.close()

    def test_locked_store_does_not_mask_finish_interruption_or_deadlock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota_path = root / 'quota.sqlite'
            with LuneQuotaStore(quota_path) as quota:
                quota.confirm(2, time.time() + 60)
            def transport(request, timeout):
                message = json.loads(request.data)
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                              'result': {'structuredContent': {'papers': []}}}).encode())
            provider = LuneProvider(client=McpClient(ServerConfig('lune', 'https://fixture.invalid',
                allow=('search_papers',)), transport=transport), ledger_path=root / 'ledger.json')
            store = SourceStore(root / 'sources.sqlite')
            config = Config(source='lune', source_store=store, cache=False)
            error = BudgetExceeded('fixture')
            held, release = threading.Event(), threading.Event()
            def hold():
                with store._lock:
                    held.set()
                    release.wait(3)
            thread = threading.Thread(target=hold)
            def finish(*args, **kwargs):
                thread.start()
                if not held.wait(1):
                    raise AssertionError('lock not acquired')
                raise error
            started = time.monotonic()
            try:
                with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(quota_path)}), \
                     patch('mcp_lune.install', return_value=provider), \
                     patch.object(store, '_ensure_ready', return_value=config), \
                     patch.object(provider, '_finish', side_effect=finish):
                    with self.assertRaises(BudgetExceeded) as caught:
                        store.invoke('lune', 'search', {'query': 'fixture'}, config, budget=Budget(5))
                self.assertIs(caught.exception, error)
                self.assertEqual(error.remote_outcome, 'completed')
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertFalse(hasattr(error, 'partial_source_result'))
            finally:
                release.set()
                if thread.ident is not None:
                    thread.join(3)
                store.close()

    def test_api_exposes_retained_result_for_both_interruptions(self):
        from providers import ProviderStore
        from webapp import ChatService, Jobs, create_server
        for error in (RunCancelled(), BudgetExceeded('fixture budget')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                quota_path = root / 'quota.sqlite'
                with LuneQuotaStore(quota_path) as quota:
                    quota.confirm(2, time.time() + 60)
                calls = []
                def transport(request, timeout):
                    calls.append(1)
                    message = json.loads(request.data)
                    result = {'structuredContent': {'papers': [{'id': 'one', 'title': 'synthetic-key'}]},
                              'audit_status': 'recorded', 'retry_safe': True}
                    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode())
                provider = LuneProvider(client=McpClient(ServerConfig('lune', 'https://fixture.invalid',
                    allow=('search_papers',)), transport=transport), ledger_path=root / 'ledger.json')
                store = SourceStore(root / 'sources.sqlite')
                providers = ProviderStore(root / 'providers.json')
                chat = ChatService(root / 'web', providers=providers)
                config = Config(source='lune', source_store=store, lune_key='synthetic-key')
                server = thread = None
                try:
                    with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(quota_path)}), \
                         patch('mcp_lune.install', return_value=provider), \
                         patch.object(store, '_ensure_ready', return_value=config), \
                         patch.object(provider, '_finish', side_effect=error):
                        server = create_server(0, jobs=Jobs(root / 'runs', providers=providers),
                                               chat=chat, providers=providers, sources=store)
                        thread = threading.Thread(target=server.serve_forever, daemon=True)
                        thread.start()
                        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                        try:
                            conn.request('POST', '/api/v1/sources/lune/skills/search',
                                         json.dumps({'arguments': {'query': 'fixture'}}),
                                         {'Content-Type': 'application/json'})
                            response = conn.getresponse()
                            payload = json.loads(response.read())
                        finally:
                            conn.close()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(payload['error']['remote_outcome'], 'completed')
                    self.assertIs(payload['error']['retry_safe'], False)
                    self.assertEqual(payload['partial_result']['audit_status'], 'failed')
                    self.assertEqual(payload['partial_result']['result']['papers'][0]['id'], 'one')
                    self.assertNotIn('synthetic-key', repr(payload))
                    self.assertEqual(calls, [1])
                    with LuneQuotaStore(quota_path) as quota:
                        self.assertEqual(quota.status()['remaining_from_snapshot'], 1)
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if thread is not None:
                        thread.join(3)
                    chat.close()
                    store.close()


if __name__ == '__main__':
    unittest.main()
