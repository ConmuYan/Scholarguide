"""MCP client tests against a local mock server (proves it is not Lune-specific)."""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from common import BudgetExceeded, RunCancelled
from tool_governance import ToolGovernanceError

import mcp_client
from mcp_client import McpClient, McpError, ServerConfig, load_servers, structured


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        message = json.loads(self.rfile.read(length) or b'{}')
        server = self.server
        server.requests.append({'method': message.get('method'),
                                'params': message.get('params'),
                                'headers': {k.lower(): v for k, v in self.headers.items()}})
        action = server.script.get(message.get('method'))
        if action is None:
            action = {'status': 200, 'body': {'jsonrpc': '2.0', 'id': message.get('id'),
                                              'result': {}}}
        if callable(action):
            action = action(message) or {}
        if action.get('sleep'):
            time.sleep(action['sleep'])
        body = action.get('body', {})
        raw = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        self.send_response(int(action.get('status', 200)))
        self.send_header('Content-Type', action.get('ctype', 'application/json'))
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class MockMcp:
    def __init__(self, script=None):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.httpd.script = script or {}
        self.httpd.requests = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.httpd.server_address[1]}/mcp'

    @property
    def requests(self):
        return self.httpd.requests

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _result(payload):
    def answer(message):
        return {'status': 200, 'body': {'jsonrpc': '2.0', 'id': message.get('id'),
                                        'result': payload}}
    return answer


class ListAndCallTests(unittest.TestCase):
    def test_tools_list_and_call(self):
        script = {
            'tools/list': _result({'tools': [{'name': 'search_papers', 'description': 'Search',
                                              'inputSchema': {'type': 'object'}},
                                             {'name': 'list_conferences', 'description': 'Venues'},
                                             {'name': '', 'description': 'nameless, dropped'}]}),
            'tools/call': _result({'structuredContent': {'papers': [{'id': 'p1'}]},
                                   'content': [{'type': 'text', 'text': '{"papers":[{"id":"p1"}]}'}]}),
        }
        with MockMcp(script) as mock:
            client = McpClient(ServerConfig('mock', mock.url, timeout=10, allow_all=True,
                local_tool_schemas={'search_papers': {'type': 'object',
                    'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer'}},
                    'required': ['query'], 'additionalProperties': False}}))
            self.assertEqual(client.tool_names(), ['search_papers', 'list_conferences'])
            result = client.call_tool('search_papers', {'query': 'rag', 'limit': 3})
            self.assertEqual(structured(result), {'papers': [{'id': 'p1'}]})
            self.assertEqual(client.usage()['requests'], 2)   # tools/list + tools/call
            self.assertEqual(client.usage()['by_tool'], {'search_papers': 1})
            sent = mock.requests[-1]['params']
            self.assertEqual(sent, {'name': 'search_papers',
                                    'arguments': {'query': 'rag', 'limit': 3}})
            self.assertEqual(mock.requests[-1]['headers']['mcp-protocol-version'],
                             mcp_client.PROTOCOL_VERSION)

    def test_sse_wrapped_response_is_accepted(self):
        body = (b'event: message\n'
                b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"x"}]}}\n\n')
        with MockMcp({'tools/list': {'status': 200, 'ctype': 'text/event-stream', 'body': body}}) as mock:
            client = McpClient(ServerConfig('mock', mock.url, timeout=10, allow_all=True))
            self.assertEqual(client.tool_names(), ['x'])

    def test_tool_error_flag_is_raised(self):
        script = {'tools/call': _result({'isError': True,
                                         'content': [{'type': 'text', 'text': 'quota exhausted'}]})}
        with MockMcp(script) as mock:
            client = McpClient(ServerConfig('mock', mock.url, timeout=10,
                               allow=('search_papers',),
                               local_tool_schemas={'search_papers': {'type': 'object'}}))
            with self.assertRaises(McpError) as caught:
                client.call_tool('search_papers', {})
            self.assertEqual(caught.exception.kind, 'protocol')
            self.assertIsInstance(caught.exception, mcp_client.McpToolError)
            self.assertIn('tool reported an error', str(caught.exception))

    def test_allow_list_blocks_before_any_request(self):
        with MockMcp({'tools/list': _result({'tools': [{'name': 'search_papers'},
                                                       {'name': 'get_paper_fulltext'}]})}) as mock:
            client = McpClient(ServerConfig('mock', mock.url, allow=('search_papers',), timeout=10))
            self.assertEqual(client.tool_names(), ['search_papers'])
            before = len(mock.requests)          # tools/list already happened once
            with self.assertRaises(McpError) as caught:
                client.call_tool('get_paper_fulltext', {'paper': 'x'})
            self.assertEqual(caught.exception.kind, 'client_error')
            self.assertEqual(len(mock.requests), before)   # blocked before any HTTP call


