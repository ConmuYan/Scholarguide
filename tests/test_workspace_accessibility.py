"""Keyboard and mobile regressions using a temporary server and synthetic records."""
import os
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import test_experience_upgrade as harness
try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
except ImportError:
    By = Keys = None


class WorkspaceAccessibilityTests(unittest.TestCase):
    setUpClass = classmethod(harness.ExperienceUpgradeTests.setUpClass.__func__)
    tearDownClass = classmethod(harness.ExperienceUpgradeTests.tearDownClass.__func__)
    setUp = harness.ExperienceUpgradeTests.setUp
    tearDown = harness.ExperienceUpgradeTests.tearDown

    def test_skip_links_reach_main_and_identify_current_page(self):
        for page in ('chat', 'settings', 'sources', 'activity'):
            with self.subTest(page=page):
                self.driver.get(self.base + '/' + page)
                link = self.driver.find_element(By.CSS_SELECTOR, '.skip-link')
                link.send_keys(Keys.ENTER)
                self.assertEqual(self.driver.execute_script('return document.activeElement.id'), 'main-content')
                self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '.side-link.active').get_attribute('aria-current'), 'page')

    def activity_fixture(self):
        self.driver.get(self.base + '/activity')
        self.wait.until(lambda d: d.execute_script('return !!state.index'))
        self.driver.execute_script('''
          const orig=window.fetch.bind(window);
          window.fetch=(url,options)=>String(url).includes('/api/activity/runs/fixture-run')
            ? Promise.resolve(new Response(JSON.stringify({kind:'run',id:'fixture-run',result:{
                status:'complete',request:{topic:'Synthetic record'},
                trace:[{action:'search',query:'synthetic offline query',step:1}],papers:[]}}),
                {status:200,headers:{'Content-Type':'application/json'}})) : orig(url,options);
          state.index={sessions:[],runs:[{id:'fixture-run',kind:'run',title:'Synthetic record',mode:'demo',status:'complete'}]};
          state.kind='runs'; state.selected=null; state.selectedKind=null;
          state.mode='all'; state.query=''; renderList(false);
        ''')

    def test_activity_tabs_keyboard_and_empty_filter(self):
        self.activity_fixture()
        runs = self.driver.find_element(By.ID, 'tab-runs')
        runs.send_keys(Keys.ARROW_LEFT)
        self.assertEqual(self.driver.execute_script('return document.activeElement.id'), 'tab-sessions')
        self.assertEqual(self.driver.find_element(By.ID, 'tab-sessions').get_attribute('aria-selected'), 'true')
        self.assertEqual(self.driver.find_element(By.ID, 'activity-panel').get_attribute('aria-labelledby'), 'tab-sessions')
        self.driver.find_element(By.ID, 'tab-sessions').send_keys(Keys.END)
        self.assertEqual(self.driver.execute_script('return document.activeElement.id'), 'tab-runs')
        self.assertEqual(self.driver.find_element(By.ID, 'tab-sessions').get_attribute('tabindex'), '-1')
        search = self.driver.find_element(By.ID, 'activity-search')
        search.send_keys('unmatched synthetic query')
        self.wait.until(lambda d: '没有符合当前筛选' in d.find_element(By.ID, 'activity-empty').text)

    def test_activity_refresh_preserves_filter_and_expanded_event_focus(self):
        self.activity_fixture()
        result = self.driver.execute_script('''
          document.querySelector('#activity-detail').hidden=false;
          document.querySelector('.act-raw-stream').open=true;
          const events=[{seq:1,action:'search',payload:{query:'fixture'}}];
          renderFilters(['search']);
          const button=document.querySelector('#activity-filters button'); button.focus();
          renderFilters(['search']);
          const filterRetained=document.activeElement.dataset.group === 'all';
          renderTimeline(events);
          const details=document.querySelector('#activity-timeline details');
          details.open=true; details.querySelector('summary').focus();
          renderTimeline([...events,{seq:2,action:'assess',payload:{}}]);
          const updated=document.querySelector('#activity-timeline details');
          return {filterRetained,open:updated.open,focus:document.activeElement === updated.querySelector('summary')};
        ''')
        self.assertEqual(result, {'filterRetained':True,'open':True,'focus':True})
        story = self.driver.execute_script('''
          const detail={kind:'run',id:'fixture-run',result:{status:'complete',trace:[
            {seq:1, action:'search', query:'one'}, {seq:2, action:'assess',query:'two'}]}};
          renderStory(detail);
          const rows=document.querySelectorAll('.trace-row');
          rows[1].click(); rows[1].focus(); const key=rows[1].dataset.spanKey;
          renderStory(detail);
          return {selected:document.querySelector('.trace-row.active').dataset.spanKey===key,
                  focused:document.activeElement.dataset.spanKey===key};
        ''')
        self.assertEqual(story, {'selected':True,'focused':True})

    def test_activity_selection_keeps_keyboard_position_on_desktop_and_moves_on_mobile(self):
        self.activity_fixture()
        row = self.driver.find_element(By.CSS_SELECTOR, '#activity-list button')
        row.send_keys(Keys.ENTER)
        self.wait.until(lambda d: d.find_element(By.ID, 'activity-detail').is_displayed())
        self.assertEqual(self.driver.execute_script('return document.activeElement.dataset.recordId'), 'fixture-run')
        self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width':390,'height':900,'deviceScaleFactor':1,'mobile':False})
        self.driver.find_element(By.CSS_SELECTOR, '#activity-list button').send_keys(Keys.ENTER)
        self.wait.until(lambda d: d.execute_script('return document.activeElement.id') == 'activity-title')
        self.assertEqual(self.driver.find_element(By.ID, 'activity-title').text, 'Synthetic record')
        for theme in ('light', 'dark'):
            self.driver.execute_script('document.documentElement.dataset.theme=arguments[0]', theme)
            info = self.driver.execute_script('''
              const row=document.querySelector('.trace-row');
              const status=row.querySelector('.trace-status');
              return {visible:getComputedStyle(status).display, size:parseFloat(getComputedStyle(status).fontSize),
                      overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};
            ''')
            self.assertNotEqual(info['visible'], 'none', info)
            self.assertGreaterEqual(info['size'], 11, info)
            self.assertLessEqual(info['overflow'], 1, info)
            output = os.environ.get('SG_A11Y_SHOTS')
            if output:
                Path(output).mkdir(parents=True, exist_ok=True)
                self.driver.find_element(By.ID, 'activity-story').screenshot(str(Path(output) / f'activity-trace-390-{theme}.png'))

    def test_landing_rail_loops_without_a_pause_button(self):
        from selenium.webdriver.common.action_chains import ActionChains
        self.driver.execute_cdp_cmd('Emulation.setEmulatedMedia', {'features':[{'name':'prefers-reduced-motion','value':'no-preference'}]})
        try:
            self.driver.get(self.base + '/')
            rail = self.wait.until(lambda d: d.find_element(By.ID, 'source-rail'))
            self.assertEqual(self.driver.find_elements(By.ID, 'rail-pause'), [])
            ActionChains(self.driver).move_to_element(rail).perform()
            motion = self.driver.execute_async_script('''
              const done=arguments[0],track=document.querySelector('.lp-rail-track');
              const before=getComputedStyle(track).transform;
              setTimeout(()=>done({before,after:getComputedStyle(track).transform,
                state:getComputedStyle(track).animationPlayState,
                iterations:getComputedStyle(track).animationIterationCount}),200);
            ''')
            self.assertEqual(motion['state'], 'running')
            self.assertEqual(motion['iterations'], 'infinite')
            self.assertNotEqual(motion['before'], motion['after'])
            self.driver.execute_cdp_cmd('Emulation.setEmulatedMedia', {'features':[{'name':'prefers-reduced-motion','value':'reduce'}]})
            self.assertEqual(self.driver.execute_script("return getComputedStyle(document.querySelector('.lp-rail-track')).animationName"), 'none')
        finally:
            self.driver.execute_cdp_cmd('Emulation.setEmulatedMedia', {'features':[]})

    def test_mobile_trace_list_scrolls_without_overlapping_footer(self):
        self.driver.get(self.base + '/search')
        self.wait.until(lambda d: d.execute_script('return !!window.ScholarGuideTrace'))
        self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width':390,'height':900,'deviceScaleFactor':1,'mobile':False})
        result = self.driver.execute_script('''
          const host=document.querySelector('#trace-list');
          host.replaceChildren(...window.ScholarGuideTrace.render(Array.from({length:18},(_,i)=>({
            seq:i,action:'tool_end',payload:{tool:'model.synthesis',duration_ms:10}}))).children);
          const footer=host.nextElementSibling; const h=host.getBoundingClientRect(), f=footer.getBoundingClientRect();
          return {scroll:host.scrollHeight, client:host.clientHeight, overflow:getComputedStyle(host).overflowY,
                  bottom:h.bottom, footTop:f.top};
        ''')
        self.assertEqual(result['overflow'], 'auto', result)
        self.assertGreater(result['scroll'], result['client'], result)
        self.assertLessEqual(result['bottom'], result['footTop'] + 1, result)


if __name__ == '__main__':
    unittest.main()
