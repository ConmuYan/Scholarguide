"""Offline settings i18n regression: local fixtures and isolated temporary Chrome."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None

from scripts.verify_delivery import apply_browser_isolation, isolated_environment

ROOT = Path(__file__).resolve().parents[1]


class Handler(SimpleHTTPRequestHandler):
    requests = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'web'), **kwargs)

    def log_message(self, *_args):
        pass

    def do_GET(self):
        Handler.requests.append(('GET', self.path))
        if self.path == '/api/providers':
            return self.reply({'active': 'fixture-id', 'profiles': [{
                'id': 'fixture-id', 'name': 'Server free text', 'protocol': 'responses',
                'base_url': 'https://example.invalid/v1', 'model': 'model-id',
                'has_key': True, 'masked_key': '***123', 'source': 'custom',
                'input_price': 1.25, 'output_price': 2.5, 'currency': 'USD',
                'max_output_tokens': 5000}]})
        if self.path == '/api/v1/memory/preferences':
            return self.reply({'api_version': 'v1', 'store_id': 'a' * 64, 'preferences': {
                'reading_goal': {'state': 'active', 'consent': True, 'expires_at': 4102444800,
                                 'revision': 2, 'value': 'frontier'}}})
        if self.path == '/settings':
            self.path = '/settings.html'
        return super().do_GET()

    def reply(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        Handler.requests.append(('POST', self.path))
        self.reply({})

    def do_DELETE(self):
        Handler.requests.append(('DELETE', self.path))
        self.reply({})


class ModelsI18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get('SG_REQUIRE_BROWSER') == '1'
        if webdriver is None:
            if required:
                raise RuntimeError('Required Selenium unavailable')
            raise unittest.SkipTest('Selenium unavailable')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        options = Options()
        for arg in ('--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=1280,1000'):
            options.add_argument(arg)
        apply_browser_isolation(options, Path(cls.tmp.name) / 'profile')
        try:
            with patch.dict(os.environ, isolated_environment(Path(cls.tmp.name)), clear=True):
                cls.driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3); cls.tmp.cleanup()
            if required:
                raise RuntimeError('Required isolated Chrome unavailable') from exc
            raise unittest.SkipTest('Isolated Chrome unavailable')

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3); cls.tmp.cleanup()

    def test_switch_preserves_all_inputs_and_makes_no_requests(self):
        d = self.driver
        d.get(self.base + '/settings')
        WebDriverWait(d, 10).until(lambda x: x.execute_script(
            "return document.querySelector('#provider-id').value === 'fixture-id' && !document.querySelector('#memory-reading-value').disabled"))
        d.execute_script('''
          const set = (id, value) => {const n = document.getElementById(id); n.value = value; n.dispatchEvent(new Event('input', {bubbles:true}));};
          set('provider-name', 'unsaved name'); set('provider-key', 'secret-value');
          set('provider-input-price', '3.75'); set('provider-output-price', '9.25');
          set('memory-reading-value', 'comparison'); set('memory-reading-expiry', '2031-06-01T12:00');
          document.querySelector('[data-key=reading_goal] [name=consent]').checked = true;
          document.getElementById('provider-activate').checked = false;
          lastTestResult = {ok:false, status:'auth', error:'Server error: 保持原文', protocol:'responses'};
          renderTestResult(lastTestResult);
          setStatus('Server message: 保持原文', 'error');
          document.getElementById('provider-key').focus();
          window.snapshotInputs = () => [...document.querySelectorAll('#provider-form input, #provider-form select, .memory-item input, .memory-item select')].map(n => [n.id || n.name, n.value, n.checked]);
        ''')
        before = d.execute_script('return window.snapshotInputs()')
        requests = list(Handler.requests)
        for lang, title, save, status in [
            ('en', 'Connect your research models.', 'Save profile', 'Authorized'),
            ('zh-Hant', '連接你的研究模型。', '儲存設定', '已授權'),
            ('zh-CN', '连接你的研究模型。', '保存配置', '已授权'),
        ]:
            d.execute_script('window.SGI18n.set(arguments[0])', lang)
            self.assertEqual(d.find_element('css selector', '.page-title').text, title)
            self.assertEqual(d.find_element('id', 'save-provider').text, save)
            self.assertIn(status, d.find_element('css selector', '[data-key=reading_goal] .memory-current').text)
            self.assertEqual(d.find_element('id', 'editor-title').text, 'Server free text')
            self.assertEqual(d.find_element('id', 'settings-status').text, 'Server message: 保持原文')
            self.assertEqual(d.find_element('css selector', '.test-error').text, 'Server error: 保持原文')
            self.assertEqual(d.execute_script('return window.snapshotInputs()'), before)
            self.assertEqual(d.execute_script('return document.activeElement.id'), 'provider-key')
        self.assertEqual(Handler.requests, requests)

    def test_page_dictionary_markers_are_complete(self):
        d = self.driver
        d.get(self.base + '/settings')
        result = d.execute_script('''
          const dict = JSON.parse(document.getElementById('sg-page-i18n').textContent);
          return [...document.querySelectorAll('[data-i18n], [data-i18n-placeholder], [data-i18n-aria-label], [data-i18n-title], [data-i18n-alt]')]
            .flatMap(e => [...e.attributes].filter(a => a.name.startsWith('data-i18n')).map(a => a.value))
            .filter(k => !dict[k] || typeof dict[k].en !== 'string' || typeof dict[k]['zh-Hant'] !== 'string');
        ''')
        self.assertEqual(result, [])


if __name__ == '__main__':
    unittest.main()
