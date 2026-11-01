import io
import json
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from common import Budget, RunCancelled
from mcp_client import McpClient, McpError, ServerConfig
from lune_quota import LuneQuotaStore


def response(request, result):
    message = json.loads(request.data)
    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode())


class DiscoveryConcurrencyTests(unittest.TestCase):
    def config(self):
        return ServerConfig('offline', 'https://example.invalid', allow=('a', 'b'),
                            allow_prompts=('a', 'b'),
                            local_tool_schemas={'a': {'type': 'object'}},
                            local_prompt_schemas={'a': {'type': 'object'}})

    def test_failed_refresh_revokes_even_local_schema_for_both_types(self):
        for key in ('tools', 'prompts'):
            with self.subTest(key=key):
                count = []
                def transport(request, timeout):
                    count.append(1)
                    if len(count) == 2:
                        raise TimeoutError()
                    return response(request, {key: [{'name': 'a', 'inputSchema': True}]})
                client = McpClient(self.config(), transport=transport)
                listing = client.list_tools if key == 'tools' else client.list_prompts
                invoke = client.call_tool if key == 'tools' else client.get_prompt
                listing()
                with self.assertRaises(McpError):
                    listing(refresh=True)
                with self.assertRaises(McpError):
                    invoke('a', {})
                self.assertEqual(len(count), 2)

    def test_raw_discovery_failure_invalidates_existing_snapshot(self):
        for key in ('tools', 'prompts'):
            with self.subTest(key=key):
                calls = []
                def transport(request, timeout):
                    calls.append(1)
                    if len(calls) == 2:
                        raise TimeoutError('synthetic')
                    return response(request, {key: [{'name': 'a', 'inputSchema': True}]})
                client = McpClient(self.config(), transport=transport)
                listing = client.list_tools if key == 'tools' else client.list_prompts
                invoke = client.call_tool if key == 'tools' else client.get_prompt
                listing()
                with self.assertRaises(McpError):
                    client.request(key + '/list')
                with self.assertRaises(McpError):
                    invoke('a', {})
                self.assertEqual(len(calls), 2)

    def test_raw_complete_discovery_replaces_prior_snapshot(self):
        for key in ('tools', 'prompts'):
            with self.subTest(key=key):
                pages = iter([{key: [{'name': 'a'}]}, {key: [{'name': 'b'}]}])
                client = McpClient(self.config(), transport=lambda request, timeout: response(request, next(pages)))
                listing = client.list_tools if key == 'tools' else client.list_prompts
                listing()
                result = client.request(key + '/list')
                self.assertEqual([item['name'] for item in result[key]], ['b'])
                self.assertEqual([item['name'] for item in listing()], ['b'])
                with self.assertRaises(McpError):
                    (client.call_tool if key == 'tools' else client.get_prompt)('a', {})

    def test_reversed_refresh_cannot_publish_older_result(self):
        for key in ('tools', 'prompts'):
            with self.subTest(key=key):
                entered, release = threading.Event(), threading.Event()
                def transport(request, timeout):
                    message = json.loads(request.data)
                    if message['id'] == 1:
                        entered.set()
                        release.wait(5)
                        return response(request, {key: [{'name': 'a'}]})
                    return response(request, {key: [{'name': 'b'}]})
                client = McpClient(self.config(), transport=transport)
                listing = client.list_tools if key == 'tools' else client.list_prompts
                errors = []
                def first():
                    try:
                        listing(refresh=True)
                    except McpError as exc:
                        errors.append(exc)
                worker = threading.Thread(target=first)
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(listing(refresh=True)[0]['name'], 'b')
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual([x['name'] for x in listing()], ['b'])
                self.assertEqual(len(errors), 1)

    def test_refresh_in_before_send_prevents_stale_dispatch(self):
        calls = []
        def transport(request, timeout):
            message = json.loads(request.data)
            calls.append(message['method'])
            return response(request, {'tools': [{'name': 'a', 'inputSchema': True}]} if message['method'] == 'tools/list' else {})
        client = McpClient(self.config(), transport=transport)
        client.list_tools()
        with patch('mcp_client.validate_remote_schema'):
            with self.assertRaises(McpError):
                client.call_tool('a', {}, _before_send=lambda: client.list_tools(refresh=True))
        self.assertEqual(calls, ['tools/list', 'tools/list'])

    def test_prompt_refresh_during_auth_blocks_dispatch(self):
        calls = []
        def transport(request, timeout):
            msg = json.loads(request.data)
            calls.append(msg['method'])
            return response(request, {'prompts': [{'name': 'a'}]} if msg['method'] == 'prompts/list' else {})
        client = McpClient(self.config(), transport=transport)
        client.list_prompts()
        def provider(force):
            client.set_auth_provider(None)
            client.list_prompts(refresh=True)
            return 'synthetic'
        client.set_auth_provider(provider)
        with patch('mcp_client.validate_remote_schema'):
            with self.assertRaises(McpError):
                client.get_prompt('a', {})
        self.assertEqual(calls, ['prompts/list', 'prompts/list'])

    def test_cancelled_new_refresh_does_not_allow_older_to_publish(self):
        entered, release = threading.Event(), threading.Event()
        def transport(request, timeout):
            entered.set()
            release.wait(5)
            return response(request, {'tools': [{'name': 'a'}]})
        client = McpClient(self.config(), transport=transport)
        errors = []
        def first():
            try:
                client.list_tools(refresh=True)
            except McpError as exc:
                errors.append(exc)
        worker = threading.Thread(target=first)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            budget = Budget(5)
            budget.cancel_token.request()
            with self.assertRaises(RunCancelled):
                client.list_tools(refresh=True, budget=budget)
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(len(errors), 1)
        with self.assertRaises(McpError):
            client.call_tool('a', {})

    def test_identity_changes_never_send_cursor(self):
        for retry in (False, True):
            with self.subTest(retry=retry):
                calls = []
                def transport(request, timeout):
                    msg = json.loads(request.data)
                    calls.append((msg, request.get_header('Authorization')))
                    if len(calls) == 1:
                        return response(request, {'tools': [], 'nextCursor': 'opaque-secret'})
                    if retry and len(calls) == 2:
                        raise HTTPError(request.full_url, 401, '', {}, io.BytesIO())
                    return response(request, {'tools': []})
                client = McpClient(self.config(), transport=transport)
                client.set_auth_provider(lambda force: 'second' if force or (not retry and calls) else 'first')
                with self.assertRaises(McpError):
                    client.list_tools(paginate=True)
                self.assertEqual(len(calls), 2 if retry else 1)
                self.assertNotIn('second', repr(calls[0]))

    def test_changed_headers_or_endpoint_cannot_receive_cursor(self):
        for changed in ('header', 'endpoint', 'provider'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                quota = LuneQuotaStore(Path(directory) / 'quota.sqlite3')
                self.addCleanup(quota.close)
                quota.confirm(5, time.time() + 60)
                config = ServerConfig('lune', 'https://example.invalid', headers={'X-Test': 'one'})
                calls = []
                client = McpClient(config, quota_store=quota)
                def first(request, timeout):
                    calls.append(1)
                    if changed == 'header':
                        config.headers['X-Test'] = 'two'
                    elif changed == 'endpoint':
                        object.__setattr__(config, 'url', 'https://different.invalid')
                    else:
                        client.set_auth_provider(lambda force: None)
                    return response(request, {'tools': [], 'nextCursor': 'private'})
                client._transport = first
                with self.assertRaises(McpError):
                    client.list_tools(paginate=True)
                self.assertEqual(len(calls), 1)
                self.assertEqual(quota.status()['remaining_from_snapshot'], 3)
                quota.close()

    def test_cursor_echo_is_never_published(self):
        for key in ('tools', 'prompts'):
            with self.subTest(key=key):
                calls = []
                def transport(request, timeout):
                    calls.append(1)
                    if len(calls) == 1:
                        return response(request, {key: [{'name': 'a', 'description': 'opaque-secret'}],
                                                  'nextCursor': 'opaque-secret'})
                    return response(request, {key: []})
                client = McpClient(self.config(), transport=transport)
                with self.assertRaises(McpError):
                    (client.list_tools if key == 'tools' else client.list_prompts)(paginate=True)

    def test_cursor_echo_in_later_schema_is_not_published(self):
        pages = [ {'tools': [], 'nextCursor': 'cursor-opaque-value'},
                  {'tools': [{'name': 'a', 'inputSchema': {'type': 'object',
                             'properties': {'q': {'description': 'cursor-opaque-value'}}}}]} ]
        def transport(request, timeout):
            return response(request, pages.pop(0))
        client = McpClient(self.config(), transport=transport)
        with self.assertRaises(McpError) as caught:
            client.list_tools(paginate=True)
        self.assertNotIn('cursor-opaque-value', str(caught.exception))
        self.assertEqual(client._tool_schemas, {})

    def test_retry_identity_change_consumes_admission_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            quota = LuneQuotaStore(Path(directory) / 'quota.sqlite3')
            self.addCleanup(quota.close)
            quota.confirm(5, time.time() + 60)
            calls = []
            def transport(request, timeout):
                calls.append(1)
                if len(calls) == 1:
                    return response(request, {'tools': [], 'nextCursor': 'cursor-private'})
                raise HTTPError(request.full_url, 401, '', {}, io.BytesIO())
            client = McpClient(ServerConfig('lune', 'https://example.invalid'),
                               transport=transport, quota_store=quota)
            client.set_auth_provider(lambda force: 'new' if force else 'old')
            with self.assertRaises(McpError) as caught:
                client.list_tools(paginate=True)
            self.assertNotIn('new', str(caught.exception))
            self.assertEqual(len(calls), 2)
            self.assertEqual(client.http_attempts, 2)
            self.assertEqual(quota.status()['remaining_from_snapshot'], 2)
            quota.close()

    def test_raw_list_respects_entry_limit(self):
        client = McpClient(self.config(), transport=lambda req, timeout: response(req, {'tools': [{'name': 'a'}, {'name': 'b'}]}))
        with patch('mcp_client.MAX_DISCOVERY_ENTRIES', 1), self.assertRaises(McpError):
            client.request('tools/list')

    def test_raw_bytes_limit_before_redaction(self):
        secret = 'S' * 300
        config = ServerConfig('offline', 'https://example.invalid', allow_all=True,
                              headers={'Authorization': 'Bearer ' + secret})
        client = McpClient(config, transport=lambda req, timeout: response(
            req, {'tools': [{'name': 'a', 'description': secret}]}))
        with patch('mcp_client.MAX_DISCOVERY_BYTES', 200), self.assertRaises(McpError) as caught:
            client.request('tools/list')
        self.assertNotIn(secret, str(caught.exception))

    def test_remote_true_cannot_grant_unreviewed_arguments(self):
        calls = []
        def transport(request, timeout):
            msg = json.loads(request.data)
            calls.append(msg['method'])
            return response(request, {'tools': [{'name': 'b', 'inputSchema': True}]} if msg['method'] == 'tools/list' else {})
        client = McpClient(self.config(), transport=transport)
        client.list_tools()
        with self.assertRaises(McpError):
            client.call_tool('b', {'arbitrary': 'payload'})
        self.assertEqual(calls, ['tools/list'])
