"""End-to-end local preference UI against the actual application server."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait

from activity import ActivityFeed
from providers import ProviderStore
from scripts.verify_delivery import apply_browser_isolation, isolated_environment
from webapp import ChatService, Jobs, create_server


class MemoryLiveUiTests(unittest.TestCase):
    def test_real_settings_grant_revoke_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = isolated_environment(root)
            env['SG_MEMORY_STORE'] = str(root / 'memory.sqlite3')
            with patch.dict(os.environ, env, clear=True):
                providers = ProviderStore(root / 'providers.json')
                chat = ChatService(root / 'chat', providers=providers)
                jobs = Jobs(root / 'runs', providers=providers)
                server = create_server(0, jobs=jobs, chat=chat, providers=providers,
                                       activity=ActivityFeed(root / 'runs'))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                options = Options()
                apply_browser_isolation(options, root / 'chrome')
                for flag in ('--headless=new', '--no-sandbox', '--disable-gpu'):
                    options.add_argument(flag)
                options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
                driver = None
                try:
                    driver = webdriver.Chrome(options=options)
                    driver.set_window_size(390, 900)
                    driver.get(f'http://127.0.0.1:{server.server_port}/settings')
                    wait = WebDriverWait(driver, 10)
                    wait.until(lambda d: '已读取本机偏好' in d.find_element(By.ID, 'memory-status').text)
                    self.assertTrue((root / 'memory.sqlite3').exists())
                    form = driver.find_element(By.CSS_SELECTOR, '[data-key="reading_goal"]')
                    Select(form.find_element(By.NAME, 'value')).select_by_value('orientation')
                    driver.execute_script('''
                      const form=arguments[0], d=new Date(Date.now()+86400000);
                      d.setMinutes(d.getMinutes()-d.getTimezoneOffset());
                      form.elements.expiry.value=d.toISOString().slice(0,16);
                      form.elements.consent.checked=true;
                    ''', form)
                    driver.execute_script('arguments[0].requestSubmit()', form)
                    wait.until(lambda d: '已授权' in d.find_element(By.CSS_SELECTOR,
                               '[data-key="reading_goal"] .memory-current').text)
                    self.assertTrue((root / 'memory.sqlite3').exists())
                    self.assertIn('未启用', driver.find_element(By.ID, 'memory-section').text)
                    driver.execute_script('arguments[0].click()', form.find_element(By.CSS_SELECTOR, '[data-action="revoke"]'))
                    driver.switch_to.alert.accept()
                    wait.until(lambda d: '已撤回' in d.find_element(By.CSS_SELECTOR,
                               '[data-key="reading_goal"] .memory-current').text)
                    driver.execute_script('arguments[0].click()', form.find_element(By.CSS_SELECTOR, '[data-action="delete"]'))
                    driver.switch_to.alert.accept()
                    wait.until(lambda d: '已删除' in d.find_element(By.CSS_SELECTOR,
                               '[data-key="reading_goal"] .memory-current').text)
                    self.assertLessEqual(driver.execute_script('return document.documentElement.scrollWidth'),
                                         driver.execute_script('return document.documentElement.clientWidth'))
                    errors = [row for row in driver.get_log('browser') if row['level'] == 'SEVERE']
                    self.assertEqual(errors, [])
                finally:
                    if driver is not None:
                        driver.quit()
                    server.shutdown(); server.server_close(); thread.join(3); chat.close()


if __name__ == '__main__':
    unittest.main()
