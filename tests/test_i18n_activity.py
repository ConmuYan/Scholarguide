"""Offline activity localization and language-switch safety."""
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / 'web'


class ActivityHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.markers = []
        self.tags = []
        self.missing = []
        self.script = False
        self.dictionary = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('id') == 'sg-page-i18n':
            self.script = True
        if tag not in ('input', 'meta', 'link', 'img', 'br', 'hr'):
            self.tags.append((tag, attrs))
        self.markers.extend(v for k, v in attrs.items() if k.startswith('data-i18n'))
        for name in ('aria-label', 'title', 'alt', 'placeholder'):
            if re.search(r'[\u4e00-\u9fff]', attrs.get(name, '')) and 'data-i18n-' + name not in attrs:
                self.missing.append((tag, name, attrs[name]))

    def handle_endtag(self, tag):
        if tag == 'script':
            self.script = False
        if self.tags and self.tags[-1][0] == tag:
            self.tags.pop()

    def handle_data(self, data):
        if self.script:
            self.dictionary += data
        elif re.search(r'[\u4e00-\u9fff]', data) and self.tags and 'data-i18n' not in self.tags[-1][1]:
            self.missing.append((self.tags[-1][0], data.strip()))


class ActivityI18nTests(unittest.TestCase):
    def test_all_static_chinese_is_marked_and_translated(self):
        parser = ActivityHTML()
        parser.feed((WEB / 'activity.html').read_text(encoding='utf-8'))
        dictionary = json.loads(parser.dictionary)
        self.assertEqual(parser.missing, [])
        for key in parser.markers:
            self.assertIn(key, dictionary)
            self.assertTrue(dictionary[key].get('en'), key)
            self.assertTrue(dictionary[key].get('zh-Hant'), key)

    def test_switch_and_delete_guards(self):
        js = (WEB / 'activity.js').read_text(encoding='utf-8')
        callback = js.split('window.addEventListener("sg:language-change", () => {', 1)[1].split('\n});', 1)[0]
        self.assertIn('renderList(false)', callback)
        for forbidden in ('fetch(', 'select(', 'stopFollow(', 'configureDelete('):
            self.assertNotIn(forbidden, callback)
        self.assertIn("window.confirm(t('删除这条本地记录？", js)
        self.assertIn('deletingRecords.has(key)', js)
        self.assertIn('data-i18n', (WEB / 'activity.html').read_text(encoding='utf-8'))


class BrowserActivityI18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import http.server
        import threading
        import tempfile
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        class Handler(http.server.BaseHTTPRequestHandler):
            calls = []
            def do_GET(self):
                from urllib.parse import urlsplit
                path = urlsplit(self.path).path
                if path.startswith('/api/'):
                    self.calls.append(path)
                    if path == '/api/activity':
                        data = {'sessions': [{'id': 'abc', 'kind': 'session', 'title': '用户标题', 'status': 'finished', 'turns': 1, 'events': 1, 'revision': 1}], 'runs': []}
                    else:
                        data = {'id': 'abc', 'kind': 'session', 'session': {'title': '用户标题', 'status': 'finished'}, 'task': {}, 'cost': {'settled': 0}, 'history': [], 'events': [{'action': 'task_initial', 'seq': 1, 'payload': {'message': '原始中文'}}]}
                    content = json.dumps(data).encode()
                    mime = 'application/json'
                else:
                    filename = 'activity.html' if path == '/' else path.lstrip('/')
                    file = WEB / filename
                    if not file.is_file():
                        self.send_error(404); return
                    content = file.read_bytes()
                    mime = 'text/html' if filename.endswith('.html') else 'text/javascript' if filename.endswith('.js') else 'text/css'
                self.send_response(200); self.send_header('Content-Type', mime + '; charset=utf-8'); self.end_headers(); self.wfile.write(content)
            def log_message(self, *args): pass
        cls.handler = Handler
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.temp = tempfile.TemporaryDirectory()
        options = Options()
        for arg in ('--headless=new', '--no-sandbox', '--disable-gpu', '--no-proxy-server', '--user-data-dir=' + cls.temp.name): options.add_argument(arg)
        cls.driver = webdriver.Chrome(options=options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit(); cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.temp.cleanup()

    def test_switch_keeps_record_raw_and_delete_state_without_requests(self):
        from selenium.webdriver.support.ui import WebDriverWait
        d = self.driver
        d.get('http://127.0.0.1:' + str(self.server.server_port) + '/')
        WebDriverWait(d, 10).until(lambda driver: driver.execute_script('return !document.getElementById("activity-detail").hidden'))
        before = len(self.handler.calls)
        d.execute_script('''
          window.originalRaw = document.querySelector('#activity-timeline pre');
          document.querySelector('#activity-timeline details').open = true;
          document.querySelector('#activity-delete').focus();
          window.confirmText = ''; window.confirm = text => { window.confirmText = text; return false; };
        ''')
        for lang, title, button in [('en', 'Research activity.', 'Delete local session'), ('zh-Hant', '研究活動記錄。', '刪除本地會話'), ('zh-CN', '研究活动记录。', '删除本机会话')]:
            d.execute_script('SGI18n.set(arguments[0])', lang)
            self.assertEqual(d.find_element('css selector', '.page-title').text, title)
            self.assertEqual(d.find_element('id', 'activity-delete').text, button)
            self.assertEqual(d.find_element('id', 'activity-title').text, '用户标题')
            self.assertTrue(d.execute_script('return originalRaw === document.querySelector("#activity-timeline pre") && document.querySelector("#activity-timeline details").open && document.activeElement.id === "activity-delete"'))
        d.execute_script("SGI18n.set('en'); document.getElementById('activity-delete').click()")
        self.assertIn('Delete this local record?', d.execute_script('return confirmText'))
        self.assertEqual(len(self.handler.calls), before)


if __name__ == '__main__':
    unittest.main()
