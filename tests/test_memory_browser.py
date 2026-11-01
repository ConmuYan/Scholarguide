"""Isolated browser checks for local memory preferences; no application service."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import tempfile
import threading
import time
import unittest

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from scripts.verify_delivery import apply_browser_isolation

ROOT = Path(__file__).resolve().parents[1]


class Fixture(BaseHTTPRequestHandler):
    records = {}
    requests = []
    conflict = False
    store_id = 'a' * 64

    def log_message(self, *args):
        pass

    def reply(self, data, status=200, content_type='application/json'):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/api/v1/memory/preferences':
            self.requests.append(('GET', self.path, None))
            return self.reply({'api_version':'v1', 'preferences':self.records, 'active':{}, 'store_id': self.store_id})
        if self.path == '/api/providers':
            return self.reply({'active':'', 'profiles':[]})
        name = 'settings.html' if self.path == '/settings' else self.path.lstrip('/')
        if name not in ('settings.html', 'settings.js', 'style.css', 'theme.js', 'memory.js', 'memory.css'):
            return self.reply({}, 404)
        path = ROOT / 'web' / name
        if not path.exists():
            return self.reply({}, 404)
        return self.reply(path.read_bytes(), content_type={'html':'text/html; charset=utf-8', 'js':'application/javascript', 'css':'text/css'}[name.rsplit('.', 1)[1]])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(('POST', self.path, body))
        parts = self.path.split('/')
        key = parts[5]
        current = self.records.get(key, {})
        if self.conflict or body.get('store_id') != self.store_id or body['expected_revision'] != current.get('revision', 0):
            type(self).conflict = False
            return self.reply({'error':'fixture conflict'}, 409)
        revision = current.get('revision', 0) + 1
        action = parts[6] if len(parts) > 6 else 'grant'
        self.records[key] = {'revision':revision, 'state':{'grant':'active','revoke':'revoked','delete':'deleted'}[action],
                             'value':body.get('value'), 'consent':action == 'grant', 'expires_at':body.get('expires_at')}
        return self.reply({'api_version':'v1'})


class MemoryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        options = Options()
        apply_browser_isolation(options, Path(cls.tmp.name) / 'chrome')
        for arg in ('--headless=new', '--no-sandbox', '--disable-gpu'):
            options.add_argument(arg)
        try:
            cls.driver = webdriver.Chrome(options=options)
        except Exception:
            cls.server.shutdown(); cls.server.server_close(); cls.tmp.cleanup()
            raise
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit(); cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2); cls.tmp.cleanup()

    def setUp(self):
        Fixture.records = {}; Fixture.requests = []; Fixture.conflict = False; Fixture.store_id = 'a' * 64
        self.driver.set_window_size(1280, 1000)
        self.driver.get(self.base + '/settings')
        self.wait = WebDriverWait(self.driver, 5)

    def ready(self):
        self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] select').is_enabled())

    def click(self, selector):
        self.driver.execute_script('arguments[0].click()', self.driver.find_element(By.CSS_SELECTOR, selector))

    def draft(self, key, value, expiry=None, consent=True):
        self.driver.execute_script('''
          const f = document.querySelector(`[data-key="${arguments[0]}"]`);
          f.elements.value.value = arguments[1];
          f.elements.expiry.value = arguments[2] || new Date(Date.now() + 86400000 - new Date().getTimezoneOffset()*60000).toISOString().slice(0,16);
          f.elements.consent.checked = arguments[3];
        ''', key, value, expiry, consent)

    def save(self, key):
        self.click(f'[data-key="{key}"] button[type="submit"]')

    def wait_status(self, text):
        self.wait.until(lambda d: text in d.find_element(By.ID, 'memory-status').text)

    def test_initial_section_exists_and_never_posts(self):
        self.assertTrue(self.driver.find_element(By.ID, 'memory-section').is_displayed())
        self.ready()
        self.assertFalse(any(r[0] == 'POST' for r in Fixture.requests))
        self.assertIn('未启用', self.driver.find_element(By.ID, 'memory-section').text)
        self.assertLessEqual(self.driver.execute_script('return document.documentElement.scrollWidth'), self.driver.execute_script('return document.documentElement.clientWidth'))

    def test_grant_requires_consent_future_expiry_and_correct_typed_cas(self):
        self.ready()
        self.draft('prefer_recent', 'false', consent=False)
        self.save('prefer_recent')
        self.assertFalse(any(r[0] == 'POST' for r in Fixture.requests))
        self.draft('prefer_recent', 'false', expiry='2020-01-01T12:00')
        self.save('prefer_recent')
        self.assertFalse(any(r[0] == 'POST' for r in Fixture.requests))
        self.draft('prefer_recent', 'false')
        self.save('prefer_recent')
        self.wait_status('已更新')
        posts = [r for r in Fixture.requests if r[0] == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertEqual(set(posts[0][2]), {'value','consent','expires_at','expected_revision','store_id'})
        self.assertIs(posts[0][2]['value'], False)
        self.assertIs(posts[0][2]['consent'], True)
        self.assertEqual(posts[0][2]['expected_revision'], 0)
        self.assertGreater(posts[0][2]['expires_at'], time.time())
        self.assertIn('已授权', self.driver.find_element(By.CSS_SELECTOR, '[data-key="prefer_recent"] .memory-current').text)
        self.draft('prefer_recent', 'true')
        self.save('prefer_recent')
        self.wait.until(lambda d: len([r for r in Fixture.requests if r[0] == 'POST']) == 2)
        self.wait_status('已更新')
        self.assertEqual([r for r in Fixture.requests if r[0] == 'POST'][-1][2]['expected_revision'], 1)

    def test_revoke_delete_confirm_and_preserve_other_draft(self):
        self.ready()
        self.draft('reading_goal', 'frontier')
        self.draft('prefer_recent', 'true')
        self.save('reading_goal')
        self.wait_status('已更新')
        self.assertEqual(self.driver.execute_script('return document.querySelector("[data-key=prefer_recent]").elements.value.value'), 'true')
        self.click('[data-key="reading_goal"] [data-action="revoke"]')
        self.driver.switch_to.alert.dismiss()
        self.assertEqual(len([r for r in Fixture.requests if r[0] == 'POST']), 1)
        self.click('[data-key="reading_goal"] [data-action="revoke"]')
        self.driver.switch_to.alert.accept()
        self.wait.until(lambda d: '已撤回' in d.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] .memory-current').text)
        self.assertEqual([r for r in Fixture.requests if r[0] == 'POST'][-1][2], {'expected_revision':1, 'store_id': Fixture.store_id})
        self.click('[data-key="reading_goal"] [data-action="delete"]')
        self.assertIn('历史', self.driver.switch_to.alert.text)
        self.driver.switch_to.alert.accept()
        self.wait.until(lambda d: '已删除' in d.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] .memory-current').text)
        self.assertEqual([r for r in Fixture.requests if r[0] == 'POST'][-1][2], {'expected_revision':2, 'store_id': Fixture.store_id})
        self.assertFalse(self.driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] [data-action="delete"]').is_enabled())
        self.assertEqual(self.driver.execute_script('return document.querySelector("[data-key=prefer_recent]").elements.value.value'), 'true')

    def test_conflict_refresh_without_replay_and_untrusted_data_is_not_rendered(self):
        self.ready()
        Fixture.records['reading_goal'] = {'revision':4, 'state':'revoked', 'value':'<img src=x onerror=alert(1)>', 'consent':False, 'expires_at':None}
        self.draft('reading_goal', 'comparison')
        self.save('reading_goal')
        self.wait_status('版本冲突')
        self.assertEqual(len([r for r in Fixture.requests if r[0] == 'POST']), 1)
        self.assertIn('已撤回', self.driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] .memory-current').text)
        self.assertNotIn('<img', self.driver.page_source)
        self.assertNotIn('secret', self.driver.find_element(By.ID, 'memory-section').text.lower())

    def test_two_tabs_replacement_conflicts_without_replay(self):
        self.ready()
        first = self.driver.current_window_handle
        self.driver.switch_to.new_window('tab')
        second = self.driver.current_window_handle
        self.driver.get(self.base + '/settings')
        self.ready()
        Fixture.store_id = 'b' * 64
        self.driver.switch_to.window(first)
        self.draft('reading_goal', 'general')
        self.save('reading_goal')
        self.wait_status('版本冲突')
        posts = [r for r in Fixture.requests if r[0] == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]['store_id'], 'a' * 64)
        self.assertEqual(Fixture.records, {})
        self.driver.switch_to.window(second)
        self.driver.close()
        self.driver.switch_to.window(first)

    def test_delayed_mutation_response_uses_refreshed_revoked_state(self):
        self.ready()
        self.driver.execute_script('''
          const original = window.fetch.bind(window);
          window.fetch = (...args) => {
            if (String(args[0]) === '/api/v1/memory/preferences/reading_goal' && args[1]?.method === 'POST') {
              return new Promise(resolve => window.__release = () => resolve(new Response('{}', {status:200})));
            }
            return original(...args);
          };
        ''')
        self.draft('reading_goal', 'general')
        self.save('reading_goal')
        self.wait.until(lambda d: d.execute_script('return typeof window.__release === "function"'))
        self.assertFalse(self.driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] button[type="submit"]').is_enabled())
        Fixture.records['reading_goal'] = {'revision':2, 'state':'revoked', 'value':None, 'consent':False, 'expires_at':None}
        self.driver.execute_script('window.__release()')
        self.wait_status('已更新')
        self.assertIn('已撤回', self.driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] .memory-current').text)
        self.assertNotIn('已授权', self.driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] .memory-current').text)

    def test_invalid_tampered_value_and_untrusted_error_never_leak(self):
        self.ready()
        self.driver.execute_script('''
          const f = document.querySelector('[data-key="reading_goal"]');
          f.elements.value.append(new Option('invalid', '<img src=x onerror=alert(1)>'));
        ''')
        self.draft('reading_goal', '<img src=x onerror=alert(1)>')
        self.save('reading_goal')
        self.assertFalse(any(r[0] == 'POST' for r in Fixture.requests))
        self.driver.execute_script('''
          const original = window.fetch.bind(window);
          window.fetch = (...args) => String(args[0]).endsWith('/reading_goal') && args[1]?.method === 'POST'
            ? Promise.resolve(new Response(JSON.stringify({error:'secret-fixture-value <img src=x>'}), {status:400}))
            : original(...args);
        ''')
        self.draft('reading_goal', 'orientation')
        self.save('reading_goal')
        self.wait_status('操作结果未知')
        self.assertNotIn('secret-fixture-value', self.driver.find_element(By.ID, 'memory-section').text)
        self.assertFalse(self.driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] button[type="submit"]').is_enabled())

    def test_expiry_updates_without_reload_and_nonconsent_is_not_authorized(self):
        Fixture.records['reading_goal'] = {'revision':3,'state':'active','value':'general','consent':True,'expires_at':time.time() + 2}
        Fixture.records['prefer_recent'] = {'revision':2,'state':'active','value':True,'consent':False,'expires_at':time.time() + 100}
        self.driver.refresh()
        self.ready()
        self.assertIn('未授权', self.driver.find_element(By.CSS_SELECTOR, '[data-key="prefer_recent"] .memory-current').text)
        self.wait.until(lambda d: '已过期' in d.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] .memory-current').text)
        self.assertFalse(self.driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] [data-action="revoke"]').is_enabled())

    def test_expired_and_mobile_layout(self):
        Fixture.records['reading_goal'] = {'revision':3,'state':'active','value':'general','consent':True,'expires_at':1}
        self.driver.set_window_size(390, 900)
        self.driver.refresh()
        self.ready()
        self.assertIn('已过期', self.driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] .memory-current').text)
        self.assertFalse(self.driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"] [data-action="revoke"]').is_enabled())
        self.assertLessEqual(self.driver.execute_script('return document.documentElement.scrollWidth'), self.driver.execute_script('return document.documentElement.clientWidth'))


if __name__ == '__main__':
    unittest.main()
