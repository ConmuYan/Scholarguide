"""Real-browser interaction checks (Selenium + headless Chrome).

Skipped when no WebDriver is available. Covers the behaviours the static test
cannot: quote/citation clipboard targets, quote expansion, missing-quote copy,
chat badges, and history navigation.
"""
from pathlib import Path
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None

from activity import ActivityFeed
from webapp import ChatService, ChatSession, Jobs, create_server
from providers import ProviderStore
from scripts.verify_delivery import apply_browser_isolation

CLIPBOARD_STUB = """
window.__copied = null;
Object.defineProperty(navigator, 'clipboard', {
  configurable: true,
  value: { writeText: async (text) => { window.__copied = text; } },
});
"""


class BrowserUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if webdriver is None:
            message = 'Browser tests require Selenium; install requirements-dev.txt.'
            if os.environ.get('SG_REQUIRE_BROWSER') == '1':
                raise RuntimeError(message)
            raise unittest.SkipTest(message)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.runs = Path(cls.tmp.name)
        cls.service = ChatService(output=cls.runs / 'web')
        cls.activity = ActivityFeed(runs=cls.runs)
        cls.providers = ProviderStore(cls.runs / 'providers.json')
        # The browser checks must not depend on the developer's local .env or
        # provider store: seed one fixture profile so the settings page and the
        # model labels have a provider with a key in any extracted tree.
        cls.providers.upsert({
            'id': 'p-browser-fixture', 'name': 'Browser Fixture',
            'protocol': 'chat_completions', 'base_url': 'https://fixture.invalid/v1',
            'model': 'fixture-model', 'api_key': 'browser-fixture-key',
            'input_price': 0.0, 'output_price': 0.0, 'max_output_tokens': 3000,
            'currency': 'USD'}, activate=True)
        cls.server = create_server(0, jobs=Jobs(output=cls.runs, providers=cls.providers),
                                   chat=cls.service, activity=cls.activity,
                                   providers=cls.providers)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        options = Options()
        apply_browser_isolation(options, cls.runs / 'chrome-profile')
        for arg in ('--headless=new', '--no-sandbox', '--disable-gpu',
                    '--window-size=1280,1000', '--hide-scrollbars'):
            options.add_argument(arg)
        try:
            cls.driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            cls.server.shutdown()
            cls.server.server_close()
            cls.tmp.cleanup()
            if os.environ.get('SG_REQUIRE_BROWSER') == '1':
                raise RuntimeError(f'Required WebDriver unavailable: {exc}') from exc
            raise unittest.SkipTest(f'WebDriver unavailable: {exc}')

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.service.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.driver.set_window_size(1280, 1000)
        self.driver.get(self.base + '/search?demo=followup')
        WebDriverWait(self.driver, 20).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, '#paper-list .paper'))
        self.driver.execute_script("""try {
          localStorage.removeItem('scholarguide-chat-session');
          // Each test owns its synthetic drafts; a prior test must not suppress
          // the next test's explicit demo deep link.
          for (const key of Object.keys(localStorage)) {
            if (key.startsWith('scholarguide-chat-draft:')) localStorage.removeItem(key);
          }
        } catch (e) {}""")
        self.driver.execute_script(CLIPBOARD_STUB)

    def _click(self, element):
        self.driver.execute_script('arguments[0].click();', element)

    def _copied(self):
        return WebDriverWait(self.driver, 5).until(
            lambda d: d.execute_script('return window.__copied;'))

    def _interrupt_polling(self, endpoint, always=False):
        self.driver.execute_script("""
          const original = window.fetch.bind(window);
          window.__pollFault = {endpoint: arguments[0], always: arguments[1], remaining: 2,
            failures: 0, attempts: 0, messagePosts: 0};
          window.fetch = (...args) => {
            const path = String(args[0]);
            const method = ((args[1] || {}).method || 'GET').toUpperCase();
            const fault = window.__pollFault;
            if (method === 'POST' && path.endsWith('/messages')) fault.messagePosts += 1;
            const watched = method === 'GET' && (fault.endpoint === 'events'
              ? /\\/api\\/chat\\/sessions\\/[a-f0-9]{32}\\/events/.test(path)
              : /\\/api\\/chat\\/sessions\\/[a-f0-9]{32}$/.test(path));
            if (watched) {
              fault.attempts += 1;
              if (fault.always || fault.remaining > 0) {
                fault.remaining -= 1;
                fault.failures += 1;
                return Promise.reject(new TypeError('offline fixture'));
              }
            }
            return original(...args);
          };
        """, endpoint, always)

    def test_quote_and_citation_copy(self):
        first = self.driver.find_element(By.CSS_SELECTOR, '#paper-list .paper')
        quote = first.find_element(By.CSS_SELECTOR, '.evidence-block blockquote').text
        self._click(first.find_element(By.CSS_SELECTOR, '[data-copy-quote]'))
        self.assertIn(quote.strip()[:40], (self._copied() or ''))

        self.driver.execute_script('window.__copied = null;')
        self._click(first.find_element(By.CSS_SELECTOR, '[data-copy-citation]'))
        citation = self._copied() or ''
        self.assertIn(first.find_element(By.CSS_SELECTOR, 'h3').text.strip()[:20], citation)
        self.assertIn('—', citation)

    def test_quote_expand_and_missing_quote_state(self):
        clamped = self.driver.find_elements(By.CSS_SELECTOR, '#paper-list blockquote.is-clamped')
        if clamped:
            toggle = clamped[0].find_element(By.XPATH, '..').find_element(By.CSS_SELECTOR, '.quote-toggle')
            self._click(toggle)
            self.assertFalse('is-clamped' in clamped[0].get_attribute('class'))
        self.driver.execute_script("""
            document.querySelector('#paper-list').append(
              paperCard({id:'x', title:'No quote paper', reason:'r'}, 0, {category:'none'})
            );
        """)
        missing = self.driver.find_elements(By.CSS_SELECTOR, '#paper-list .evidence-missing')
        self.assertEqual(len(missing), 1)
        self.assertIn('缺少可核验引文', missing[0].text)

    def test_chat_badges_and_missing_quote(self):
        self.driver.get(self.base + '/chat?demo=followup')
        WebDriverWait(self.driver, 20).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        badges = self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn-paper .badge')
        self.assertGreaterEqual(len(badges), 2)
        self.driver.execute_script("""
            document.querySelector('#turns').append(
              paperRow({id:'x', title:'No quote paper', reason:'r'}, 0)
            );
        """)
        missing = self.driver.find_elements(By.CSS_SELECTOR, '#turns .evidence-missing')
        self.assertEqual(len(missing), 1)

    def test_chat_shows_active_profile_and_model(self):
        self.driver.get(self.base + '/chat')
        wait = WebDriverWait(self.driver, 10)
        provider = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#chat-model-label .model-provider'))
        model = self.driver.find_element(By.CSS_SELECTOR, '#chat-model-label .model-name')
        self.assertTrue(provider.text.strip())
        self.assertTrue(model.text.strip())
        self.assertIn(provider.text.strip(), self.driver.find_element(By.ID, 'chat-model-label').get_attribute('title'))

    def test_chat_exhausted_budget_disables_send_and_actions(self):
        self.driver.get(self.base + '/chat')
        self.driver.execute_script("localStorage.clear(); resetSession();")
        self.driver.execute_script("""
          state.sessionId = 'f'.repeat(32);
          applyCapabilities({archived:false, feedback_phases_left:0, feedback_phases_cap:2,
            capabilities:{can_send:false, can_retry:false, feedback_phases_left:0,
              feedback_phases_cap:2, awaiting_user:false}});
        """)
        wait = WebDriverWait(self.driver, 5)
        budget = wait.until(lambda d: d.find_element(By.ID, 'budget-status'))
        self.assertFalse(budget.get_attribute('hidden'))
        self.assertIn('反馈预算已用尽', budget.text)
        self.assertTrue(self.driver.find_element(By.ID, 'send').get_attribute('disabled'))
        self.assertTrue(self.driver.find_element(By.ID, 'message').get_attribute('disabled'))
        self.assertFalse(self.driver.find_element(By.ID, 'archive-session').get_attribute('hidden'))

    def test_chat_turn_trace_follows_each_turn(self):
        self.driver.get(self.base + '/chat?demo=followup')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn .turn-trace'))
        first = self.driver.find_element(By.CSS_SELECTOR, '#turns .turn .turn-trace')
        self.assertFalse(first.get_attribute('open'))
        self._click(first.find_element(By.TAG_NAME, 'summary'))
        wait.until(lambda d: len(first.find_elements(By.CSS_SELECTOR, '.chat-trace-item')) > 0)
        self.assertIn('模型配置', first.text)
        self.assertIn('选择下一步', first.text)
        self.assertTrue(first.get_attribute('open'))
        box = self.driver.find_element(By.ID, 'message')
        box.clear(); box.send_keys('Add one survey paper.')
        self._click(self.driver.find_element(By.ID, 'send'))
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) >= 2)
        second = self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn .turn-trace')[1]
        self.assertFalse(second.get_attribute('open'))
        self._click(second.find_element(By.TAG_NAME, 'summary'))
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn:nth-of-type(2) .chat-trace-item')) > 0)
        traces = self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn .turn-trace')
        self.assertEqual(len(traces), 2)
        self.assertFalse(traces[0].get_attribute('open'))
        self.assertTrue(traces[1].get_attribute('open'))
        second = traces[1]
        self.assertGreater(len(second.find_elements(By.CSS_SELECTOR, '.chat-trace-item')), 0)
        self.assertIn('Agent 轨迹', second.text)
        self.assertIn('/activity?session=', second.find_element(By.TAG_NAME, 'a').get_attribute('href'))
        self.driver.execute_script("setBusy(true); appendLiveTrace({seq: 999, action: 'search', payload: {query: 'live probe', returned: 2}});")
        live = self.driver.find_element(By.ID, 'running-trace')
        self.assertFalse(live.get_attribute('hidden'))
        self.assertIn('检索论文', live.text)
        self.assertIn('live probe', live.text)

    def test_chat_trace_has_no_horizontal_overflow_at_390(self):
        self.driver.set_window_size(390, 900)
        self.driver.get(self.base + '/chat?demo=followup')
        wait = WebDriverWait(self.driver, 20)
        trace = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#turns .turn-trace summary'))
        self._click(trace)
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .chat-trace-item'))
        self.assertLessEqual(
            self.driver.execute_script('return document.documentElement.scrollWidth'),
            self.driver.execute_script('return document.documentElement.clientWidth'))

    def test_chat_ask_then_resume_in_browser(self):
        self.driver.get(self.base + '/chat?demo=ask')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: d.find_element(By.ID, 'question').is_displayed())
        self.assertIn('awaiting_user', self.driver.find_element(By.ID, 'status').get_attribute('data-kind'))
        box = self.driver.find_element(By.ID, 'message')
        box.clear()
        box.send_keys('It is for a survey, one survey paper please.')
        self._click(self.driver.find_element(By.ID, 'send'))
        wait.until(lambda d: not d.find_element(By.ID, 'question').is_displayed())
        self.assertIn('费用', self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn-foot')[-1].text)

    def test_intent_goal_and_paper_type_badges(self):
        self.driver.get(self.base + '/search')
        wait = WebDriverWait(self.driver, 10)
        wait.until(lambda d: d.execute_script('return Boolean(state.config) && !state.busy && !state.polling;'))
        self.driver.execute_script("setMode('live');")
        self._click(self.driver.find_element(By.CSS_SELECTOR, "#advanced-settings > summary"))
        goal = wait.until(lambda d: d.find_element(By.ID, 'reading-goal'))
        self.assertTrue(goal.is_displayed())
        self.assertEqual(len(goal.find_elements(By.TAG_NAME, 'option')), 5)
        self.driver.execute_script("""
          renderResult({
            status: 'partial', mode: 'live',
            request: {topic: 'RAG', target_count: 5, reading_goal: 'orientation'},
            papers: [
              {id:'s', title:'Survey', reason:'r', paper_type:'survey', sources:['arxiv'],
               evidence_quote:'survey quote long enough', evidence_verified:true},
              {id:'b', title:'Bench', reason:'r', paper_type:'benchmark', sources:['arxiv'],
               evidence_quote:'bench quote long enough', evidence_verified:true},
              {id:'p', title:'Research', reason:'r', paper_type:'research', sources:['arxiv'],
               evidence_quote:'research quote long enough', evidence_verified:true}
            ],
            gaps: [{requirement:'mix', value:'综述 1/2 · 研究论文 2/3', found:3, required:5, missing:1}],
            trace: [{action:'intent', goal:'orientation', policy:'deterministic_intent_v1',
                     mix:'综述 2 · 研究论文 3'}],
            metrics: {}, warnings: []
          });
        """)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#paper-list .badge.type')) == 3)
        self.assertIn('综述 1/2', self.driver.find_element(By.ID, 'gap-section').text)
        self.assertIn('deterministic_intent_v1', self.driver.find_element(By.ID, 'coverage-status').get_attribute('title'))

    def test_source_filter_and_coverage_feedback(self):
        self.driver.get(self.base + '/search')
        WebDriverWait(self.driver, 10).until(lambda d: d.execute_script(
            'return Boolean(state.config) && !state.busy && !state.polling;'))
        self.driver.execute_script("""
          renderResult({
            status: 'partial', mode: 'live', request: {topic: 'RAG', target_count: 5,
              category: 'evaluation', minimum_category_count: 2},
            papers: [
              {id:'a', title:'Arxiv RAG', reason:'r', sources:['arxiv'], evidence_quote:'a quote long enough',
               evidence_verified:true, category_status:'yes'},
              {id:'b', title:'OpenAlex RAG', reason:'r', sources:['openalex'], evidence_quote:'b quote long enough',
               evidence_verified:true, category_status:'no'}
            ],
            gaps: [{requirement:'evaluation', found:1, required:2}],
            metrics: {search_rounds: 1, elapsed_seconds: 1, llm_calls: 1}, warnings: []
          });
        """)
        wait = WebDriverWait(self.driver, 5)
        wait.until(lambda d: not d.find_element(By.ID, 'source-filter').get_attribute('hidden'))
        self.assertIn('仍有 1 项缺口', self.driver.find_element(By.ID, 'coverage-status').text)
        buttons = self.driver.find_elements(By.CSS_SELECTOR, '#source-filter .chip')
        self.assertGreaterEqual(len(buttons), 3)
        self._click(buttons[1])
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#paper-list .paper:not([hidden])')) == 1)
        self.assertIn('/', self.driver.find_element(By.ID, 'result-count').text)

    def test_theme_persists_and_mobile_drawer_opens(self):
        self.driver.set_window_size(390, 900)
        self.driver.get(self.base + '/search')
        self.driver.execute_script("localStorage.removeItem('scholarguide-theme')")
        self.driver.refresh()
        wait = WebDriverWait(self.driver, 10)
        wait.until(lambda d: d.find_element(By.ID, 'theme-toggle').is_displayed())
        self._click(self.driver.find_element(By.ID, 'theme-toggle'))  # system -> light
        self.assertEqual(self.driver.execute_script("return document.documentElement.dataset.theme"), 'light')
        self.driver.refresh()
        self.assertEqual(self.driver.execute_script("return document.documentElement.dataset.themeMode"), 'light')
        menu = self.driver.find_element(By.ID, 'menu-toggle')
        self.assertTrue(menu.is_displayed())
        self._click(menu)
        self.assertIn('open', self.driver.find_element(By.ID, 'sidebar').get_attribute('class'))
        self.assertEqual(menu.get_attribute('aria-expanded'), 'true')

    def test_mobile_drawer_removes_hidden_links_from_focus_and_restores_desktop(self):
        self.driver.set_window_size(390, 900)
        self.driver.get(self.base + '/search')
        wait = WebDriverWait(self.driver, 10)
        wait.until(lambda d: d.execute_script('return document.querySelector("#sidebar").inert'))
        sidebar = self.driver.find_element(By.ID, 'sidebar')
        menu = self.driver.find_element(By.ID, 'menu-toggle')
        self.assertEqual(sidebar.get_attribute('aria-hidden'), 'true')
        self.assertFalse(self.driver.execute_script('''
          const link = document.querySelector('#sidebar a'); link.focus();
          return document.activeElement === link;
        '''))
        self._click(menu)
        self.assertFalse(self.driver.execute_script('return document.querySelector("#sidebar").inert'))
        self.assertTrue(self.driver.execute_script(
            'return document.querySelector("#sidebar").contains(document.activeElement)'))
        self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        self.assertEqual(menu.get_attribute('aria-expanded'), 'false')
        self.assertTrue(self.driver.execute_script('return document.querySelector("#sidebar").inert'))
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'menu-toggle')
        self.driver.set_window_size(1280, 1000)
        wait.until(lambda d: not d.execute_script('return document.querySelector("#sidebar").inert'))
        self.assertIsNone(sidebar.get_attribute('aria-hidden'))
        self.assertTrue(self.driver.execute_script('''
          const link = document.querySelector('#sidebar a'); link.focus();
          return document.activeElement === link;
        '''))

    def test_activity_deep_link_from_chat(self):
        self.driver.get(self.base + '/chat?demo=followup')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        session_id = self.driver.find_element(By.ID, 'session-id').text
        self.driver.get(self.base + '/activity?session=' + session_id)
        wait.until(lambda d: d.find_element(By.ID, 'activity-title').text not in ('', '—'))
        self.assertIn('会话', self.driver.find_element(By.ID, 'activity-kind').text)
        self.assertTrue(self.driver.find_element(By.ID, 'activity-story').is_displayed())
        self.assertGreater(len(self.driver.find_elements(By.CSS_SELECTOR, '#activity-trace-tree .trace-row')), 0)
        self.assertGreater(len(self.driver.find_elements(By.CSS_SELECTOR, '#activity-graph .act-graph-node')), 0)

    def test_saved_session_resumes_after_reload(self):
        self.driver.get(self.base + '/chat?demo=followup')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        session_id = self.driver.find_element(By.ID, 'session-id').text
        self.driver.get(self.base + '/chat')
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        self.assertEqual(self.driver.find_element(By.ID, 'session-id').text, session_id)
        self.assertFalse(self.driver.find_element(By.ID, 'open-activity').get_attribute('hidden'))
        self.assertIn('/activity?session=' + session_id,
                      self.driver.find_element(By.ID, 'open-activity').get_attribute('href'))
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#saved-sessions li'))

    def test_running_first_turn_survives_reload_and_streams_progress(self):
        self.driver.get(self.base + '/chat')
        wait = WebDriverWait(self.driver, 20)
        started, release = threading.Event(), threading.Event()
        original = ChatSession.start

        def blocked(chat, *args, **kwargs):
            chat.store.append_event(chat.session_id, 'agent_node', {'node': 'retrieve'})
            chat.store.commit()
            started.set()
            release.wait(15)
            return original(chat, *args, **kwargs)

        try:
            with patch.object(ChatSession, 'start', blocked):
                self.driver.execute_script('document.querySelector("#mode").value = "demo"; document.querySelector("#mode").dispatchEvent(new Event("change"));')
                self.driver.find_element(By.ID, 'message').send_keys('Recommend five RAG papers.')
                self._click(self.driver.find_element(By.ID, 'send'))
                wait.until(lambda d: len(d.find_element(By.ID, 'session-id').text) == 32)
                session_id = self.driver.find_element(By.ID, 'session-id').text
                self.assertTrue(started.wait(2))
                wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#running-trace .chat-trace-item'))
                self.assertTrue(self.driver.find_element(By.ID, 'new-session').get_attribute('disabled'))
                self.assertTrue(self.driver.find_element(By.ID, 'mode').get_attribute('disabled'))
                self.driver.refresh()
                wait.until(lambda d: d.find_element(By.ID, 'session-id').text == session_id)
                self.assertTrue(self.driver.find_element(By.ID, 'running').is_displayed())
                # The composer stays editable while the turn runs so the next
                # request can be pre-written; it is kept as a draft.
                self.assertIsNone(self.driver.find_element(By.ID, 'message').get_attribute('disabled'))
                self.assertIn('预写', self.driver.find_element(By.ID, 'composer-keyboard-help').text)
        finally:
            release.set()
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) == 1)
        wait.until(lambda d: not d.execute_script('return state.busy;'))
        self.assertEqual(self.driver.find_element(By.ID, 'session-id').text, session_id)

    def test_running_feedback_survives_reload_and_shows_actual_reuse(self):
        self.driver.get(self.base + '/chat?demo=followup')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled())
        session_id = self.driver.find_element(By.ID, 'session-id').text
        chat = self.service.sessions[session_id]['chat']
        started, release = threading.Event(), threading.Event()
        original = chat.send

        def blocked(message):
            started.set()
            release.wait(15)
            return original(message)

        chat.send = blocked
        try:
            self.driver.find_element(By.ID, 'message').send_keys('Exclude P01 and keep the rest.')
            self._click(self.driver.find_element(By.ID, 'send'))
            self.assertTrue(started.wait(2))
            self.driver.get(self.base + '/chat?session=' + session_id)
            wait.until(lambda d: d.find_element(By.ID, 'session-id').text == session_id)
            self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn')), 1)
            self.assertTrue(self.driver.find_element(By.ID, 'running').is_displayed())
        finally:
            release.set()
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) == 2)
        wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled())
        foot = self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn-foot')[-1].text
        self.assertIn('复用 5/5', foot)
        self.assertIn('1.00', foot)

    def test_multi_source_fork_redirect_follows_the_running_turn(self):
        self.driver.get(self.base + '/chat?demo=followup')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled() and
                   d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        parent_id = self.driver.find_element(By.ID, 'session-id').text
        release = threading.Event()
        original = ChatSession.start

        def blocked(chat, *args, **kwargs):
            release.wait(15)
            return original(chat, *args, **kwargs)

        try:
            with patch.object(ChatSession, 'start', blocked):
                self.driver.execute_script("forkSource('multi');")
                # The address bar already carries the parent session, so wait
                # for the fork's own session id instead of for "?session=".
                wait.until(lambda d: '?session=' in d.current_url and
                           d.current_url.rsplit('=', 1)[1] != parent_id)
                fork_id = self.driver.current_url.rsplit('=', 1)[1]
                self.assertNotEqual(fork_id, parent_id)
                wait.until(lambda d: d.find_element(By.ID, 'session-id').text == fork_id)
                self.assertTrue(self.driver.find_element(By.ID, 'running').is_displayed())
                self.assertEqual(self.driver.find_element(By.ID, 'source').get_attribute('value'), 'multi')
        finally:
            release.set()
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) == 1)
        wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled())

    def test_transient_event_and_snapshot_disconnects_resume_without_reposting(self):
        wait = WebDriverWait(self.driver, 20)
        for endpoint in ('events', 'snapshot'):
            with self.subTest(endpoint=endpoint):
                self.driver.get(self.base + '/chat?demo=followup')
                wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn') and
                           d.find_element(By.ID, 'message').is_enabled())
                session_id = self.driver.find_element(By.ID, 'session-id').text
                chat = self.service.sessions[session_id]['chat']
                started, release = threading.Event(), threading.Event()
                original = chat.send

                def blocked(message):
                    started.set()
                    release.wait(15)
                    return original(message)

                chat.send = blocked
                self._interrupt_polling(endpoint)
                try:
                    self.driver.find_element(By.ID, 'message').send_keys('Exclude P01 and keep the rest.')
                    self._click(self.driver.find_element(By.ID, 'send'))
                    self.assertTrue(started.wait(2))
                    wait.until(lambda d: '正在重连' in d.find_element(By.ID, 'status').text)
                    wait.until(lambda d: d.execute_script('return window.__pollFault.failures') == 2)
                    wait.until(lambda d: '已重新连接' in d.find_element(By.ID, 'status').text)
                finally:
                    release.set()
                wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) == 2)
                wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled())
                self.assertEqual(self.driver.execute_script('return window.__pollFault.messagePosts'), 1)
                self.assertEqual(self.driver.find_element(By.ID, 'session-id').text, session_id)
                self.assertTrue(self.driver.find_element(By.ID, 'chat-reconnect').get_attribute('hidden'))

    def test_poll_disconnect_stops_and_manual_reconnect_only_reads_the_same_session(self):
        self.driver.get(self.base + '/chat?demo=followup')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn') and
                   d.find_element(By.ID, 'message').is_enabled())
        session_id = self.driver.find_element(By.ID, 'session-id').text
        chat = self.service.sessions[session_id]['chat']
        started, release = threading.Event(), threading.Event()
        original = chat.send

        def blocked(message):
            started.set()
            release.wait(15)
            return original(message)

        chat.send = blocked
        self._interrupt_polling('events', always=True)
        try:
            self.driver.find_element(By.ID, 'message').send_keys('Exclude P01 and keep the rest.')
            self._click(self.driver.find_element(By.ID, 'send'))
            self.assertTrue(started.wait(2))
            wait.until(lambda d: d.find_element(By.ID, 'chat-reconnect').is_displayed())
            self.assertIn('不会重新发送消息', self.driver.find_element(By.ID, 'status').text)
            self.assertEqual(self.driver.execute_script('return window.__pollFault.attempts'), 4)
            self.assertFalse(self.driver.execute_script('return state.busy'))
            self.assertFalse(self.driver.find_element(By.ID, 'send').is_enabled())
            self.assertEqual(self.driver.execute_script('return state.pendingSessionId'), session_id)
            self.driver.execute_script('window.__pollFault.always = false; window.__pollFault.remaining = 0;')
        finally:
            release.set()
        self._click(self.driver.find_element(By.ID, 'chat-reconnect'))
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) == 2)
        wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled())
        self.assertEqual(self.driver.execute_script('return window.__pollFault.messagePosts'), 1)
        self.assertEqual(self.driver.find_element(By.ID, 'session-id').text, session_id)

        # An old response arriving after reset must not replace the new draft.
        self.driver.execute_script('''
          const original = window.fetch.bind(window);
          window.fetch = (...args) => {
            if (String(args[0]) === '/api/chat/sessions/' + state.sessionId) {
              return new Promise(resolve => {
                window.__releaseOldSnapshot = () => original(...args).then(resolve);
              });
            }
            return original(...args);
          };
          resumeSession(state.sessionId);
        ''')
        wait.until(lambda d: d.execute_script('return typeof window.__releaseOldSnapshot === "function"'))
        self.driver.execute_script('resetSession(); document.querySelector("#message").value = "New draft";')
        self.driver.execute_async_script('window.__releaseOldSnapshot().then(() => setTimeout(arguments[0], 0));')
        self.assertEqual(self.driver.find_element(By.ID, 'session-id').text, '—')
        self.assertEqual(self.driver.find_element(By.ID, 'message').get_attribute('value'), 'New draft')
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn')), 0)

    def test_exhausted_budget_keeps_new_session_source_fallback_available(self):
        created = self.service.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        session_id = created['session_id']
        for _ in range(2):
            self.service.send(session_id, {'message': 'Exclude P01 and keep the rest.'})
        self.driver.get(self.base + '/chat?session=' + session_id)
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled())
        wait.until(lambda d: not d.execute_script('return state.busy'))
        self.driver.execute_script('''
          renderResult({turn:4, status:'no_results', papers:[],
            diagnosis:{code:'source_degraded', summary:'Fixture source unavailable', actions:[
              {code:'relax_year', label:'Same-session recovery', message:'Retry', retry:true},
              {code:'new_session', label:'New source session', retry:false}
            ]}});
          applyCapabilities({feedback_phases_left:0, feedback_phases_cap:2,
            capabilities:{can_send:false, can_retry:false}});
          document.querySelector('#fork-source').value = 'multi';
        ''')
        fork = self.driver.find_element(By.CSS_SELECTOR, 'button.diagnosis-action[data-new-session="true"]')
        self.assertTrue(fork.is_enabled())
        self.assertFalse(self.driver.find_element(By.CSS_SELECTOR,
            'button.diagnosis-action:not([data-new-session])').is_enabled())
        self._click(fork)
        wait.until(lambda d: '?session=' in d.current_url and not d.current_url.endswith(session_id))
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) == 1 and
                   d.find_element(By.ID, 'message').is_enabled())
        self.assertEqual(self.driver.find_element(By.ID, 'source').get_attribute('value'), 'multi')
        self.assertEqual(len(self.service.snapshot(session_id)['history']), 3)

    def test_chat_diagnosis_offers_actionable_recovery(self):
        self.driver.get(self.base + '/chat')
        self.driver.execute_script("localStorage.clear(); resetSession();")
        self.driver.execute_script("""
          renderResult({
            turn: 1, status: 'no_results', message: 'CVPR 2026 research papers',
            papers: [], gaps: [{requirement:'paper_count', required:5, found:0, missing:5}],
            diagnosis: {
              code: 'constraints_unmet',
              summary: '目标 5 篇，已交付 0 篇；当前约束没有足够命中。 检索侧异常：OpenAlex。',
              near_misses: [{id:'openreview:x', title:'VGGT-Ω'}],
              actions: [
                {code:'relax_year', label:'不限年份', description:'保留 venue', message:'放宽年份限制，保留当前主题和会议条件，重试。', retry:true},
                {code:'relax_venue', label:'不限 venue', description:'保留主题', message:'放宽会议限制，重试。', retry:true}
              ]
            },
            metrics: {}, warnings: []
          });
        """)
        wait = WebDriverWait(self.driver, 5)
        note = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#turns .turn-note.diagnosis'))
        self.assertIn('当前约束没有足够命中', note.text)
        self.assertIn('VGGT-Ω', note.text)
        self.assertTrue(note.find_elements(By.CSS_SELECTOR, '.diagnosis-action'))
        # This fixture tests diagnosis rendering, independently of setUp's
        # saved demo job. Clear it from a passive page so asynchronous Search
        # restoration cannot later replace the injected diagnosis result.
        self.driver.get(self.base + '/settings')
        self.driver.execute_script('sessionStorage.clear(); localStorage.clear();')
        self.driver.get(self.base + '/search')
        wait.until(lambda d: d.execute_script('return Boolean(state.config) && !state.busy && !state.job;'))
        self.driver.execute_script("""
          renderResult({
            status:'no_results', mode:'live', request:{topic:'CVPR 2026', target_count:5},
            papers:[], gaps:[{requirement:'paper_count', required:5, found:0, missing:5}],
            diagnosis:{code:'constraints_unmet', summary:'没有足够 CVPR 2026 命中。',
              near_misses:[{title:'VGGT-Ω'}], actions:[{label:'不限年份'}]},
            metrics:{}, warnings:[]
          });
        """)
        diag = wait.until(lambda d: d.find_element(By.ID, 'diagnosis-section'))
        self.assertFalse(diag.get_attribute('hidden'))
        self.assertIn('没有足够 CVPR 2026 命中', diag.text)
        self.assertIn('VGGT-Ω', diag.text)

    def test_search_result_links_to_imported_chat_session(self):
        wait = WebDriverWait(self.driver, 20)
        link = wait.until(lambda d: d.find_element(By.ID, 'continue-chat'))
        wait.until(lambda d: link.is_displayed())
        href = link.get_attribute('href')
        self.assertIn('/chat?session=', href)
        self.driver.get(href)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn-paper')) >= 5)
        self.assertIn(href.rsplit('=', 1)[1], self.driver.find_element(By.ID, 'session-id').text)
        wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled())
        box = self.driver.find_element(By.ID, 'message')
        box.clear(); box.send_keys('Add one survey paper.')
        self._click(self.driver.find_element(By.ID, 'send'))
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) >= 2)

    def test_search_trace_details_stay_inside_card(self):
        self.driver.get(self.base + '/search?demo=followup')
        WebDriverWait(self.driver, 20).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, '#trace-list li')) > 3)
        def overflow_at(width):
            self.driver.set_window_size(width, 900)
            WebDriverWait(self.driver, 5).until(
                lambda d: d.execute_script('return document.querySelector(".trace-card").getBoundingClientRect().right') > 0)
            return self.driver.execute_script('''
                const card = document.querySelector('.trace-card').getBoundingClientRect();
                const bad = [...document.querySelectorAll('#trace-list small')].filter(x => {
                    const r = x.getBoundingClientRect();
                    return r.right > card.right + 0.5;
                });
                return {bad: bad.length, scroll: document.documentElement.scrollWidth,
                        client: document.documentElement.clientWidth};
            ''')
        for width in (1440, 390):
            result = overflow_at(width)
            self.assertEqual(result['bad'], 0, result)
            self.assertLessEqual(result['scroll'], result['client'], result)
        self.driver.set_window_size(1440, 1000)

    def test_search_trace_labels_provider_step(self):
        self.driver.get(self.base + '/search?demo=followup')
        WebDriverWait(self.driver, 20).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, '#paper-list .paper'))
        trace = self.driver.find_element(By.ID, 'trace-list').text
        self.assertIn('读取模型配置', trace)
        details = self.driver.find_element(By.ID, 'trace-list').get_attribute('textContent').lower()
        self.assertIn('profile', details)
        self.assertIn('model', details)

    def test_cost_breakdown_is_visible_in_search_result(self):
        self.driver.get(self.base + '/search')
        WebDriverWait(self.driver, 10).until(lambda d: d.execute_script(
            'return Boolean(state.config) && !state.busy && !state.polling;'))
        self.driver.execute_script("""
          renderResult({
            status: 'complete', mode: 'live', request: {topic: 'RAG', target_count: 5},
            papers: [], gaps: [], metrics: {search_rounds: 1, elapsed_seconds: .5, llm_calls: 2},
            cost: {input_tokens: 100, output_tokens: 20, input_price: 2, output_price: 8,
                   model: .00036, tools: .005, total: .00536, currency: 'CNY',
                   estimate: true, known: true},
            trace: [], warnings: []
          });
        """)
        wait = WebDriverWait(self.driver, 5)
        self._click(self.driver.find_element(By.CSS_SELECTOR, '#run-details summary'))
        line = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '.cost-breakdown'))
        self.assertIn('100 in', line.text)
        self.assertIn('prices 2 / 8', line.text)
        self.assertIn('tools 0.005', line.text)

    def test_unknown_tool_cost_and_currencies_are_visible_on_mobile(self):
        self.driver.set_window_size(390, 900)
        try:
            self.driver.get(self.base + '/search')
            WebDriverWait(self.driver, 10).until(lambda d: d.execute_script(
                'return Boolean(state.config) && !state.busy && !state.polling;'))
            self.driver.execute_script("""
              renderResult({
                status: 'partial', mode: 'live', request: {topic: 'RAG', target_count: 5},
                papers: [], gaps: [], metrics: {search_rounds: 1, elapsed_seconds: .5, llm_calls: 2},
                cost: {input_tokens: 100, output_tokens: 20, input_price: 2, output_price: 8,
                       model: .00036, tools: null, total: null, currency: 'USD',
                       tools_currency: 'CNY', estimate: true, known: false},
                trace: [], warnings: []
              });
            """)
            self._click(self.driver.find_element(By.CSS_SELECTOR, '#run-details summary'))
            text = self.driver.find_element(By.CSS_SELECTOR, '.cost-breakdown').text
            self.assertIn('model 0.00036 USD', text)
            self.assertIn('tools unknown CNY', text)
            self.assertIn('unknown USD total', text)
            dimensions = self.driver.execute_script(
                'return [document.documentElement.clientWidth, document.documentElement.scrollWidth]')
            self.assertLessEqual(dimensions[1], dimensions[0])
            self.driver.get(self.base + '/activity')
            WebDriverWait(self.driver, 10).until(
                lambda d: d.execute_script('return typeof renderCostBreakdown') == 'function')
            self.driver.execute_script("""
              renderCostBreakdown({result: {cost: {model: .00036, tools: null,
                total: null, currency: 'USD', tools_currency: 'CNY', estimate: true}}});
            """)
            text = self.driver.execute_script(
                'return document.querySelector("#activity-cost-breakdown").innerText')
            self.assertIn('模型 0.00036 USD', text)
            self.assertIn('工具 unknown CNY', text)
        finally:
            self.driver.set_window_size(1280, 1000)

    def test_partial_candidates_are_visible_without_recommendation_label(self):
        self.driver.set_window_size(390, 900)
        try:
            self.driver.get(self.base + '/search')
            WebDriverWait(self.driver, 10).until(lambda d: d.execute_script(
                'return Boolean(state.config) && !state.busy && !state.polling;'))
            self.driver.execute_script("""
              renderResult({status:'no_results', stop_reason:'user_cancelled', mode:'live',
                request:{topic:'RAG', target_count:5}, papers:[], gaps:[], metrics:{},
                candidates:[{paper_id:'lune:first', title:'Partial candidate', url:'javascript:alert(1)'}],
                trace:[], warnings:[]});
            """)
            details = self.driver.find_element(By.CSS_SELECTOR, '#paper-list .candidate-discovery')
            self.assertIn('1 篇未交付候选', details.text)
            self._click(details.find_element(By.TAG_NAME, 'summary'))
            self.assertIn('Partial candidate', details.text)
            self.assertEqual(details.find_elements(By.TAG_NAME, 'a'), [])
            self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list .paper'), [])
            self.driver.get(self.base + '/chat')
            WebDriverWait(self.driver, 10).until(
                lambda d: d.execute_script('return typeof renderResult') == 'function')
            self.driver.execute_script("""
              renderResult({turn:77, status:'no_results', stop_reason:'user_cancelled',
                request:{topic:'RAG', target_count:5}, papers:[], gaps:[],
                candidates:[{paper_id:'lune:first', title:'Partial candidate', url:'javascript:alert(1)'}]});
            """)
            details = self.driver.find_element(By.CSS_SELECTOR, '#turn-77 .turn-candidates')
            self.assertIn('1 篇未交付候选', details.text)
            self._click(details.find_element(By.TAG_NAME, 'summary'))
            self.assertIn('Partial candidate', details.text)
            self.assertEqual(details.find_elements(By.TAG_NAME, 'a'), [])
            self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#turn-77 .turn-paper'), [])
            dimensions = self.driver.execute_script(
                'return [document.documentElement.clientWidth, document.documentElement.scrollWidth]')
            self.assertLessEqual(dimensions[1], dimensions[0])
        finally:
            self.driver.set_window_size(1280, 1000)

    def test_settings_create_activate_and_delete_via_ui(self):
        self.driver.get(self.base + '/settings')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#provider-list .provider-item')) >= 1)
        base = len(self.driver.find_elements(By.CSS_SELECTOR, '#provider-list .provider-item'))
        self._click(self.driver.find_element(By.ID, 'new-provider'))
        self.driver.find_element(By.ID, 'provider-name').send_keys('UI test provider')
        self.driver.find_element(By.ID, 'provider-url').send_keys('https://example.invalid/v1')
        self.driver.find_element(By.ID, 'provider-model').send_keys('ui-model')
        self.driver.find_element(By.ID, 'provider-key').send_keys('ui-secret-key')
        self._click(self.driver.find_element(By.ID, 'save-provider'))
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#provider-list .provider-item')) >= base + 1)
        self.assertIn('UI test provider', self.driver.find_element(By.ID, 'active-model-label').text)
        self._click(self.driver.find_element(By.ID, 'delete-provider'))
        self.driver.switch_to.alert.accept()
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#provider-list .provider-item')) == base)

    def test_activity_cost_breakdown_renders_tokens_and_prices(self):
        self.driver.get(self.base + '/activity')
        WebDriverWait(self.driver, 10).until(
            lambda d: d.execute_script('return typeof renderCostBreakdown') == 'function')
        self.driver.execute_script("""
          renderCostBreakdown({result: {cost: {
            input_tokens: 100, output_tokens: 20, input_price: 2, output_price: 8,
            model: .00036, tools: .005, total: .00536, currency: 'CNY', estimate: true
          }}});
        """)
        text = self.driver.execute_script(
            'return document.querySelector("#activity-cost-breakdown").innerText')
        self.assertIn('100 输入', text)
        self.assertIn('单价 2 / 8 每 1M', text)
        self.assertIn('工具 0.005', text)

    def test_settings_page_masks_key_and_mobile_does_not_overflow(self):
        self.driver.set_window_size(390, 900)
        self.driver.get(self.base + '/settings')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#provider-list .provider-item')) >= 1)
        key = self.driver.find_element(By.ID, 'provider-key')
        self.assertEqual(key.get_attribute('value'), '')
        self.assertIn('••••', key.get_attribute('placeholder'))
        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '#active-model-label .model-provider'))
        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '#active-model-label .model-name'))
        self.assertEqual(
            self.driver.execute_script('return document.documentElement.scrollWidth'),
            self.driver.execute_script('return document.documentElement.clientWidth'))
        self.assertNotIn('sk-secret', self.driver.page_source)

    def test_index_and_chat_have_no_horizontal_overflow_at_390(self):
        self.driver.set_window_size(390, 900)
        for path in ('/search', '/chat'):
            self.driver.get(self.base + path)
            WebDriverWait(self.driver, 10).until(
                lambda d: d.execute_script('return document.readyState') == 'complete')
            self.assertEqual(
                self.driver.execute_script('return document.documentElement.scrollWidth'),
                self.driver.execute_script('return document.documentElement.clientWidth'), path)

    def test_activity_has_no_horizontal_overflow_at_390(self):
        self.driver.set_window_size(390, 900)
        self.driver.get(self.base + '/activity')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: d.find_element(By.ID, 'activity-story').is_displayed())
        self.assertEqual(
            self.driver.execute_script('return document.documentElement.scrollWidth'),
            self.driver.execute_script('return document.documentElement.clientWidth'))

    def test_stale_local_session_is_cleared(self):
        self.driver.get(self.base + '/chat')
        self.driver.execute_script(
            "localStorage.setItem('scholarguide-chat-session', 'f'.repeat(32))")
        self.driver.get(self.base + '/chat?stale=1')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: '已切换为新会话' in d.find_element(By.ID, 'status').text)
        self.assertEqual(self.driver.find_element(By.ID, 'session-id').text, '—')
        self.assertIsNone(self.driver.execute_script(
            "return localStorage.getItem('scholarguide-chat-session')"))
        self.assertTrue(self.driver.find_element(By.ID, 'empty-chat').is_displayed())

    def test_chat_has_no_empty_doi_links_and_has_source_selector(self):
        self.driver.get(self.base + '/chat?demo=followup')
        WebDriverWait(self.driver, 20).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn-paper'))
        links = self.driver.find_elements(By.CSS_SELECTOR, '#turns .paper-link')
        for link in links:
            self.assertNotEqual(link.get_attribute('href'), 'https://doi.org/')
        # Demo has a fixed fixture; source selection is a Live-only control.
        self.assertFalse(self.driver.find_element(By.ID, 'source').is_displayed())
        self.driver.execute_script("resetSession(); document.querySelector('#mode').value='live'; document.querySelector('#mode').dispatchEvent(new Event('change'));")
        self.assertTrue(self.driver.find_element(By.ID, 'source').is_displayed())

    def test_chat_history_navigation(self):
        self.driver.get(self.base + '/chat?demo=followup')
        wait = WebDriverWait(self.driver, 20)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) == 1)
        box = self.driver.find_element(By.ID, 'message')
        box.clear()
        box.send_keys('Add one survey paper as well.')
        self._click(self.driver.find_element(By.ID, 'send'))
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) == 2)
        items = self.driver.find_elements(By.CSS_SELECTOR, '#history li button')
        self.assertEqual(len(items), 2)
        self._click(items[-1])  # oldest turn
        self.assertTrue(self.driver.find_element(By.ID, 'turn-1').is_displayed())


if __name__ == '__main__':
    unittest.main()
