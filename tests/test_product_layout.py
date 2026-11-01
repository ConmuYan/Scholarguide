"""Geometry guards for readable mobile cards, beyond mere overflow checks."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_ui_browser as browser_support


class ProductLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        browser_support.BrowserUiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        browser_support.BrowserUiTests.tearDownClass.__func__(cls)

    def test_mobile_search_and_chat_keep_text_columns_readable(self):
        self.driver.set_window_size(390, 900)
        wait = browser_support.WebDriverWait(self.driver, 15)
        for path, selector in (('/search?demo=followup', '#paper-list .paper-body'),
                               ('/chat?demo=followup', '#turns .turn-paper h4')):
            with self.subTest(path=path):
                self.driver.get(self.base + path)
                wait.until(lambda d: len(d.find_elements(browser_support.By.CSS_SELECTOR, selector)) >= 5)
                widths = self.driver.execute_script('return [...document.querySelectorAll(arguments[0])].map(n=>n.getBoundingClientRect().width)', selector)
                self.assertTrue(all(width >= 220 for width in widths), widths)
                self.assertLessEqual(self.driver.execute_script('return document.documentElement.scrollWidth'),
                                     self.driver.execute_script('return document.documentElement.clientWidth'))


if __name__ == '__main__':
    unittest.main()
