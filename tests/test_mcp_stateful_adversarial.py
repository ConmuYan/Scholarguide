"""Adversarial offline tests for bound MCP sessions."""
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from common import Budget, BudgetExceeded, CancellationToken, RunCancelled
from mcp_client import McpClient, McpError, McpToolError, ServerConfig
from scripts.verify_delivery import isolated_environment

VERSION = '2025-03-26'
SECRET = 'synthetic-session-secret-417'
INIT = {'protocolVersion': VERSION, 'capabilities': {},
        'serverInfo': {'name': 'fixture', 'version': '1'}}


class Response(io.BytesIO):
    def __init__(self, body=b'', status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers if headers is not None else {}


def rpc(msg, result, headers=None):
    return Response(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode(),
                    headers=headers)


class Quota:
    def __init__(self, fail_finish=None):
        self.events = []
        self.fail_finish = fail_finish

    def reserve(self, method):
        self.events.append(('reserve', method))
        return method

    def finish(self, method, outcome):
        self.events.append(('finish', method, outcome))
        if method == self.fail_finish:
            raise OSError('synthetic audit failure')


class Transport:
    def __init__(self, handler=None):
        self.seen = []
        self.handler = handler

    def __call__(self, request, timeout):
        msg = json.loads(request.data)
        self.seen.append(msg['method'])
        if self.handler:
            response = self.handler(request, msg)
            if response is not None:
                return response
        if msg['method'] == 'initialize':
            return rpc(msg, INIT, {'Mcp-Session-Id': SECRET})
        if msg['method'] == 'notifications/initialized':
            return Response(status=202)
        if msg['method'] == 'tools/list':
            return rpc(msg, {'tools': [{'name': 't', 'inputSchema': {'type': 'object'}}]})
        if msg['method'] == 'tools/call':
            return rpc(msg, {'content': []})
        return rpc(msg, {})


class StatefulAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = isolated_environment(Path(self.tmp.name), inherited={})
        self.environment = patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def client(self, transport, quota=None, **kwargs):
        config = ServerConfig('offline', 'http://mcp.luneresearch.com', protocol=VERSION,
                              allow=('t',), local_tool_schemas={'t': {'type': 'object'}}, **kwargs)
        return McpClient(config, stateful=True, transport=transport, quota_store=quota or Quota())

    def test_protocol_mismatch_and_unsupported_versions(self):
        for version in ('2099-01-01', None):
            with self.subTest(version=version):
                t = Transport(lambda req, msg: rpc(msg, {**INIT, 'protocolVersion': version},
                                                   {'Mcp-Session-Id': SECRET}))
                with self.assertRaises(McpError) as error:
                    self.client(t).call_tool('t')
                self.assertEqual(error.exception.kind, 'protocol')
                self.assertEqual(error.exception.remote_outcome, 'not_started')
                self.assertEqual(t.seen, ['initialize'])
        with self.assertRaises(McpError):
            McpClient(ServerConfig('offline', 'http://example.invalid', protocol='2099-01-01'),
                      stateful=True, transport=Transport())

    def test_supported_server_version_is_negotiated_and_used(self):
        observed = []
        def handler(req, msg):
            if msg['method'] == 'initialize':
                return rpc(msg, {**INIT, 'protocolVersion': '2025-11-25'}, {'Mcp-Session-Id': SECRET})
            observed.append({k.casefold(): v for k, v in req.header_items()})
        client = self.client(Transport(handler))
        client.request('ping')
        self.assertEqual([h['mcp-protocol-version'] for h in observed], ['2025-11-25', '2025-11-25'])

    def test_duplicate_and_invalid_session_headers_do_not_leak(self):
        duplicate = Message()
        duplicate.add_header('Mcp-Session-Id', SECRET)
        duplicate.add_header('mcp-session-id', 'another-secret')
        cases = [duplicate, {}, {'Mcp-Session-Id': SECRET + ' bad'},
                 {'Mcp-Session-Id': SECRET + '\n'}, {'Mcp-Session-Id': SECRET * 12},
                 {'Mcp-Session-Id': ''}]
        for headers in cases:
            with self.subTest(headers=repr(headers)):
                t = Transport(lambda req, msg: rpc(msg, INIT, headers))
                with self.assertRaises(McpError) as caught:
                    self.client(t).request('ping')
                self.assertNotIn(SECRET, repr(caught.exception.as_dict()))
                self.assertNotIn('another-secret', repr(caught.exception.as_dict()))
                self.assertEqual(t.seen, ['initialize'])

    def test_session_echo_is_redacted_from_results_and_metadata(self):
        def handler(req, msg):
            if msg['method'] == 'initialize':
                return rpc(msg, {**INIT, 'serverInfo': {'name': SECRET, 'version': '1'}},
                           {'Mcp-Session-Id': SECRET})
            if msg['method'] == 'tools/list':
                return rpc(msg, {'tools': [{'name': 't', 'description': SECRET,
                                             'inputSchema': {'type': 'object'}}]})
            if msg['method'] == 'tools/call':
                return rpc(msg, {'content': [{'type': 'text', 'text': SECRET}],
                                 'structuredContent': {'nested': SECRET}})
        client = self.client(Transport(handler))
        init = client.initialize()
        tools = client.list_tools()
        result = client.call_tool('t')
        self.assertNotIn(SECRET, repr((init, tools, result, client.usage())))

    def test_reserved_headers_are_case_insensitively_rejected(self):
        for header in ('Mcp-Session-Id', 'mcp-session-id', 'MCP-PROTOCOL-VERSION', 'mcp-protocol-version'):
            with self.subTest(header=header):
                with self.assertRaises(McpError) as caught:
                    self.client(Transport(), headers={header: SECRET})
                self.assertEqual(caught.exception.kind, 'client_error')
                self.assertNotIn(SECRET, str(caught.exception))

    def test_three_attempts_reserve_and_finish_in_order(self):
        quota, t = Quota(), Transport()
        self.client(t, quota).call_tool('t')
        self.assertEqual(quota.events, [(phase, method, *outcome)
            for method in ('initialize', 'notifications/initialized', 'tools/call')
            for phase, outcome in (('reserve', ()), ('finish', ('response_received',)))])

    def test_finalization_failure_prevents_discovery_publication(self):
        for stage, expected in [('initialize', ['initialize']),
                                ('notifications/initialized', ['initialize', 'notifications/initialized']),
                                ('tools/list', ['initialize', 'notifications/initialized', 'tools/list'])]:
            with self.subTest(stage=stage):
                quota, t = Quota(stage), Transport()
                client = self.client(t, quota)
                with self.assertRaises(McpError):
                    client.list_tools()
                self.assertIsNone(client._tools)
                self.assertEqual(t.seen, expected)
                self.assertIn(('finish', stage, 'response_received'), quota.events)

    def test_business_finalization_failure_exposes_audit_status_without_session(self):
        quota = Quota('tools/call')
        def handler(req, msg):
            if msg['method'] == 'tools/call':
                return rpc(msg, {'content': [{'type': 'text', 'text': SECRET}]})
        result = self.client(Transport(handler), quota).call_tool('t')
        self.assertTrue(result.quota_finalization_failed)
        self.assertEqual(result.audit_status, 'failed')
        self.assertFalse(result.retry_safe)
        self.assertNotIn(SECRET, repr((result, vars(result))))
        self.assertIn(('finish', 'tools/call', 'response_received'), quota.events)

    def test_failed_attempts_are_conservatively_finalized_unknown(self):
        for stage in ('initialize', 'notifications/initialized', 'tools/call'):
            with self.subTest(stage=stage):
                def handler(req, msg):
                    if msg['method'] == stage:
                        raise HTTPError(req.full_url, 503, 'synthetic', {}, io.BytesIO())
                quota, t = Quota(), Transport(handler)
                with self.assertRaises(McpError) as caught:
                    self.client(t, quota).call_tool('t')
                self.assertIn(('finish', stage, 'unknown'), quota.events)
                self.assertEqual(caught.exception.remote_outcome,
                                 'unknown' if stage == 'tools/call' else 'not_started')
                self.assertEqual(t.seen[-1], stage)

    def test_http_401_and_404_do_not_retry_or_reinitialize(self):
        for status in (401, 404):
            with self.subTest(status=status):
                def handler(req, msg):
                    if msg['method'] == 'tools/call':
                        raise HTTPError(req.full_url, status, 'synthetic', {}, io.BytesIO(SECRET.encode()))
                t = Transport(handler)
                client = self.client(t)
                refresh = []
                client.set_auth_provider(lambda force: refresh.append(force) or 'synthetic-token')
                with self.assertRaises(McpError) as caught:
                    client.call_tool('t')
                self.assertEqual(caught.exception.status, status)
                self.assertNotIn(SECRET, repr(caught.exception.as_dict()))
                with self.assertRaises(McpError):
                    client.request('ping')
                self.assertEqual(t.seen, ['initialize', 'notifications/initialized', 'tools/call'])
                self.assertFalse(any(refresh))

    def test_pre_cancel_and_exhausted_budget_use_no_quota(self):
        token = CancellationToken()
        token.request()
        for budget, exception in ((Budget(10, cancel_token=token), RunCancelled),
                                  (Budget(0), BudgetExceeded)):
            with self.subTest(exception=exception):
                quota, t = Quota(), Transport()
                with self.assertRaises(exception):
                    self.client(t, quota).call_tool('t', budget=budget)
                self.assertEqual(t.seen, [])
                self.assertEqual(quota.events, [])

    def test_cancel_while_waiting_for_lock_does_not_reserve(self):
        entered, release = threading.Event(), threading.Event()
        def handler(req, msg):
            if msg['method'] == 'ping':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('fixture timed out')
        quota, t = Quota(), Transport(handler)
        client = self.client(t, quota)
        token = CancellationToken()
        waiting = threading.Event()
        class ObservedBudget(Budget):
            checks = 0
            def check_cancelled(self):
                super().check_cancelled()
                self.checks += 1
                if self.checks >= 2:
                    waiting.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.request, 'ping')
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(client.call_tool, 't', budget=ObservedBudget(5, cancel_token=token))
                self.assertTrue(waiting.wait(2))
                token.request()
                with self.assertRaises(RunCancelled):
                    second.result(timeout=2)
            finally:
                release.set()
                first.result(timeout=3)
        self.assertNotIn(('reserve', 'tools/call'), quota.events)
        self.assertEqual(t.seen, ['initialize', 'notifications/initialized', 'ping'])

    def test_concurrent_and_repeated_initialize_only_send_once(self):
        t = Transport()
        client = self.client(t)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: client.initialize(), range(8)))
        client.initialize()
        self.assertEqual(results, [INIT] * 8)
        self.assertEqual(t.seen, ['initialize', 'notifications/initialized'])

    def test_is_error_must_be_boolean_and_true_is_tool_error(self):
        for flag, expected in [('true', McpError), (1, McpError), (None, McpError), (True, McpToolError)]:
            with self.subTest(flag=flag):
                def handler(req, msg):
                    if msg['method'] == 'tools/call':
                        return rpc(msg, {'content': [], 'isError': flag})
                with self.assertRaises(expected) as caught:
                    self.client(Transport(handler)).call_tool('t')
                self.assertEqual(caught.exception.remote_outcome, 'error' if flag is True else 'response_received')
        t = Transport(lambda req, msg: rpc(msg, {'content': [], 'isError': False})
                      if msg['method'] == 'tools/call' else None)
        self.assertIs(self.client(t).call_tool('t')['isError'], False)

    def test_sse_server_request_is_rejected(self):
        def handler(req, msg):
            if msg['method'] == 'ping':
                server = {'jsonrpc': '2.0', 'method': 'sampling/createMessage', 'id': 99, 'params': {}}
                answer = {'jsonrpc': '2.0', 'id': msg['id'], 'result': {}}
                return Response(('data: ' + json.dumps(server) + '\n\ndata: ' + json.dumps(answer) + '\n\n').encode())
        t = Transport(handler)
        with self.assertRaises(McpError) as caught:
            self.client(t).request('ping')
        self.assertEqual(caught.exception.kind, 'protocol')

    def test_auth_identity_change_blocks_business_before_quota(self):
        quota, t = Quota(), Transport()
        client = self.client(t, quota)
        tokens = iter(('same', 'same', 'different'))
        client.set_auth_provider(lambda force: next(tokens))
        with self.assertRaises(McpError) as caught:
            client.call_tool('t')
        self.assertEqual(caught.exception.kind, 'auth')
        self.assertEqual(caught.exception.remote_outcome, 'not_started')
        self.assertEqual(t.seen, ['initialize', 'notifications/initialized'])
        self.assertNotIn(('reserve', 'tools/call'), quota.events)


if __name__ == '__main__':
    unittest.main()
