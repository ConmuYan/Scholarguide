"""Real Chrome checks for the home page's local, code-rendered walkthrough.

The fixture owns a temporary service and never touches the user's running app.
Deterministic frames use the public export event; playback uses the real clock.
"""
from pathlib import Path
import re
import sys
import time
import unittest
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_ui_browser as browser_support


By = browser_support.By if browser_support.webdriver is not None else None
Keys = browser_support.Keys if browser_support.webdriver is not None else None


class LandingMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        browser_support.BrowserUiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        browser_support.BrowserUiTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.session_count = len(self.service.sessions)
        self.driver.execute_cdp_cmd('Emulation.setScriptExecutionDisabled', {'value': False})
        self.driver.execute_cdp_cmd('Emulation.setEmulatedMedia', {'features': []})
        self.driver.set_window_size(1440, 1000)
        self.driver.get(self.base + '/')
        self.driver.execute_script('localStorage.clear(); sessionStorage.clear();')
        self.driver.get(self.base + '/')
        self.wait = browser_support.WebDriverWait(self.driver, 10, poll_frequency=0.05)
        self.wait.until(lambda d: d.find_element(By.ID, 'research-film').get_attribute(
            'data-enhanced') == 'true')

    def tearDown(self):
        # Both emulations are shared by this fixture's Chrome instance.
        self.driver.execute_cdp_cmd('Emulation.setScriptExecutionDisabled', {'value': False})
        self.driver.execute_cdp_cmd('Emulation.setEmulatedMedia', {'features': []})

    def _element(self, selector):
        return self.driver.find_element(By.CSS_SELECTOR, selector)

    def _center(self):
        self.driver.execute_script(
            'arguments[0].scrollIntoView({block:"center", behavior:"instant"});',
            self._element('#research-film'))

    def _click(self, selector):
        element = self._element(selector)
        self.driver.execute_script(
            'arguments[0].scrollIntoView({block:"center", behavior:"instant"});', element)
        element.click()

    def _time(self):
        return float(self._element('#research-film').get_attribute('data-time'))

    def _playing(self):
        return self._element('#research-film').get_attribute('data-playing') == 'true'

    def _cycle(self):
        return int(self._element('#research-film').get_attribute('data-cycle'))

    def _frame(self, milliseconds):
        self.driver.execute_script('''
            document.getElementById('research-film').dispatchEvent(
              new CustomEvent('scholarguide:film-frame', {detail:{time:arguments[0]}}));
        ''', milliseconds)
        self.wait.until(lambda d: abs(self._time() - milliseconds) < 1 and not self._playing())

    def _assert_no_timeline_controls(self):
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR,
            '#film-progress, #film-time, [data-film-chapter], '
            '#research-film input[type="range"], #research-film [role="slider"]'), [])

    def _assert_stable(self, seconds=0.4):
        first = self._time()
        time.sleep(seconds)
        self.assertAlmostEqual(self._time(), first, delta=1)

    def _assert_scene(self, scene):
        self.assertEqual(self._element('#research-film').get_attribute('data-scene'), scene)
        for name in ('compose', 'results', 'synthesis', 'chat'):
            panel = self._element('#film-' + name)
            self.assertEqual(panel.get_property('hidden'), name != scene)
            self.assertEqual(panel.is_displayed(), name == scene)

    def _background_round_trip(self, should_resume):
        self.driver.execute_script('''
            window.__filmVisibilityEvents = [];
            if (window.__filmVisibilityRecorder) {
              document.removeEventListener('visibilitychange', window.__filmVisibilityRecorder);
            }
            window.__filmVisibilityRecorder = () => {
              const film = document.getElementById('research-film');
              window.__filmVisibilityEvents.push({hidden:document.hidden,
                time:Number(film.dataset.time), playing:film.dataset.playing === 'true'});
            };
            document.addEventListener('visibilitychange', window.__filmVisibilityRecorder);
        ''')
        original = self.driver.current_window_handle
        self.driver.switch_to.new_window('tab')
        other = self.driver.current_window_handle
        try:
            time.sleep(0.4)
            self.driver.switch_to.window(original)
            self.wait.until(lambda d: d.execute_script(
                'return window.__filmVisibilityEvents.length >= 2;'))
            events = self.driver.execute_script('return window.__filmVisibilityEvents;')
            self.assertTrue(events[0]['hidden'], 'A real background tab must report document.hidden')
            self.assertFalse(events[1]['hidden'])
            self.assertFalse(events[0]['playing'])
            self.assertAlmostEqual(events[0]['time'], events[1]['time'], delta=1)
            self.assertEqual(events[1]['playing'], should_resume)
        finally:
            self.driver.switch_to.window(other)
            self.driver.close()
            self.driver.switch_to.window(original)

    def test_real_playback_speed_pause_and_automatic_loop(self):
        self._center()
        self.wait.until(lambda d: self._playing())
        self.assertEqual(self._element('#research-film').get_attribute('data-speed'), '1.5')
        advance = self.driver.execute_async_script('''
            const done = arguments[arguments.length - 1];
            const film = document.getElementById('research-film');
            const first = Number(film.dataset.time);
            setTimeout(() => done(Number(film.dataset.time) - first), 400);
        ''')
        self.assertGreaterEqual(advance, 450, 'The logical clock must advance at the default 1.5x speed')
        self.assertLessEqual(advance, 1000, 'Allow scheduling variance without accepting an arbitrary jump')
        self.assertEqual(self._element('#film-play').get_attribute('data-state'), 'playing')
        self.assertRegex(self._element('#film-play').get_attribute('aria-label'), r'暂停|[Pp]ause')

        self._click('#film-play')
        self.wait.until(lambda d: not self._playing())
        self.assertEqual(self._element('#film-play').get_attribute('data-state'), 'paused')
        self.assertRegex(self._element('#film-play').get_attribute('aria-label'), r'播放|继续|[Pp]lay|[Rr]esume')
        self._assert_stable()
        paused = self._time()
        self._click('#film-play')
        self.wait.until(lambda d: self._playing() and self._time() >= paused + 300)

        self._frame(29600)
        cycle = self._cycle()
        self._click('#film-play')
        self.wait.until(lambda d: self._cycle() == cycle + 1 and self._playing()
                        and 500 <= self._time() < 2000)
        self._assert_scene('compose')
        first = self._time()
        self.wait.until(lambda d: self._playing() and self._time() >= first + 300)
        self.assertEqual(self._element('#film-play').get_attribute('data-state'), 'playing')

    def test_export_frames_reconstruct_scenes_text_and_loop_fades(self):
        self._center()
        self._assert_no_timeline_controls()
        self._frame(1600)
        first_input = self._element('#film-input').get_attribute('textContent').strip()
        self._frame(6000)
        full_input = self._element('#film-input').get_attribute('textContent').strip()
        self.assertTrue(first_input)
        self.assertGreater(len(full_input), len(first_input))
        self.assertTrue(full_input.startswith(first_input))

        for value, scene, reveal in ((12000, 'results', 'quote'),
                                     (20000, 'synthesis', 'hypothesis'),
                                     (28500, 'chat', 'chat-answer')):
            with self.subTest(time=value):
                self._frame(value)
                self._assert_scene(scene)
                shown = self._element(f'[data-reveal="{reveal}"]')
                self.assertEqual(shown.get_attribute('data-visible'), 'true')
                self.assertTrue(shown.is_displayed())
                if value == 12000:
                    results_snapshot = self._element('#film-app').get_attribute('innerHTML')

        self._frame(12000)
        self.assertEqual(self._element('#film-app').get_attribute('innerHTML'), results_snapshot,
                         'Reverse seeking must reconstruct the same frame without accumulated state')

        self._frame(24000)
        first_chat = self._element('#film-chat-input').get_attribute('textContent').strip()
        self._frame(25600)
        full_chat = self._element('#film-chat-input').get_attribute('textContent').strip()
        self.assertTrue(first_chat)
        self.assertGreater(len(full_chat), len(first_chat))
        self.assertTrue(full_chat.startswith(first_chat))
        self._frame(27500)
        self.assertEqual(self._element('#film-chat-input').get_attribute('textContent').strip(), '')
        sent_message = self._element('[data-reveal="chat-message"]')
        self.assertEqual(sent_message.get_attribute('data-visible'), 'true')
        self.assertTrue(sent_message.is_displayed())

        for scene, value in (('compose', 500), ('results', 8000),
                             ('synthesis', 15500), ('chat', 23000)):
            with self.subTest(scene=scene):
                self._frame(value)
                self._assert_scene(scene)

        for milliseconds, expected in ((0, 0), (250, 0.5), (500, 1),
                                       (29300, 1), (29650, 0.5), (30000, 0)):
            with self.subTest(fade_time=milliseconds):
                self._frame(milliseconds)
                opacity = float(self.driver.execute_script(
                    'return getComputedStyle(document.getElementById("film-app")).opacity;'))
                self.assertAlmostEqual(opacity, expected, delta=0.03)
        self.assertEqual(self._element('#research-film').get_attribute('data-scene'), 'chat')
        self._frame(0)
        self.assertEqual(self._element('#research-film').get_attribute('data-scene'), 'compose')
        self.assertFalse(self._element('#film-compose').get_property('hidden'))
        self._assert_stable()

    def test_reduced_motion_and_no_javascript_have_readable_fallbacks(self):
        self.driver.execute_cdp_cmd('Emulation.setEmulatedMedia', {
            'features': [{'name': 'prefers-reduced-motion', 'value': 'reduce'}]})
        self.driver.get(self.base + '/')
        self._center()
        self.wait.until(lambda d: self._time() == 20000 and not self._playing())
        self._assert_scene('synthesis')
        self._assert_stable()
        for milliseconds in (0, 250, 29650, 30000, 20000):
            self._frame(milliseconds)
            self.assertEqual(self.driver.execute_script(
                'return getComputedStyle(document.getElementById("film-app")).opacity;'), '1')
        animations = self.driver.execute_script('''
            return [...document.querySelectorAll('#research-film, #research-film *')]
              .flatMap(el => [el, '::before', '::after'].map(pseudo => {
                const style = getComputedStyle(el, typeof pseudo === 'string' ? pseudo : null);
                return {name:style.animationName, duration:style.animationDuration};
              })).filter(x => x.name !== 'none' &&
                x.duration.split(',').some(duration => parseFloat(duration) > 0.01));
        ''')
        self.assertEqual(animations, [], 'Reduced motion must remove decorative motion')
        self._click('#film-play')
        self.wait.until(lambda d: self._playing() and self._time() >= 20300)

        self.driver.execute_cdp_cmd('Emulation.setScriptExecutionDisabled', {'value': True})
        try:
            self.driver.get(self.base + '/')
            compose = self._element('#film-compose')
            self.assertTrue(compose.is_displayed())
            self.assertTrue(self._element('#film-input').get_attribute('textContent').strip())
            self.assertRegex(self._element('#research-film').text, r'示意|演示')
            self.assertTrue(self._element('#start-research').is_displayed())
        finally:
            self.driver.execute_cdp_cmd('Emulation.setScriptExecutionDisabled', {'value': False})

    def test_offscreen_pause_resumes_intent_and_preserves_manual_pause(self):
        self._center()
        self.wait.until(lambda d: self._playing())
        first = self._time()
        self.wait.until(lambda d: self._time() >= first + 300)
        self.driver.execute_script('window.scrollTo({top:document.body.scrollHeight, behavior:"instant"});')
        self.wait.until(lambda d: not self._playing())
        self._assert_stable()
        stopped = self._time()
        self._center()
        self.wait.until(lambda d: self._playing() and self._time() >= stopped + 300)
        self._background_round_trip(should_resume=True)
        self.wait.until(lambda d: self._playing())

        self._click('#film-play')
        self.wait.until(lambda d: not self._playing())
        self.driver.execute_script('window.scrollTo({top:document.body.scrollHeight, behavior:"instant"});')
        time.sleep(0.2)
        self._center()
        self._assert_stable()
        self.assertFalse(self._playing(), 'Scrolling must not overwrite an explicit pause')
        self._background_round_trip(should_resume=False)
        self._assert_stable()
        self.assertFalse(self._playing(), 'Tab visibility must preserve an explicit pause')

    def test_dark_responsive_layout_and_keyboard_controls(self):
        self.driver.execute_script("localStorage.setItem('scholarguide-theme', 'dark');")
        self.driver.get(self.base + '/')
        self.assertEqual(self.driver.execute_script('return document.documentElement.dataset.theme'), 'dark')
        for width in (390, 1440):
            with self.subTest(width=width):
                self.driver.set_window_size(width, 1000)
                self._center()
                self._frame(20000)
                dimensions = self.driver.execute_script('''
                    const figure = document.getElementById('research-film').getBoundingClientRect();
                    const viewport = document.getElementById('film-viewport').getBoundingClientRect();
                    return {width:document.documentElement.clientWidth,
                      scroll:document.documentElement.scrollWidth,
                      figureLeft:figure.left, figureRight:figure.right,
                      filmWidth:viewport.width, filmHeight:viewport.height};
                ''')
                self.assertLessEqual(dimensions['scroll'], dimensions['width'])
                self.assertGreaterEqual(dimensions['figureLeft'], -1)
                self.assertLessEqual(dimensions['figureRight'], dimensions['width'] + 1)
                self.assertGreater(dimensions['filmWidth'], 240)
                self.assertGreater(dimensions['filmHeight'], 180)
                self._assert_no_timeline_controls()
                self._frame(24000)
                self._assert_scene('chat')
                play = self._element('#film-play')
                self.assertTrue(play.is_displayed())
                self.assertTrue(play.is_enabled())
                self.assertGreaterEqual(play.rect['height'], 24)
                self.driver.execute_script('arguments[0].focus();', play)
                self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'film-play')
                self.driver.switch_to.active_element.send_keys(Keys.SPACE)
                self.wait.until(lambda d: self._playing())
                self.driver.switch_to.active_element.send_keys(Keys.SPACE)
                self.wait.until(lambda d: not self._playing())
                self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'film-play')
                self._assert_stable()

    def test_walkthrough_is_local_and_never_creates_research(self):
        self.driver.execute_script('''
            window.__filmRequests = [];
            const fetchOriginal = window.fetch;
            window.fetch = function(input, options) {
              window.__filmRequests.push(String(input instanceof Request ? input.url : input));
              return fetchOriginal.apply(this, arguments);
            };
            const openOriginal = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url) {
              window.__filmRequests.push(String(url));
              return openOriginal.apply(this, arguments);
            };
        ''')
        self._center()
        for value in (0, 12000, 20000, 28500, 30000):
            self._frame(value)
        self._frame(29600)
        cycle = self._cycle()
        self._click('#film-play')
        self.wait.until(lambda d: self._playing() and self._cycle() == cycle + 1)
        self._click('#film-play')
        self.assertEqual(self.driver.execute_script('return window.__filmRequests;'), [])
        self.assertEqual(len(self.service.sessions), self.session_count)
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#research-film video, #research-film canvas'), [])
        film_text = self._element('#research-film').get_attribute('textContent')
        self.assertRegex(film_text, r'候选.*A')
        self.assertRegex(film_text, r'候选.*B')
        self.assertRegex(film_text, r'示意|演示')
        self.assertRegex(film_text, r'虚拟样例|流程演示')
        resources = self.driver.execute_script('return performance.getEntriesByType("resource").map(x => x.name);')
        for resource in resources:
            with self.subTest(resource=resource):
                if urlparse(resource).scheme == 'data':
                    continue
                self.assertEqual(urlparse(resource).netloc, urlparse(self.base).netloc)
                self.assertIsNone(re.search(r'/api(?:/|$)', urlparse(resource).path))
        errors = [entry for entry in self.driver.get_log('browser') if entry['level'] == 'SEVERE']
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
