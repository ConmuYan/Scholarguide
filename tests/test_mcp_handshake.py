"""Offline tests for explicit bounded Streamable HTTP initialization."""
import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError

from mcp_client import McpClient, McpError, ServerConfig
from common import Budget, CancellationToken, RunCancelled


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


def rpc(message, result, headers=None):
    return Response(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode(), headers=headers)


INIT = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
        'serverInfo': {'name': 'fixture', 'version': '1'}}


class HandshakeTests(unittest.TestCase):
    def test_latest_supported_protocol_version_handshake(self):
        requests = []
        def transport(request, timeout):
            message = json.loads(request.data)
            requests.append((message, {k.lower(): v for k, v in request.header_items()}))
            if message['method'] == 'initialize':
                return rpc(message, {'protocolVersion': '2025-11-25', 'capabilities': {},
                                     'serverInfo': {'name': 'fixture', 'version': '1'}},
                           {'Mcp-Session-Id': 'opaque-id'})
            if message['method'] == 'notifications/initialized':
                return Response(b'', 202)
            return rpc(message, {})
        client = McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2025-11-25'),
                           stateful=True, transport=transport)
        self.assertEqual(client.request('ping'), {})
        self.assertEqual([item['method'] for item, _ in requests],
                         ['initialize', 'notifications/initialized', 'ping'])
        self.assertEqual(requests[-1][1]['mcp-protocol-version'], '2025-11-25')

    def test_handshake_precedes_concurrent_business_requests(self):
        requests = []
        def transport(request, timeout):
            msg = json.loads(request.data)
            requests.append((msg, {k.lower(): v for k, v in request.header_items()}))
            if msg['method'] == 'initialize':
                return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized':
                return Response(b'', 202)
            return rpc(msg, {})
        client = McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26'),
                           stateful=True, transport=transport)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: client.request('ping'), range(4)))
        self.assertEqual([m['method'] for m, _ in requests],
                         ['initialize', 'notifications/initialized'] + ['ping'] * 4)
        self.assertNotIn('id', requests[1][0])
        for _, headers in requests[1:]:
            self.assertEqual(headers['mcp-session-id'], 'opaque-id')
            self.assertEqual(headers['mcp-protocol-version'], '2025-03-26')

    def test_bad_session_fails_closed_without_business_and_without_replay(self):
        seen = []
        def transport(request, timeout):
            msg = json.loads(request.data)
            seen.append(msg['method'])
            return rpc(msg, INIT, {'Mcp-Session-Id': 'secret bad'})
        client = McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26'),
                           stateful=True, transport=transport)
        for _ in range(2):
            with self.assertRaises(McpError) as caught:
                client.request('ping')
            self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(seen, ['initialize'])

    def test_notification_failure_poisons_session(self):
        seen = []
        def transport(request, timeout):
            msg = json.loads(request.data)
            seen.append(msg['method'])
            if msg['method'] == 'initialize':
                return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            return Response(b'{"unexpected":true}', 200)
        client = McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26'),
                           stateful=True, transport=transport)
        for _ in range(2):
            with self.assertRaises(McpError):
                client.request('ping')
        self.assertEqual(seen, ['initialize', 'notifications/initialized'])

    def test_every_lune_handshake_attempt_reserves_quota(self):
        class Quota:
            def __init__(self): self.events = []
            def reserve(self, method):
                self.events.append(('reserve', method))
                return method
            def finish(self, handle, outcome): self.events.append(('finish', handle, outcome))
        quota = Quota()
        def transport(request, timeout):
            msg = json.loads(request.data)
            if msg['method'] == 'initialize': return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized': return Response(b'', 202)
            return rpc(msg, {})
        client = McpClient(ServerConfig('lune', 'http://example.invalid', protocol='2025-03-26'),
                           stateful=True, transport=transport, quota_store=quota)
        client.request('ping')
        self.assertEqual(quota.events, [
            ('reserve', 'initialize'), ('finish', 'initialize', 'response_received'),
            ('reserve', 'notifications/initialized'), ('finish', 'notifications/initialized', 'response_received'),
            ('reserve', 'ping'), ('finish', 'ping', 'response_received')])

    def test_changed_auth_identity_rejects_before_notification(self):
        seen = []
        count = []
        def provider(force):
            count.append(force)
            return 'token-one' if len(count) == 1 else 'token-two'
        def transport(request, timeout):
            msg = json.loads(request.data)
            seen.append(msg['method'])
            return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
        client = McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26'),
                           stateful=True, transport=transport)
        client.set_auth_provider(provider)
        with self.assertRaises(McpError): client.request('ping')
        self.assertEqual(seen, ['initialize'])
        self.assertFalse(any(count))

    def test_progress_notification_does_not_discard_valid_tool_result(self):
        def transport(request, timeout):
            msg = json.loads(request.data)
            if msg['method'] == 'initialize': return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized': return Response(b'', 202)
            items = [{'jsonrpc': '2.0', 'method': 'notifications/progress',
                      'params': {'progressToken': 'fixture', 'progress': 1}},
                     {'jsonrpc': '2.0', 'id': msg['id'], 'result': {'content': []}}]
            body = ''.join('data: ' + json.dumps(x) + '\n\n' for x in items)
            return Response(body.encode())
        config = ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26',
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}})
        client = McpClient(config, stateful=True, transport=transport)
        self.assertEqual(client.call_tool('t', {}), {'content': []})

    def test_server_request_preserves_received_tool_result_without_replay(self):
        seen = []
        def transport(request, timeout):
            msg = json.loads(request.data)
            seen.append(msg['method'])
            if msg['method'] == 'initialize': return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized': return Response(b'', 202)
            messages = [{'jsonrpc': '2.0', 'method': 'sampling/createMessage', 'id': 99},
                        {'jsonrpc': '2.0', 'id': msg['id'], 'result': {
                            'content': [{'type': 'text', 'text': 'retained opaque-id'}]}}]
            return Response(json.dumps(messages).encode())
        config = ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26',
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}})
        client = McpClient(config, stateful=True, transport=transport)
        with self.assertRaises(McpError) as caught:
            client.call_tool('t', {})
        self.assertEqual(caught.exception.remote_outcome, 'response_received')
        self.assertFalse(caught.exception.retry_safe)
        self.assertEqual(caught.exception.mcp_result['content'][0]['text'], 'retained [redacted]')
        with self.assertRaises(McpError): client.call_tool('t', {})
        self.assertEqual(seen, ['initialize', 'notifications/initialized', 'tools/call'])

    def test_server_initiated_request_is_not_ignored(self):
        seen = []
        def transport(request, timeout):
            msg = json.loads(request.data)
            seen.append(msg['method'])
            body = [{'jsonrpc': '2.0', 'method': 'sampling/createMessage', 'id': 99},
                    {'jsonrpc': '2.0', 'id': msg['id'], 'result': INIT}]
            return Response(json.dumps(body).encode(), headers={'Mcp-Session-Id': 'opaque-id'})
        client = McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26'),
                           stateful=True, transport=transport)
        with self.assertRaises(McpError): client.request('ping')
        self.assertEqual(seen, ['initialize'])

    def test_waiting_session_lock_observes_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        def transport(request, timeout):
            msg = json.loads(request.data)
            if msg['method'] == 'initialize': return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized': return Response(b'', 202)
            entered.set()
            release.wait(3)
            return rpc(msg, {})
        client = McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26'),
                           stateful=True, transport=transport)
        token = CancellationToken()
        budget = Budget(5, cancel_token=token)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.request, 'ping')
            self.assertTrue(entered.wait(1))
            second = pool.submit(client.request, 'ping', budget=budget)
            token.request()
            try:
                with self.assertRaises(RunCancelled): second.result(timeout=1)
            finally:
                release.set()
                first.result(timeout=2)

    def test_handshake_failure_marks_business_not_started(self):
        def transport(request, timeout):
            raise HTTPError(request.full_url, 401, 'error', {}, io.BytesIO(b''))
        config = ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26',
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}})
        client = McpClient(config, stateful=True, transport=transport)
        with self.assertRaises(McpError) as caught:
            client.call_tool('t', {})
        self.assertEqual(caught.exception.remote_outcome, 'not_started')
        self.assertFalse(caught.exception.retry_safe)

    def test_initialization_rejects_equal_but_wrong_id_type(self):
        def transport(request, timeout):
            msg = json.loads(request.data)
            return Response(json.dumps({'jsonrpc': '2.0', 'id': float(msg['id']), 'result': INIT}).encode(),
                            headers={'Mcp-Session-Id': 'opaque-id'})
        client = McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26'),
                           stateful=True, transport=transport)
        with self.assertRaises(McpError): client.request('ping')

    def test_malformed_tool_result_is_not_success(self):
        def transport(request, timeout):
            msg = json.loads(request.data)
            if msg['method'] == 'initialize': return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized': return Response(b'', 202)
            return rpc(msg, {'isError': 'true'})
        config = ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26',
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}})
        client = McpClient(config, stateful=True, transport=transport)
        with self.assertRaises(McpError) as caught: client.call_tool('t', {})
        self.assertEqual(caught.exception.remote_outcome, 'response_received')
        self.assertFalse(caught.exception.retry_safe)

    def test_mixed_invalid_content_never_counts_as_success(self):
        def transport(request, timeout):
            msg = json.loads(request.data)
            if msg['method'] == 'initialize': return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized': return Response(b'', 202)
            return rpc(msg, {'content': [None], 'structuredContent': 'invalid'})
        config = ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26',
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}})
        client = McpClient(config, stateful=True, transport=transport)
        with self.assertRaises(McpError) as caught: client.call_tool('t', {})
        self.assertEqual(caught.exception.remote_outcome, 'response_received')

    def test_http_error_id_wrong_type_is_unknown(self):
        def transport(request, timeout):
            msg = json.loads(request.data)
            if msg['method'] == 'initialize': return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized': return Response(b'', 202)
            error = {'jsonrpc': '2.0', 'id': float(msg['id']), 'error': {'code': -1, 'message': 'x'}}
            raise HTTPError(request.full_url, 500, 'error', {}, io.BytesIO(json.dumps(error).encode()))
        config = ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26',
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}})
        client = McpClient(config, stateful=True, transport=transport)
        with self.assertRaises(McpError) as caught: client.call_tool('t', {})
        self.assertEqual(caught.exception.remote_outcome, 'unknown')

    def test_cancelled_before_validation_reports_not_started(self):
        token = CancellationToken()
        token.request()
        config = ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26',
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}})
        client = McpClient(config, stateful=True, transport=lambda *a, **kw: self.fail('sent'))
        with self.assertRaises(RunCancelled) as caught:
            client.call_tool('t', {}, budget=Budget(5, cancel_token=token))
        self.assertEqual(caught.exception.remote_outcome, 'not_started')
        self.assertFalse(caught.exception.retry_safe)

    def test_discovery_audit_failure_is_not_published(self):
        class Quota:
            def reserve(self, method): return method
            def finish(self, method, outcome):
                if method == 'tools/list': raise OSError('synthetic failure')
        def transport(request, timeout):
            msg = json.loads(request.data)
            if msg['method'] == 'initialize': return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized': return Response(b'', 202)
            return rpc(msg, {'tools': [{'name': 't', 'inputSchema': {'type': 'object'}}]})
        config = ServerConfig('lune', 'http://example.invalid', protocol='2025-03-26', allow=('t',))
        client = McpClient(config, stateful=True, transport=transport, quota_store=Quota())
        with self.assertRaises(McpError) as caught: client.list_tools()
        self.assertTrue(caught.exception.quota_finalization_failed)
        self.assertIsNone(client._tools)

    def test_401_never_refreshes_or_replays_bound_session(self):
        seen = []
        refresh = []
        def transport(request, timeout):
            msg = json.loads(request.data)
            seen.append(msg['method'])
            if msg['method'] == 'initialize':
                return rpc(msg, INIT, {'Mcp-Session-Id': 'opaque-id'})
            if msg['method'] == 'notifications/initialized':
                return Response(b'', 202)
            raise HTTPError(request.full_url, 401, 'error', {}, io.BytesIO(b''))
        client = McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2025-03-26'),
                           stateful=True, transport=transport)
        client.set_auth_provider(lambda force: refresh.append(force) or 'synthetic-token')
        with self.assertRaises(McpError): client.request('ping')
        with self.assertRaises(McpError): client.request('ping')
        self.assertEqual(seen, ['initialize', 'notifications/initialized', 'ping'])
        self.assertFalse(any(refresh))
