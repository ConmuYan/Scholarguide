"""Offline fault injection for quota and search ledger finalization."""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from common import BudgetExceeded, RunCancelled
from execution_events import bind_events
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, McpError, ServerConfig
from mcp_lune import LuneProvider


class FinalizationCancellationTests(unittest.TestCase):
    def test_quota_finish_retains_validated_response(self):
        for error_type in (RunCancelled, BudgetExceeded):
            with self.subTest(error=error_type), tempfile.TemporaryDirectory() as d:
                with LuneQuotaStore(Path(d) / 'quota.sqlite') as quota:
                    quota.confirm(2, time.time() + 60)
                    calls, events = [], []
                    def transport(request, timeout):
                        calls.append(1)
                        msg = json.loads(request.data)
                        result = {'structuredContent': {'papers': [{'id': 'one', 'title': 'First'}]}}
                        return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode())
                    client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                                       transport=transport, quota_store=quota)
                    error = error_type('synthetic-secret') if error_type is BudgetExceeded else error_type()
                    with patch.object(quota, 'finish', side_effect=error), bind_events(lambda a, p: events.append((a, p))):
                        with self.assertRaises(error_type) as caught:
                            client.call_tool('search_papers', {'query': 'q'})
                    exc = caught.exception
                    self.assertEqual(exc.remote_outcome, 'response_received')
                    self.assertFalse(exc.retry_safe)
                    self.assertEqual(exc.mcp_result['structuredContent']['papers'][0]['id'], 'one')
                    self.assertEqual(calls, [1])
                    self.assertEqual(quota.status()['remaining_from_snapshot'], 1)
                    self.assertTrue(any(action == 'source_audit_failed' for action, _ in events))
                    self.assertNotIn('secret', repr(events))
                    self.assertNotIn('secret', str(exc))

    def test_finalization_never_masks_invalid_response_or_transport_error(self):
        for body in (b'invalid secret', b'{"jsonrpc":"2.0","id":1,"error":{"code":-1,"message":"secret"}}', None):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as d:
                with LuneQuotaStore(Path(d) / 'quota.sqlite') as quota:
                    quota.confirm(2, time.time() + 60)
                    def transport(request, timeout):
                        if body is None:
                            raise TimeoutError('secret')
                        return io.BytesIO(body)
                    client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                                       transport=transport, quota_store=quota)
                    with patch.object(quota, 'finish', side_effect=RunCancelled()), self.assertRaises(McpError) as caught:
                        client.call_tool('search_papers', {'query': 'q'})
                    self.assertNotIn('secret', str(caught.exception))
                    self.assertEqual(quota.status()['remaining_from_snapshot'], 1)

    def test_provider_retains_candidates_after_quota_cancellation(self):
        with tempfile.TemporaryDirectory() as d, LuneQuotaStore(Path(d) / 'quota.sqlite') as quota:
            quota.confirm(2, time.time() + 60)
            def transport(request, timeout):
                msg = json.loads(request.data)
                payload = {'papers': [{'id': 'one', 'title': 'First'}], 'has_more': False}
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],
                                              'result': {'structuredContent': payload}}).encode())
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                               transport=transport, quota_store=quota)
            provider = LuneProvider(client=client, ledger_path=Path(d) / 'ledger.json')
            with patch.object(quota, 'finish', side_effect=RunCancelled()), self.assertRaises(RunCancelled) as caught:
                provider.search('q', 10)
            self.assertEqual(caught.exception.remote_outcome, 'completed')
            self.assertEqual([p.id for p in caught.exception.partial_papers], ['lune:one'])
            self.assertFalse(caught.exception.retry_safe)
            self.assertEqual(client.http_attempts, 1)

    def test_academic_source_retains_completed_page_after_finalization_cancel(self):
        from common import Budget, Config
        from source import AcademicSource, retained_partial_batch
        for error_type in (RunCancelled, BudgetExceeded):
            with self.subTest(error=error_type), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                with LuneQuotaStore(root / 'quota.sqlite') as quota:
                    quota.confirm(2, time.time() + 60)
                    def transport(request, timeout):
                        msg = json.loads(request.data)
                        result = {'structuredContent': {'papers': [{'id': 'one', 'title': 'First'}],
                                                          'has_more': False}}
                        return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode())
                    client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                                       transport=transport, quota_store=quota)
                    provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
                    source = AcademicSource(Config(source='lune', cache=False), Budget(5))
                    with patch('mcp_lune.install', return_value=provider), \
                         patch.object(source, 'available', return_value=True), \
                         patch.object(source, '_wait_for'), \
                         patch.object(quota, 'finish', side_effect=error_type()):
                        with self.assertRaises(error_type) as caught:
                            source.search_batch('fixture', limit=10)
                    error = caught.exception
                    self.assertEqual(error.remote_outcome, 'completed')
                    self.assertFalse(error.retry_safe)
                    batch = retained_partial_batch(error, 'fixture', ['lune'], 10)
                    self.assertIsNotNone(batch)
                    self.assertEqual([paper.id for paper in batch.papers], ['lune:one'])
                    self.assertEqual(batch.runs[-1].audit_status, 'failed')
                    self.assertEqual(client.http_attempts, 1)
                    self.assertEqual(quota.status()['remaining_from_snapshot'], 1)

    def test_owned_close_fault_and_empty_success(self):
        for error_type in (RunCancelled, BudgetExceeded):
            with self.subTest(error=error_type), tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'quota.sqlite'
                with LuneQuotaStore(path) as quota:
                    quota.confirm(2, time.time() + 60)
                close = LuneQuotaStore.close
                def failing_close(store):
                    close(store)
                    raise error_type()
                def transport(request, timeout):
                    msg = json.loads(request.data)
                    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result':
                                                  {'structuredContent': {'papers': [], 'has_more': False}}}).encode())
                client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                                   transport=transport)
                provider = LuneProvider(client=client, ledger_path=Path(d) / 'ledger.json')
                with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(path)}), \
                     patch.object(LuneQuotaStore, 'close', failing_close), self.assertRaises(error_type) as caught:
                    provider.search('q', 10)
                self.assertEqual(caught.exception.remote_outcome, 'completed')
                self.assertEqual(caught.exception.partial_papers, [])
                self.assertFalse(getattr(caught.exception, 'partial_search', False))
                with LuneQuotaStore(path) as quota:
                    self.assertEqual(quota.status()['remaining_from_snapshot'], 1)

    def test_empty_success_not_partial_after_ordinary_audit_failure(self):
        with tempfile.TemporaryDirectory() as d:
            class Client:
                def call_tool(self, name, arguments):
                    return {'structuredContent': {'papers': [], 'has_more': False}}
            provider = LuneProvider(client=Client(), ledger_path=Path(d) / 'ledger.json')
            with patch.object(provider, '_finish', side_effect=OSError('secret')):
                result = provider.search('q', 10)
            self.assertEqual(result, [])
            self.assertFalse(result.partial)
            self.assertEqual(result.audit_status, 'failed')

    def test_ledger_cancellation_completed_and_incomplete(self):
        for more, items, error_type in ((False, [{'id': 'one', 'title': 'First'}], RunCancelled),
                                        (True, [{'id': 'one', 'title': 'First'}], BudgetExceeded),
                                        (False, [], BudgetExceeded), (True, [], RunCancelled)):
            with self.subTest(more=more, items=items), tempfile.TemporaryDirectory() as d:
                class Client:
                    calls = 0
                    def call_tool(self, name, arguments):
                        self.calls += 1
                        return {'structuredContent': {'papers': items, 'has_more': more}}
                client = Client()
                provider = LuneProvider(client=client, ledger_path=Path(d) / 'ledger.json')
                fault = error_type('synthetic-secret') if error_type is BudgetExceeded else error_type()
                with patch.object(provider, '_finish', side_effect=fault), self.assertRaises(error_type) as caught:
                    provider.search('q', 10)
                exc = caught.exception
                self.assertEqual(exc.remote_outcome, 'unknown' if more else 'completed')
                self.assertEqual([p.id for p in exc.partial_papers], ['lune:one'] if items else [])
                self.assertEqual(getattr(exc, 'partial_search', False), more)
                self.assertFalse(exc.retry_safe)
                self.assertNotIn('secret', str(exc))
                self.assertEqual(client.calls, 1)


if __name__ == '__main__':
    unittest.main()
