"""Offline i18n contract, served on a temporary loopback-only origin."""
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

WEB = Path(__file__).resolve().parents[1] / 'web'
HTML = '''<!doctype html><html lang="zh-CN"><head><link rel="stylesheet" href="/style.css">
<script type="application/json" id="sg-page-i18n">{"你好":{"en":"Hello","zh-Hant":"你好繁"},"提示":{"en":"Hint","zh-Hant":"提示繁"}}</script>
<script defer src="/theme.js"></script><script defer src="/later.js"></script></head><body>
<div class="topbar"><button id="theme-toggle"></button></div>
<button id="menu-toggle"></button><nav id="sidebar"><a href="#">link</a></nav>
<p id="text" data-i18n="你好">你好</p><p id="nested" data-i18n="你好">keep <b>child</b></p>
<input id="input" value="research data" data-i18n="你好" data-i18n-placeholder="提示" data-i18n-title="提示" data-i18n-aria-label="提示" data-i18n-alt="提示">
<p id="unmarked">你好</p></body></html>'''


class Handler(http.server.BaseHTTPRequestHandler):
    requests = []
    def do_GET(self):
        self.requests.append(self.path)
        if self.path == '/':
            data, mime = HTML.encode(), 'text/html; charset=utf-8'
        elif self.path == '/theme.js':
            data, mime = (WEB / 'theme.js').read_bytes(), 'text/javascript; charset=utf-8'
        elif self.path == '/later.js':
            data, mime = b'window.earlyTranslation=SGI18n.t("\\u4f60\\u597d");', 'text/javascript'
        elif self.path == '/style.css':
            data, mime = (WEB / 'style.css').read_bytes(), 'text/css'
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *args):
        pass


