"""User journeys for Chat onboarding, drafts, keyboard, results and stopping.

All submitted conversations are deterministic Demo fixtures. Browser config
responses are local test doubles; no model or scholarly API is called.
"""
from pathlib import Path
from copy import deepcopy
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_ui_browser as browser_support


class ChatExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        browser_support.BrowserUiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        browser_support.BrowserUiTests.tearDownClass.__func__(cls)

    def setUp(self):
        browser_support.BrowserUiTests.setUp(self)
        self.driver.execute_script("localStorage.clear();")
        self.driver.get(self.base + '/chat')
        self.wait = browser_support.WebDriverWait(self.driver, 20)
        self.wait.until(lambda d: d.execute_script('return state.configReady !== null'))

    def click(self, selector):
        self.driver.execute_script('arguments[0].click();',
            self.driver.find_element(browser_support.By.CSS_SELECTOR, selector))

    def js(self, script, *args):
        return self.driver.execute_script(script, *args)

    def demo(self):
        self.js("document.querySelector('#mode').value='demo'; document.querySelector('#mode').dispatchEvent(new Event('change'));")

    def ready(self, value):
        self.driver.execute_async_script("""
          const original = window.fetch.bind(window);
          const ready = arguments[0], done = arguments[1];
          window.fetch = (...args) => String(args[0]) === '/api/config'
            ? Promise.resolve(new Response(JSON.stringify({ready, provider:'Local fixture',
                model:'fixture-model', source:'arxiv'}), {status:200}))
            : original(...args);
          loadActiveConfig().then(done);
        """, value)

    def send_demo(self):
        self.demo()
        self.js("setDraft('Recommend five RAG papers including one evaluation paper.');")
        self.click('#send')
        self.wait.until(lambda d: len(d.find_elements(browser_support.By.CSS_SELECTOR, '#turns .turn')) == 1 and
                        d.find_element(browser_support.By.ID, 'message').is_enabled())
        return self.js('return state.sessionId')

    def test_saved_session_deletion_is_safely_unavailable(self):
        self.send_demo()
        self.wait.until(lambda d: d.find_elements(browser_support.By.CSS_SELECTOR, '#saved-sessions .saved-delete'))
        self.wait.until(lambda d: any(button.is_enabled() for button in
                        d.find_elements(browser_support.By.CSS_SELECTOR, '#saved-sessions .saved-delete')))
        target = next(button for button in
                      self.driver.find_elements(browser_support.By.CSS_SELECTOR, '#saved-sessions .saved-delete')
                      if button.is_enabled())
        deleted_id = target.get_attribute('data-session-id')
        self.js('''window.confirm = () => true;
          const original = window.fetch.bind(window);
          window.fetch = async (...args) => {
            const response = await original(...args);
            if (args[1]?.method === 'DELETE') window.__deleteStatus = response.status;
            return response;
          };''')
        self.driver.execute_script("arguments[0].click();", target)
        self.wait.until(lambda d: d.execute_script('return window.__deleteStatus') == 503)
        self.assertTrue(self.driver.find_elements(browser_support.By.CSS_SELECTOR,
                                                  f'#saved-sessions .saved-delete[data-session-id=\"{deleted_id}\"]'))

    def test_switching_session_clears_old_task_while_the_new_one_loads(self):
        self.send_demo()
        self.js("""
          document.getElementById('question').hidden=false;
          document.getElementById('question-text').textContent='OLD QUESTION';
          const original=window.fetch.bind(window);
          window.fetch=(url,options)=>String(url).startsWith('/api/chat/sessions/'+'b'.repeat(32))
            ? new Promise(()=>{}) : original(url,options);
          resumeSession('b'.repeat(32));
        """)
        self.wait.until(lambda d: d.execute_script('return state.busy'))
        self.assertFalse(self.driver.find_element(browser_support.By.ID, 'question').is_displayed())
        self.assertFalse(self.driver.find_element(browser_support.By.ID, 'task-context-grid').is_displayed())
        self.assertIn('读取', self.driver.find_element(browser_support.By.ID, 'session-title').text)
        self.assertIn('读取', self.driver.find_element(browser_support.By.ID, 'task-context-mode').text)
        self.assertEqual(self.driver.find_elements(browser_support.By.CSS_SELECTOR, '#turns .turn'), [])

    def test_examples_are_honest_drafts_and_missing_model_has_a_setup_path(self):
        self.js('state.modeChosen = false;')
        self.ready(False)
        self.assertEqual(self.js("return document.querySelector('#mode').value"), 'demo')
        self.assertIn('固定 RAG', self.driver.find_element(browser_support.By.ID, 'chat-mode-note').text)
        self.assertTrue(self.driver.find_element(browser_support.By.ID, 'chat-setup').is_displayed())
        self.assertIn('/settings', self.driver.find_element(browser_support.By.CSS_SELECTOR, '#chat-setup a').get_attribute('href'))
        self.click('[data-example="medical"]')
        self.assertIn('医疗 RAG', self.js("return document.querySelector('#message').value"))
        self.assertEqual(self.js('return state.sessionId'), None)
        self.assertFalse(self.driver.find_element(browser_support.By.ID, 'send').is_enabled())
        self.assertTrue(self.driver.find_element(browser_support.By.ID, 'message').is_enabled())
        self.click('#choose-demo')
        self.assertEqual(self.js("return document.querySelector('#mode').value"), 'demo')
        self.assertIn('RAG', self.js("return document.querySelector('#message').value"))
        self.assertIsNone(self.js('return state.sessionId'))

    def test_ready_model_defaults_to_live_and_respects_explicit_demo(self):
        self.js('state.modeChosen = false;')
        self.ready(True)
        self.assertEqual(self.js("return document.querySelector('#mode').value"), 'live')
        self.assertEqual(self.js("return document.querySelector('#source').value"), 'arxiv')
        self.js("document.querySelector('#source').value='multi'; document.querySelector('#source').dispatchEvent(new Event('change'));")
        self.ready(True)
        self.assertEqual(self.js("return document.querySelector('#source').value"), 'multi')
        self.assertIn('API 费用', self.driver.find_element(browser_support.By.ID, 'chat-mode-note').text)
        self.demo()
        self.ready(True)
        self.assertEqual(self.js("return document.querySelector('#mode').value"), 'demo')
        self.assertTrue(self.driver.find_element(browser_support.By.ID, 'scenario-field').is_displayed())

    def test_keyboard_preserves_newlines_and_ime_then_sends_once(self):
        self.demo()
        box = self.driver.find_element(browser_support.By.ID, 'message')
        box.send_keys('Recommend five RAG papers.')
        box.send_keys(browser_support.Keys.SHIFT, browser_support.Keys.ENTER, browser_support.Keys.NULL)
        box.send_keys('Include one evaluation paper.')
        self.assertIn('\n', box.get_attribute('value'))
        self.js("""
          const box = document.querySelector('#message');
          box.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}));
          box.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', isComposing:true, bubbles:true, cancelable:true}));
        """)
        self.assertIsNone(self.js('return state.sessionId'))
        self.js("document.querySelector('#message').dispatchEvent(new CompositionEvent('compositionend', {bubbles:true}));")
        box.send_keys(browser_support.Keys.CONTROL, browser_support.Keys.ENTER, browser_support.Keys.NULL)
        self.wait.until(lambda d: len(d.find_elements(browser_support.By.CSS_SELECTOR, '#turns .turn')) == 1 and
                        d.find_element(browser_support.By.ID, 'message').is_enabled())
        self.assertEqual(self.js("return document.querySelector('#message').value"), '')
        self.assertEqual(self.driver.find_element(browser_support.By.ID, 'message-count').text, '0 / 2000')
        self.assertEqual(len(self.service.snapshot(self.js('return state.sessionId'))['history']), 1)

    def test_draft_mode_and_growth_survive_reload_and_session_switches(self):
        self.demo()
        draft = '\n'.join('Draft line ' + str(n) for n in range(10))
        self.js('setDraft(arguments[0]);', draft)
        self.assertGreater(self.js("return document.querySelector('#message').rows"), 2)
        self.assertLessEqual(self.js("return document.querySelector('#message').rows"), 8)
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script("return document.querySelector('#message').value") == draft)
        self.assertEqual(self.js("return document.querySelector('#mode').value"), 'demo')
        session_id = self.send_demo()
        self.js("setDraft('Feedback draft for this session');")
        self.click('#new-session')
        self.assertEqual(self.js("return document.querySelector('#message').value"), '')
        self.js("setDraft('A different new request');")
        self.driver.get(self.base + '/chat?session=' + session_id)
        self.wait.until(lambda d: d.find_element(browser_support.By.ID, 'message').is_enabled() and
                        not d.execute_script('return state.busy;'))
        self.assertEqual(self.js("return document.querySelector('#message').value"), 'Feedback draft for this session')
        self.click('#new-session')
        self.assertEqual(self.js("return document.querySelector('#message').value"), 'A different new request')

    def test_result_context_followups_exports_and_trace_are_progressive(self):
        session_id = self.send_demo()
        topic = self.service.snapshot(session_id)['history'][0]['request']['topic']
        self.assertIn(topic, self.driver.find_element(browser_support.By.ID, 'task-context').text)
        self.assertIn('已交付 5/5', self.driver.find_element(browser_support.By.CSS_SELECTOR, '.turn-summary').text)
        self.assertFalse(self.js("return document.querySelector('.turn-trace').open"))
        self.assertEqual(self.js("return document.querySelectorAll('.turn-trace .chat-trace-item').length"), 0)
        links = self.driver.find_elements(browser_support.By.CSS_SELECTOR, '.turn-exports a')
        self.assertEqual(len(links), 3)
        self.assertTrue(all('/turns/1/' in item.get_attribute('href') for item in links))
        self.click('.followup-chip')
        first_id = self.service.snapshot(session_id)['history'][0]['papers'][0]['id']
        self.assertIn(first_id, self.js("return document.querySelector('#message').value"))
        self.assertEqual(len(self.service.snapshot(session_id)['history']), 1)
        self.click('.turn-trace summary')
        self.wait.until(lambda d: d.find_elements(browser_support.By.CSS_SELECTOR, '.turn-trace .chat-trace-item'))

    def test_demo_exclusion_really_changes_list_and_survives_reload(self):
        session_id = self.send_demo()
        before = self.service.snapshot(session_id)['history'][0]
        excluded = before['papers'][0]['id']
        self.assertIn('仅支持排除第一篇', self.driver.find_element(
            browser_support.By.ID, 'chat-mode-note').text)
        self.assertEqual(len(self.driver.find_elements(browser_support.By.CSS_SELECTOR, '.followup-chip')), 1)
        self.click('.followup-chip')
        self.assertIn(excluded, self.js("return document.querySelector('#message').value"))
        self.click('#send')
        self.wait.until(lambda d: len(d.find_elements(browser_support.By.CSS_SELECTOR, '#turns .turn')) == 2 and
                        d.find_element(browser_support.By.ID, 'message').is_enabled())
        snapshot = self.service.snapshot(session_id)
        after = snapshot['history'][1]
        self.assertIn(excluded, after['request']['excluded_paper_ids'])
        self.assertNotIn(excluded, [paper['id'] for paper in after['papers']])
        self.driver.refresh()
        self.wait.until(lambda d: len(d.find_elements(browser_support.By.CSS_SELECTOR, '#turns .turn')) == 2 and
                        d.find_element(browser_support.By.ID, 'message').is_enabled())
        self.assertEqual(self.js("return document.querySelector('#mode').value"), 'demo')
        self.assertEqual(len(self.driver.find_elements(browser_support.By.CSS_SELECTOR, '#turns .turn:last-child .followup-chip')), 1)
        self.assertNotIn(excluded, [paper['id'] for paper in self.service.snapshot(session_id)['history'][1]['papers']])

    def test_history_search_includes_active_session_and_turn_navigation_moves_focus(self):
        session_id = self.send_demo()
        self.js('loadSaved();')
        self.wait.until(lambda d: d.find_elements(browser_support.By.CSS_SELECTOR, '.saved-item.active'))
        self.js("document.querySelector('#session-search').value=arguments[0]; document.querySelector('#session-search').dispatchEvent(new Event('input'));", session_id)
        self.assertEqual(len(self.driver.find_elements(browser_support.By.CSS_SELECTOR, '#saved-sessions .saved-item')), 1)
        self.click('#history button')
        self.assertEqual(self.js('return document.activeElement.id'), 'turn-1')
        self.assertEqual(self.driver.find_element(browser_support.By.CSS_SELECTOR, '#history button').get_attribute('aria-current'), 'step')

    def test_failed_turn_retains_editable_draft_after_reload(self):
        session_id = self.send_demo()
        record = self.service.sessions[session_id]

        def failed(message):
            result = deepcopy(record['results'][-1])
            result.update(status='failed', stop_reason='fixture_failure',
                          user_input=message, papers=[], revision=2)
            return result

        record['chat'].send = failed
        message = 'Keep this feedback so I can edit and retry.'
        self.js('setDraft(arguments[0]);', message)
        self.click('#send')
        self.wait.until(lambda d: len(d.find_elements(browser_support.By.CSS_SELECTOR, '#turns .turn')) == 2 and
                        d.find_element(browser_support.By.ID, 'message').is_enabled() and
                        not d.execute_script('return state.busy;'))
        self.assertEqual(self.js("return document.querySelector('#message').value"), message)
        self.driver.get(self.base + '/chat?session=' + session_id)
        self.wait.until(lambda d: d.find_element(browser_support.By.ID, 'message').is_enabled() and
                        not d.execute_script('return state.busy;'))
        self.assertEqual(self.js("return document.querySelector('#message').value"), message)

    def test_stop_requests_cancellation_without_losing_existing_turn(self):
        session_id = self.send_demo()
        chat = self.service.sessions[session_id]['chat']
        started, release = threading.Event(), threading.Event()
        original = chat.send

        def blocked(message):
            started.set()
            release.wait(15)
            return original(message)

        chat.send = blocked
        try:
            self.js("setDraft('Exclude P01 and keep the rest.');")
            self.click('#send')
            self.assertTrue(started.wait(2))
            self.wait.until(lambda d: d.find_element(browser_support.By.ID, 'stop-turn').is_displayed())
            self.click('#stop-turn')
            self.wait.until(lambda d: self.service.snapshot(session_id)['cancel_requested'])
            self.assertFalse(self.driver.find_element(browser_support.By.ID, 'stop-turn').is_enabled())
            self.assertEqual(len(self.service.snapshot(session_id)['history']), 1)
        finally:
            release.set()
        self.wait.until(lambda d: len(d.find_elements(browser_support.By.CSS_SELECTOR, '#turns .turn')) == 2 and
                        not d.execute_script('return state.busy'))
        self.assertIn('已停止', self.driver.find_elements(browser_support.By.CSS_SELECTOR, '.turn-summary')[-1].text)
        self.assertEqual(self.service.snapshot(session_id)['history'][-1]['stop_reason'], 'user_cancelled')


if __name__ == '__main__':
    unittest.main()
