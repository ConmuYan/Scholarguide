"""Browser acceptance against an isolated HTTP service; no model or source API.

Blocked deterministic demo workers exercise the real submit, poll, resume and
rename endpoints. Renderers are not called directly from the browser fixture.
"""
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import test_experience_upgrade as _harness
from demo import demo_run
from session import ChatSession
try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
except ImportError:
    pass


class DeepUIUpgradeTests(unittest.TestCase):
    # Reuse only the temporary-service/browser harness, not its test methods.
    setUpClass = classmethod(_harness.ExperienceUpgradeTests.setUpClass.__func__)
    tearDownClass = classmethod(_harness.ExperienceUpgradeTests.tearDownClass.__func__)
    setUp = _harness.ExperienceUpgradeTests.setUp
    tearDown = _harness.ExperienceUpgradeTests.tearDown
    click = _harness.ExperienceUpgradeTests.click
    set_theme = _harness.ExperienceUpgradeTests.set_theme
    capture_element = _harness.ExperienceUpgradeTests.capture_element

    def shot(self, name, selector='body'):
        output = os.environ.get('SG_DEEP_UI_SHOTS')
        if output:
            self.capture_element(selector, Path(output) / (name + '.png'))

    def viewport(self, width):
        self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
            'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})

    def assert_no_overflow(self):
        self.assertLessEqual(self.driver.execute_script('return document.documentElement.scrollWidth;'),
                             self.driver.execute_script('return document.documentElement.clientWidth;'))

    def wait_chat(self):
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        self.wait.until(lambda d: not d.execute_script('return state.busy;'))
        return self.driver.find_element(By.ID, 'session-id').text

    def test_search_live_counts_and_completion_preserve_focus_and_scroll(self):
        entered, release = threading.Event(), threading.Event()

        def runner(data, progress):
            for event in [dict(action='search_result', returned=42, new_candidates=42, unique_candidates=42),
                          dict(action='assess', assessed=3, eligible=2),
                          dict(action='synthesis', status='preparing')]:
                progress(event)
            entered.set()
            release.wait(40)
            return demo_run('satisfied', 'A')

        with patch.object(self.jobs, 'runner', runner):
            try:
                self.driver.get(self.base + '/search?demo=satisfied')
                self.assertTrue(entered.wait(5))
                expected = ('候选论文', '次摘要核对', '当前可推荐')
                self.wait.until(lambda d: all(value in d.find_element(By.ID, 'search-progress').text
                                              for value in expected))
                facts = self.driver.find_element(By.ID, 'search-progress').text
                for value in expected:
                    self.assertIn(value, facts)
                # The title follows the newest rendered trace event, so wait for
                # the synthesis event instead of assuming it was already drawn.
                self.wait.until(lambda d: '正在整理研究脉络' in
                                d.find_element(By.ID, 'running-title').text)
                self.assertNotIn('%', facts)
                for width in (1440, 390):
                    self.viewport(width)
                    for theme in ('light', 'dark'):
                        self.set_theme(theme)
                        self.assert_no_overflow()
                        self.shot(f'search-running-{width}-{theme}', '.results-card')
                self.driver.execute_script('window.scrollTo(0,0); document.querySelector("#theme-toggle").focus();')
                old_scroll = self.driver.execute_script('return scrollY;')
                release.set()
                self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#paper-list .paper'))
                self.assertEqual(self.driver.execute_script('return document.activeElement.id;'), 'theme-toggle')
                self.assertAlmostEqual(self.driver.execute_script('return scrollY;'), old_scroll, delta=2)
                self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, '.result-arrival').is_displayed())
            finally:
                release.set()

    def test_reading_navigation_supports_keyboard_round_trip_and_themes(self):
        self.driver.get(self.base + '/search?demo=followup')
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#paper-list .paper'))
        links = self.driver.find_elements(By.CSS_SELECTOR, '.reading-nav a')
        self.assertEqual(len(links), len(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list .paper')))
        self.driver.execute_script('document.querySelector(".result-arrival")?.remove();')
        links[2].send_keys(Keys.ENTER)
        self.wait.until(lambda d: d.execute_script('return document.activeElement.id;') == 'search-paper-3')
        self.click('#search-paper-3 .paper-details > summary')
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, '#search-paper-3 .paper-abstract').is_displayed())
        self.click('#search-paper-3 .reading-back')
        self.assertEqual(self.driver.execute_script('return document.activeElement.id;'), 'search-reading')
        for width in (1440, 390):
            self.viewport(width)
            for theme in ('light', 'dark'):
                self.set_theme(theme)
                self.assert_no_overflow()
                self.shot(f'search-reading-{width}-{theme}', '.results-card')

    def test_running_chat_draft_survives_http_resume_and_completion(self):
        entered, release = threading.Event(), threading.Event()
        original = ChatSession.start

        def blocked(chat, *args, **kwargs):
            chat.store.append_event(chat.session_id, 'search', {'query': 'offline test', 'returned': 9, 'new': 7})
            chat.store.append_event(chat.session_id, 'assess', {'assessed': 3, 'verified': 2})
            chat.store.commit()
            entered.set(); release.wait(40)
            return original(chat, *args, **kwargs)

        with patch.object(ChatSession, 'start', blocked):
            try:
                self.driver.get(self.base + '/chat?demo=followup')
                self.assertTrue(entered.wait(5))
                self.wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled() and
                                 d.find_element(By.ID, 'message').get_attribute('value') == '')
                self.wait.until(lambda d: '引文匹配' in d.find_element(By.ID, 'chat-progress').text)
                self.assertNotIn('可推荐', self.driver.find_element(By.ID, 'chat-progress').text)
                session_id = self.driver.find_element(By.ID, 'session-id').text
                draft = '下一轮重点比较评测，先保留这份草稿。'
                self.driver.find_element(By.ID, 'message').send_keys(draft)
                self.assertTrue(self.driver.find_element(By.ID, 'send').get_attribute('disabled'))
                self.driver.find_element(By.ID, 'message').send_keys(Keys.ENTER)
                self.driver.refresh()
                self.wait.until(lambda d: d.find_element(By.ID, 'session-id').text == session_id)
                self.wait.until(lambda d: d.find_element(By.ID, 'message').is_enabled())
                self.assertEqual(self.driver.find_element(By.ID, 'message').get_attribute('value'), draft)
                self.assertTrue(self.driver.find_element(By.ID, 'send').get_attribute('disabled'))
                self.viewport(390); self.set_theme('dark'); self.assert_no_overflow()
                self.shot('chat-running-draft-390-dark', '.chat-panel')
                self.driver.execute_script('document.querySelector("#message").focus();')
                release.set()
                self.wait_chat()
                self.assertEqual(self.driver.find_element(By.ID, 'message').get_attribute('value'), draft)
                self.assertEqual(self.driver.execute_script('return document.activeElement.id;'), 'message')
                self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn')), 1)
                self.assertIn('session=' + session_id, self.driver.current_url)
            finally:
                release.set()

    def test_chat_reading_navigation_does_not_auto_jump_on_completion(self):
        self.driver.get(self.base + '/chat?demo=followup')
        self.wait_chat()
        for width in (1440, 390):
            self.viewport(width)
            for theme in ('light', 'dark'):
                self.set_theme(theme); self.assert_no_overflow()
                self.shot(f'chat-reading-{width}-{theme}', '.chat-panel')
        link = self.driver.find_element(By.CSS_SELECTOR, '#turn-1 .reading-nav a:nth-child(1)')
        link.send_keys(Keys.ENTER)
        self.assertEqual(self.driver.execute_script('return document.activeElement.id;'), 'turn-1-paper-1')
        self.click('#turn-1-paper-1 .reading-back')
        self.assertEqual(self.driver.execute_script('return document.activeElement.id;'), 'turn-1-reading')

    def test_rename_filter_and_switch_keep_each_sessions_draft(self):
        self.driver.get(self.base + '/chat?demo=followup')
        first = self.wait_chat()
        self.click('#rename-session')
        title = self.driver.find_element(By.ID, 'session-title-input')
        title.clear(); title.send_keys('我的 RAG 研究 <安全标题>')
        self.click('#save-session-title')
        self.wait.until(lambda d: d.find_element(By.ID, 'session-title').text == '我的 RAG 研究 <安全标题>')
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, '#session-title script')), 0)
        self.driver.find_element(By.ID, 'message').send_keys('第一份会话独立草稿')
        self.click('#new-session')
        self.click('[data-example="demo"]'); self.click('#send')
        second = self.wait_chat()
        self.assertNotEqual(first, second)
        self.driver.find_element(By.ID, 'message').send_keys('第二份会话独立草稿')
        self.driver.find_element(By.ID, 'session-search').send_keys('安全标题')
        self.wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#saved-sessions .saved-item')) == 1)
        self.click('#saved-sessions .saved-item')
        self.wait.until(lambda d: d.find_element(By.ID, 'session-id').text == first and not d.execute_script('return state.busy;'))
        self.assertEqual(self.driver.find_element(By.ID, 'message').get_attribute('value'), '第一份会话独立草稿')
        self.assertIn('session=' + first, self.driver.current_url)
        query = self.driver.find_element(By.ID, 'session-search'); query.clear(); query.send_keys(second)
        self.wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#saved-sessions .saved-item')) == 1)
        self.click('#saved-sessions .saved-item')
        self.wait.until(lambda d: d.find_element(By.ID, 'session-id').text == second and not d.execute_script('return state.busy;'))
        self.assertEqual(self.driver.find_element(By.ID, 'message').get_attribute('value'), '第二份会话独立草稿')
        self.click('#rename-session')
        self.driver.find_element(By.ID, 'session-title-input').clear()
        self.click('#save-session-title')
        self.wait.until(lambda d: not d.find_element(By.ID, 'session-title-form').is_displayed())
        self.assertTrue(self.driver.find_element(By.ID, 'session-title').text)
        self.assertEqual(self.driver.find_element(By.ID, 'message').get_attribute('value'), '第二份会话独立草稿')
        for width in (1440, 390):
            self.viewport(width)
            for theme in ('light', 'dark'):
                self.set_theme(theme); self.assert_no_overflow()
                self.shot(f'session-manager-{width}-{theme}', '.session-panel')


if __name__ == '__main__':
    unittest.main()
