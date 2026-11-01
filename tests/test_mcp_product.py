"""Product contracts for the bounded MCP/Lune integration."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import mcp_client
import mcp_lune
import mcp_plugin
from common import Budget, Config
from tests.test_mcp_client import MockMcp, _result
from tests.lune_quota_fixture import confirmed_quota


class ProductTests(unittest.TestCase):
    def setUp(self):
        mcp_plugin.reset_for_tests()
        mcp_lune.reset_for_tests()
        self.quota = confirmed_quota(self)

    def tearDown(self):
        mcp_plugin.reset_for_tests()
        mcp_lune.reset_for_tests()

    def test_html_429_preserves_status_and_closes_error_body(self):
        class ErrorBody:
            closed = False

            def read(self, size=-1):
                return b'<html>rate limited</html>'

            def close(self):
                self.closed = True

        body = ErrorBody()
        error = __import__('urllib.error', fromlist=['HTTPError']).HTTPError(
            'https://example.invalid', 429, 'busy', {}, body)
        def transport(request, timeout):
            raise error
        client = mcp_client.McpClient(mcp_client.ServerConfig('x', 'https://example.invalid'),
                                       transport=transport)
        with self.assertRaises(mcp_client.McpError) as caught:
            client.list_tools()
        self.assertEqual((caught.exception.kind, caught.exception.status), ('rate_limited', 429))
        self.assertEqual(caught.exception.raw, '')  # Untrusted body must not enter logs.
        self.assertTrue(body.closed)

    def test_sse_progress_notification_and_response_are_matched_by_id(self):
        body = (b'event: message\n'
                b'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\n\n'
                b'event: message\n'
                b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"x"}]}}\n\n')
        with MockMcp({'tools/list': {'ctype': 'text/event-stream', 'body': body}}) as mock:
            client = mcp_client.McpClient(mcp_client.ServerConfig('x', mock.url, allow_all=True))
            self.assertEqual(client.tool_names(), ['x'])

    def test_shared_client_ids_are_unique_and_alias_calls_are_whitelisted(self):
        with MockMcp({'tools/call': _result({'ok': True})}) as mock:
            client = mcp_client.McpClient(mcp_client.ServerConfig(
                'x', mock.url, allow=('search_related_papers',),
                local_tool_schemas={'search_related_papers': {
                    'type': 'object', 'properties': {'paper_id': {'type': 'string'}},
                    'required': ['paper_id'], 'additionalProperties': False}}))
            ids = []
            def call():
                client.call('related', {'paper_id': 'p'})
            threads = [threading.Thread(target=call) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            ids = [request['method'] for request in mock.requests]
            self.assertEqual(len(ids), 8)
            with self.assertRaises(mcp_client.McpError):
                client.call('arbitrary_tool', {})

    def test_oauth_refresh_is_locked_and_retries_one_401(self):
        class Handler(BaseHTTPRequestHandler):
            calls = []
            def log_message(self, *args):
                pass
            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                message = json.loads(self.rfile.read(length))
                self.calls.append(self.headers.get('Authorization'))
                status = 401 if len(self.calls) == 1 else 200
                body = {'jsonrpc': '2.0', 'id': message['id'], 'result': {}}
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        calls = []
        def bearer(force):
            calls.append(force)
            return 'fresh-token' if force else 'cached-token'
        client = mcp_client.McpClient(mcp_client.ServerConfig('x',
            f'http://127.0.0.1:{server.server_address[1]}'))
        client.set_auth_provider(bearer)
        client.request('ping')
        self.assertEqual(calls, [False, True])
        self.assertEqual(Handler.calls, ['Bearer cached-token', 'Bearer fresh-token'])

    def test_provider_passes_academic_budget_and_paginates_native_filters(self):
        pages = [
            {'papers': [{'id': 'old', 'title': 'Old', 'abstract': 'a', 'year': 2019,
                         'url': 'https://example.invalid/old'}], 'has_more': True},
            {'papers': [{'id': 'new', 'title': 'New', 'abstract': 'b', 'year': 2024,
                         'url': 'https://example.invalid/new'}], 'has_more': False},
        ]
        calls = []
        def action(message):
            calls.append(message)
            return {'status': 200, 'body': {'jsonrpc': '2.0', 'id': message['id'],
                                            'result': {'structuredContent': pages[len(calls) - 1]}}}
        with MockMcp({'tools/call': action}) as mock:
            client = mcp_client.McpClient(mcp_client.ServerConfig(
                'lune', mock.url, allow=('search_papers',)), quota_store=self.quota)
            with tempfile.TemporaryDirectory() as directory:
                provider = mcp_lune.LuneProvider(client=client,
                    ledger_path=Path(directory) / 'ledger.json', max_requests=3)
                budget = Budget(seconds=30)
                papers = provider.search('q', limit=1,
                    filters={'venues': ['NeurIPS'], 'year_min': 2020, 'year_max': 2024},
                    budget=budget)
                self.assertEqual([paper.id for paper in papers], ['lune:new'])
                self.assertEqual(budget.search_http_calls, 2)
                self.assertEqual(calls[0]['params']['arguments']['year_min'], 2020)
                self.assertEqual(calls[0]['params']['arguments']['year_max'], 2024)
                self.assertEqual(calls[0]['params']['arguments']['venues'], ['NeurIPS'])
                self.assertEqual(calls[1]['params']['arguments']['offset'], 1)

    def test_local_ready_check_does_not_open_network(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'mcp.json'
            config.write_text(json.dumps({'servers': {'lune': {
                'url': 'https://example.invalid/mcp',
                'headers': {'Authorization': 'Bearer local'}}}}), encoding='utf-8')
            provider = mcp_lune.LuneProvider(config_path=config,
                ledger_path=Path(directory) / 'ledger.json', max_requests=0)
            with patch('mcp_lune.urlopen', side_effect=AssertionError('network')):
                self.assertTrue(provider.ready())


if __name__ == '__main__':
    unittest.main()
