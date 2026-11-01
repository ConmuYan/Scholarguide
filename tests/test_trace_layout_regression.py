"""Trace rows remain legible in the real Chat and Search layouts.

Uses a temporary server and synthetic events only; no provider or research
source is contacted. Exercises the legacy Chat grid and Search timeline rules.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import test_experience_upgrade as harness
try:
    from selenium.webdriver.common.by import By
except ImportError:
    By = None  # The shared browser harness handles missing Selenium.


class TraceLayoutRegressionTests(unittest.TestCase):
    setUpClass = classmethod(harness.ExperienceUpgradeTests.setUpClass.__func__)
    tearDownClass = classmethod(harness.ExperienceUpgradeTests.tearDownClass.__func__)
    setUp = harness.ExperienceUpgradeTests.setUp
    tearDown = harness.ExperienceUpgradeTests.tearDown

    def test_synthetic_event_rows_fit_chat_and_search(self):
        for page, host in (('/chat', '#running-trace-list'), ('/search', '#trace-list')):
            self.driver.get(self.base + page)
            self.wait.until(lambda d: d.execute_script('return !!window.ScholarGuideTrace'))
            self.driver.execute_script('''
              const events = Array.from({length: 18}, (_, i) => ({
                seq: i + 1, action: i === 0 ? 'tool_start' : 'tool_end',
                at: '2026-10-30T15:36:55Z',
                payload: {category: 'model', tool: 'model.synthesis',
                          span_id: 'span-' + i, duration_ms: 5354.9,
                          reason: i === 0 ? '研究综合正在处理' : ''}
              }));
              const host = document.querySelector(arguments[0]);
              host.replaceChildren(...window.ScholarGuideTrace.render(events).children);
              if (arguments[0] === '#running-trace-list') {
                document.querySelector('#running').hidden = false;
                document.querySelector('#running-trace').hidden = false;
              }
            ''', host)
            for width in (1440, 1024, 390, 320):
                with self.subTest(page=page, width=width):
                    self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                        'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
                    result = self.driver.execute_script('''
                      const host = document.querySelector(arguments[0]);
                      const row = host.querySelector('.sg-trace-item');
                      const title = row.querySelector('strong');
                      const summary = row.querySelector('.sg-trace-summary');
                      const badge = row.querySelector('.sg-trace-status');
                      const r = row.getBoundingClientRect();
                      const t = title.getBoundingClientRect();
                      const s = summary.getBoundingClientRect();
                      const b = badge.getBoundingClientRect();
                      const dot = getComputedStyle(row, '::before');
                      return {rowWidth:r.width, titleWidth:t.width, titleHeight:t.height,
                        summaryWidth:s.width, badgeRight:b.right, summaryRight:s.right,
                        display:getComputedStyle(row).display, dotWidth:parseFloat(dot.width),
                        overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,
                        count:host.querySelectorAll('.sg-trace-item').length};
                    ''', host)
                    self.assertEqual(result['display'], 'block', result)
                    self.assertGreater(result['titleWidth'], 90, result)
                    self.assertLess(result['titleHeight'], 50, result)
                    self.assertLessEqual(result['badgeRight'], result['summaryRight'] + 1, result)
                    self.assertLessEqual(result['overflow'], 1, result)
                    self.assertEqual(result['dotWidth'], 6, result)
                    self.assertEqual(result['count'], 18)
                    screenshot_dir = os.environ.get('SG_TRACE_SCREENSHOTS_DIR')
                    if screenshot_dir and width in (1440, 390):
                        target = self.driver.find_element(By.CSS_SELECTOR, host)
                        self.driver.execute_script('arguments[0].scrollIntoView({block:"center",behavior:"instant"})', target)
                        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                        target.screenshot(str(Path(screenshot_dir) / f'{page.strip("/")}-trace-{width}.png'))
            detail = self.driver.find_element(By.CSS_SELECTOR, host + ' .sg-trace-details')
            self.assertFalse(detail.get_attribute('open'))
            self.driver.execute_script('arguments[0].querySelector("summary").click()', detail)
            self.assertTrue(detail.get_attribute('open'))
            self.assertIn('model.synthesis', detail.find_element(By.TAG_NAME, 'pre').text)

    def test_research_styling_is_served_and_readable_in_both_workspaces(self):
        for page in ('/chat', '/search'):
            self.driver.get(self.base + page)
            self.wait.until(lambda d: d.execute_script('return !!window.ScholarGuideResearch'))
            self.wait.until(lambda d: d.execute_script('''return Array.from(document.styleSheets)
              .some(sheet => (sheet.href || '').endsWith('/style.css'));'''))
            self.driver.execute_script('''
              const claim = {text:'这是基于摘要的测试说明。',
                evidence:[{paper_id:'synthetic', quote:'Synthetic abstract evidence.'}]};
              const brief = {status:'partial', source_level:'abstract_only', paper_notes:[{
                paper_id:'synthetic', contribution:claim, method:claim,
                author_limitation:null, model_inference:claim}]};
              const papers = [{id:'synthetic', title:'A long research title with a very long token '
                + 'SyntheticResearchArchitecture'.repeat(5), url:'https://example.invalid/paper'}];
              const wrap=document.createElement('div'); wrap.id='research-test-host';
              wrap.style.cssText='max-width:760px;margin:20px auto;padding:0 16px';
              wrap.append(window.ScholarGuideResearch.render(brief,{papers}));
              document.querySelector('main').append(wrap);
            ''')
            for theme in ('light', 'dark'):
                self.driver.execute_script('document.documentElement.dataset.theme=arguments[0]', theme)
                for width in (1440, 390, 320):
                    with self.subTest(page=page, theme=theme, width=width):
                        self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                            'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
                        layout = self.driver.execute_script('''
                          const note=document.querySelector('#research-test-host .research-paper-note');
                          const claim=note.querySelector('.research-claim');
                          const rect=note.getBoundingClientRect();
                          const title=note.querySelector('h4,h5,h6').getBoundingClientRect();
                          return {border:getComputedStyle(note).borderTopStyle,
                            padding:parseFloat(getComputedStyle(note).paddingLeft),
                            claimSize:parseFloat(getComputedStyle(claim.querySelector('p')).fontSize),
                            noteWidth:rect.width,titleRight:title.right,noteRight:rect.right,
                            overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};''')
                        self.assertEqual(layout['border'], 'solid', layout)
                        self.assertGreaterEqual(layout['padding'], 14, layout)
                        self.assertEqual(layout['claimSize'], 14, layout)
                        self.assertLessEqual(layout['titleRight'], layout['noteRight'], layout)
                        self.assertLessEqual(layout['overflow'], 1, layout)
            host = self.driver.find_element(By.ID, 'research-test-host')
            self.assertIn('仅已选论文的摘要', host.text)
            self.assertIn('模型推论', host.text)
            details = host.find_element(By.CSS_SELECTOR, '.research-evidence')
            self.driver.execute_script('arguments[0].querySelector("summary").click()', details)
            self.assertIn('Synthetic abstract evidence.', details.text)


if __name__ == '__main__':
    unittest.main()
