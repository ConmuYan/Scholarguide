"""Deletion UI checks: static-only ephemeral server and fully mocked fetch.

No application server, persistent fixture data, or external network is used.
"""
import functools
import http.server
import os
from pathlib import Path
import tempfile
import threading
import unittest

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

WEB = Path(__file__).resolve().parents[1] / 'web'


class StaticOnly(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def do_GET(self):
        if self.path.startswith('/api/'):
            self.send_error(403)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


MOCK = r"""
window.__calls = []; window.__confirm = true; window.__deleteStatus = 503;
window.__hold = false; window.__refreshFail = false;
window.confirm = () => window.__confirm;
window.fetch = async (path, opts = {}) => {
  const method = opts.method || 'GET'; path = String(path);
  window.__calls.push([method, path]);
  const reply = (status, data) => new Response(JSON.stringify(data), {status, headers: {'Content-Type':'application/json'}});
  if (method === 'DELETE') {
    if (window.__hold) await new Promise(resolve => window.__release = resolve);
    return reply(window.__deleteStatus, window.__deleteStatus === 200 ? {} : {error:'删除不可用 503'});
  }
  if (path.startsWith('/api/activity')) {
    if (window.__refreshFail) return reply(503, {error:'offline'});
    return reply(200, {sessions:[], runs:[], totals:{}});
  }
  if (path === '/api/chat/sessions') {
    if (window.__refreshFail) return reply(503, {error:'offline'});
    return reply(200, {sessions:[]});
  }
  return reply(200, {});
};
"""


class DeleteUIIsolated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), StaticOnly)
        if cls.server.server_port == 8765:
            cls.server.server_close()
            raise RuntimeError('Refusing port 8765')
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        options = Options()
        for arg in ('--headless=new', '--disable-gpu', '--no-sandbox', '--no-proxy-server',
                    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
                    '--user-data-dir=' + str(Path(cls.temp.name) / 'profile')):
            options.add_argument(arg)
        cls.driver = webdriver.Chrome(options=options)
        cls.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': MOCK})
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
        cls.temp.cleanup()

    def page(self, page, query=''):
        self.driver.get(self.base + '/' + page + '.html' + query)
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script('return typeof state !== "undefined"'))

    def js(self, script):
        return self.driver.execute_script(script)

    def wait(self, script):
        return WebDriverWait(self.driver, 5).until(lambda d: d.execute_script(script))

    def test_chat_cancel_503_duplicate_switch_and_refresh(self):
        self.page('chat')
        self.js("state.sessionId='a'; state.watchVersion++; state.savedItems=[{session_id:'a', status:'complete'}]; renderSaved(state.savedItems); el('message').value='draft'; window.__confirm=false; deleteSavedSession(state.savedItems[0]);")
        self.assertEqual(self.js("return __calls.filter(c=>c[0]==='DELETE').length"), 0)
        self.js("window.__confirm=true; deleteSavedSession(state.savedItems[0]);")
        self.wait("return el('status').textContent.includes('503')")
        self.assertEqual(self.js("return el('message').value"), 'draft')
        self.js("window.__hold=true; window.__deleteStatus=200; deleteSavedSession(state.savedItems[0]); renderSaved(state.savedItems); deleteSavedSession(state.savedItems[0]);")
        self.assertTrue(self.js("return document.querySelector('.saved-delete').disabled"))
        self.assertEqual(self.js("return __calls.filter(c=>c[0]==='DELETE').length"), 2)
        self.js("state.sessionId='b'; state.watchVersion++; setStatus('current',''); window.__release();")
        self.wait("return !deletingSessions.size")
        self.assertEqual(self.js("return [state.sessionId,el('status').textContent,el('message').value]"), ['b', 'current', 'draft'])
        self.js("state.savedItems=[{session_id:'b',status:'running'}]; renderSaved(state.savedItems)")
        self.assertTrue(self.js("return document.querySelector('.saved-delete').disabled"))
        self.js("state.savedItems=[{session_id:'b',status:'complete'}]; window.__hold=false; window.__refreshFail=true; deleteSavedSession(state.savedItems[0]);")
        self.wait("return el('status').textContent.includes('列表刷新失败')")

    def test_activity_deeplink_pending_running_and_stale(self):
        self.page('activity', '?run=a&keep=1#part')
        self.wait("return state.index !== null")
        self.js("state.selected='a'; state.selectedKind='run'; state.detailVersion++; window.d={id:'a',kind:'run',result:{status:'running'}}; configureDelete(d);")
        self.assertTrue(self.js("return el('activity-delete').disabled"))
        self.js("d.result.status='complete'; configureDelete(d); window.__confirm=false; deleteDetail(d)")
        self.assertEqual(self.js("return __calls.filter(c=>c[0]==='DELETE').length"), 0)
        self.js("window.__confirm=true; deleteDetail(d)")
        self.wait("return el('activity-placeholder').textContent.includes('503')")
        self.assertEqual(self.js("return state.selected"), 'a')
        self.js("window.__deleteStatus=200; window.__hold=true; deleteDetail(d); configureDelete(d); deleteDetail(d)")
        self.assertTrue(self.js("return el('activity-delete').disabled"))
        self.assertEqual(self.js("return __calls.filter(c=>c[0]==='DELETE').length"), 2)
        self.js("state.selected='b'; state.selectedKind='run'; state.detailVersion++; el('activity-placeholder').textContent='current'; window.__release()")
        self.wait("return !deletingRecords.size")
        self.assertEqual(self.js("return [state.selected,el('activity-placeholder').textContent,location.search,location.hash]"), ['b','current','?keep=1','#part'])
        self.js("state.selected='b'; state.selectedKind='run'; state.detailVersion++; window.__hold=false; window.__refreshFail=true; deleteDetail({id:'b',kind:'run',result:{status:'complete'}})")
        self.wait("return el('activity-placeholder').textContent.includes('列表刷新失败')")


if __name__ == '__main__':
    unittest.main()
