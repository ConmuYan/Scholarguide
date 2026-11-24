"""OAuth diagnostics contracts. All credential and outbound HTTP calls are mocked."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scholarguide.common import Config, GuideError
from scholarguide.network import BufferedResponse
from scholarguide import oauth_diagnostics as diagnostics
from scholarguide.oauth_diagnostics import OpenAIOAuthDiagnostics, OAuthDiagnosticsBusy
from scholarguide.providers import ProviderProfile, ProviderStore
from scholarguide.webapp import ChatService, Jobs, create_server

PATH = '/api/providers/oauth/openai/test'
SECRET = 'test-credential-never-public'
PROFILE = ProviderProfile('openai-oauth', 'OAuth', 'responses',
                          'https://api.openai.com/v1', 'saved-model')


def catalog(rows=None):
    return BufferedResponse(json.dumps({'models': rows if rows is not None else [
        {'visibility': 'list', 'slug': 'listed-model', 'display_name': 'Listed Model'}
    ]}).encode())


def stream(*, status='completed', text='{"ok":true}', usage=None):
    response = {'status': status, 'model': 'server-identity-must-not-escape',
                'usage': usage if usage is not None else {'input_tokens': 12, 'output_tokens': 5},
                'output': [{'content': [{'type': 'output_text', 'text': text}]}]}
    kind = 'response.completed' if status == 'completed' else 'response.' + status
    event = {'type': kind, 'response': response}
    return BufferedResponse(('data: ' + json.dumps(event) + '\n\n').encode(), status=200)


def http_error(code):
    return HTTPError('https://api.openai.com/v1', code, SECRET, {},
                     BytesIO(json.dumps({'error': {'message': SECRET}, 'account': SECRET}).encode()))


class DiagnosticServiceTests(unittest.TestCase):
    def setUp(self):
        self.providers = Mock()
        self.providers.get.return_value = PROFILE
        self.service = OpenAIOAuthDiagnostics(self.providers, config_factory=lambda: Config(request_timeout=90))
        self.token = patch('scholarguide.openai_oauth.access_token', return_value=SECRET).start()
        self.proxy = patch('scholarguide.openai_oauth.proxy_url', return_value='http://127.0.0.1:7890').start()
        self.catalog_http = patch('scholarguide.network.urlopen', side_effect=lambda *a, **k: catalog()).start()
        self.probe_http = patch('scholarguide.llm.urlopen', side_effect=lambda *a, **k: stream()).start()
        self.addCleanup(patch.stopall)

    def test_fixed_official_urls_proxy_redirect_and_bounded_probe(self):
        before = time.monotonic()
        value = self.service.test({})
        after = time.monotonic()
        self.assertTrue(value['catalog']['ok'])
        self.assertTrue(value['probe']['ok'])
        self.assertEqual(value['probe']['model'], 'saved-model')
        self.assertEqual(value['probe']['usage'], {'input_tokens': 12, 'output_tokens': 5})
        self.assertEqual(value['probe']['http_status'], 200)
        self.assertGreaterEqual(value['probe']['latency_ms'], 0)
        self.assertEqual(value['account_usage'], {
            'available': False, 'manage_url': 'https://chatgpt.com/settings/usage'})
        self.assertIsInstance(value['checked_at'], str)
        self.catalog_http.assert_called_once()
        request = self.catalog_http.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.openai.com/v1/models')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + SECRET)
        for call in (self.catalog_http.call_args, self.probe_http.call_args):
            self.assertTrue(call.kwargs['reject_redirects'])
            self.assertEqual(call.kwargs['model_proxy'], 'http://127.0.0.1:7890')
            self.assertGreater(call.kwargs['timeout'], 0)
            self.assertLessEqual(call.kwargs['timeout'], 30)
        probe_request = self.probe_http.call_args.args[0]
        self.assertEqual(probe_request.full_url, 'https://api.openai.com/v1/responses')
        self.assertEqual(probe_request.get_header('Authorization'), 'Bearer ' + SECRET)
        payload = json.loads(probe_request.data)
        self.assertEqual(payload['model'], 'saved-model')
        self.assertEqual(payload['max_output_tokens'], 256)
        self.assertTrue(payload['stream'])
        self.assertFalse(payload['store'])
        self.assertTrue(payload['text']['format']['strict'])
        self.assertLess(len(probe_request.data), 1500)
        self.assertEqual(self.token.call_count, 2)
        for call in self.token.call_args_list + self.proxy.call_args_list:
            self.assertGreater(call.kwargs['deadline'], before)
            self.assertLessEqual(call.kwargs['deadline'], after + 45)
            self.assertTrue(callable(call.kwargs['cancelled']))
        self.assertLessEqual(self.token.call_args_list[0].kwargs['deadline'], before + 15.1)
        self.assertNotIn(SECRET, json.dumps(value))
        self.assertNotIn('server-identity', json.dumps(value))
        self.providers.upsert.assert_not_called()
        self.providers.activate.assert_not_called()
        self.providers.config.assert_not_called()

    def test_catalog_only_list_visibility_safe_fields_and_original_order(self):
        self.catalog_http.return_value = None
        rows = [
            {'visibility': 'hidden', 'slug': 'hidden', 'display_name': SECRET},
            {'slug': 'unspecified', 'display_name': 'unspecified'},
            {'visibility': 'list', 'slug': 'z-model', 'display_name': 'Z', 'account': SECRET},
            {'visibility': 'list', 'slug': 'a-model', 'display_name': 'A'},
            {'visibility': 'list', 'slug': 'x' * 200, 'display_name': '名' * 200},
            {'visibility': 'LIST', 'slug': 'wrong-case', 'display_name': SECRET},
        ]
        self.catalog_http.side_effect = lambda *a, **k: catalog(rows)
        value = self.service.test({'model': 'explicit-model'})
        self.assertEqual(value['models'], [
            {'slug': 'z-model', 'display_name': 'Z'},
            {'slug': 'a-model', 'display_name': 'A'},
            {'slug': 'x' * 200, 'display_name': '名' * 200}])
        self.assertEqual(value['probe']['model'], 'explicit-model')
        self.assertEqual(json.loads(self.probe_http.call_args.args[0].data)['model'], 'explicit-model')
        self.assertNotIn(SECRET, json.dumps(value))

    def test_large_official_catalog_keeps_only_display_metadata(self):
        # The official catalog carries instructions and capabilities (~650 KB).
        # Its size must not be confused with the tiny public slug/name list.
        rows = [{'visibility': 'list', 'slug': 'listed-model',
                 'display_name': 'Listed Model', 'base_instructions': 'x' * 650_000}]
        self.catalog_http.side_effect = lambda *a, **k: catalog(rows)
        result = self.service.test({})
        self.assertTrue(result['catalog']['ok'])
        self.assertEqual(result['models'], [{'slug': 'listed-model', 'display_name': 'Listed Model'}])
        self.assertLess(len(json.dumps(result)), 2000)
        self.assertNotIn('base_instructions', json.dumps(result))

    def test_bad_rows_cannot_expose_unbounded_or_control_strings(self):
        bad = [None, 10, {}, {'visibility': 'list', 'slug': ['bad'], 'display_name': SECRET}]
        for value in ('', ' ', 'x' * 201, 'x\n', 'x\x7f', 'x\x85', 'x\u202e', 'x\ud800', 42, None):
            bad.extend([{'visibility': 'list', 'slug': value, 'display_name': SECRET},
                        {'visibility': 'list', 'slug': 'm', 'display_name': value}])
        self.catalog_http.side_effect = lambda *a, **k: catalog(bad)
        result = self.service.test({})
        self.assertEqual(result['models'], [])
        self.assertTrue(result['catalog']['ok'])
        self.assertTrue(result['probe']['ok'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_oversized_and_malformed_catalog_is_safe_and_probe_independent(self):
        for raw in (b'x' * (diagnostics.MAX_CATALOG_BYTES + 1), b'not-json:' + SECRET.encode(),
                    b'\xff', b'[]', b'{}', b'{"models":{}}',
                    json.dumps({'models': [{}] * (diagnostics.MAX_MODELS + 1)}).encode()):
            with self.subTest(size=len(raw)):
                response = Mock(wraps=BufferedResponse(raw))
                response.status = 200
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                self.catalog_http.side_effect = None
                self.catalog_http.return_value = response
                result = self.service.test({})
                self.assertFalse(result['catalog']['ok'])
                self.assertEqual(result['catalog']['http_status'], 200)
                self.assertEqual(result['models'], [])
                self.assertTrue(result['probe']['ok'])
                self.assertNotIn(SECRET, json.dumps(result))
                response.read.assert_called_once_with(diagnostics.MAX_CATALOG_BYTES + 1)

    def test_catalog_http_failure_keeps_probe_for_saved_and_explicit_models(self):
        for raw in ({}, {'model': 'explicit-model'}):
            for code in (302, 401, 403, 429, 500):
                with self.subTest(raw=raw, code=code):
                    error = http_error(code)
                    self.catalog_http.side_effect = error
                    result = self.service.test(raw)
                    self.assertEqual(result['catalog'], {
                        'ok': False, 'http_status': code,
                        'error': 'OpenAI OAuth model catalog could not be retrieved.'})
                    self.assertTrue(result['probe']['ok'])
                    self.assertEqual(result['probe']['model'], raw.get('model', 'saved-model'))
                    self.assertNotIn(SECRET, json.dumps(result))
                    self.assertTrue(error.fp is None or error.fp.closed)

    def test_probe_http_failure_preserves_catalog_status_and_never_retries(self):
        for code in (302, 400, 401, 403, 422, 429, 500):
            with self.subTest(code=code):
                self.probe_http.reset_mock()
                self.probe_http.side_effect = http_error(code)
                result = self.service.test({})
                self.assertTrue(result['catalog']['ok'])
                self.assertEqual(len(result['models']), 1)
                self.assertFalse(result['probe']['ok'])
                self.assertEqual(result['probe']['http_status'], code)
                self.assertEqual(result['probe']['usage'], {'input_tokens': None, 'output_tokens': None})
                self.assertEqual(result['probe']['error'], 'OpenAI OAuth model probe failed.')
                self.assertNotIn(SECRET, json.dumps(result))
                self.probe_http.assert_called_once()

    def test_failed_stream_reports_observed_tokens_without_output(self):
        self.probe_http.side_effect = lambda *a, **k: stream(status='failed', text=SECRET)
        value = self.service.test({})
        self.assertFalse(value['probe']['ok'])
        self.assertEqual(value['probe']['http_status'], 200)
        self.assertEqual(value['probe']['usage'], {'input_tokens': 12, 'output_tokens': 5})
        self.assertNotIn(SECRET, json.dumps(value))
        self.probe_http.assert_called_once()

    def test_missing_usage_in_repair_does_not_present_a_partial_total(self):
        self.probe_http.side_effect = [stream(text='{"ok":false}'), stream(usage={})]
        result = self.service.test({})
        self.assertTrue(result['probe']['ok'])
        self.assertFalse(result['probe']['usage_complete'])
        self.assertEqual(result['probe']['model_calls'], 2)
        self.assertEqual(result['probe']['usage'], {'input_tokens': None, 'output_tokens': None})

    def test_bad_usage_is_unknown_and_one_structured_repair_is_bounded(self):
        self.probe_http.side_effect = [stream(text='{"ok":false}'), stream()]
        result = self.service.test({})
        self.assertTrue(result['probe']['ok'])
        self.assertEqual(self.probe_http.call_count, 2)
        self.assertEqual(result['probe']['usage'], {'input_tokens': 24, 'output_tokens': 10})
        self.probe_http.reset_mock()
        self.probe_http.side_effect = lambda *a, **k: stream(text='{"ok":false}', usage={
            'input_tokens': True, 'output_tokens': SECRET})
        result = self.service.test({})
        self.assertFalse(result['probe']['ok'])
        self.assertEqual(self.probe_http.call_count, 2)
        self.assertEqual(result['probe']['usage'], {'input_tokens': None, 'output_tokens': None})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_credential_and_transport_errors_use_fixed_text(self):
        self.token.side_effect = RuntimeError(SECRET)
        result = self.service.test({})
        self.assertFalse(result['catalog']['ok'])
        self.assertFalse(result['probe']['ok'])
        self.assertNotIn(SECRET, json.dumps(result))
        self.catalog_http.assert_not_called()
        self.probe_http.assert_not_called()
        self.token.side_effect = None
        self.catalog_http.side_effect = TimeoutError(SECRET)
        self.probe_http.side_effect = TimeoutError(SECRET)
        result = self.service.test({})
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIsNone(result['catalog']['http_status'])
        self.assertIsNone(result['probe']['http_status'])

    def test_no_profile_conflicts_unknown_parameters_and_invalid_models(self):
        invalid = [None, [], '', {'extra': 1}, {'model': None}, {'model': True},
                   {'model': 1}, {'model': ''}, {'model': ' '}, {'model': 'a b'},
                   {'model': 'x' * 201}, {'model': 'x\n'}, {'model': 'x\x7f'},
                   {'model': 'x\u202e'}]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(GuideError):
                self.service.test(raw)
        for profile in (None, replace(PROFILE, base_url='https://example.test/v1'),
                        replace(PROFILE, base_url=PROFILE.base_url + '/'),
                        replace(PROFILE, protocol='chat_completions'),
                        replace(PROFILE, api_key=SECRET), replace(PROFILE, model='bad\nmodel')):
            self.providers.get.return_value = profile
            with self.subTest(profile=profile), self.assertRaises(GuideError):
                self.service.test({})
        self.token.assert_not_called()
        self.catalog_http.assert_not_called()
        self.probe_http.assert_not_called()
        self.providers.get.return_value = PROFILE
        self.assertTrue(self.service.test({})['probe']['ok'])  # Failures release the lock.

    def test_total_deadline_and_catalog_cap_leave_probe_budget(self):
        clock = [100.0]
        observations = {}

        def slow_catalog(*args, **kwargs):
            observations['catalog_timeout'] = kwargs['timeout']
            observations['scope_end'] = __import__('scholarguide.network', fromlist=[''])._scope.get()[0]
            clock[0] += 10
            return catalog()

        def factory(config, budget):
            observations['config'] = config
            observations['remaining'] = budget.remaining()
            observations['probe_scope_end'] = __import__('scholarguide.network', fromlist=[''])._scope.get()[0]
            fake = Mock(last_http_status=200)
            fake.generate.return_value = {'ok': True}
            return fake

        self.catalog_http.side_effect = slow_catalog
        self.service._llm_factory = factory
        with patch('scholarguide.oauth_diagnostics.time.monotonic', side_effect=lambda: clock[0]):
            result = self.service.test({})
        self.assertTrue(result['probe']['ok'])
        self.assertEqual(observations['catalog_timeout'], 15)
        self.assertEqual(observations['scope_end'], 115)
        self.assertEqual(observations['remaining'], 35)
        self.assertEqual(observations['probe_scope_end'], 145)
        self.assertEqual(observations['config'].request_timeout, 30)
        self.assertEqual(observations['config'].max_output_tokens, 256)
        self.assertEqual(self.token.call_args.kwargs['deadline'], 115)

    def test_expired_total_budget_starts_no_probe(self):
        clock = [100.0]

        def exhausted(*args, **kwargs):
            clock[0] = 145.0
            raise TimeoutError(SECRET)

        self.catalog_http.side_effect = exhausted
        with patch('scholarguide.oauth_diagnostics.time.monotonic', side_effect=lambda: clock[0]):
            result = self.service.test({})
        self.assertFalse(result['catalog']['ok'])
        self.assertFalse(result['probe']['ok'])
        self.probe_http.assert_not_called()

    def test_service_rejects_concurrent_diagnostics_without_blocking(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test release timed out')
            return catalog()

        self.catalog_http.side_effect = blocked
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.service.test, {})
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(OAuthDiagnosticsBusy):
                    self.service.test({})
                self.assertEqual(self.token.call_count, 1)
            finally:
                release.set()
            self.assertTrue(future.result(3)['probe']['ok'])
        self.assertTrue(self.service.test({})['probe']['ok'])


class DiagnosticWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.store = ProviderStore(cls.root / 'providers.json')
        cls.store.upsert({'id': PROFILE.id, 'name': PROFILE.name, 'protocol': PROFILE.protocol,
                          'base_url': PROFILE.base_url, 'model': PROFILE.model})
        cls.store.upsert({'id': 'other', 'name': 'Other', 'base_url': 'https://example.test/v1',
                          'model': 'other-model'}, activate=True)
        cls.service = OpenAIOAuthDiagnostics(cls.store, config_factory=lambda: Config())
        cls.chat = ChatService(output=cls.root / 'web', providers=cls.store)
        cls.server = create_server(0, jobs=Jobs(output=cls.root / 'runs', providers=cls.store),
                                   chat=cls.chat, providers=cls.store, oauth_diagnostics=cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.chat.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.token = patch('scholarguide.openai_oauth.access_token', return_value=SECRET).start()
        patch('scholarguide.openai_oauth.proxy_url', return_value='').start()
        self.catalog_http = patch('scholarguide.network.urlopen', side_effect=lambda *a, **k: catalog()).start()
        self.probe_http = patch('scholarguide.llm.urlopen', side_effect=lambda *a, **k: stream()).start()
        self.addCleanup(patch.stopall)

    def request(self, body=b'{}', *, headers=None, method='POST', path=PATH):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=5)
        merged = {'Host': f'127.0.0.1:{self.port}', 'Content-Type': 'application/json'}
        merged.update(headers or {})
        connection.request(method, path, body=body, headers=merged)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)

    def test_post_preserves_store_active_profile_and_files(self):
        before = self.store.path.read_bytes()
        state = self.store.public_state()
        files = {p.relative_to(self.root) for p in self.root.rglob('*') if p.is_file()}
        status, value = self.request(b'{"model":"temporary-model"}')
        self.assertEqual(status, 200)
        self.assertTrue(value['probe']['ok'])
        self.assertEqual(value['probe']['model'], 'temporary-model')
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.public_state(), state)
        self.assertEqual(self.store.active().id, 'other')
        self.assertEqual(self.store.get(PROFILE.id).model, 'saved-model')
        self.assertEqual({p.relative_to(self.root) for p in self.root.rglob('*') if p.is_file()}, files)
        self.assertNotIn(SECRET, json.dumps(value))

    def test_host_origin_and_json_rules_reject_before_network(self):
        cases = [(b'{}', {'Host': 'evil.test'}, 403),
                 (b'{}', {'Origin': 'https://evil.test'}, 403),
                 (b'{}', {'Origin': 'null'}, 403),
                 (b'{}', {'Content-Type': 'text/plain'}, 415),
                 (b'{}' + b' ' * 16000, {}, 413),
                 (b'{', {}, 400), (b'[]', {}, 400),
                 (b'{"model":"a","model":"b"}', {}, 400),
                 (b'{"model":null}', {}, 400), (b'{"extra":1}', {}, 400)]
        for body, headers, expected in cases:
            with self.subTest(body=body[:60], headers=headers):
                status, _ = self.request(body, headers=headers)
                self.assertEqual(status, expected)
        self.token.assert_not_called()
        self.catalog_http.assert_not_called()
        self.probe_http.assert_not_called()

    def test_matching_origin_is_accepted(self):
        status, value = self.request(headers={'Origin': f'http://127.0.0.1:{self.port}'})
        self.assertEqual(status, 200)
        self.assertTrue(value['probe']['ok'])

    def test_no_automatic_get_probe(self):
        status, _ = self.request(method='GET', body=None)
        self.assertEqual(status, 404)
        self.request(method='GET', path='/api/providers', body=None)
        self.request(method='GET', path='/api/providers/oauth/openai', body=None)
        self.token.assert_not_called()
        self.catalog_http.assert_not_called()
        self.probe_http.assert_not_called()

    def test_concurrent_http_request_returns_409(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test release timed out')
            return catalog()

        self.catalog_http.side_effect = blocked
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.request)
            try:
                self.assertTrue(entered.wait(2))
                status, value = self.request()
                self.assertEqual(status, 409)
                self.assertIn('already in progress', value['error'])
                self.assertEqual(self.token.call_count, 1)
            finally:
                release.set()
            self.assertEqual(first.result(3)[0], 200)

    def test_missing_and_conflicting_profile_return_400(self):
        for profile in (None, replace(PROFILE, api_key=SECRET)):
            with self.subTest(profile=profile), patch.object(self.store, 'get', return_value=profile):
                status, value = self.request()
                self.assertEqual(status, 400)
                self.assertNotIn(SECRET, json.dumps(value))
        self.token.assert_not_called()


if __name__ == '__main__':
    unittest.main()
