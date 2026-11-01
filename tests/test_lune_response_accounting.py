"""Offline regressions for retained responses and incomplete Lune searches."""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from execution_events import bind_events
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, McpError, ServerConfig, _parse_messages
from mcp_lune import LuneProvider


class ResponseAccountingTests(unittest.TestCase):
    def test_quota_finish_failure_retains_response_and_fixed_metadata(self):
        with tempfile.TemporaryDirectory() as directory, LuneQuotaStore(Path(directory) / 'q.sqlite') as quota:
            quota.confirm(3, time.time() + 60)
            calls, events = [], []
            def transport(request, timeout):
                calls.append(1)
                msg = json.loads(request.data)
                result = {'structuredContent': {'papers': [{'id': 'one'}]}, 'audit_status': 'recorded'}
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode())
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                               transport=transport, quota_store=quota)
            with patch.object(quota, 'finish', side_effect=OSError('private')), bind_events(lambda a, p: events.append((a, p))):
                result = client.call_tool('search_papers', {'query': 'q'})
            self.assertEqual(result['structuredContent']['papers'][0]['id'], 'one')
            self.assertEqual(result.audit_status, 'failed')
            self.assertFalse(result.retry_safe)
            self.assertEqual(calls, [1])
            self.assertEqual(quota.status()['remaining_from_snapshot'], 2)
            self.assertTrue(any(a == 'source_audit_failed' for a, _ in events))
            self.assertNotIn('private', repr(events))

    def test_audit_event_sink_cannot_erase_received_response(self):
        from common import RunCancelled
        with tempfile.TemporaryDirectory() as directory, LuneQuotaStore(Path(directory) / 'q.sqlite') as quota:
            quota.confirm(2, time.time() + 60)
            def transport(request, timeout):
                msg = json.loads(request.data)
                result = {'structuredContent': {'papers': [{'id': 'one'}]}}
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode())
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                               transport=transport, quota_store=quota)
            def sink(action, payload):
                if action == 'source_audit_failed':
                    raise RunCancelled()
            with patch.object(quota, 'finish', side_effect=OSError('private')), bind_events(sink):
                result = client.call_tool('search_papers', {'query': 'fixture'})
            self.assertEqual(result['structuredContent']['papers'][0]['id'], 'one')
            self.assertEqual(result.audit_status, 'failed')
            self.assertFalse(result.retry_safe)
            self.assertEqual(quota.status()['remaining_from_snapshot'], 1)

    def test_owned_close_failure_keeps_response_and_consumes_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'q.sqlite'
            with LuneQuotaStore(path) as quota:
                quota.confirm(2, time.time() + 60)
            closed = LuneQuotaStore.close
            def failing_close(store):
                closed(store)
                raise OSError('private')
            def transport(request, timeout):
                msg = json.loads(request.data)
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],
                                              'result': {'content': []}}).encode())
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                               transport=transport)
            with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(path)}), \
                 patch.object(LuneQuotaStore, 'close', failing_close):
                result = client.call_tool('search_papers', {'query': 'q'})
            self.assertEqual(result['content'], [])
            self.assertEqual(result.audit_status, 'failed')
            with LuneQuotaStore(path) as quota:
                self.assertEqual(quota.status()['remaining_from_snapshot'], 1)

    def test_close_failure_does_not_replace_transport_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'q.sqlite'
            with LuneQuotaStore(path) as quota:
                quota.confirm(2, time.time() + 60)
            closed = LuneQuotaStore.close
            def failing_close(store):
                closed(store)
                raise OSError('private close')
            def transport(request, timeout):
                raise TimeoutError('private transport')
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                               transport=transport)
            with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(path)}), \
                 patch.object(LuneQuotaStore, 'close', failing_close), self.assertRaises(McpError) as caught:
                client.call_tool('search_papers', {'query': 'q'})
            self.assertEqual(caught.exception.kind, 'timeout')
            self.assertTrue(caught.exception.quota_finalization_failed)
            self.assertFalse(caught.exception.retry_safe)
            self.assertNotIn('private', str(caught.exception))
            with LuneQuotaStore(path) as quota:
                self.assertEqual(quota.status()['remaining_from_snapshot'], 1)

    def test_source_store_receives_actual_response_and_failure_event(self):
        from common import Config
        from source_store import SourceStore
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            path = root / 'q.sqlite'
            with LuneQuotaStore(path) as quota:
                quota.confirm(2, time.time() + 60)
            calls, events = [], []
            def transport(request, timeout):
                calls.append(1)
                msg = json.loads(request.data)
                result = {'structuredContent': {'papers': [{'id': 'one'}]}}
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode())
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                               transport=transport)
            provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
            store = SourceStore(root / 'source.sqlite')
            stack.callback(store.close)
            config = Config(source='lune', source_store=store, cache=False)
            with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(path)}), \
                 patch('mcp_lune.install', return_value=provider), \
                 patch.object(store, '_ensure_ready', return_value=config), \
                 patch.object(LuneQuotaStore, 'finish', side_effect=OSError('private')), \
                 bind_events(lambda a, p: events.append((a, p))):
                result = store.invoke('lune', 'search', {'query': 'q'}, config)
            self.assertEqual(result['result']['papers'][0]['id'], 'one')
            self.assertEqual(result['audit_status'], 'failed')
            self.assertEqual(calls, [1])
            self.assertTrue(any(a == 'source_audit_failed' and p.get('reason') == 'lune_quota_finalization_failed'
                                for a, p in events))
            self.assertNotIn('private', repr(events))
            with LuneQuotaStore(path) as quota:
                self.assertEqual(quota.status()['remaining_from_snapshot'], 1)

    def test_source_skill_finalization_cancel_preserves_redacted_result(self):
        from common import Budget, BudgetExceeded, Config, RunCancelled
        from source_store import SourceStore
        for error_type in (RunCancelled, BudgetExceeded):
            with self.subTest(error=error_type), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / 'quota.sqlite'
                with LuneQuotaStore(path) as quota:
                    quota.confirm(2, time.time() + 60)
                calls = []
                def transport(request, timeout):
                    calls.append(1)
                    msg = json.loads(request.data)
                    result = {'structuredContent': {'papers': [{'id': 'one', 'title': 'First'}],
                                                     'api_key': 'synthetic-secret'}}
                    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode())
                client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                                   transport=transport)
                provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
                store = SourceStore(root / 'sources.sqlite')
                config = Config(source='lune', source_store=store, cache=False)
                try:
                    with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(path)}), \
                         patch('mcp_lune.install', return_value=provider), \
                         patch.object(store, '_ensure_ready', return_value=config), \
                         patch.object(LuneQuotaStore, 'finish', side_effect=error_type()):
                        with self.assertRaises(error_type) as caught:
                            store.invoke('lune', 'search', {'query': 'fixture'}, config, budget=Budget(5))
                    error = caught.exception
                    self.assertEqual(error.remote_outcome, 'completed')
                    self.assertIs(error.retry_safe, False)
                    self.assertEqual(error.partial_source_result['result']['papers'][0]['id'], 'one')
                    self.assertEqual(error.partial_source_result['audit_status'], 'failed')
                    self.assertNotIn('synthetic-secret', repr(error.partial_source_result))
                    self.assertEqual(calls, [1])
                    with LuneQuotaStore(path) as quota:
                        self.assertEqual(quota.status()['remaining_from_snapshot'], 1)
                finally:
                    store.close()

    def test_search_stops_on_quota_failure_with_candidates(self):
        with tempfile.TemporaryDirectory() as directory, LuneQuotaStore(Path(directory) / 'q.sqlite') as quota:
            quota.confirm(3, time.time() + 60)
            calls = []
            def transport(request, timeout):
                calls.append(1)
                msg = json.loads(request.data)
                result = {'structuredContent': {'papers': [{'id': 'one'}], 'has_more': True}}
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode())
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                               transport=transport, quota_store=quota)
            provider = LuneProvider(client=client, ledger_path=Path(directory) / 'ledger.json')
            with patch.object(quota, 'finish', side_effect=OSError('private')):
                result = provider.search('q', 10)
            self.assertEqual([p.id for p in result], ['lune:one'])
            self.assertTrue(result.partial)
            self.assertEqual(result.stop_reason, 'quota_finalization_failed')
            self.assertEqual(result.audit_status, 'failed')
            self.assertFalse(result.retry_safe)
            self.assertEqual(calls, [1])

    def test_early_stops_are_observable(self):
        for mode in ('ledger', 'offset', 'page_limit'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                class Client:
                    calls = 0
                    def call_tool(self, name, arguments):
                        self.calls += 1
                        return {'structuredContent': {'papers': [{'id': str(self.calls)}],
                                'has_more': True, 'next_offset': 0 if mode == 'offset' else self.calls}}
                client = Client()
                provider = LuneProvider(client=client, ledger_path=Path(directory) / 'ledger.json')
                with patch.object(provider, '_finish', side_effect=OSError('private') if mode == 'ledger' else None,
                                  wraps=None if mode == 'ledger' else provider._finish):
                    result = provider.search('q', 10)
                self.assertIsInstance(result, list)
                self.assertTrue(result.partial)
                self.assertFalse(result.retry_safe)
                self.assertEqual(result.stop_reason, {'ledger': 'lune_ledger_unavailable',
                                 'offset': 'nonadvancing_offset', 'page_limit': 'max_pages'}[mode])
                self.assertEqual(len(result), 3 if mode == 'page_limit' else 1)

    def test_source_search_reports_nonadvancing_page_as_partial(self):
        from common import Budget, Config, GuideError
        from source import AcademicSource, retained_partial_batch
        with tempfile.TemporaryDirectory() as directory, LuneQuotaStore(Path(directory) / 'q.sqlite') as quota:
            root = Path(directory)
            quota.confirm(3, time.time() + 60)
            calls = []
            def transport(request, timeout):
                calls.append(1)
                message = json.loads(request.data)
                result = {'structuredContent': {'papers': [{'id': 'one', 'title': 'First'}],
                                                 'has_more': True, 'next_offset': 0}}
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode())
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                               transport=transport, quota_store=quota)
            provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
            source = AcademicSource(Config(source='lune', cache=False), Budget(5))
            with patch('mcp_lune.install', return_value=provider), \
                 patch.object(source, 'available', return_value=True), patch.object(source, '_wait_for'):
                with self.assertRaises(GuideError) as caught:
                    source.search_batch('fixture', limit=10)
            batch = retained_partial_batch(caught.exception, 'fixture', ['lune'], 10)
            self.assertIsNotNone(batch)
            self.assertEqual([paper.id for paper in batch.papers], ['lune:one'])
            self.assertEqual(batch.runs[-1].status, 'partial')
            self.assertEqual(batch.runs[-1].code, 'nonadvancing_offset')
            self.assertIs(batch.runs[-1].retry_safe, False)
            self.assertEqual(calls, [1])

    def test_invalid_pagination_offset_preserves_partial_without_remote_text(self):
        from common import Budget, Config, GuideError
        from source import AcademicSource, retained_partial_batch
        for value in ('synthetic-private', True, -1, 10001, {'secret': 'synthetic-private'}):
            with self.subTest(value=type(value).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                class Client:
                    def call_tool(self, name, arguments):
                        return {'structuredContent': {'papers': [{'id': 'one', 'title': 'First'}],
                                                      'has_more': True, 'next_offset': value}}
                provider = LuneProvider(client=Client(), ledger_path=root / 'ledger.json')
                source = AcademicSource(Config(source='lune', cache=False), Budget(3))
                with patch('mcp_lune.install', return_value=provider), \
                     patch.object(source, 'available', return_value=True), patch.object(source, '_wait_for'):
                    with self.assertRaises(GuideError) as caught:
                        source.search_batch('fixture', limit=10)
                batch = retained_partial_batch(caught.exception, 'fixture', ['lune'], 10)
                self.assertEqual([p.id for p in batch.papers], ['lune:one'])
                self.assertEqual(batch.runs[-1].code, 'invalid_offset')
                self.assertNotIn('synthetic-private', str(caught.exception))
                self.assertNotIn('synthetic-private', repr(batch.summary()))

    def test_sse_multiline_and_event_boundaries(self):
        body = b': keepalive\r\nevent: message\r\ndata: {"jsonrpc":"2.0",\r\ndata: "id":1,\r\ndata: "result":{}}\r\n\r\ndata: {"id":2}\n'
        self.assertEqual(_parse_messages(body, 'fixture'), [{'jsonrpc': '2.0', 'id': 1, 'result': {}}, {'id': 2}])
        with self.assertRaises(McpError):
            _parse_messages(b'data: {"id":\ndata: }\n', 'fixture')


if __name__ == '__main__':
    unittest.main()
