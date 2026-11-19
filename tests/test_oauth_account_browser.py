"""Real DOM checks for the OAuth test panel, with local HTTP fixtures only."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scripts.verify_delivery import apply_browser_isolation, isolated_environment

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {'id': 'openai-oauth', 'name': 'ChatGPT', 'protocol': 'responses',
           'base_url': 'https://api.openai.com/v1', 'model': 'fixture-one',
           'source': 'custom', 'has_key': False, 'max_output_tokens': 3000,
           'input_price': None, 'output_price': None, 'currency': 'USD'}


class Handler(SimpleHTTPRequestHandler):
    calls = []
    probe_ok = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'web'), **kwargs)

    def log_message(self, *_):
        pass

    def reply(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.calls.append(('GET', self.path))
        if self.path == '/api/providers':
            return self.reply({'active': 'env', 'profiles': [PROFILE]})
        if self.path == '/api/providers/oauth/openai':
            # A previous timed-out login does not prove saved credentials expired.
            return self.reply({'state': 'failed', 'message': 'OpenAI login timed out.',
                               'has_profile': True, 'profile': PROFILE, 'running': False})
        if self.path == '/settings':
            self.path = '/settings.html'
        return super().do_GET()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        self.calls.append(('POST', self.path, body))
        if self.path != '/api/providers/oauth/openai/test':
            raise AssertionError('unexpected mutation: ' + self.path)
        return self.reply({'models': [{'slug': 'fixture-one', 'display_name': 'First model'},
                                     {'slug': 'fixture-two', 'display_name': 'Second model <safe>'}],
                           'catalog': {'ok': True, 'http_status': 200},
                           'probe': {'ok': self.probe_ok, 'http_status': 200 if self.probe_ok else 429,
                                     'model': body.get('model'), 'latency_ms': 1200,
                                     'usage': {'input_tokens': 15, 'output_tokens': 5}},
                           'account_usage': {'available': False,
                                             'manage_url': 'https://chatgpt.com/settings/usage'}})


@unittest.skipIf(webdriver is None, 'Selenium unavailable')
class OAuthAccountBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        options = Options()
        for arg in ('--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=1200,1000'):
            options.add_argument(arg)
        apply_browser_isolation(options, Path(cls.tmp.name) / 'profile')
        try:
            with patch.dict(os.environ, isolated_environment(Path(cls.tmp.name)), clear=True):
                cls.driver = webdriver.Chrome(options=options)
        except Exception:
            cls.server.shutdown(); cls.server.server_close(); cls.tmp.cleanup()
            raise
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit(); cls.server.shutdown(); cls.server.server_close()
        cls.thread.join(2); cls.tmp.cleanup()

    def setUp(self):
        Handler.calls.clear()
        Handler.probe_ok = True
        self.driver.set_window_size(1200, 1000)
        self.driver.get(self.base + '/settings')
        WebDriverWait(self.driver, 10).until(lambda d: d.find_element('id', 'oauth-model').get_attribute('value') == 'fixture-one')

    def test_explicit_probe_models_usage_and_selection(self):
        d = self.driver
        self.assertFalse(any(c[0] == 'POST' for c in Handler.calls))
        d.find_element('id', 'oauth-test').click()
        WebDriverWait(d, 10).until(lambda d: bool(d.find_elements('css selector', '.oauth-test-summary')))
        self.assertEqual(d.find_element('id', 'oauth-badge').get_attribute('data-kind'), 'connected')
        self.assertEqual([n.text for n in d.find_elements('css selector', '.oauth-test-summary dd')], ['1.20 s', '15', '5', '200'])
        d.find_element('css selector', '.oauth-model-catalog summary').click()
        choices = d.find_elements('css selector', '.oauth-model-choice')
        self.assertEqual(len(choices), 2)
        self.assertEqual(choices[1].text, 'Second model <safe>')
        choices[1].click()
        self.assertEqual(d.find_element('id', 'oauth-model').get_attribute('value'), 'fixture-two')
        posts = [c for c in Handler.calls if c[0] == 'POST']
        self.assertEqual(posts, [('POST', '/api/providers/oauth/openai/test', {'model': 'fixture-one'})])
        # Selecting a catalog item never saves/activates it or incurs another test.
        self.assertFalse(d.find_element('id', 'memory-section').is_displayed())
        # The preferences panel is deferred from the UI and stays hidden even
        # though its script remains loaded for tests.
        self.assertFalse(d.find_element('id', 'memory-section').is_displayed())
        self.assertEqual(d.find_element('css selector', '.oauth-manage').get_attribute('href'), 'https://chatgpt.com/settings/usage')
        for language in ('en', 'zh-Hant', 'zh-CN'):
            d.execute_script('window.SGI18n.set(arguments[0])', language)
            self.assertTrue(d.find_element('css selector', '.oauth-test-heading strong').text)
        self.assertEqual(len([c for c in Handler.calls if c[0] == 'POST']), 1)

    def test_probe_failure_is_separate_from_catalog_and_mobile_fits(self):
        Handler.probe_ok = False
        d = self.driver
        d.find_element('id', 'oauth-test').click()
        WebDriverWait(d, 10).until(lambda d: bool(d.find_elements('css selector', '.oauth-test-summary')))
        self.assertEqual(d.find_element('css selector', '.oauth-test-heading').get_attribute('data-kind'), 'error')
        self.assertTrue(d.find_element('css selector', '.oauth-model-catalog').is_displayed())
        self.assertFalse(d.find_element('id', 'oauth-test').get_attribute('disabled'))
        for width in (740, 390, 320):
            d.set_window_size(width, 1000)
            self.assertTrue(d.execute_script('return document.documentElement.scrollWidth <= window.innerWidth'), width)


if __name__ == '__main__':
    unittest.main()