class RawProtocolGovernanceTests(unittest.TestCase):
    def _client(self):
        client = McpClient(ServerConfig('offline', 'http://invalid.example/mcp',
                                        allow=('allowed',), allow_prompts=('permitted',)))
        client._post = Mock(return_value={})
        return client

    def test_auth_provider_uses_explicit_request_budget(self):
        from common import Budget
        with MockMcp({'ping': _result({})}) as mock:
            client = McpClient(ServerConfig('mock', mock.url, timeout=10))
            client.set_budget(Budget(3))
            explicit, seen = Budget(3), []
            client.set_auth_provider(lambda force, *, budget, rejected_token: seen.append(budget) or 'synthetic-token',
                                     budgeted=True)
            client.request('ping', {}, budget=explicit)
            self.assertEqual(seen, [explicit])

    def test_refresh_receives_this_request_rejected_token(self):
        from common import Budget
        attempts = []
        def action(message):
            attempts.append(1)
            return ({'status': 401, 'body': {}} if len(attempts) == 1 else
                    _result({})(message))
        with MockMcp({'ping': action}) as mock:
            client = McpClient(ServerConfig('mock', mock.url, timeout=10))
            seen = []
            def auth(force, *, budget, rejected_token):
                seen.append((force, rejected_token))
                return 'synthetic-new' if force else 'synthetic-old'
            client.set_auth_provider(auth, budgeted=True)
            client.request('ping', {}, budget=Budget(3))
        self.assertEqual(seen, [(False, None), (True, 'synthetic-old')])

    def test_proxy_auth_failure_does_not_refresh_oauth_or_retry(self):
        with MockMcp({'ping': {'status': 407, 'body': {}}}) as mock:
            client = McpClient(ServerConfig('mock', mock.url, timeout=10))
            seen = []
            client.set_auth_provider(lambda force: seen.append(force) or 'synthetic-token')
            with self.assertRaises(McpError) as caught:
                client.request('ping', {})
            self.assertEqual(caught.exception.kind, 'auth')
            self.assertEqual(caught.exception.status, 407)
            self.assertEqual(seen, [False])
            self.assertEqual(len(mock.requests), 1)

    def test_raw_protocol_rejects_unexpected_arguments_before_transport(self):
        client = self._client()
        for method in ('ping', 'tools/list', 'prompts/list', 'initialize'):
            with self.subTest(method=method), self.assertRaises(McpError):
                client.request(method, {'unexpected': 'SYNTHETIC_SECRET'})
        client._post.assert_not_called()

    def test_protocol_operation_is_audited_without_input_values(self):
        from execution_events import bind_events
        client = self._client(); events = []
        with bind_events(lambda action, payload: events.append((action, payload))):
            client.request('ping', {})
        self.assertEqual([action for action, _ in events], ['tool_start', 'tool_end'])
        self.assertEqual(events[0][1]['tool'], 'mcp.protocol.ping')

    def test_raw_discovery_filters_disallowed_metadata(self):
        client = self._client()
        client._post.side_effect = lambda message, **kw: {
            'tools/list': {'tools': [{'name': 'allowed'}, {'name': 'denied'}]},
            'prompts/list': {'prompts': [{'name': 'permitted'}, {'name': 'denied'}]},
        }[message['method']]
        self.assertEqual([t['name'] for t in client.request('tools/list')['tools']], ['allowed'])
        self.assertEqual([p['name'] for p in client.request('prompts/list')['prompts']], ['permitted'])

    def test_unknown_alias_does_not_reflect_input(self):
        client = self._client()
        with self.assertRaises(McpError) as caught:
            client.call('SYNTHETIC_SECRET')
        self.assertNotIn('SYNTHETIC_SECRET', str(caught.exception))
        client._post.assert_not_called()


