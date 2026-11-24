"""Local web contract for ChatGPT sign-in; never performs a real authorization."""
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scholarguide.common import GuideError
from scholarguide.providers import ProviderStore
from scholarguide.webapp import ChatService, Jobs, create_server


class OAuthWebApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ProviderStore(root / 'providers.json')
        self.chat = ChatService(output=root / 'web', providers=self.store)
        self.server = create_server(0, jobs=Jobs(output=root / 'runs', providers=self.store),
                                    chat=self.chat, providers=self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.chat.close(); self.tmp.cleanup()

    def request(self, method, path, body=None):
        con = HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Host': f'127.0.0.1:{self.port}', 'Content-Type': 'application/json'}
        payload = json.dumps(body).encode() if body is not None else None
        con.request(method, path, body=payload, headers=headers)
        response = con.getresponse()
        raw = response.read(); con.close()
        return response.status, raw.decode('utf-8')

    def wait_for(self, state, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, body = self.request('GET', '/api/providers/oauth/openai')
            value = json.loads(body)
            if value['state'] == state:
                return status, value
            time.sleep(0.02)
        self.fail('sign-in did not reach ' + state)

    def test_status_is_empty_before_sign_in(self):
        status, body = self.request('GET', '/api/providers/oauth/openai')
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value['state'], 'idle')
        self.assertIsNone(value['profile'])
        self.assertFalse(value['running'])

    def test_login_rejects_invalid_input_before_authorization(self):
        for body in ({}, {'model': ''}, {'model': 'm', 'extra': 1}):
            with self.subTest(body=body), patch('scholarguide.openai_oauth.login') as login:
                status, _ = self.request('POST', '/api/providers/oauth/openai/login', body)
                self.assertEqual(status, 400)
                login.assert_not_called()

    def test_sign_in_state_profile_and_single_flight(self):
        release = threading.Event()

        def fake_login():
            release.wait(5)

        with patch('scholarguide.openai_oauth.login', side_effect=fake_login) as login:
            status, body = self.request('POST', '/api/providers/oauth/openai/login',
                                        {'model': 'gpt-test'})
            self.assertEqual(status, 202, body)
            self.assertEqual(json.loads(body)['state'], 'running')
            status, body = self.request('POST', '/api/providers/oauth/openai/login',
                                        {'model': 'gpt-test'})
            self.assertEqual(status, 409)
            release.set()
            status, value = self.wait_for('succeeded')
        self.assertEqual(status, 200)
        login.assert_called_once()
        self.assertEqual(value['profile']['model'], 'gpt-test')
        self.assertEqual(value['profile']['base_url'], 'https://api.openai.com/v1')
        self.assertNotIn('api_key', value['profile'])
        # The profile exists but the active provider was not changed.
        self.assertEqual(self.store.public_state()['active'], 'env')

    def test_failure_is_reported_without_leaking_details(self):
        with patch('scholarguide.openai_oauth.login', side_effect=GuideError('OpenAI authorization was declined.')):
            status, _ = self.request('POST', '/api/providers/oauth/openai/login',
                                     {'model': 'gpt-test'})
            self.assertEqual(status, 202)
            status, value = self.wait_for('failed')
        self.assertEqual(value['message'], 'OpenAI authorization was declined.')
        self.assertIsNone(value['profile'])
        with patch('scholarguide.openai_oauth.login', side_effect=RuntimeError('token=secret-value')):
            self.request('POST', '/api/providers/oauth/openai/login', {'model': 'gpt-test'})
            status, value = self.wait_for('failed')
        self.assertNotIn('secret-value', json.dumps(value))

    def test_conflicting_reserved_profile_is_refused(self):
        self.store.upsert({'id': 'openai-oauth', 'name': 'Other', 'protocol': 'responses',
                           'base_url': 'https://example.test/v1', 'model': 'm'})
        with patch('scholarguide.openai_oauth.login') as login:
            status, body = self.request('POST', '/api/providers/oauth/openai/login',
                                        {'model': 'gpt-test'})
        self.assertEqual(status, 400)
        login.assert_not_called()


if __name__ == '__main__':
    unittest.main()
