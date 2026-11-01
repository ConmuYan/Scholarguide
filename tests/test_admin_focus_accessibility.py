"""Keyboard focus regressions against static assets and a synthetic loopback API."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_delivery import apply_browser_isolation, isolated_environment

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None

SOURCES = [dict(name='alpha', label='Alpha', enabled=True, configured=True,
                priority=1, capabilities={'abstract': True}),
           dict(name='beta', label='Beta', enabled=True, configured=True,
                priority=2, capabilities={'publication': True})]
PROFILES = [dict(id=name, name=name.title(), protocol='chat_completions',
                 base_url='http://127.0.0.1:1', model='fixture', currency='USD',
                 max_output_tokens=1000, has_key=False) for name in ('one', 'two')]


class Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.requests.append(('GET', self.path))
        data = {'/api/sources': {'sources': SOURCES, 'routing': [], 'cooling_down': []},
                '/api/sources/lune/skills': {'skills': []},
                '/api/sources/lune/quota': {'snapshot_present': False},
                '/api/providers': {'active': 'one', 'profiles': PROFILES}}
        if self.path in data:
            body = json.dumps(data[self.path]).encode()
            content_type = 'application/json'
        else:
            name = {'/sources': 'sources.html', '/settings': 'settings.html'}.get(self.path, self.path.lstrip('/'))
            if name not in {'sources.html', 'settings.html', 'sources.js', 'settings.js',
                            'style.css', 'sources.css', 'memory.css', 'memory.js', 'theme.js'}:
                self.send_error(404)
                return
            body = (ROOT / 'web' / name).read_bytes()
            content_type = 'text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript'
        self.send_response(200)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.requests.append(('POST', self.path))
        self.send_error(405)


class AdminFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if webdriver is None:
            if os.environ.get('SG_REQUIRE_BROWSER') == '1':
                raise RuntimeError('Selenium required')
            raise unittest.SkipTest('Selenium unavailable')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = isolated_environment(Path(cls.tmp.name))
        driver_path = cls.env.get('SE_CHROMEDRIVER')
        if not driver_path:
            cls.tmp.cleanup()
            if os.environ.get('SG_REQUIRE_BROWSER') == '1':
                raise RuntimeError('Installed chromedriver required; downloads disabled')
            raise unittest.SkipTest('Installed chromedriver unavailable; downloads disabled')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        while cls.server.server_port == 8765:
            cls.server.server_close()
            cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        opts = Options()
        apply_browser_isolation(opts, Path(cls.tmp.name) / 'profile')
        for flag in ('--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=1000,650'):
            opts.add_argument(flag)
        try:
            with patch.dict(os.environ, cls.env, clear=True):
                cls.driver = webdriver.Chrome(service=Service(driver_path, env=cls.env), options=opts)
        except WebDriverException as exc:
            cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3); cls.tmp.cleanup()
            if os.environ.get('SG_REQUIRE_BROWSER') == '1':
                raise RuntimeError('Isolated Chrome required') from exc
            raise unittest.SkipTest('Isolated Chrome unavailable') from exc
        cls.wait = WebDriverWait(cls.driver, 10)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3)
        cls.tmp.cleanup()

    def setUp(self):
        Handler.requests.clear()

    def tearDown(self):
        self.assertFalse([r for r in Handler.requests if r[0] != 'GET'])

    def open(self, path, selector):
        self.driver.get(self.base + path)
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, selector))

    def focus(self, selector):
        node = self.driver.find_element(By.CSS_SELECTOR, selector)
        self.driver.execute_script('arguments[0].focus()', node)
        return node

    def active(self, selector):
        return self.driver.execute_script('return document.activeElement.matches(arguments[0])', selector)

    def test_filter_enter_space_and_tab(self):
        self.open('/sources', '#capability-filter button[data-filter="摘要"]')
        self.focus('#capability-filter button[data-filter="摘要"]').send_keys(Keys.ENTER)
        self.assertTrue(self.active('#capability-filter button[data-filter="摘要"][aria-pressed="true"]'))
        self.assertIn('1 / 2', self.driver.find_element(By.ID, 'source-count').text)
        self.driver.switch_to.active_element.send_keys(Keys.SPACE)
        self.assertTrue(self.active('#capability-filter button[data-filter="摘要"]'))
        self.driver.switch_to.active_element.send_keys(Keys.TAB)
        self.assertTrue(self.active('#capability-filter button[data-filter="出版信息"]'))
        self.focus('#probe-query')
        self.driver.execute_script('renderFilters()')
        self.assertTrue(self.active('#probe-query'))

    def test_profile_selection_retains_focus_and_tab_order(self):
        self.open('/settings', '#provider-list button[data-profile-id="two"]')
        self.focus('#provider-list button[data-profile-id="two"]').send_keys(Keys.SPACE)
        self.assertTrue(self.active('#provider-list button[data-profile-id="two"][aria-pressed="true"]'))
        self.assertEqual(self.driver.find_element(By.ID, 'provider-name').get_attribute('value'), 'Two')
        self.driver.switch_to.active_element.send_keys(Keys.SHIFT, Keys.TAB)
        self.assertTrue(self.active('#provider-list button[data-profile-id="one"]'))
        self.driver.switch_to.active_element.send_keys(Keys.ENTER)
        self.assertTrue(self.active('#provider-list button[data-profile-id="one"][aria-pressed="true"]'))
        self.focus('#provider-name')
        self.driver.execute_script('renderList()')
        self.assertTrue(self.active('#provider-name'))

    def test_editor_focus_scroll_and_rebuilt_opener(self):
        self.open('/sources', '.source-row[data-name="alpha"] .source-actions button:nth-child(2)')
        selector = '.source-row[data-name="alpha"] .source-actions button:nth-child(2)'
        self.focus(selector).send_keys(Keys.ENTER)
        self.assertTrue(self.active('#source-name'))
        self.wait.until(lambda d: d.execute_script('const r=document.activeElement.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight'))
        self.driver.switch_to.active_element.send_keys(Keys.TAB)
        self.assertTrue(self.active('#source-priority'))
        self.driver.execute_script('renderSources()')
        self.focus('#cancel-edit').send_keys(Keys.SPACE)
        self.assertTrue(self.active(selector))
        self.assertFalse(self.driver.find_element(By.ID, 'source-editor-panel').is_displayed())

    def test_editor_return_fallback_and_hero_opener(self):
        self.open('/sources', '.source-row[data-name="alpha"] .source-actions button:nth-child(2)')
        self.focus('.source-row[data-name="alpha"] .source-actions button:nth-child(2)').send_keys(Keys.SPACE)
        self.driver.execute_script("state.sources = state.sources.filter(s => s.name !== 'alpha'); renderSources()")
        self.focus('#close-editor').send_keys(Keys.ENTER)
        self.assertTrue(self.active('#new-source'))
        self.driver.switch_to.active_element.send_keys(Keys.ENTER)
        self.assertTrue(self.active('#source-name'))
        self.focus('#close-editor').send_keys(Keys.ENTER)
        self.assertTrue(self.active('#new-source'))


if __name__ == '__main__':
    unittest.main()
