"""Offline browser checks for the search editing safety contract and validation."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_delivery import apply_browser_isolation, _installed_chromedriver

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None


class StaticOnly(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/'):
            self.send_error(403)
            return
        if self.path == '/search':
            self.path = '/index.html'
        super().do_GET()

    def do_POST(self):
        self.send_error(403)

    def log_message(self, *args):
        pass


class SearchResultEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get('SG_REQUIRE_BROWSER') == '1'
        driver = _installed_chromedriver(os.environ)
        if webdriver is None or not driver:
            if required:
                raise RuntimeError('An installed Selenium and ChromeDriver are required; downloads are disabled.')
            raise unittest.SkipTest('Installed Selenium/ChromeDriver unavailable; no downloads attempted.')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(StaticOnly, directory=str(ROOT / 'web')))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        options = Options()
        apply_browser_isolation(options, Path(cls.tmp.name) / 'profile')
        for flag in ('--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=1440,1100'):
            options.add_argument(flag)
        try:
            cls.driver = webdriver.Chrome(service=Service(executable_path=driver), options=options)
        except WebDriverException as exc:
            cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2); cls.tmp.cleanup()
            if required:
                raise RuntimeError('Installed Chrome unavailable') from exc
            raise unittest.SkipTest(str(exc))
        cls.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': '''
          window.__requests=[];
          window.fetch=(url,options={})=>{
            window.__requests.push({url:String(url),method:options.method||'GET'});
            const config={ready:true,provider:'Fixture',model:'none',research_default_source:'multi'};
            return Promise.resolve(new Response(JSON.stringify(String(url)==='/api/config'?config:{error:'blocked'}),
              {status:String(url)==='/api/config'?200:403,headers:{'Content-Type':'application/json'}}));
          };
        '''})

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit(); cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2); cls.tmp.cleanup()

    def setUp(self):
        self.driver.get(self.base + '/search')
        self.driver.execute_script('sessionStorage.clear(); localStorage.clear();')
        self.driver.refresh()
        self.wait = WebDriverWait(self.driver, 10)
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config);'))

    def change(self, identity, value):
        self.driver.execute_script('''
          const field=document.getElementById(arguments[0]); field.value=arguments[1];
          field.dispatchEvent(new Event('input',{bubbles:true}));
        ''', identity, value)

    def assert_no_posts(self):
        self.assertEqual(self.driver.execute_script("return window.__requests.filter(r=>r.method.toUpperCase()!=='GET');"), [])

    def test_edit_clears_result_downloads_and_saved_job_without_submission(self):
        self.change('topic', 'synthetic original')
        self.driver.execute_script('''
          state.job='a'.repeat(32); state.started=Date.now();
          renderResult({mode:'live',status:'complete',request:{topic:'synthetic original'},
            papers:[{id:'fixture',title:'Synthetic paper'}],metrics:{}});
          persistJob('done');
        ''')
        self.assertTrue(self.driver.find_element(By.ID, 'downloads').is_displayed())
        self.change('topic', 'synthetic changed')
        self.assertTrue(self.driver.find_element(By.ID, 'empty-state').is_displayed())
        self.assertFalse(self.driver.find_element(By.ID, 'downloads').is_displayed())
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list article'), [])
        self.assertIsNone(self.driver.execute_script('return state.job;'))
        self.assert_no_posts()
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config);'))
        self.assertEqual(self.driver.find_element(By.ID, 'topic').get_attribute('value'), 'synthetic changed')
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list article'), [])
        self.assert_no_posts()

    def test_year_validation_association_and_visible_status_recover(self):
        self.change('topic', 'synthetic query')
        self.driver.execute_script("document.querySelector('#advanced-settings').open=true;")
        self.change('year-from', '2025')
        self.change('year-to', '2023')
        start = self.driver.find_element(By.ID, 'year-from')
        end = self.driver.find_element(By.ID, 'year-to')
        for field in (start, end):
            self.assertIn('input-feedback', field.get_attribute('aria-describedby').split())
            self.assertIn('scope-help', field.get_attribute('aria-describedby').split())
        feedback = self.driver.find_element(By.ID, 'input-feedback')
        self.assertTrue(feedback.is_displayed())
        self.assertIn('不能晚于', feedback.text)
        self.assertEqual(start.get_attribute('aria-invalid'), 'true')
        self.assertIn('年份条件无效', self.driver.find_element(By.ID, 'search-scope-summary').text)
        self.assertFalse(self.driver.find_element(By.ID, 'submit-button').is_enabled())
        self.change('year-to', '2026')
        self.assertEqual(feedback.text, '')
        self.assertNotEqual(start.get_attribute('aria-invalid'), 'true')
        self.assertIn('2025–2026', self.driver.find_element(By.ID, 'search-scope-summary').text)
        self.change('year-to', '2101')
        self.assertEqual(end.get_attribute('aria-invalid'), 'true')
        self.assertIn('1900–2100', feedback.text)
        self.assert_no_posts()


if __name__ == '__main__':
    unittest.main()
