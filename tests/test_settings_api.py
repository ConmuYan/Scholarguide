"""Settings page and provider-profile HTTP contract; no external model calls."""
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from providers import ProviderStore
from webapp import ChatService, Jobs, create_server


class SettingsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.root = root
        cls.store = ProviderStore(root / 'providers.json')
        cls.chat = ChatService(output=root / 'web', providers=cls.store)
        cls.server = create_server(0, jobs=Jobs(output=root / 'runs', providers=cls.store),
                                   chat=cls.chat, providers=cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)
        cls.chat.close(); cls.tmp.cleanup()

    def request(self, method, path, body=None):
        con = HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Host': f'127.0.0.1:{self.port}', 'Content-Type': 'application/json'}
        payload = json.dumps(body).encode() if body is not None else None
        con.request(method, path, body=payload, headers=headers)
        response = con.getresponse(); raw = response.read(); con.close()
        return response.status, raw.decode('utf-8')

    def test_settings_assets_and_masked_profile_contract(self):
        status, body = self.request('GET', '/settings')
        self.assertEqual(status, 200); self.assertIn('Model providers', body)
        status, body = self.request('GET', '/settings.js')
        self.assertEqual(status, 200); self.assertIn('api/providers', body)

        secret = 'sk-super-secret-9876'
        status, body = self.request('POST', '/api/providers', {
            'name': 'Test endpoint', 'protocol': 'responses',
            'base_url': 'https://example.com/v1', 'model': 'test-model',
            'api_key': secret, 'input_price': 1, 'output_price': 2,
            'currency': 'USD', 'activate': True})
        self.assertEqual(status, 200, body)
        self.assertNotIn(secret, body)
        profile = json.loads(body)['profile']
        self.assertNotIn('api_key', profile)
        self.assertTrue(profile['has_key'])
        status, body = self.request('GET', '/api/providers')
        self.assertNotIn(secret, body)
        state = json.loads(body)
        self.assertEqual(state['active'], profile['id'])
        status, body = self.request('GET', '/api/config')
        config = json.loads(body)
        self.assertTrue(config['ready'])
        self.assertEqual(config['model'], 'test-model')

        status, body = self.request('POST', '/api/providers/activate', {'id': 'env'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['active'], 'env')

        status, body = self.request('DELETE', '/api/providers/' + profile['id'])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['deleted'], profile['id'])

    def test_invalid_url_is_rejected(self):
        status, body = self.request('POST', '/api/providers', {
            'name': 'Bad', 'protocol': 'chat_completions',
            'base_url': 'file:///tmp/model', 'model': 'x'})
        self.assertEqual(status, 400)
        self.assertIn('Base URL', body)

    def test_explicit_keyless_remote_profile_is_ready_without_an_automatic_probe(self):
        status, _ = self.request('POST', '/api/providers', {
            'id': 'anonymous', 'name': 'Remote compatible model',
            'base_url': 'http://10.0.0.20:8000/v1', 'model': 'local-research-model',
            'api_key': '', 'activate': True})
        self.assertEqual(status, 200)
        with patch('llm.JsonLLM._call', side_effect=AssertionError('readiness must not call a model')) as call:
            status, body = self.request('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ready'])
        self.assertEqual(json.loads(body)['model'], 'local-research-model')
        call.assert_not_called()
        self.request('POST', '/api/providers/activate', {'id': 'env'})

    def test_malformed_urls_return_400_not_disconnect(self):
        for url in ('http://[::1', 'http://exa mple.com'):
            status, body = self.request('POST', '/api/providers', {
                'name': 'Bad', 'protocol': 'chat_completions',
                'base_url': url, 'model': 'm'})
            self.assertEqual(status, 400, url)
            self.assertIn('Base URL', body)

    def test_test_endpoint_reports_failure_without_history_or_secret(self):
        runs = (self.root / 'runs')
        sessions = (self.root / 'web')
        before = (len(list(runs.glob('*/result.json'))), len(list(sessions.glob('*/state.sqlite3'))))
        status, body = self.request('POST', '/api/providers/test', {
            'name': 'Probe', 'protocol': 'chat_completions',
            'base_url': 'http://127.0.0.1:1', 'model': 'x', 'api_key': ''})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertFalse(result['ok'])
        self.assertIn('latency_ms', result)
        self.assertNotIn('api_key', result)
        after = (len(list(runs.glob('*/result.json'))), len(list(sessions.glob('*/state.sqlite3'))))
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