class IsolatedSchemaRoutingTests(unittest.TestCase):
    """Discovery and invocation use an in-memory transport; no network is used."""

    def _client(self, *, local=True):
        tool_schema = {'type': 'object'}
        prompt_schema = {'type': 'object'}
        config = ServerConfig('offline', 'http://invalid.example/mcp', allow_all=True,
                              local_tool_schemas={'list_conferences': tool_schema} if local else {},
                              local_prompt_schemas={'p': prompt_schema} if local else {})
        client = McpClient(config)
        post = Mock(side_effect=lambda message, **kw: {
            'tools/list': {'tools': [{'name': 'list_conferences', 'inputSchema': {'type': 'object'}}]},
            'prompts/list': {'prompts': [{'name': 'p', 'arguments': [{'name': 'q'}]}]},
            'tools/call': {}, 'prompts/get': {},
        }[message['method']])
        client._post = post
        return client, post

    def test_discovered_schemas_use_same_snapshot_and_active_budget_for_all_entries(self):
        entries = ('request_tool', 'call_tool', 'alias', 'request_prompt', 'get_prompt')
        for entry in entries:
            with self.subTest(entry=entry):
                client, post = self._client()
                is_prompt = 'prompt' in entry
                (client.list_prompts if is_prompt else client.list_tools)()
                original = {'q': 'before'} if is_prompt else {'q': ['before']}
                explicit = Mock()
                client.budget = Mock()
                local_calls = []
                real_validate = mcp_client.validate_arguments

                def local_validate(value, schema=None):
                    if schema is not None:
                        local_calls.append(value)
                    return real_validate(value, schema)

                def remote_validate(value, schema, *, budget):
                    self.assertIs(budget, explicit)
                    self.assertIs(value, local_calls[0])
                    self.assertIsNot(value, original)
                    original['q'] = 'changed'
                    self.assertEqual(value['q'], 'before' if is_prompt else ['before'])

                with patch.object(mcp_client, 'validate_arguments', side_effect=local_validate), \
                     patch.object(mcp_client, 'validate_remote_schema', side_effect=remote_validate) as remote:
                    if entry == 'request_tool':
                        client.request('tools/call', {'name': 'list_conferences', 'arguments': original}, budget=explicit)
                    elif entry == 'call_tool':
                        client.call_tool('list_conferences', original, budget=explicit)
                    elif entry == 'alias':
                        client.call('listconferences', original, budget=explicit)
                    elif entry == 'request_prompt':
                        client.request('prompts/get', {'name': 'p', 'arguments': original}, budget=explicit)
                    else:
                        client.get_prompt('p', original, budget=explicit)
                remote.assert_called_once()
                self.assertIs(post.call_args.args[0]['params']['arguments'], local_calls[0])
                self.assertIs(post.call_args.kwargs['budget'], explicit)

    def test_local_only_does_not_use_remote_validator_and_budget_defaults_to_client(self):
        client, _ = self._client()
        with patch.object(mcp_client, 'validate_remote_schema') as remote:
            client.call_tool('list_conferences', {})
            client.get_prompt('p', {})
            remote.assert_not_called()
        client.list_tools()
        client.list_prompts()
        client.budget = Mock()
        with patch.object(mcp_client, 'validate_remote_schema') as remote:
            client.call_tool('list_conferences', {})
            client.get_prompt('p', {})
            self.assertEqual(remote.call_count, 2)
            for call in remote.call_args_list:
                self.assertIs(call.kwargs['budget'], client.budget)

    def test_transport_uses_the_same_budget_selected_before_validation(self):
        from common import Budget
        client, post = self._client()
        client.list_tools(); post.reset_mock()
        original, replacement = Budget(5), Budget(5)
        client.set_budget(original)
        def validate(*args, **kwargs):
            client.set_budget(replacement)
        with patch.object(mcp_client, 'validate_remote_schema', side_effect=validate):
            client.call_tool('list_conferences', {})
        self.assertIs(post.call_args.kwargs['budget'], original)

    def test_remote_only_discovery_does_not_authorize_tools_or_prompts(self):
        client, post = self._client(local=False)
        client.list_tools()
        client.list_prompts()
        post.reset_mock()
        with patch.object(mcp_client, 'validate_remote_schema') as remote:
            for invoke in (lambda: client.call_tool('list_conferences', {}),
                           lambda: client.get_prompt('p', {})):
                with self.assertRaises(McpError):
                    invoke()
            remote.assert_not_called()
            post.assert_not_called()

    def test_remote_errors_budget_and_cancellation_stop_before_http(self):
        for is_prompt in (False, True):
            for error in (ToolGovernanceError('invalid'), BudgetExceeded('budget'), RunCancelled()):
                with self.subTest(prompt=is_prompt, error=type(error).__name__):
                    client, post = self._client()
                    (client.list_prompts if is_prompt else client.list_tools)()
                    post.reset_mock()
                    invoke = (lambda: client.get_prompt('p', {})) if is_prompt else (
                        lambda: client.call_tool('list_conferences', {}))
                    expected = McpError if isinstance(error, ToolGovernanceError) else type(error)
                    with patch.object(mcp_client, 'validate_remote_schema', side_effect=error), \
                         self.assertRaises(expected) as caught:
                        invoke()
                    if expected is McpError:
                        self.assertEqual(caught.exception.kind, 'client_error')
                    else:
                        self.assertIs(caught.exception, error)
                    post.assert_not_called()

    def test_missing_schema_fails_closed_and_local_rejection_precedes_remote(self):
        client, post = self._client(local=False)
        with patch.object(mcp_client, 'validate_remote_schema') as remote:
            for invoke in (lambda: client.call_tool('list_conferences', {}),
                           lambda: client.get_prompt('p', {})):
                with self.assertRaises(McpError):
                    invoke()
            remote.assert_not_called()
            post.assert_not_called()
        client, post = self._client()
        client.list_tools()
        client.list_prompts()
        client._local_tool_schemas['list_conferences'] = False
        client._local_prompt_schemas['p'] = False
        post.reset_mock()
        with patch.object(mcp_client, 'validate_remote_schema') as remote:
            for invoke in (lambda: client.call_tool('list_conferences', {}),
                           lambda: client.get_prompt('p', {})):
                with self.assertRaises(McpError):
                    invoke()
            remote.assert_not_called()
            post.assert_not_called()


