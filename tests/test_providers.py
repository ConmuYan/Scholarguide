"""Provider profiles and both HTTP protocols (local mock only)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import Budget, BudgetExceeded, Config, GuideError
from costing import cost_breakdown
from llm import JsonLLM
from providers import ProviderStore

SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
          'required': ['ok'], 'additionalProperties': False}


def validate(value):
    if value != {'ok': True}:
        raise GuideError('bad probe')


class MockHandler(BaseHTTPRequestHandler):
    requests = []
    mode = 'chat'
    status = 200
    missing_usage = False
    raw_body = None
    delay = 0

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        if type(self).delay:
            import time
            time.sleep(type(self).delay)
        body = json.loads(self.rfile.read(length) or b'{}')
        type(self).requests.append((self.path, body, self.headers.get('Authorization')))
        if self.status != 200:
            payload = {'error': {'message': 'mock auth failure'}}
        elif self.mode == 'responses':
            payload = {'id': 'resp_1', 'model': 'mock-model', 'status': 'completed',
                       'output': [{'type': 'message', 'content': [
                           {'type': 'output_text', 'text': '{"ok": true}'}]}]}
            if not self.missing_usage:
                payload['usage'] = {'input_tokens': 11, 'output_tokens': 7}
        else:
            payload = {'id': 'chat_1', 'model': 'mock-model',
                       'choices': [{'message': {'content': '{"ok": true}'}, 'finish_reason': 'stop'}]}
            if not self.missing_usage:
                payload['usage'] = {'prompt_tokens': 11, 'completion_tokens': 7}
        raw = type(self).raw_body if type(self).raw_body is not None else json.dumps(payload).encode()
        self.send_response(self.status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (ConnectionError, OSError):
            pass  # The timeout test deliberately disconnects before this write.


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), MockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)

    def setUp(self):
        MockHandler.requests = []
        MockHandler.mode = 'chat'
        MockHandler.status = 200
        MockHandler.missing_usage = False
        MockHandler.raw_body = None
        MockHandler.delay = 0

    def call(self, protocol='chat_completions'):
        MockHandler.mode = 'responses' if protocol == 'responses' else 'chat'
        config = Config(base_url=self.base, model='mock-model', api_key='secret-key',
                        protocol=protocol, request_timeout=5, input_price=2.0,
                        output_price=8.0, currency='CNY')
        budget = Budget(10)
        llm = JsonLLM(config, budget)
        value = llm.generate('probe', 'Return JSON.', {}, SCHEMA, validate)
        self.assertEqual(value, {'ok': True})
        return config, budget, llm

    def test_chat_completions_path_auth_usage_and_cost(self):
        config, budget, llm = self.call()
        path, body, auth = MockHandler.requests[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(auth, 'Bearer secret-key')
        self.assertIn('response_format', body)
        self.assertEqual((budget.input_tokens, budget.output_tokens), (11, 7))
        cost = cost_breakdown(config, budget.metrics(), [])
        self.assertEqual(cost['input_tokens'], 11)
        self.assertAlmostEqual(cost['model'], 0.000078)

    def test_responses_path_structured_output_usage(self):
        config, budget, llm = self.call('responses')
        path, body, auth = MockHandler.requests[0]
        self.assertEqual(path, '/v1/responses')
        self.assertEqual(auth, 'Bearer secret-key')
        self.assertIn('text', body)
        self.assertEqual((budget.input_tokens, budget.output_tokens), (11, 7))
        self.assertEqual(llm.last_model, 'mock-model')

    def test_unauthorized_and_forbidden_are_clean_errors(self):
        for status in (401, 403):
            MockHandler.status = status
            config = Config(base_url=self.base, model='mock-model', api_key='bad',
                            protocol='chat_completions', request_timeout=5)
            with self.assertRaises(GuideError) as caught:
                JsonLLM(config, Budget(10)).generate('probe', 'Return JSON.', {}, SCHEMA, validate)
            self.assertIn(f'HTTP {status}', str(caught.exception))

    def test_not_found_is_a_clean_error(self):
        MockHandler.status = 404
        config = Config(base_url=self.base, model='missing-model', api_key='secret',
                        protocol='chat_completions', request_timeout=5)
        with self.assertRaises(GuideError) as caught:
            JsonLLM(config, Budget(10)).generate('probe', 'Return JSON.', {}, SCHEMA, validate)
        self.assertIn('HTTP 404', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_invalid_json_and_timeout_are_clean_errors(self):
        MockHandler.raw_body = b'not-json'
        config = Config(base_url=self.base, model='mock-model', api_key='secret',
                        protocol='chat_completions', request_timeout=5)
        with self.assertRaises(GuideError) as caught:
            JsonLLM(config, Budget(10)).generate('probe', 'Return JSON.', {}, SCHEMA, validate)
        self.assertIn('invalid JSON', str(caught.exception))
        MockHandler.raw_body = None
        MockHandler.delay = 0.4
        config = Config(base_url=self.base, model='mock-model', api_key='secret',
                        protocol='chat_completions', request_timeout=0.1)
        with self.assertRaises(GuideError) as caught:
            JsonLLM(config, Budget(10)).generate('probe', 'Return JSON.', {}, SCHEMA, validate)
        self.assertIn('timed out', str(caught.exception))

    def test_missing_usage_is_unknown_not_zero(self):
        MockHandler.missing_usage = True
        config, budget, _ = self.call()
        cost = cost_breakdown(config, budget.metrics(), [])
        self.assertIsNone(cost['input_tokens'])
        self.assertIsNone(cost['model'])
        self.assertIsNone(cost['total'])

    def test_failed_or_truncated_content_still_counts_reported_usage(self):
        for protocol, payload in (
            ('chat_completions', {'choices': [{'finish_reason': 'length',
              'message': {'content': '{'}}], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}),
            ('responses', {'status': 'incomplete', 'output': [],
              'usage': {'input_tokens': 11, 'output_tokens': 7}}),
        ):
            with self.subTest(protocol=protocol):
                MockHandler.raw_body = json.dumps(payload).encode()
                config = Config(base_url=self.base, model='mock-model', protocol=protocol,
                                input_price=2, output_price=8)
                budget = Budget(10)
                llm = JsonLLM(config, budget)
                with self.assertRaises(GuideError):
                    llm.generate('probe', 'Return JSON.', {}, SCHEMA, validate)
                self.assertEqual((budget.llm_calls, budget.input_tokens, budget.output_tokens), (1, 11, 7))
                self.assertEqual(llm.last_usage, {'input_tokens': 11, 'output_tokens': 7})
                self.assertAlmostEqual(cost_breakdown(config, budget.metrics(), [])['model'], 0.000078)

    def test_request_guard_checks_each_actual_request_including_repair(self):
        MockHandler.raw_body = json.dumps({'choices': [{'message': {'content': 'invalid'}}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}).encode()
        budget = Budget(10)

        def guard():
            if budget.llm_calls >= 1:
                raise BudgetExceeded('LLM request budget exhausted.')

        budget.request_guard = guard
        llm = JsonLLM(Config(base_url=self.base, model='mock-model'), budget)
        with self.assertRaises(BudgetExceeded):
            llm.generate('probe', 'Return JSON.', {}, SCHEMA, validate)
        self.assertEqual(len(MockHandler.requests), 1)
        self.assertEqual(budget.llm_calls, 1)
        self.assertEqual((budget.input_tokens, budget.output_tokens), (11, 7))

    def test_negative_or_boolean_usage_never_becomes_known_cost(self):
        for token in (-10, True):
            with self.subTest(token=token):
                MockHandler.raw_body = json.dumps({'choices': [{'message': {'content': '{"ok": true}'}}],
                    'usage': {'prompt_tokens': token, 'completion_tokens': 7}}).encode()
                config, budget, _ = self.call()
                self.assertIsNone(cost_breakdown(config, budget.metrics(), [])['total'])
                self.assertEqual(budget.input_tokens, 0)

    def test_usage_is_retained_when_deadline_expires_after_response(self):
        budget = Budget(10)
        original = budget.remaining
        calls = 0

        def deadline():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise BudgetExceeded('Task time budget exhausted.')
            return original()

        budget.remaining = deadline
        with self.assertRaises(BudgetExceeded):
            JsonLLM(Config(base_url=self.base, model='mock-model'), budget).generate(
                'probe', 'Return JSON.', {}, SCHEMA, validate)
        self.assertEqual((budget.input_tokens, budget.output_tokens), (11, 7))

    def test_failed_probe_exposes_reported_usage_and_cost_without_saving_profile(self):
        MockHandler.raw_body = json.dumps({'choices': [{'finish_reason': 'length',
            'message': {'content': '{'}}], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            store = ProviderStore(Path(directory) / 'providers.json')
            response = store.test({'name': 'Probe', 'base_url': self.base, 'model': 'm',
                                   'input_price': 2, 'output_price': 8})
            self.assertFalse(response['ok'])
            self.assertEqual(response['usage'], {'input_tokens': 11, 'output_tokens': 7})
            self.assertAlmostEqual(response['cost']['model'], 0.000078)
            self.assertFalse(store.path.exists())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProviderStore(Path(self.tmp.name) / 'providers.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_updates_preserve_all_profiles_and_persisted_state(self):
        barrier = threading.Barrier(6)

        def save(index):
            barrier.wait(timeout=10)
            return self.store.upsert({'id': f'concurrent-{index}', 'name': f'Profile {index}',
                                     'base_url': 'https://example.com/v1', 'model': 'm'})

        with ThreadPoolExecutor(max_workers=6) as pool:
            created = list(pool.map(save, range(6)))
        expected = {profile.id for profile in created}
        self.assertEqual({p.id for p in self.store.profiles() if p.source == 'custom'}, expected)
        self.assertEqual({p.id for p in ProviderStore(self.store.path).profiles() if p.source == 'custom'}, expected)
        self.assertEqual(list(self.store.path.parent.glob('*.tmp')), [])

    def test_failed_persistence_does_not_change_active_profile_or_memory(self):
        original = self.store.upsert({'id': 'original', 'name': 'Original',
            'base_url': 'https://example.com/v1', 'model': 'm'}, activate=True)
        before = self.store.path.read_bytes()
        operations = (
            lambda: self.store.upsert({'id': 'new-profile', 'name': 'New',
                                      'base_url': 'https://example.com/v1', 'model': 'm'}, activate=True),
            lambda: self.store.activate('env'),
            lambda: self.store.delete(original.id),
        )
        for operation in operations:
            with self.subTest(operation=operation), patch.object(Path, 'replace', side_effect=OSError('disk unavailable')):
                with self.assertRaisesRegex(GuideError, 'could not be saved'):
                    operation()
            self.assertEqual(self.store.active().id, original.id)
            self.assertIsNone(self.store.get('new-profile'))
            self.assertEqual(self.store.path.read_bytes(), before)
            self.assertEqual(list(self.store.path.parent.glob('*.tmp')), [])

    def test_invalid_prices_ports_tokens_and_header_characters_are_rejected(self):
        base = {'name': 'Test', 'base_url': 'https://example.com/v1', 'model': 'm'}
        invalid = [{'input_price': v} for v in ('NaN', 'Infinity', float('-inf'), True)]
        invalid += [{'output_price': 'nan'}, {'base_url': 'https://example.com:abc/v1'},
                    {'base_url': 'https://example.com:65536/v1'}, {'base_url': 'http://localhost:0'},
                    {'max_output_tokens': float('inf')}, {'max_output_tokens': 3000.5},
                    {'api_key': 'key\r\nInjected: value'}]
        for patch_value in invalid:
            with self.subTest(patch=patch_value), self.assertRaises(GuideError):
                self.store.upsert({**base, **patch_value})
        self.assertFalse(self.store.path.exists())

    def test_new_profiles_with_same_name_receive_distinct_ids(self):
        data = {'name': 'Same name', 'base_url': 'https://example.com/v1', 'model': 'm'}
        self.assertNotEqual(self.store.upsert(data).id, self.store.upsert(data).id)

    def test_environment_configuration_rejects_nonfinite_limits_and_invalid_urls(self):
        from dataclasses import replace
        for changes in ({'task_timeout': float('nan')}, {'request_timeout': float('inf')},
                        {'source_timeout': float('-inf')}, {'input_price': float('nan')},
                        {'output_price': float('inf')}, {'base_url': 'https://example.com:bad'},
                        {'base_url': 'https://[::1'}, {'base_url': 'https://example.com/#fragment'},
                        {'base_url': 'https://exa mple.com/v1'}, {'base_url': 'https://example.com/\ninjected'},
                        {'api_key': 'key\nInjected: value'}):
            with self.subTest(changes=changes), self.assertRaises(GuideError):
                replace(Config(), **changes).validate()

    def test_profile_round_trip_masks_key_and_persists_active(self):
        created = self.store.upsert({'name': 'Local OpenAI', 'protocol': 'responses',
                                     'base_url': 'https://api.openai.com/v1',
                                     'model': 'gpt-test', 'api_key': 'sk-secret-1234',
                                     'input_price': 1.5, 'output_price': 6,
                                     'currency': 'USD'}, activate=True)
        self.assertNotIn('sk-secret-1234', json.dumps(created.public()))
        self.assertNotIn('sk-secret-1234', json.dumps(created.config().public()))
        self.assertTrue(created.public()['has_key'])
        self.assertIn('1234', created.public()['masked_key'])
        reloaded = ProviderStore(self.store.path)
        self.assertEqual(reloaded.active().id, created.id)
        self.assertEqual(reloaded.active().model, 'gpt-test')

    def test_blank_update_keeps_stored_key_and_delete_falls_back_to_env(self):
        created = self.store.upsert({'name': 'One', 'protocol': 'chat_completions',
                                     'base_url': 'https://example.com/v1', 'model': 'm',
                                     'api_key': 'sk-keep'}, activate=True)
        self.store.upsert({'id': created.id, 'name': 'One', 'protocol': 'chat_completions',
                           'base_url': 'https://example.com/v1', 'model': 'm2',
                           'api_key': ''}, activate=True)
        self.assertEqual(self.store.active().api_key, 'sk-keep')
        self.store.delete(created.id)
        self.assertEqual(self.store.active().id, 'env')

    def test_key_never_written_into_chat_result_or_meta(self):
        from webapp import ChatService
        secret = 'sk-result-secret-4321'
        self.store.upsert({'name': 'Secret', 'protocol': 'chat_completions',
                           'base_url': 'https://example.com/v1', 'model': 'm',
                           'api_key': secret}, activate=True)
        service = ChatService(output=Path(self.tmp.name) / 'secure-web', providers=self.store)
        try:
            service.create({'mode': 'demo', 'scenario': 'followup',
                            'message': 'Recommend five RAG papers.'})
            files = list((Path(self.tmp.name) / 'secure-web').rglob('*.json'))
            self.assertTrue(files)
            for path in files:
                self.assertNotIn(secret, path.read_text(encoding='utf-8'), path)
        finally:
            service.close()

    def test_invalid_profiles_rejected(self):
        for data in ({'name': 'x', 'protocol': 'chat_completions', 'base_url': 'file:///tmp',
                      'model': 'm'},
                     {'name': 'x', 'protocol': 'bad', 'base_url': 'https://example.com',
                      'model': 'm'},
                     {'name': 'x', 'protocol': 'chat_completions', 'base_url': 'https://example.com',
                      'model': '', 'api_key': 'k'},
                     {'id': 'env', 'name': 'x', 'protocol': 'chat_completions',
                      'base_url': 'https://example.com', 'model': 'm'}):
            with self.assertRaises(GuideError):
                self.store.upsert(data)

    def test_active_switch_is_applied_to_live_chat_turn(self):
        from demo import ChatDemoLLM, DemoSource
        from session import ChatSession
        from state import StateStore
        from webapp import ChatService
        first = self.store.upsert({'name': 'A', 'protocol': 'chat_completions',
                                   'base_url': 'https://a.example/v1', 'model': 'model-a',
                                   'api_key': 'ka'}, activate=True)
        second = self.store.upsert({'name': 'B', 'protocol': 'responses',
                                    'base_url': 'https://b.example/v1', 'model': 'model-b',
                                    'api_key': 'kb'})
        self.store.activate(second.id)
        tmp = Path(self.tmp.name) / 'chat-state.sqlite3'
        store = StateStore(tmp)
        try:
            chat = ChatSession(Config(agent_runtime='legacy'), store,
                               source=DemoSource(), llm=ChatDemoLLM())
            service = ChatService(output=Path(self.tmp.name) / 'web', providers=self.store)
            try:
                record = {'mode': 'live', 'source': 'arxiv', 'chat': chat}
                service._refresh_live_provider(record)
                self.assertEqual(chat.config.model, 'model-b')
                self.assertEqual(chat.config.protocol, 'responses')
                self.assertEqual(chat.config.profile_id, second.id)
            finally:
                service.close()
        finally:
            store.close()

    def test_active_switch_applies_to_send_path(self):
        from threading import Lock
        from webapp import ChatService
        first = self.store.upsert({'name': 'A', 'protocol': 'chat_completions',
                                   'base_url': 'https://a.example/v1', 'model': 'model-a',
                                   'api_key': 'ka'}, activate=True)
        second = self.store.upsert({'name': 'B', 'protocol': 'responses',
                                    'base_url': 'https://b.example/v1', 'model': 'model-b',
                                    'api_key': 'kb'})
        self.store.activate(second.id)
        class Chat:
            def __init__(self):
                self.config = None
                self.calls = []
            def rebind_provider(self, config):
                self.config = config
            def send(self, message):
                self.calls.append(message)
                return {'status': 'complete', 'papers': [], 'metrics': {},
                        'mode': 'live', 'method': 'V2', 'request': {'topic': 'RAG'},
                        'stop_reason': 'requirements_met', 'gaps': [], 'warnings': [],
                        'revision': 1, 'user_input': message}
        class Store:
            def close(self): pass
        service = ChatService(output=Path(self.tmp.name) / 'web', providers=self.store)
        try:
            fake = Chat()
            directory = Path(self.tmp.name) / 'web' / ('f' * 32)
            directory.mkdir(parents=True)
            service.sessions['f' * 32] = {'chat': fake, 'store': Store(), 'directory': directory,
                                           'created_at': 'now', 'results': [], 'lock': Lock(),
                                           'mode': 'live', 'source': 'arxiv', 'origin': 'chat',
                                           'running': False}
            payload = service.send('f' * 32, {'message': 'follow up', 'mode': 'live'})
            self.assertEqual(fake.config.model, 'model-b')
            self.assertEqual(fake.config.protocol, 'responses')
            self.assertEqual(fake.calls, ['follow up'])
            self.assertEqual(payload['result']['status'], 'complete')
        finally:
            service.close()

    def test_probe_does_not_write_profile(self):
        MockHandler.requests = []
        MockHandler.mode = 'chat'
        MockHandler.status = 200
        # The probe uses a local mock URL and a fresh unsaved profile.
        before = self.store.public_state()
        result = self.store.test({'name': 'probe', 'protocol': 'chat_completions',
                                  'base_url': 'http://127.0.0.1:1',
                                  'model': 'm', 'api_key': ''})
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'connection')
        self.assertEqual(self.store.public_state(), before)

    def test_active_profile_drives_new_chat_and_search_config(self):
        from unittest.mock import patch
        from webapp import ChatService, Jobs
        created = self.store.upsert({'name': 'Custom', 'protocol': 'responses',
                                     'base_url': 'https://custom.example/v1', 'model': 'custom-model',
                                     'api_key': 'custom-key', 'input_price': 1,
                                     'output_price': 2}, activate=True)
        chat = ChatService(output=Path(self.tmp.name) / 'web', providers=self.store)
        try:
            config, source, llm = chat._new_chat({'mode': 'live', 'source': ''})
            self.assertEqual(config.protocol, 'responses')
            self.assertEqual(config.model, 'custom-model')
            self.assertEqual(config.base_url, 'https://custom.example/v1')
            self.assertEqual(config.profile_id, created.id)
        finally:
            chat.close()
        jobs = Jobs(output=Path(self.tmp.name) / 'runs', providers=self.store)
        with patch('webapp.run', return_value={'status': 'complete'}) as runner:
            jobs._execute({'mode': 'live', 'method': 'A', 'request': 'RAG', 'source': 'arxiv',
                           'cache': False, 'reading_goal': ''}, lambda _event: None)
            used = runner.call_args[0][1]
            self.assertEqual(used.model, 'custom-model')
            self.assertEqual(used.protocol, 'responses')
