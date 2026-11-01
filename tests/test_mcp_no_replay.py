"""Offline regression tests for ambiguous paid MCP invocations."""
import io
import json
from http.client import IncompleteRead
import socket
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

from common import RunCancelled
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, McpError, ServerConfig


class NoReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.quota = LuneQuotaStore(Path(self.tmp.name) / 'quota.sqlite3')
        self.addCleanup(self.quota.close)
        self.quota.confirm(20, time.time() + 60)
        self.config = ServerConfig('lune', 'https://example.invalid/mcp',
                                   allow=('search_papers',))
        self.calls = []
        self.auth = []

    def client(self, transport):
        client = McpClient(self.config, transport=transport, quota_store=self.quota)
        client.set_auth_provider(lambda force: self.auth.append(force) or 'synthetic-secret')
        return client

    def test_401_does_not_refresh_or_replay_even_with_available_quota(self):
        def transport(request, timeout):
            self.calls.append('possibly executed')
            raise HTTPError(request.full_url, 401, 'secret', {},
                            io.BytesIO(b'synthetic-secret response-secret'))
        client = self.client(transport)
        with self.assertRaises(McpError) as caught:
            client.call_tool('search_papers', {'query': 'fixture'})
        err = caught.exception
        self.assertEqual((err.kind, err.status), ('auth', 401))
        self.assertEqual(err.as_dict()['remote_outcome'], 'unknown')
        self.assertIs(err.as_dict()['retry_safe'], False)
        self.assertEqual(str(err), 'lune: auth: tool invocation outcome unknown; automatic retry disabled')
        self.assertNotIn('secret', repr(err.as_dict()))
        self.assertEqual(self.calls, ['possibly executed'])
        self.assertEqual(self.auth, [False])
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 19)

    def test_ambiguous_transport_and_invalid_responses(self):
        cases = [socket.timeout(), URLError('secret'), OSError('secret'), IncompleteRead(b'secret'),
                 b'', b'not json secret', b'{"jsonrpc":"2.0","id":1,"error":"secret"}',
                 b'{"id":1,"result":{}}', b'{"jsonrpc":"2.0","id":999,"result":{}}',
                 b'{"jsonrpc":"2.0","id":1,"result":null}']
        for case in cases:
            with self.subTest(case=repr(case)):
                def transport(request, timeout):
                    if isinstance(case, Exception):
                        raise case
                    return io.BytesIO(case)
                with self.assertRaises(McpError) as caught:
                    self.client(transport).call_tool('search_papers', {'query': 'fixture'})
                self.assertEqual(caught.exception.as_dict()['remote_outcome'], 'unknown')
                self.assertIs(caught.exception.as_dict()['retry_safe'], False)
                self.assertNotIn('secret', repr(caught.exception.as_dict()))

    def test_explicit_json_rpc_error_is_not_reported_as_unknown(self):
        def transport(request, timeout):
            msg = json.loads(request.data)
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],
                                           'error': {'code': -32602, 'message': 'secret'}}).encode())
        with self.assertRaises(McpError) as caught:
            self.client(transport).call_tool('search_papers', {'query': 'fixture'})
        self.assertEqual(caught.exception.code, -32602)
        self.assertEqual(caught.exception.as_dict()['remote_outcome'], 'error')
        self.assertIs(caught.exception.as_dict()['retry_safe'], False)
        self.assertNotIn('secret', repr(caught.exception.as_dict()))

    def test_cancellation_after_dispatch_preserves_type_and_marks_unknown(self):
        def transport(request, timeout):
            raise RunCancelled()
        with self.assertRaises(RunCancelled) as caught:
            self.client(transport).call_tool('search_papers', {'query': 'fixture'})
        self.assertEqual(caught.exception.remote_outcome, 'unknown')
        self.assertIs(caught.exception.retry_safe, False)

    def test_http_json_rpc_error_remains_explicit(self):
        def transport(request, timeout):
            msg = json.loads(request.data)
            body = json.dumps({'jsonrpc': '2.0', 'id': msg['id'],
                               'error': {'code': -32602, 'message': 'secret'}}).encode()
            raise HTTPError(request.full_url, 400, '', {}, io.BytesIO(body))
        with self.assertRaises(McpError) as caught:
            self.client(transport).call_tool('search_papers', {'query': 'fixture'})
        self.assertEqual(caught.exception.as_dict()['remote_outcome'], 'error')
        self.assertEqual(caught.exception.code, -32602)
        self.assertNotIn('secret', repr(caught.exception.as_dict()))

    def test_finish_failure_after_response_is_structured_and_no_replay(self):
        def transport(request, timeout):
            self.calls.append(1)
            msg = json.loads(request.data)
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],
                                           'result': {'content': []}}).encode())
        self.quota.finish = lambda *args: (_ for _ in ()).throw(RuntimeError('secret'))
        result = self.client(transport).call_tool('search_papers', {'query': 'fixture'})
        self.assertEqual(result['content'], [])
        self.assertIs(result.retry_safe, False)
        self.assertIs(result.quota_finalization_failed, True)
        self.assertEqual(result.audit_status, 'failed')
        self.assertNotIn('secret', repr(result))
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 19)

    def test_finish_failure_does_not_hide_explicit_errors(self):
        for payload in ({'error': {'code': -32602, 'message': 'secret'}},
                        {'result': {'isError': True}}):
            with self.subTest(payload=payload):
                def transport(request, timeout):
                    msg = json.loads(request.data)
                    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], **payload}).encode())
                self.quota.finish = lambda *args: (_ for _ in ()).throw(RuntimeError('secret'))
                with self.assertRaises(McpError) as caught:
                    self.client(transport).call_tool('search_papers', {'query': 'fixture'})
                data = caught.exception.as_dict()
                self.assertEqual(data['remote_outcome'], 'error')
                self.assertIs(data['quota_finalization_failed'], True)
                self.assertNotIn('secret', repr(data))

    def test_prompts_get_does_not_replay_on_401(self):
        attempts = []
        def transport(request, timeout):
            attempts.append(1)
            raise HTTPError(request.full_url, 401, '', {}, io.BytesIO())
        config = ServerConfig('offline', 'https://example.invalid', allow_prompts=('p',),
                              local_prompt_schemas={'p': {'type': 'object'}})
        client = McpClient(config, transport=transport)
        client.set_auth_provider(lambda force: self.auth.append(force) or 'token')
        with self.assertRaises(McpError):
            client.get_prompt('p', {})
        self.assertEqual(attempts, [1])
        self.assertEqual(self.auth, [False])

    def test_read_only_methods_refresh_once(self):
        for method in ('initialize', 'ping', 'tools/list', 'prompts/list'):
            with self.subTest(method=method):
                attempts, auth = [], []
                def transport(request, timeout):
                    msg = json.loads(request.data)
                    attempts.append(msg['method'])
                    if len(attempts) == 1:
                        raise HTTPError(request.full_url, 401, '', {}, io.BytesIO())
                    result = ({'tools': []} if method == 'tools/list' else
                              {'prompts': []} if method == 'prompts/list' else {})
                    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],
                                                   'result': result}).encode())
                client = McpClient(ServerConfig('offline', 'https://example.invalid'), transport=transport)
                client.set_auth_provider(lambda force: auth.append(force) or 'token')
                if method == 'initialize':
                    client.initialize()
                else:
                    client.request(method)
                self.assertEqual(attempts, [method, method])
                self.assertEqual(auth, [False, True])


if __name__ == '__main__':
    unittest.main()
