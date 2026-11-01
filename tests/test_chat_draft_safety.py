"""Offline browser regressions: fixture HTTP server only, no model calls."""
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
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None

from scripts.verify_delivery import apply_browser_isolation, isolated_environment

ROOT = Path(__file__).resolve().parents[1]


class FixtureHandler(SimpleHTTPRequestHandler):
    posts = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'web'), **kwargs)

    def log_message(self, *_args):
        pass

    def respond(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/config':
            return self.respond({'ready': False})
        if path == '/api/sources':
            return self.respond({'sources': [
                {'name': name, 'label': name, 'enabled': True, 'configured': True}
                for name in ('arxiv', 'openalex', 'openreview')]})
        if path == '/api/chat/sessions':
            return self.respond({'sessions': []})
        if path == '/api/chat/sessions/fixture':
            return self.respond({'mode': 'demo', 'status': 'active', 'history': [], 'capabilities': {}})
        if path.startswith('/api/'):
            return self.respond({'events': []})
        if path == '/chat':
            self.path = '/chat.html'
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length) or b'{}')
        self.posts.append((self.path, body))
        if self.path == '/api/chat/sessions':
            return self.respond({'session_id': 'fixture', 'result': {'turn': 1, 'status': 'complete', 'message': body['message']}})
        if self.path.endswith('/messages'):
            return self.respond({'result': {'turn': 2, 'status': 'complete', 'message': body['message']}})
        return self.respond({'result': {'turn': 3, 'status': 'complete'}})


class ChatDraftSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get('SG_REQUIRE_BROWSER') == '1'
        if webdriver is None:
            if required:
                raise RuntimeError('Required Selenium unavailable')
            raise unittest.SkipTest('Selenium unavailable')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
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

    def setUp(self):
        FixtureHandler.posts = []
        self.driver.get(self.base + '/chat')
        self.driver.execute_script('localStorage.clear()')
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script('return state.configReady !== null'))
        self.driver.execute_script("""
          state.sessionId = 'fixture'; state.draftContext = 'fixture';
          renderResult({turn: 1, status: 'failed', message: 'previous fixture',
            diagnosis: {code: 'empty', summary: 'fixture', actions: [
              {retry:true, code:'other', label:'suggest', message:'suggested fixture'},
              {retry:true, code:'relax_year', label:'recover', message:'recovery fixture', patch:{year:2020}},
              {code:'new_session', label:'fork'}] }}, {announce:false});
          updateComposer();
        """)

    def draft(self, text):
        self.driver.execute_script("setDraft(arguments[0]);", text)

    def click(self, selector):
        self.driver.execute_script('document.querySelector(arguments[0]).click()', selector)

    def value(self):
        return self.driver.find_element(By.ID, 'message').get_attribute('value')

    def wait_post(self):
        WebDriverWait(self.driver, 5).until(lambda _: len(FixtureHandler.posts) > 0)
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script('return !state.busy'))

    def test_retry_and_suggestion_no_draft_cancel_confirm(self):
        for selector, expected in [('.turn-retry', 'previous fixture'),
                                   ('.diagnosis-action', 'suggested fixture')]:
            with self.subTest(action=selector):
                FixtureHandler.posts = []
                self.draft('')
                self.click(selector)
                self.wait_post()
                self.assertEqual(FixtureHandler.posts[0][1]['message'], expected)
                self.assertEqual(self.value(), '')
                self.draft('private fixture draft')
                self.driver.execute_script('window.confirm = () => false')
                FixtureHandler.posts = []
                self.click(selector)
                self.assertEqual(FixtureHandler.posts, [])
                self.assertEqual(self.value(), 'private fixture draft')
                self.driver.execute_script('window.confirm = () => true')
                self.click(selector)
                self.wait_post()
                self.assertEqual(len(FixtureHandler.posts), 1)
                self.assertEqual(FixtureHandler.posts[0][1]['message'], expected)
                self.assertEqual(self.value(), 'private fixture draft')
                self.assertEqual(self.driver.execute_script("return JSON.parse(localStorage.getItem(draftKey())).message"), 'private fixture draft')

    def test_recovery_and_fork_cancel_confirm_and_empty(self):
        for index, endpoint in [(1, '/recover'), (2, '/fork')]:
            with self.subTest(endpoint=endpoint):
                selector = f'.diagnosis-action:nth-of-type({index + 1})'
                self.draft('private fixture draft')
                self.driver.execute_script('window.confirm = () => false')
                FixtureHandler.posts = []
                self.click(selector)
                self.assertEqual(FixtureHandler.posts, [])
                self.assertEqual(self.value(), 'private fixture draft')
                self.driver.execute_script('window.confirm = () => true')
                self.click(selector)
                self.wait_post()
                self.assertTrue(FixtureHandler.posts[0][0].endswith(endpoint))
                self.assertEqual(self.value(), 'private fixture draft')
                FixtureHandler.posts = []
                self.draft('')
                self.driver.execute_script('window.confirm = () => { throw new Error("unexpected confirmation") }')
                self.click(selector)
                self.wait_post()
                self.assertTrue(FixtureHandler.posts[0][0].endswith(endpoint))

    def test_examples_do_not_replace_an_existing_draft_without_confirmation(self):
        self.driver.execute_script('state.sessionId = null; state.busy = false')
        self.draft('private fixture draft')
        self.driver.execute_script('window.confirm = () => false; useExample("demo")')
        self.assertEqual(self.value(), 'private fixture draft')
        self.assertEqual(FixtureHandler.posts, [])
        self.driver.execute_script('window.confirm = () => true; useExample("demo")')
        self.assertNotEqual(self.value(), 'private fixture draft')
        self.assertEqual(FixtureHandler.posts, [])

    def test_recovery_running_success_and_uncertain_failures(self):
        # Override transport only in this synthetic page; never contact a model or real service.
        for case in ('success', 'poll_error', 'post_error', 'unknown_result'):
            with self.subTest(case=case):
                result = self.driver.execute_async_script("""
                  const scenario = arguments[0], done = arguments[arguments.length - 1];
                  const originalPost = post, originalPoll = pollTurn;
                  state.sessionId = 'fixture'; state.pendingSessionId = null; state.recoveryUncertainSessionId = null;
                  state.busy = false; state.canRetry = true; reconnect.hidden = true;
                  let posts = 0;
                  post = async () => { posts++; if (scenario === 'post_error') throw Error('network fixture');
                    return {running:true}; };
                  pollTurn = async () => {
                    if (scenario === 'poll_error') throw Error('poll fixture');
                    return {turn:42, status:scenario === 'unknown_result' ? 'unknown' : 'complete'};
                  };
                  runRecovery({code:'relax_year', label:'fixture'}).then(async () => {
                    await runRecovery({code:'relax_year', label:'fixture'});
                    const answer = {pending:state.pendingSessionId, reconnect:!reconnect.hidden,
                      disabled:el('send').disabled, posts, status:el('status').textContent};
                    post = originalPost; pollTurn = originalPoll; done(answer);
                  });
                """, case)
                if case == 'success':
                    self.assertIsNone(result['pending'])
                    self.assertFalse(result['reconnect'])
                    self.assertFalse(result['disabled'])
                    self.assertEqual(result['posts'], 2)
                else:
                    self.assertEqual(result['pending'], 'fixture')
                    self.assertTrue(result['reconnect'])
                    self.assertTrue(result['disabled'])
                    self.assertEqual(result['posts'], 1)
                    self.assertIn('重新连接', result['status'])

    def test_uncertain_recovery_reconnect_does_not_trust_old_active_snapshot(self):
        result = self.driver.execute_async_script("""
          const done = arguments[arguments.length - 1], originalPost = post, originalRead = readTurn;
          state.sessionId = 'fixture'; state.pendingSessionId = null; state.busy = false;
          state.recoveryUncertainSessionId = null;
          post = async () => { throw Error('synthetic network failure'); };
          readTurn = async () => ({feed:{events:[]}, snapshot:{mode:'demo', status:'active', history:[], capabilities:{}}});
          runRecovery({code:'relax_year', label:'fixture'}).then(() => resumeSession('fixture')).then(() => {
            const result = {pending:state.pendingSessionId, reconnect:!reconnect.hidden,
              uncertain:state.recoveryUncertainSessionId, disabled:el('send').disabled};
            post = originalPost; readTurn = originalRead; done(result);
          });
        """)
        self.assertEqual(result, {'pending': 'fixture', 'reconnect': True, 'uncertain': 'fixture', 'disabled': True})

    def test_poll_unknown_snapshot_is_not_terminal(self):
        result = self.driver.execute_async_script("""
          const done = arguments[arguments.length - 1], original = readTurn;
          state.sessionId = 'fixture'; const version = ++state.watchVersion;
          readTurn = async () => ({feed:{events:[]}, snapshot:{status:'unknown', history:[{status:'complete'}]}});
          pollTurn('fixture', version).then(() => done('unexpected success'), error => {
            readTurn = original; done(error.code);
          });
        """)
        self.assertEqual(result, 'poll_disconnected')

    def test_demo_link_preserves_new_draft_and_does_not_replay(self):
        saved = {'message': 'synthetic private draft', 'mode': 'live', 'source': 'openalex', 'scenario': 'ask'}
        self.driver.execute_script("localStorage.setItem('scholarguide-chat-draft:new', arguments[0])", json.dumps(saved))
        FixtureHandler.posts = []
        self.driver.get(self.base + '/chat?demo=ask&keep=1#fixture')
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script('return state.configReady !== null'))
        self.assertEqual(self.value(), saved['message'])
        self.assertEqual(json.loads(self.driver.execute_script("return localStorage.getItem('scholarguide-chat-draft:new')")), saved)
        self.assertEqual(self.driver.find_element(By.ID, 'mode').get_attribute('value'), 'live')
        self.assertEqual(self.driver.find_element(By.ID, 'source').get_attribute('value'), 'openalex')
        self.assertIn('草稿', self.driver.find_element(By.ID, 'status').text)
        self.assertIn('未自动运行', self.driver.find_element(By.ID, 'status').text)
        self.assertNotIn('demo=', self.driver.current_url)
        self.assertIn('keep=1#fixture', self.driver.current_url)
        self.driver.refresh()
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script('return state.configReady !== null'))
        self.assertEqual(self.value(), saved['message'])
        self.assertEqual(FixtureHandler.posts, [])

    def test_demo_link_without_draft_still_submits(self):
        self.driver.execute_script('localStorage.clear()')
        FixtureHandler.posts = []
        self.driver.get(self.base + '/chat?demo=ask')
        WebDriverWait(self.driver, 5).until(lambda _: len(FixtureHandler.posts) == 1)
        path, body = FixtureHandler.posts[0]
        self.assertEqual(path, '/api/chat/sessions')
        self.assertEqual(body['mode'], 'demo')
        self.assertEqual(body['scenario'], 'ask')
        self.assertIn('not sure', body['message'])

    def test_copy_fallback_failure_and_focus(self):
        for outcome in ('false', 'throw', 'true'):
            with self.subTest(outcome=outcome):
                result = self.driver.execute_async_script("""
                  const outcome = arguments[0], done = arguments[arguments.length - 1];
                  Object.defineProperty(navigator, 'clipboard', {configurable:true, value:undefined});
                  document.execCommand = () => { if (outcome === 'throw') throw Error('fixture'); return outcome === 'true'; };
                  const input = document.getElementById('message'), button = document.getElementById('copy-session');
                  input.focus();
                  copyText('fixture', button).then(() => done({label:button.textContent,
                    focused:document.activeElement === input,
                    temporary:document.querySelectorAll('body > textarea').length}));
                """, outcome)
                self.assertEqual(result['label'], '已复制' if outcome == 'true' else '复制失败')
                self.assertTrue(result['focused'])
                self.assertEqual(result['temporary'], 0)


if __name__ == '__main__':
    unittest.main()
