"""Real Chrome checks for the local ScholarGuide home page."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_ui_browser as browser_support


class LandingBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        browser_support.BrowserUiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        browser_support.BrowserUiTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.session_count = len(self.service.sessions)
        self.driver.set_window_size(1440, 1000)
        self.driver.get(self.base + '/settings')
        self.driver.execute_script("localStorage.clear(); sessionStorage.clear(); localStorage.setItem('scholarguide-theme','light');")
        self.driver.get(self.base + '/')
        self.wait = browser_support.WebDriverWait(self.driver, 20)
        self.wait.until(lambda driver: driver.find_elements(browser_support.By.ID, 'landing-main'))

    def click(self, selector):
        element = self.driver.find_element(browser_support.By.CSS_SELECTOR, selector)
        self.driver.execute_script('arguments[0].scrollIntoView({block:"center"});', element)
        element.click()

    def test_start_cta_and_workspace_brand_make_a_round_trip(self):
        self.assertEqual(len(self.service.sessions), self.session_count)
        self.click('#start-research')
        self.wait.until(lambda driver: driver.find_elements(browser_support.By.ID, 'search-form'))
        self.assertEqual(self.driver.current_url, self.base + '/search')
        self.click('.brand')
        self.wait.until(lambda driver: driver.find_elements(browser_support.By.ID, 'landing-main'))
        self.assertEqual(len(self.service.sessions), self.session_count)

    def test_demo_cta_opens_a_labelled_offline_result(self):
        self.click('#try-demo')
        self.wait.until(lambda driver: len(driver.find_elements(browser_support.By.CSS_SELECTOR, '#paper-list article')) == 5)
        self.assertIn('/search?demo=followup', self.driver.current_url)
        self.assertIn('演示', self.driver.find_element(browser_support.By.ID, 'result-section').text)

    def test_theme_preference_is_shared_with_workspace(self):
        self.click('#theme-toggle')
        self.assertEqual(self.driver.execute_script('return document.documentElement.dataset.theme'), 'dark')
        background = self.driver.execute_script('return getComputedStyle(document.body).backgroundColor')
        self.click('#start-research')
        self.wait.until(lambda driver: driver.find_elements(browser_support.By.ID, 'search-form'))
        self.assertEqual(self.driver.execute_script('return document.documentElement.dataset.theme'), 'dark')
        self.assertEqual(self.driver.execute_script('return getComputedStyle(document.body).backgroundColor'), background)

    def test_mobile_navigation_is_visible_and_keyboard_operable(self):
        self.driver.set_window_size(390, 900)
        links = self.driver.find_elements(browser_support.By.CSS_SELECTOR, '#landing-nav a')
        self.assertEqual(len(links), 5)
        self.assertTrue(all(link.is_displayed() and link.rect['height'] >= 44 for link in links))
        chat = self.driver.find_element(browser_support.By.CSS_SELECTOR, '#landing-nav a[href="/chat"]')
        self.driver.execute_script('arguments[0].focus()', chat)
        self.driver.switch_to.active_element.send_keys(browser_support.Keys.ENTER)
        self.wait.until(lambda driver: driver.current_url == self.base + '/chat')
        self.wait.until(lambda driver: driver.find_elements(browser_support.By.ID, 'composer'))

    def test_preview_minimal_controls_pause_locally_and_keep_the_specimen_label(self):
        self.assertEqual(self.driver.find_elements(browser_support.By.CSS_SELECTOR,
            '#film-progress, #film-time, [data-film-chapter], '
            '#research-film input[type="range"], #research-film [role="slider"]'), [])
        film = self.driver.find_element(browser_support.By.ID, 'research-film')
        self.driver.execute_script('arguments[0].scrollIntoView({block:"center",behavior:"instant"});', film)
        self.wait.until(lambda driver: film.get_attribute('data-playing') == 'true')
        self.click('#film-play')
        self.wait.until(lambda driver: film.get_attribute('data-playing') == 'false')
        self.assertEqual(self.driver.find_element(browser_support.By.ID, 'film-play').get_attribute('data-state'), 'paused')
        self.click('#film-play')
        self.wait.until(lambda driver: film.get_attribute('data-playing') == 'true')
        self.assertIn('虚拟样例', self.driver.find_element(browser_support.By.ID, 'preview-caption').text)
        self.assertEqual(len(self.service.sessions), self.session_count)

    def test_layout_and_local_preview_remain_readable_at_three_sizes(self):
        for width in (390, 768, 1440):
            with self.subTest(width=width):
                self.driver.set_window_size(width, 1000)
                dimensions = self.driver.execute_script('return {width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}')
                self.assertLessEqual(dimensions['scroll'], dimensions['width'])
                title = self.driver.find_element(browser_support.By.CSS_SELECTOR, '#landing-main h1')
                self.assertGreater(title.rect['width'], 240)
                self.assertTrue(self.driver.find_element(browser_support.By.ID, 'start-research').is_displayed())
        self.assertEqual(len(self.service.sessions), self.session_count)
        errors = [entry for entry in self.driver.get_log('browser') if entry['level'] == 'SEVERE']
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