class DiscoverySnapshotTests(unittest.TestCase):
    def test_discovery_results_are_detached_from_server_and_caller(self):
        for prompt in (False, True):
            with self.subTest(prompt=prompt):
                client = McpClient(ServerConfig('offline', 'http://invalid.example', allow_all=True))
                item = ({'name': 'x', 'arguments': [{'name': 'q', 'required': True}]}
                        if prompt else {'name': 'x', 'inputSchema': {
                            'type': 'object', 'required': ['q']}})
                payload = {'prompts' if prompt else 'tools': [item]}
                client._post = Mock(return_value=payload)
                listing = client.list_prompts if prompt else client.list_tools
                invoke = client.get_prompt if prompt else client.call_tool
                returned = listing()
                if prompt:
                    returned[0]['arguments'][0]['required'] = False
                    item['arguments'][0]['name'] = 'changed'
                else:
                    returned[0]['inputSchema']['required'].clear()
                    item['inputSchema']['type'] = 'string'
                self.assertEqual(listing()[0]['name'], 'x')
                if prompt:
                    self.assertEqual(listing()[0]['arguments'][0], {'name': 'q', 'required': True})
                else:
                    self.assertEqual(listing()[0]['inputSchema']['required'], ['q'])
                    self.assertEqual(listing()[0]['inputSchema']['type'], 'object')
                with self.assertRaises(McpError):
                    invoke('x', {})

    def test_validation_and_transport_do_not_hold_state_lock(self):
        for prompt in (False, True):
            with self.subTest(prompt=prompt):
                client = McpClient(ServerConfig('offline', 'http://invalid.example', allow_all=True,
                    local_tool_schemas={'x': {'type': 'object'}},
                    local_prompt_schemas={'x': {'type': 'object'}}))
                payload = ({'prompts': [{'name': 'x', 'arguments': []}]} if prompt else
                           {'tools': [{'name': 'x', 'inputSchema': {'type': 'object'}}]})
                def check_lock():
                    acquired = threading.Event()
                    def probe():
                        with client._lock:
                            acquired.set()
                    thread = threading.Thread(target=probe)
                    thread.start()
                    ok = acquired.wait(1)
                    thread.join(2)
                    self.assertTrue(ok, 'state lock held across validation or transport')
                def post(message, **kw):
                    check_lock()
                    return payload if message['method'].endswith('/list') else {}
                client._post = post
                (client.list_prompts if prompt else client.list_tools)()
                with patch.object(mcp_client, 'validate_remote_schema', side_effect=lambda *a, **kw: check_lock()):
                    (client.get_prompt if prompt else client.call_tool)('x', {})

    def test_refresh_does_not_mix_advertisement_and_schema(self):
        for prompt in (False, True):
            with self.subTest(prompt=prompt):
                client = McpClient(ServerConfig('offline', 'http://invalid.example', allow_all=True,
                    local_tool_schemas={'x': {'type': 'object'}},
                    local_prompt_schemas={'x': {'type': 'object'}}))
                key = 'prompts' if prompt else 'tools'
                def item(required):
                    return ({'name': 'x', 'arguments': [{'name': required, 'required': True}]}
                            if prompt else {'name': 'x', 'inputSchema': {
                                'type': 'object', 'required': [required]}})
                current = {key: [item('old')]}
                client._post = Mock(side_effect=lambda message, **kw: current if message['method'].endswith('/list') else {})
                listing = client.list_prompts if prompt else client.list_tools
                invoke = client.get_prompt if prompt else client.call_tool
                listing()
                entered, release, refreshed = threading.Event(), threading.Event(), threading.Event()
                class PausingSchemas(dict):
                    def __contains__(self, name):
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError('snapshot synchronization timed out')
                        return super().__contains__(name)
                attr = '_prompt_schemas' if prompt else '_tool_schemas'
                setattr(client, attr, PausingSchemas(getattr(client, attr)))
                errors = []
                def call():
                    try:
                        invoke('x', {'old': 'value'})
                    except Exception as exc:
                        errors.append(exc)
                def refresh():
                    try:
                        listing(refresh=True)
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        refreshed.set()
                worker = threading.Thread(target=call)
                worker.start()
                self.assertTrue(entered.wait(3))
                current = {key: [item('new')]}
                refresher = threading.Thread(target=refresh)
                refresher.start()
                refreshed.wait(0.3)
                release.set()
                worker.join(3); refresher.join(3)
                self.assertFalse(worker.is_alive() or refresher.is_alive())
                self.assertEqual(errors, [])
                with self.assertRaises(McpError):
                    invoke('x', {'old': 'value'})
                invoke('x', {'new': 'value'})


