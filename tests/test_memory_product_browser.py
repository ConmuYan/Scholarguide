"""Real local Chat and isolated Chrome presentation checks."""
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from memory_store import MemoryStore
from scripts.verify_delivery import apply_browser_isolation
from test_memory_product_view import ProductViewTests


class ProductBrowserTests(ProductViewTests):
    def test_late_response_cannot_change_switched_session(self):
        from memory_view import MemoryView
        with MemoryStore(self.db) as store:
            store.grant('prefer_recent', True, consent=True, expires_at=time.time() + 300,
                        expected_revision=0)
        other = self.chat.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})['session_id']
        started, release = threading.Event(), threading.Event()
        original = MemoryView.present
        def delayed(view, *args, **kwargs):
            started.set()
            release.wait(10)
            return original(view, *args, **kwargs)
        options = Options()
        apply_browser_isolation(options, self.root / 'chrome-switch')
        for flag in ('--headless=new', '--no-sandbox', '--disable-gpu'):
            options.add_argument(flag)
        driver = webdriver.Chrome(options=options)
        self.addCleanup(driver.quit)
        wait = WebDriverWait(driver, 15)
        try:
            with patch.object(MemoryView, 'present', delayed):
                driver.get(f'http://127.0.0.1:{self.server.server_port}/chat?session={self.session}')
                button = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#turn-1 button[aria-pressed]'))
                driver.execute_script('arguments[0].click()', button)
                self.assertTrue(started.wait(5))
                driver.execute_script('resumeSession(arguments[0])', other)
                wait.until(lambda d: d.find_element(By.ID, 'session-id').text == other)
                release.set()
                wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#turn-1 button[aria-pressed]'))
                time.sleep(.5)
                self.assertEqual(driver.find_element(By.CSS_SELECTOR, '#turn-1 button[aria-pressed]').get_attribute('aria-pressed'), 'false')
        finally:
            release.set()

    def test_actual_chat_opt_in_and_revoke(self):
        with MemoryStore(self.db) as store:
            store.grant('prefer_recent', True, consent=True, expires_at=time.time() + 300,
                        expected_revision=0)
        before = self.chat.snapshot(self.session)
        options = Options()
        apply_browser_isolation(options, self.root / 'chrome')
        for flag in ('--headless=new', '--no-sandbox', '--disable-gpu'):
            options.add_argument(flag)
        driver = webdriver.Chrome(options=options)
        self.addCleanup(driver.quit)
        wait = WebDriverWait(driver, 15)
        with patch.object(self.chat, 'presentation', wraps=self.chat.presentation) as present:
            for width in (1280, 390):
                driver.set_window_size(width, 900)
                driver.get(f'http://127.0.0.1:{self.server.server_port}/chat?session={self.session}')
                button = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#turn-1 button[aria-pressed]'))
                self.assertEqual(present.call_count, 0)
                driver.execute_script('arguments[0].click()', button)
                wait.until(lambda d: button.get_attribute('aria-pressed') == 'true')
                titles = [e.text for e in driver.find_elements(By.CSS_SELECTOR, '#turn-1 .reading-nav-title')]
                expected = sorted(before['history'][0]['papers'], key=lambda p: (0, -p['year'])
                                  if type(p['year']) is int and 1 <= p['year'] <= 9999 else (1, 0))
                self.assertEqual(titles, [p['title'] for p in expected])
                exclude = driver.find_element(By.CSS_SELECTOR, '#turn-1 .followup-chip')
                self.assertEqual(exclude.text, '排除第一篇')
                driver.execute_script('arguments[0].click()', exclude)
                self.assertIn(expected[0]['id'], driver.find_element(By.ID, 'message').get_attribute('value'))
                driver.execute_script('arguments[0].click()', button)
                self.assertEqual(button.get_attribute('aria-pressed'), 'false')
                self.assertEqual([e.text for e in driver.find_elements(By.CSS_SELECTOR, '#turn-1 .reading-nav-title')],
                                 [p['title'] for p in before['history'][0]['papers']])
                self.assertLessEqual(driver.execute_script('return document.documentElement.scrollWidth'),
                                     driver.execute_script('return document.documentElement.clientWidth'))
                present.reset_mock()
            with MemoryStore(self.db) as store:
                store.revoke('prefer_recent', expected_revision=1)
            driver.execute_script('arguments[0].click()', button)
            wait.until(lambda d: '无法应用偏好' in d.find_element(By.CSS_SELECTOR, '#turn-1 [role=status]').text)
            self.assertEqual(button.get_attribute('aria-pressed'), 'false')
        self.assertEqual(self.chat.snapshot(self.session), before)


if __name__ == '__main__':
    unittest.main()