class I18nCore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        opts = Options()
        for arg in ('--headless=new', '--no-sandbox', '--disable-gpu', '--no-proxy-server',
                    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
                    '--user-data-dir=' + str(Path(cls.temp.name) / 'profile')):
            opts.add_argument(arg)
        cls.driver = webdriver.Chrome(options=opts)
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port) + '/'

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit(); cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.temp.cleanup()

    def setUp(self):
        self.driver.get(self.base)
        self.driver.execute_script('localStorage.clear(); SGI18n.set("zh-CN")')

    def js(self, code):
        return self.driver.execute_script(code)

    def test_early_registration_and_persistence(self):
        self.js('SGI18n.set("en")')
        self.driver.refresh()
        self.assertEqual(self.js('return [earlyTranslation, SGI18n.get(), document.documentElement.lang]'), ['Hello', 'en', 'en'])
        self.assertEqual(self.js('return [SGI18n.t("missing"), SGI18n.t("主题：{mode}",{mode:"X"})]'), ['missing', 'Theme: X'])
        self.assertFalse(self.js('return SGI18n.set("fr")'))
        self.assertEqual(self.js('return SGI18n.get()'), 'en')

    def test_attributes_focus_data_and_no_network(self):
        before = self.js("return performance.getEntriesByType('resource').length")
        result = self.js('''
          let count=0; window.fetch=()=>{count++; throw Error('network')};
          const input=document.querySelector('#input'); input.focus(); input.setSelectionRange(2,5);
          const child=document.querySelector('#nested b'); let events=[];
          window.addEventListener('sg:language-change',e=>events.push(e.detail.lang));
          SGI18n.set('en'); const attrs=['placeholder','title','aria-label','alt'].map(a=>input.getAttribute(a));
          return {attrs, value:input.value, focus:document.activeElement===input, selection:[input.selectionStart,input.selectionEnd],
            child:child===document.querySelector('#nested b'), nested:document.querySelector('#nested').textContent,
            text:document.querySelector('#text').textContent, unmarked:document.querySelector('#unmarked').textContent,
            count, events, lang:document.documentElement.lang};''')
        self.assertEqual(result, {'attrs':['Hint']*4, 'value':'research data', 'focus':True, 'selection':[2,5],
                                  'child':True, 'nested':'keep child', 'text':'Hello', 'unmarked':'你好',
                                  'count':0, 'events':['en'], 'lang':'en'})
        self.assertEqual(self.js("return performance.getEntriesByType('resource').length"), before)
        self.js("SGI18n.register({'局部':{en:'Local','zh-Hant':'局部繁'}}); let p=document.createElement('p'); p.id='local'; p.dataset.i18n='局部'; p.append('局部'); document.body.append(p); SGI18n.apply(p)")
        self.assertEqual(self.js("return document.querySelector('#local').textContent"), 'Local')

    def test_cycle_button_keyboard_and_persistence(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        button = self.driver.find_element(By.ID, 'sg-language-toggle')
        self.assertEqual(button.tag_name, 'button')
        self.assertTrue(self.js("return document.querySelector('#sg-language-toggle svg') !== null"))
        self.assertFalse(self.js("return !!document.querySelector('#sg-language-select')"))
        before = self.js("return performance.getEntriesByType('resource').length")
        button.send_keys(Keys.ENTER)
        self.assertEqual(self.js('return SGI18n.get()'), 'zh-Hant')
        self.assertEqual(button.get_attribute('data-language'), 'zh-Hant')
        self.assertEqual(button.find_element(By.CSS_SELECTOR, '.sg-language-badge').text, '繁')
        button.send_keys(Keys.SPACE)
        self.assertEqual(self.js('return SGI18n.get()'), 'en')
        self.assertEqual(button.find_element(By.CSS_SELECTOR, '.sg-language-badge').text, 'EN')
        self.assertIn('switch to 简体中文', button.get_attribute('aria-label'))
        self.driver.refresh()
        self.assertEqual(self.js('return SGI18n.get()'), 'en')
        self.driver.find_element(By.ID, 'sg-language-toggle').click()
        self.assertEqual(self.js('return SGI18n.get()'), 'zh-CN')
        self.assertEqual(self.js("return document.querySelector('#sg-language-toggle').previousElementSibling.id"), 'theme-toggle')

    def test_cross_tab_storage(self):
        first = self.driver.current_window_handle
        self.driver.switch_to.new_window('tab')
        second = self.driver.current_window_handle
        try:
            self.driver.get(self.base)
            self.js("SGI18n.set('en')")
            self.driver.switch_to.window(first)
            from selenium.webdriver.support.ui import WebDriverWait
            WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return SGI18n.get() === 'en'"))
            self.assertEqual(self.js("return document.documentElement.lang"), 'en')
        finally:
            self.driver.switch_to.window(second); self.driver.close(); self.driver.switch_to.window(first)

    def test_storage_event_and_failure(self):
        self.js("window.dispatchEvent(new StorageEvent('storage',{key:'scholarguide-language',newValue:'zh-Hant'}))")
        self.assertEqual(self.js("return [SGI18n.get(),document.querySelector('#text').textContent]"), ['zh-Hant','你好繁'])
        self.js("window.dispatchEvent(new StorageEvent('storage',{key:'scholarguide-language',newValue:'bad'}))")
        self.assertEqual(self.js('return SGI18n.get()'), 'zh-Hant')
        self.js("Object.defineProperty(window,'localStorage',{configurable:true,get(){throw Error('denied')}}); SGI18n.set('en'); document.querySelector('#theme-toggle').click();")
        self.assertEqual(self.js("return [SGI18n.get(),document.documentElement.lang,document.querySelector('#theme-toggle').getAttribute('aria-label')]"), ['en','en','Theme: Light (click to change)'])

    def test_mobile_theme_navigation(self):
        self.driver.set_window_size(320, 800)
        for theme in ('light', 'dark'):
            result = self.js("""document.documentElement.dataset.theme=arguments[0]; SGI18n.set('en');
              document.querySelector('#menu-toggle').click();
              const result=[document.querySelector('#menu-toggle').getAttribute('aria-label'), document.querySelector('#menu-toggle').getAttribute('aria-expanded')];
              document.querySelector('#menu-toggle').click(); return result;""".replace('arguments[0]', repr(theme)))
            self.assertEqual(result, ['Close navigation','true'])
            self.assertLessEqual(self.js('return document.documentElement.scrollWidth - document.documentElement.clientWidth'), 1)


if __name__ == '__main__':
    unittest.main()