class ErrorClassificationTests(unittest.TestCase):
    def _client(self, script, **kw):
        mock = MockMcp(script)
        self.addCleanup(mock.__exit__)
        return McpClient(ServerConfig('mock', mock.url, timeout=kw.get('timeout', 10), allow_all=True)), mock

    def test_status_codes_map_to_kinds(self):
        for status, kind in ((401, 'auth'), (403, 'forbidden'), (429, 'rate_limited'),
                             (400, 'client_error'), (404, 'client_error'),
                             (500, 'server_error'), (503, 'server_error')):
            with self.subTest(status=status):
                client, _ = self._client({'tools/list': {
                    'status': status,
                    'body': {'jsonrpc': '2.0', 'id': 1,
                             'error': {'code': -32000, 'message': f'boom {status}'}}}})
                with self.assertRaises(McpError) as caught:
                    client.list_tools()
                error = caught.exception
                self.assertEqual(error.kind, kind)
                self.assertEqual(error.status, status)
                self.assertEqual(error.code, -32000)
                self.assertNotIn(f'boom {status}', str(error))
                self.assertIn(f'HTTP {status}', str(error))

    def test_bodyless_http_error_still_classifies(self):
        client, _ = self._client({'tools/list': {'status': 429, 'body': b''}})
        with self.assertRaises(McpError) as caught:
            client.list_tools()
        self.assertEqual((caught.exception.kind, caught.exception.status), ('rate_limited', 429))

    def test_malformed_body_is_protocol(self):
        client, _ = self._client({'tools/list': {'status': 200, 'body': b'<html>nope</html>'}})
        with self.assertRaises(McpError) as caught:
            client.list_tools()
        self.assertEqual(caught.exception.kind, 'protocol')

    def test_jsonrpc_error_on_200_is_protocol(self):
        client, _ = self._client({'tools/list': {
            'status': 200,
            'body': {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32601, 'message': 'no such method'}}}})
        with self.assertRaises(McpError) as caught:
            client.list_tools()
        self.assertEqual((caught.exception.kind, caught.exception.code), ('protocol', -32601))

    def test_result_without_object_is_protocol(self):
        client, _ = self._client({'tools/list': {'status': 200,
                                                 'body': {'jsonrpc': '2.0', 'id': 1, 'result': []}}})
        with self.assertRaises(McpError) as caught:
            client.list_tools()
        self.assertEqual(caught.exception.kind, 'protocol')

    def test_timeout_is_classified(self):
        client, _ = self._client({'tools/list': {'sleep': 1.5, 'status': 200,
                                                 'body': {'jsonrpc': '2.0', 'id': 1, 'result': {}}}},
                                 timeout=0.4)
        started = time.monotonic()
        with self.assertRaises(McpError) as caught:
            client.list_tools()
        self.assertEqual(caught.exception.kind, 'timeout')
        self.assertLess(time.monotonic() - started, 3.0)

    def test_unreachable_server_is_transport(self):
        # A refused connection must surface as transport, not timeout: give the
        # subprocess transport enough wall clock to report the refusal.
        client = McpClient(ServerConfig('mock', 'http://127.0.0.1:9/mcp', timeout=8))
        with self.assertRaises(McpError) as caught:
            client.list_tools()
        self.assertEqual(caught.exception.kind, 'transport')

    def test_error_dict_is_serialisable(self):
        client, _ = self._client({'tools/list': {'status': 429, 'body': b''}})
        with self.assertRaises(McpError) as caught:
            client.list_tools()
        payload = json.loads(json.dumps(caught.exception.as_dict()))
        self.assertEqual(payload['kind'], 'rate_limited')
        self.assertEqual(payload['status'], 429)


