import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Budget, BudgetExceeded, GuideError, RunCancelled
from execution_events import bind_events
from lune_quota import LuneQuotaStore
import mcp_client
from mcp_client import McpClient, McpError, ServerConfig


class Pages:
    def __init__(self, pages):
        self.pages = list(pages)
        self.messages = []

    def __call__(self, request, timeout):
        message = json.loads(request.data)
        self.messages.append(message)
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                      'result': page}).encode())


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.quota = LuneQuotaStore(Path(self.tmp.name) / 'quota.sqlite3')
        self.addCleanup(self.quota.close)
        self.quota.confirm(50, time.time() + 300)

    def client(self, pages, budget=None):
        transport = Pages(pages)
        config = ServerConfig('lune', 'https://example.invalid', allow=('a', 'b'),
                              allow_prompts=('a', 'b'))
        return McpClient(config, transport=transport, quota_store=self.quota, budget=budget), transport

    def test_paginated_discovery_filters_and_accounts_each_page(self):
        for key in ('tools', 'prompts'):
            with self.subTest(key=key):
                item = lambda name: ({'name': name, 'inputSchema': True} if key == 'tools'
                                     else {'name': name, 'arguments': []})
                client, transport = self.client([{key: [item('a'), item('denied')], 'nextCursor': 'CURSOR_SECRET'},
                                                 {key: [item('b')]}], Budget(15))
                events = []
                before = self.quota.status()['remaining_from_snapshot']
                with bind_events(lambda action, payload: events.append((action, payload))):
                    result = (client.list_tools if key == 'tools' else client.list_prompts)(paginate=True)
                self.assertEqual([x['name'] for x in result], ['a', 'b'])
                self.assertEqual([x['params'] for x in transport.messages], [{}, {'cursor': 'CURSOR_SECRET'}])
                self.assertEqual(client.budget.search_http_calls, 2)
                self.assertEqual(before - self.quota.status()['remaining_from_snapshot'], 2)
                self.assertNotIn('CURSOR_SECRET', repr(events))
                self.assertNotIn('inputSchema', repr(events))
                self.assertEqual((client._tool_schemas if key == 'tools' else client._prompt_schemas).keys(), {'a', 'b'})

    def test_default_rejects_incomplete_and_invalidates_prior_snapshot(self):
        for key in ('tools', 'prompts'):
            with self.subTest(key=key):
                item = lambda name: {'name': name, **({'inputSchema': True} if key == 'tools' else {})}
                client, transport = self.client([{key: [item('a')]},
                                                 {key: [item('b')], 'nextCursor': 'hidden'}])
                listing = client.list_tools if key == 'tools' else client.list_prompts
                self.assertEqual(listing()[0]['name'], 'a')
                with self.assertRaises(McpError):
                    listing(refresh=True)
                with self.assertRaises(McpError):
                    listing()
                with self.assertRaises(McpError):
                    (client.call_tool if key == 'tools' else client.get_prompt)('a', {})
                with self.assertRaises(McpError):
                    (client.call_tool if key == 'tools' else client.get_prompt)('b', {})
                self.assertEqual(len(transport.messages), 2)

    def test_failures_do_not_publish_partial(self):
        for key in ('tools', 'prompts'):
            for second in (TimeoutError(), RunCancelled(), BudgetExceeded('deadline'),
                           {key: [], 'nextCursor': 'same'}, {key: [], 'nextCursor': None}):
                with self.subTest(key=key, second=type(second).__name__):
                    client, transport = self.client([{key: [{'name': 'a'}], 'nextCursor': 'same'}, second])
                    listing = client.list_tools if key == 'tools' else client.list_prompts
                    with self.assertRaises((McpError, RunCancelled, BudgetExceeded)):
                        listing(paginate=True)
                    self.assertIsNone(client._tools if key == 'tools' else client._prompts)
                    self.assertEqual(client._tool_schemas if key == 'tools' else client._prompt_schemas, {})
                    with self.assertRaises(McpError):
                        (client.call_tool if key == 'tools' else client.get_prompt)('a', {})
                    self.assertEqual(len(transport.messages), 2)

    def test_limits_and_invalid_cursors(self):
        for page in ({'tools': [], 'nextCursor': ''}, {'tools': [], 'nextCursor': 42},
                     {'tools': [{'name': 'a'}, {'name': 'b'}]},
                     {'tools': [{'name': 'a', 'description': 'x' * 500}]}):
            client, _ = self.client([page])
            with patch.object(mcp_client, 'MAX_DISCOVERY_ENTRIES', 1, create=True), \
                 patch.object(mcp_client, 'MAX_DISCOVERY_BYTES', 200, create=True), \
                 self.assertRaises(McpError):
                client.list_tools(paginate=True)
        client, transport = self.client([{'tools': [], 'nextCursor': 'next'}])
        with patch.object(mcp_client, 'MAX_DISCOVERY_PAGES', 1, create=True), self.assertRaises(McpError):
            client.list_tools(paginate=True)
        self.assertEqual(len(transport.messages), 1)

    def test_cancellation_between_pages_stops_before_second_reservation(self):
        budget = Budget(15)
        client, transport = self.client([{'tools': [{'name': 'a'}], 'nextCursor': 'secret'}], budget)
        original = client._transport
        def cancel_after_first(request, timeout):
            response = original(request, timeout)
            budget.cancel_token.request()
            return response
        client._transport = cancel_after_first
        before = self.quota.status()['remaining_from_snapshot']
        with self.assertRaises(RunCancelled):
            client.list_tools(paginate=True)
        self.assertIsNone(client._tools)
        self.assertEqual(len(transport.messages), 1)
        self.assertEqual(before - self.quota.status()['remaining_from_snapshot'], 1)

    def test_opaque_cursor_not_redacted_and_not_exposed_by_raw_request(self):
        secret = 'SYNTHETIC_TOKEN'
        transport = Pages([{'tools': [], 'nextCursor': secret}, {'tools': []}])
        config = ServerConfig('offline', 'https://example.invalid',
                              headers={'Authorization': 'Bearer ' + secret}, allow_all=True)
        client = McpClient(config, transport=transport)
        events = []
        with bind_events(lambda action, payload: events.append((action, payload))):
            self.assertEqual(client.list_tools(paginate=True), [])
        self.assertEqual(transport.messages[1]['params']['cursor'], secret)
        self.assertNotIn(secret, repr(events))
        client = McpClient(config, transport=Pages([{'tools': [], 'nextCursor': secret}]))
        with self.assertRaises(McpError) as caught:
            client.request('tools/list')
        self.assertNotIn(secret, str(caught.exception))

    def test_quota_exhaustion_on_second_page_invalidates_snapshot(self):
        self.quota.close()
        self.quota = LuneQuotaStore(Path(self.tmp.name) / 'limited.sqlite3')
        self.addCleanup(self.quota.close)
        self.quota.confirm(2, time.time() + 300)
        client, transport = self.client([{'tools': [{'name': 'a', 'inputSchema': True}]},
                                         {'tools': [{'name': 'b', 'inputSchema': True}], 'nextCursor': 'private'}])
        client.list_tools()
        with self.assertRaises(GuideError):
            client.list_tools(refresh=True, paginate=True)
        with self.assertRaises(McpError):
            client.list_tools()
        with self.assertRaises(McpError):
            client.call_tool('a', {})
        with self.assertRaises(McpError):
            client.call_tool('b', {})
        self.assertEqual(len(transport.messages), 2)
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 0)

    def test_discovery_without_caller_budget_uses_one_bounded_budget(self):
        client, transport = self.client([{'tools': [], 'nextCursor': 'cursor'}, {'tools': []}])
        budget = Budget(.05)
        original = transport.__call__
        def slow(request, timeout):
            response = original(request, timeout)
            time.sleep(.08)
            return response
        client._transport = slow
        with patch.object(mcp_client, 'Budget', return_value=budget, create=True) as factory:
            with self.assertRaises(BudgetExceeded):
                client.list_tools(paginate=True)
        factory.assert_called_once()
        self.assertEqual(len(transport.messages), 1)
        self.assertIsNone(client._tools)

    def test_explicit_discovery_budget_overrides_client_budget(self):
        expired = Budget(.001)
        time.sleep(.01)
        client, transport = self.client([{'tools': []}], budget=expired)
        self.assertEqual(client.list_tools(budget=Budget(2)), [])
        self.assertEqual(len(transport.messages), 1)
        self.assertIs(client.budget, expired)

    def test_snapshot_stays_complete_during_refresh(self):
        for key in ('tools', 'prompts'):
            with self.subTest(key=key):
                client, transport = self.client([{key: [{'name': 'a'}]},
                                                 {key: [{'name': 'b'}], 'nextCursor': 'private'},
                                                 {key: [{'name': 'a'}]}])
                listing = client.list_tools if key == 'tools' else client.list_prompts
                listing()
                entered, release = threading.Event(), threading.Event()
                original = client._transport
                def pause(request, timeout):
                    if len(transport.messages) == 2:
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError('test synchronization failed')
                    return original(request, timeout)
                client._transport = pause
                errors = []
                def refresh():
                    try:
                        listing(refresh=True, paginate=True)
                    except BaseException as exc:
                        errors.append(exc)
                worker = threading.Thread(target=refresh)
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaises(McpError):
                        listing()
                    with self.assertRaises(McpError):
                        (client.call_tool if key == 'tools' else client.get_prompt)('a', {})
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual([x['name'] for x in listing()], ['b', 'a'])

    def test_raw_cursor_forbidden_and_boolean_schema_preserved(self):
        client, transport = self.client([{'tools': [{'name': 'a', 'inputSchema': False}]}])
        for method in ('tools/list', 'prompts/list'):
            with self.assertRaises(McpError):
                client.request(method, {'cursor': 'hidden'})
        self.assertEqual(transport.messages, [])
        client.list_tools()
        client._local_tool_schemas['a'] = True
        self.assertIs(client._tool_schemas['a'], False)
        with patch.object(mcp_client, 'validate_remote_schema', side_effect=GuideError('blocked')) as validator:
            with self.assertRaises(GuideError):
                client.call_tool('a', {})
            self.assertIs(validator.call_args.args[1], False)


if __name__ == '__main__':
    unittest.main()