class ConfigTests(unittest.TestCase):
    def test_load_servers_resolves_env_and_keeps_allow_list(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mcp_servers.json'
            path.write_text(json.dumps({'servers': {'lune': {
                'url': 'https://mcp.example.com/',
                'headers': {'Authorization': 'Bearer ${TEST_MCP_KEY}'},
                'timeout': 12, 'allow': ['search_papers']}}}), encoding='utf-8')
            os.environ['TEST_MCP_KEY'] = 'secret-value'
            try:
                servers = load_servers(path)
            finally:
                os.environ.pop('TEST_MCP_KEY', None)
            config = servers['lune']
            self.assertEqual(config.url, 'https://mcp.example.com')
            self.assertEqual(config.timeout, 12.0)
            self.assertEqual(config.allow, ('search_papers',))
            self.assertTrue(config.permits('search_papers'))
            self.assertFalse(config.permits('extract_from_papers'))
            self.assertEqual(config.headers['Authorization'], 'Bearer secret-value')

    def test_missing_env_var_is_an_auth_error_without_echoing_it(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mcp_servers.json'
            path.write_text(json.dumps({'servers': {'lune': {
                'url': 'https://mcp.example.com',
                'headers': {'Authorization': 'Bearer ${DEFINITELY_UNSET_KEY}'}}}}), encoding='utf-8')
            with self.assertRaises(McpError) as caught:
                load_servers(path)
            self.assertEqual(caught.exception.kind, 'auth')
            self.assertIn('DEFINITELY_UNSET_KEY', str(caught.exception))

    def test_shipped_config_is_well_formed(self):
        root = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {'LUNE_API_KEY': 'lune_placeholder_for_parse_only'}):
            servers = load_servers(root / 'mcp_servers.json')
        self.assertIn('lune', servers)
        self.assertEqual(servers['lune'].url, 'https://mcp.luneresearch.com')
        self.assertTrue(servers['lune'].permits('search_papers'))
        self.assertFalse(servers['lune'].permits('subscribe_conference'))


if __name__ == '__main__':
    unittest.main()
