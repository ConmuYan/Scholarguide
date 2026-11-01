"""Real-browser contracts for the research workspace upgrade.

All API traffic uses a temporary service, offline demo or explicit fetch fixture;
no research/model request can escape through the UI scenarios below.
"""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None

from activity import ActivityFeed
from providers import ProviderStore
from webapp import ChatService, Jobs, create_server
from scripts.verify_delivery import apply_browser_isolation

CONFIG_FIXTURE = """
(() => {
  const real=window.fetch.bind(window);
  window.__configReady=true; window.__configCalls=0; window.__posts=[];
  window.fetch=(url, options={})=>{
    const path=String(url);
    if ((options.method||'GET').toUpperCase()==='POST') {
      window.__posts.push({path, body:options.body});
      return Promise.resolve(new Response(JSON.stringify({error:'No live calls in this fixture'}),{status:503}));
    }
    if (path==='/api/config') {
      window.__configCalls++;
      return Promise.resolve(new Response(JSON.stringify(window.__configReady
        ? {ready:true,provider:'Offline fixture',model:'no-model-call',source:'arxiv',research_default_source:'multi'}
        : {error:'Temporary config outage'}),{status:window.__configReady?200:503}));
    }
    return real(url,options);
  };
})();
"""


class ExperienceUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get('SG_REQUIRE_BROWSER') == '1'
        if webdriver is None:
            if required:
                raise RuntimeError('Experience upgrade requires Selenium')
            raise unittest.SkipTest('Experience upgrade requires Selenium')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.quota_env = patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(cls.root / 'quota.sqlite3')})
        cls.quota_env.start()
        cls.addClassCleanup(cls.quota_env.stop)
        cls.providers = ProviderStore(cls.root / 'providers.json')
        cls.chat = ChatService(cls.root / 'runs' / 'web', providers=cls.providers)
        cls.jobs = Jobs(output=cls.root / 'runs', providers=cls.providers)
        cls.server = create_server(0, jobs=cls.jobs, chat=cls.chat,
                                   activity=ActivityFeed(cls.root / 'runs'), providers=cls.providers)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        options = Options()
        apply_browser_isolation(options, cls.root / 'chrome-profile')
        for flag in ('--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=1440,900'):
            options.add_argument(flag)
        try:
            cls.driver = webdriver.Chrome(options=options)
        except WebDriverException as error:
            cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)
            cls.chat.close(); cls.tmp.cleanup()
            if required:
                raise RuntimeError('Required Chrome unavailable') from error
            raise unittest.SkipTest(str(error))

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)
        cls.chat.close(); cls.tmp.cleanup()

    def setUp(self):
        self.driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
        self.driver.set_window_size(1440, 900)
        self.driver.get(self.base + '/settings')
        self.driver.execute_script('sessionStorage.clear(); localStorage.clear();')
        self.injections = []
        self.wait = WebDriverWait(self.driver, 15)

    def tearDown(self):
        for identifier in self.injections:
            self.driver.execute_cdp_cmd('Page.removeScriptToEvaluateOnNewDocument', {'identifier': identifier})

    def inject(self, source):
        entry = self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': source})
        self.injections.append(entry['identifier'])

    def click(self, selector):
        self.driver.execute_script('arguments[0].click();', self.driver.find_element(By.CSS_SELECTOR, selector))

    def set_theme(self, theme):
        for _ in range(3):
            if self.driver.execute_script('return document.documentElement.dataset.theme;') == theme:
                return
            self.click('#theme-toggle')
        self.assertEqual(self.driver.execute_script('return document.documentElement.dataset.theme;'), theme)

    def capture_element(self, selector, path):
        bounds = self.driver.execute_script('''
          const rect=document.querySelector(arguments[0]).getBoundingClientRect();
          return {x:rect.x+scrollX,y:rect.y+scrollY,width:rect.width,height:rect.height,scale:1};
        ''', selector)
        image = self.driver.execute_cdp_cmd('Page.captureScreenshot', {
            'captureBeyondViewport': True, 'clip': bounds})
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(base64.b64decode(image['data']))

    def search(self, source=CONFIG_FIXTURE):
        self.inject(source)
        self.driver.get(self.base + '/search')
        self.wait.until(lambda d: d.execute_script('return typeof state!=="undefined" && !state.configLoading;'))

    def test_primary_search_action_is_next_to_input_and_inside_initial_viewport(self):
        self.search()
        evidence = []
        for width in (1440, 390):
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
            self.driver.execute_script('window.scrollTo(0,0);')
            self.assertFalse(self.driver.find_element(By.ID, 'advanced-settings').get_attribute('open'))
            self.driver.find_element(By.ID, 'topic').send_keys('3D scene reconstruction')
            bounds = self.driver.execute_script('''
              const input=document.querySelector('#topic').getBoundingClientRect();
              const button=document.querySelector('#submit-button').getBoundingClientRect();
              return {width:innerWidth,height:innerHeight,inputBottom:input.bottom,
                buttonTop:button.top,buttonBottom:button.bottom,buttonWidth:button.width,
                overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};
            ''')
            self.assertEqual(bounds['width'], width)
            self.assertEqual(bounds['height'], 900)
            self.assertGreaterEqual(bounds['buttonTop'], bounds['inputBottom'])
            self.assertLess(bounds['buttonTop'] - bounds['inputBottom'], 165)
            self.assertLess(bounds['buttonBottom'], bounds['height'])
            self.assertGreater(bounds['buttonWidth'], 100)
            self.assertEqual(bounds['overflow'], 0)
            self.assertFalse(self.driver.find_element(By.ID, 'submit-button').get_attribute('disabled'))
            evidence.append(bounds)
            out = os.environ.get('SG_EXPERIENCE_SHOTS')
            if out:
                folder = Path(out); folder.mkdir(parents=True, exist_ok=True)
                self.set_theme('light')
                self.driver.save_screenshot(str(folder / f'search-{width}-light.png'))
                self.set_theme('dark')
                self.assertFalse(self.driver.find_element(By.ID, 'submit-button').get_attribute('disabled'))
                self.driver.save_screenshot(str(folder / f'search-{width}-dark.png'))
                self.set_theme('light')
            self.driver.find_element(By.ID, 'topic').clear()
        self.assertEqual(self.driver.execute_script('return window.__posts'), [])
        if os.environ.get('SG_EXPERIENCE_SHOTS'):
            (Path(os.environ['SG_EXPERIENCE_SHOTS']) / 'primary-action-bounds.json').write_text(json.dumps(evidence,indent=2))

    def test_advanced_controls_are_readable_and_do_not_overflow_at_true_390(self):
        self.search()
        for width in (1440, 390):
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
            self.driver.execute_script('''
              document.querySelector('#advanced-settings').open=true;
              document.querySelector('#research-settings').open=true;
              document.querySelector('#source-settings').open=true;
            ''')
            self.assertFalse(self.driver.find_elements(By.ID, 'target-count'))
            for identity in ('reading-goal', 'venue', 'year-from', 'source', 'publication-type'):
                self.assertTrue(self.driver.find_element(By.ID, identity).is_displayed())
            self.assertEqual(self.driver.execute_script('return innerWidth;'), width)
            self.assertLessEqual(self.driver.execute_script('return document.documentElement.scrollWidth;'),
                                 self.driver.execute_script('return document.documentElement.clientWidth;'))
            out = os.environ.get('SG_EXPERIENCE_SHOTS')
            if out:
                self.set_theme('light')
                self.capture_element('#advanced-settings', Path(out) / f'advanced-{width}-light.png')

    def test_config_retry_and_focus_visibility_recovery_preserve_draft_and_source(self):
        self.search(CONFIG_FIXTURE.replace('window.__configReady=true;', 'window.__configReady=false;'))
        self.wait.until(lambda d: d.execute_script('return Boolean(state.configError);'))
        self.assertTrue(self.driver.find_element(By.ID, 'retry-config').is_displayed())
        self.click('#advanced-settings > summary')
        self.click('#source-settings > summary')
        self.driver.execute_script('''
          const source=document.querySelector('#source'); source.value='openreview';
          source.dispatchEvent(new Event('change',{bubbles:true}));
        ''')
        self.driver.find_element(By.ID, 'topic').send_keys('my unfinished research question')
        self.driver.execute_script('window.__configReady=true;')
        self.click('#retry-config')
        self.wait.until(lambda d: d.execute_script('return state.config?.ready && !state.configError;'))
        self.assertFalse(self.driver.find_element(By.ID, 'submit-button').get_attribute('disabled'))
        self.assertEqual(self.driver.find_element(By.ID, 'source').get_attribute('value'), 'openreview')
        for event in ('focus', 'visibilitychange'):
            self.driver.execute_script('window.__configReady=false; window.dispatchEvent(new Event("focus"));')
            self.wait.until(lambda d: d.execute_script('return Boolean(state.configError);'))
            self.driver.execute_script('window.__configReady=true; ' +
                ('window' if event == 'focus' else 'document') + '.dispatchEvent(new Event(arguments[0]));', event)
            self.wait.until(lambda d: d.execute_script('return state.config?.ready && !state.configError;'))
            self.assertEqual(self.driver.find_element(By.ID, 'topic').get_attribute('value'), 'my unfinished research question')
            self.assertEqual(self.driver.find_element(By.ID, 'source').get_attribute('value'), 'openreview')
        self.assertEqual(self.driver.execute_script('return window.__posts'), [])
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.configError);'))
        self.driver.execute_script('window.__configReady=true;')
        self.click('#retry-config')
        self.wait.until(lambda d: d.execute_script('return state.config?.ready;'))
        self.assertEqual(self.driver.find_element(By.ID, 'source').get_attribute('value'), 'openreview')
        self.assertEqual(self.driver.find_element(By.ID, 'topic').get_attribute('value'), 'my unfinished research question')

    def test_advanced_conditions_survive_collapse_and_natural_mode_does_not_stack(self):
        self.search()
        self.click('#advanced-settings > summary')
        self.driver.execute_script('''
          for (const [id,value] of Object.entries({'topic':'推荐3篇RAG evaluation论文',
            'category':'survey','minimum':'2','venue':'ACL','year-from':'2024',
            'year-to':'2026','venue-scope':'preferred_only','publication-type':'conference'})) {
            const node=document.getElementById(id);node.value=value;
            node.dispatchEvent(new Event('input',{bubbles:true}));
          }
          document.querySelector('#presentation-oral').checked=true;
          document.querySelector('#presentation-oral').dispatchEvent(new Event('change',{bubbles:true}));
        ''')
        self.click('#advanced-settings > summary')
        summary = self.driver.find_element(By.ID, 'search-scope-summary').text
        for expected in ('3', '2', 'ACL', '2024', '2026', '仅限优选', '会议', 'oral'):
            self.assertIn(expected, summary)
        guided = self.driver.execute_script('return inputPayload();')
        self.assertEqual(guided['request_constraints']['minimum_category_count'], 2)
        self.assertTrue(guided['research_preferences']['accepted_only'])
        self.assertEqual(guided['research_preferences']['presentation_types'], ['oral'])
        self.click('#toggle-input')
        natural = self.driver.execute_script('return inputPayload();')
        self.assertNotIn('request_constraints', natural)
        self.assertNotIn('research_preferences', natural)
        self.assertEqual(self.driver.find_element(By.ID, 'advanced-count').text, '可选')
        self.assertIn('阅读目的', self.driver.find_element(By.ID, 'advanced-label').text)
        self.assertIn('ACL', natural['request'])
        self.click('#toggle-input')
        self.assertEqual(self.driver.execute_script('return inputPayload();'), guided)
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return state.config?.ready;'))
        self.assertFalse(self.driver.find_element(By.ID, 'advanced-settings').get_attribute('open'))
        self.assertEqual(self.driver.execute_script('return inputPayload();'), guided)

    def activity_fixture(self):
        self.inject('''
          (()=>{
            const real=window.fetch.bind(window);
            window.__pending={};
            window.__detail=(id)=>({id,session:{title:'Record '+id,status:'active'},
              cost:{settled:0},task:{topic:id},history:[],events:[]});
            window.fetch=(url,options={})=>{
              const path=String(url);
              if(path==='/api/activity') return Promise.resolve(new Response(JSON.stringify({
                sessions:['A','B'].map(id=>({id,kind:'session',title:'Record '+id,status:'active'})),runs:[]})));
              if(path.startsWith('/api/activity/sessions/')) return new Promise((resolve,reject)=>{
                const id=decodeURIComponent(path.split('/').at(-1));
                (window.__pending[id] ||= []).push({resolve,reject});
              });
              return real(url,options);
            };
            window.__finish=(id,fail=false)=>{
              const request=window.__pending[id].shift();
              if(fail) request.reject(new Error('stale failure'));
              else request.resolve(new Response(JSON.stringify(window.__detail(id))));
            };
          })();
        ''')
        self.driver.get(self.base + '/activity')
        self.wait.until(lambda d: d.execute_script('return window.__pending.A?.length===1;'))

    def test_lune_quota_confirmation_stays_local_and_preserves_history(self):
        self.driver.get(self.base + '/sources')
        self.wait.until(lambda d: d.execute_script('return state.luneQuota !== null'))
        self.assertFalse(self.driver.execute_script('return state.luneQuota.snapshot_present'))
        self.driver.execute_script("""
          const real = window.fetch.bind(window);
          window.__quotaRequests = [];
          window.fetch = (url, options = {}) => {
            window.__quotaRequests.push({path: String(url), method: options.method || 'GET'});
            return real(url, options);
          };
          document.querySelector('#lune-quota-remaining').value = '3';
          const d = new Date(Date.now() + 300000);
          const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
          document.querySelector('#lune-quota-expiry').value = local;
          document.querySelector('#lune-quota-ack').checked = true;
          document.querySelector('#lune-quota-form').requestSubmit();
        """)
        self.wait.until(lambda d: d.execute_script('return state.luneQuota?.local_available === 3'))
        self.assertIn('本机可用 3 次', self.driver.find_element(By.ID, 'lune-quota-status').text)
        requests = self.driver.execute_script('return window.__quotaRequests')
        self.assertEqual([r['path'] for r in requests if r['method'] == 'POST'],
                         ['/api/sources/lune/quota'])
        self.assertEqual(self.driver.execute_script(
            'return state.luneQuota.external_usage_unknown && !state.luneQuota.account_remaining_known'), True)

    def test_source_editor_switches_to_the_selected_sources_values(self):
        self.driver.get(self.base + '/sources')
        self.wait.until(lambda d: d.execute_script('return state.sources.length > 0'))
        self.driver.execute_script("""
          state.sources = [
            {name:'openalex',label:'OpenAlex',priority:10,enabled:true,configured:true},
            {name:'arxiv',label:'arXiv',priority:90,enabled:false,configured:true},
          ];
          renderSources(); populateNameSelect(); openEditor('openalex');
          window.__sourceConfirmations=0;
          window.confirm=()=>{window.__sourceConfirmations++; return true;};
        """)
        self.driver.find_element(By.ID, 'source-key').send_keys('only-for-openalex')
        self.driver.execute_script("""
          const field=document.getElementById('source-name'); field.value='arxiv';
          field.dispatchEvent(new Event('change',{bubbles:true}));
        """)
        self.assertEqual(self.driver.find_element(By.ID, 'source-priority').get_attribute('value'), '90')
        self.assertFalse(self.driver.find_element(By.ID, 'source-enabled').is_selected())
        self.assertEqual(self.driver.find_element(By.ID, 'source-key').get_attribute('value'), '')
        self.assertIn('arXiv', self.driver.find_element(By.ID, 'editor-title').text)
        self.assertEqual(self.driver.execute_script('return window.__sourceConfirmations'), 1)

    def test_source_switch_can_be_cancelled_without_losing_unsaved_values(self):
        self.driver.get(self.base + '/sources')
        self.wait.until(lambda d: d.execute_script('return state.sources.length > 0'))
        self.driver.execute_script("""
          state.sources = [
            {name:'openalex',label:'OpenAlex',priority:10,enabled:true,configured:true},
            {name:'arxiv',label:'arXiv',priority:90,enabled:false,configured:true},
          ];
          renderSources(); populateNameSelect(); openEditor('openalex');
          window.confirm=()=>false;
        """)
        self.driver.find_element(By.ID, 'source-key').send_keys('unsaved-local-only')
        self.driver.execute_script("""
          const field=document.getElementById('source-name'); field.value='arxiv';
          field.dispatchEvent(new Event('change',{bubbles:true}));
        """)
        self.assertEqual(self.driver.find_element(By.ID, 'source-name').get_attribute('value'), 'openalex')
        self.assertEqual(self.driver.find_element(By.ID, 'source-key').get_attribute('value'), 'unsaved-local-only')
        self.assertEqual(self.driver.find_element(By.ID, 'source-priority').get_attribute('value'), '10')

    def test_source_test_query_change_discards_the_late_old_test_output(self):
        self.driver.get(self.base + '/sources')
        self.wait.until(lambda d: d.execute_script('return state.sources.length > 0'))
        self.driver.execute_script("""
          state.sources=[{name:'openalex',label:'OpenAlex',priority:10,enabled:true,configured:true}];
          renderSources();
          const original=window.fetch.bind(window);
          window.fetch=(url,options)=>{
            if(String(url)==='/api/sources/openalex/test') return new Promise(resolve => {
              window.__finishSource=()=>resolve(new Response(JSON.stringify({ok:true,latency_ms:12,
                papers:[{title:'old-query'}]})));
            });
            if(String(url)==='/api/sources') { window.__sourceRead=true; return Promise.resolve(new Response(JSON.stringify({sources:state.sources}))); }
            return original(url,options);
          };
          document.querySelector('.source-row .source-actions .btn-text').click();
        """)
        self.wait.until(lambda d: d.execute_script('return Boolean(window.__finishSource)'))
        self.driver.execute_script("""
          const input=document.getElementById('probe-query'); input.value='new query';
          input.dispatchEvent(new Event('input',{bubbles:true})); window.__finishSource();
        """)
        self.wait.until(lambda d: d.execute_script('return Boolean(window.__sourceRead) && document.querySelector(".source-row .test-result").hidden'))
        self.assertEqual(self.driver.execute_script('return Object.keys(state.lastTest)'), [])
        self.assertFalse(self.driver.find_element(By.CSS_SELECTOR, '.source-row .test-result').is_displayed())

    def test_switching_lune_tools_collapses_the_previous_tools_output(self):
        self.driver.get(self.base + '/sources')
        self.wait.until(lambda d: d.execute_script('return state.sources.length > 0'))
        self.driver.execute_script("""
          state.skills=[{name:'search_papers',parameters:{}},{name:'get_paper_citations',parameters:{}}];
          renderSkills();
        """)
        self.click('#skills-list details:nth-child(1) > summary')
        self.driver.execute_script("""
          const output=document.querySelector('#skills-list details:nth-child(1) .operation-output');
          output.hidden=false; output.textContent='FIRST TOOL RESULT';
        """)
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, '#skills-list details:nth-child(1) .operation-output').is_displayed())
        self.click('#skills-list details:nth-child(2) > summary')
        self.assertFalse(self.driver.find_element(By.CSS_SELECTOR, '#skills-list details:nth-child(1) .operation-output').is_displayed())
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, '#skills-list details:nth-child(2)').get_attribute('open'))

    def test_lune_tool_parameter_change_invalidates_inflight_old_result(self):
        self.driver.get(self.base + '/sources')
        self.wait.until(lambda d: d.execute_script('return state.sources.length > 0'))
        self.driver.execute_script("""
          state.skills=[{name:'search_papers',parameters:{query:{type:'string',required:true}}}];
          renderSkills(); document.querySelector('#skills-list details').open=true;
          const original=window.fetch.bind(window); window.__skillCalls=0;
          window.fetch=(path, options)=>String(path).includes('/skills/search_papers')
            ? new Promise(resolve => { window.__skillCalls++; window.__finishSkill=(title)=>resolve(new Response(
                JSON.stringify({result:{papers:[{title}]}}), {status:200})); })
            : original(path,options);
        """)
        field = self.driver.find_element(By.CSS_SELECTOR, '#skills-list input[name="query"]')
        field.send_keys('first query')
        self.click('#skills-list button[type="submit"]')
        self.wait.until(lambda d: d.execute_script('return Boolean(window.__finishSkill)'))
        field.clear(); field.send_keys('new query')
        self.driver.execute_script('window.__finishSkill("OLD QUERY RESULT")')
        self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#skills-list button[type="submit"]').is_enabled())
        output = self.driver.find_element(By.CSS_SELECTOR, '#skills-list .operation-output')
        self.assertNotIn('OLD QUERY RESULT', output.text)
        self.assertEqual(self.driver.execute_script('return window.__skillCalls'), 1)
        self.click('#skills-list button[type="submit"]')
        self.wait.until(lambda d: d.execute_script('return window.__skillCalls === 2'))
        self.driver.execute_script('window.__finishSkill("NEW QUERY RESULT")')
        self.wait.until(lambda d: 'NEW QUERY RESULT' in d.find_element(By.CSS_SELECTOR, '#skills-list .operation-output').text)
        self.assertNotIn('OLD QUERY RESULT', output.text)

    def test_lune_interrupted_accounting_shows_retained_result_without_retry(self):
        self.driver.get(self.base + '/sources')
        self.wait.until(lambda d: d.execute_script('return state.sources.length > 0'))
        self.driver.execute_script("""
          state.skills=[{name:'search_papers',parameters:{query:{type:'string',required:true}}}];
          renderSkills(); document.querySelector('#skills-list details').open=true;
          const original=window.fetch.bind(window); window.__partialCalls=0;
          window.fetch=(path, options)=>String(path).includes('/skills/search_papers')
            ? (window.__partialCalls++, Promise.resolve(new Response(JSON.stringify({
                error:{message:'Local accounting interrupted',remote_outcome:'completed',retryable:false,retry_safe:false},
                partial_result:{name:'lune',skill:'search_papers',audit_status:'failed',
                                result:{papers:[{id:'one',title:'Retained candidate'}]}}
              }), {status:400}))) : original(path,options);
        """)
        self.driver.find_element(By.CSS_SELECTOR, '#skills-list input[name="query"]').send_keys('fixture')
        self.click('#skills-list button[type="submit"]')
        self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#skills-list button[type="submit"]').is_enabled())
        output = self.driver.find_element(By.CSS_SELECTOR, '#skills-list .operation-output').text
        self.assertIn('Retained candidate', output)
        self.assertIn('审计', output)
        self.assertIn('不要自动重试', output)
        self.assertEqual(self.driver.execute_script('return window.__partialCalls'), 1)

    def test_activity_search_does_not_display_an_older_out_of_order_response(self):
        self.activity_fixture()
        self.driver.execute_script("""
          window.__finish('A');
          const real=window.fetch.bind(window); window.__searches={};
          window.fetch=(url,options)=>String(url).startsWith('/api/activity?q=')
            ? new Promise(resolve => { window.__searches[decodeURIComponent(String(url).split('q=')[1])]=resolve; })
            : real(url,options);
          window.__finishSearch=(query)=>window.__searches[query](new Response(JSON.stringify({
            sessions:[{id:query,kind:'session',title:'Record '+query,status:'active'}],runs:[]})));
          const input=document.getElementById('activity-search'); input.value='alpha';
          input.dispatchEvent(new Event('input',{bubbles:true}));
        """)
        self.wait.until(lambda d: d.execute_script('return Boolean(window.__searches.alpha)'))
        self.driver.execute_script("""
          const input=document.getElementById('activity-search'); input.value='beta';
          input.dispatchEvent(new Event('input',{bubbles:true})); window.__finishSearch('alpha');
        """)
        self.assertNotIn('Record alpha', self.driver.find_element(By.ID, 'activity-list').text)
        self.wait.until(lambda d: d.execute_script('return Boolean(window.__searches.beta)'))
        self.driver.execute_script("window.__finishSearch('beta')")
        self.wait.until(lambda d: 'Record beta' in d.find_element(By.ID, 'activity-list').text)
        self.assertNotIn('Record alpha', self.driver.find_element(By.ID, 'activity-list').text)

    def test_activity_ignores_stale_success_after_new_selection(self):
        self.activity_fixture()
        self.click('#activity-list li:nth-child(2) button')
        self.wait.until(lambda d: d.execute_script('return window.__pending.B?.length===1;'))
        self.driver.execute_script('window.__finish("B");')
        self.wait.until(lambda d: d.find_element(By.ID, 'activity-title').text == 'Record B')
        self.driver.execute_script('window.__finish("A");')
        self.wait.until(lambda d: d.execute_script('return window.__pending.A.length===0;'))
        self.assertEqual(self.driver.find_element(By.ID, 'activity-title').text, 'Record B')
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '.act-item.active .act-item-title').text, 'Record B')
        self.assertEqual(self.driver.find_element(By.ID, 'activity-continue').get_attribute('href'), self.base + '/chat?session=B')

    def test_activity_ignores_stale_error_and_repeated_identity_requests(self):
        self.activity_fixture()
        self.click('#activity-list li:nth-child(2) button')
        self.wait.until(lambda d: d.execute_script('return window.__pending.B?.length===1;'))
        self.driver.execute_script('window.__finish("B");')
        self.wait.until(lambda d: d.find_element(By.ID, 'activity-title').text == 'Record B')
        self.driver.execute_script('window.__finish("A",true);')
        self.wait.until(lambda d: d.execute_script('return window.__pending.A.length===0;'))
        self.assertTrue(self.driver.find_element(By.ID, 'activity-detail').is_displayed())
        self.assertFalse(self.driver.find_element(By.ID, 'activity-placeholder').is_displayed())
        # Same-id re-selection also needs request versions, not just an id guard.
        self.click('#activity-list li:nth-child(2) button')
        self.click('#activity-list li:nth-child(2) button')
        self.wait.until(lambda d: d.execute_script('return window.__pending.B.length===2;'))
        self.driver.execute_script('window.__pending.B.reverse(); window.__finish("B");')
        self.wait.until(lambda d: d.find_element(By.ID, 'activity-detail').is_displayed())
        self.driver.execute_script('window.__finish("B",true);')
        self.wait.until(lambda d: d.execute_script('return window.__pending.B.length===0;'))
        self.assertTrue(self.driver.find_element(By.ID, 'activity-detail').is_displayed())

    def test_activity_deep_link_outside_index_remains_selected_and_can_open_chat(self):
        self.activity_fixture()
        self.driver.get(self.base + '/activity?session=outside-index')
        self.wait.until(lambda d: d.execute_script('return window.__pending["outside-index"]?.length===1;'))
        self.driver.execute_script('window.__finish("outside-index");')
        self.wait.until(lambda d: d.find_element(By.ID, 'activity-title').text == 'Record outside-index')
        self.assertEqual(self.driver.execute_script('return state.selected;'), 'outside-index')
        self.assertIn('session=outside-index', self.driver.find_element(By.ID, 'activity-continue').get_attribute('href'))
        self.assertIsNone(self.driver.execute_script('return window.__pending.A;'))

    def test_search_and_chat_share_full_authors_locator_and_reading_details(self):
        self.search()
        paper = {'id': 'fixture-id', 'title': 'Readable <script>paper</script>',
                 'authors': ['First Author', 'Second Author', 'Third Author', 'Fourth Author'],
                 'year': 2026, 'venue': 'Journal Fixture', 'doi': 'https://doi.org/10.1234/test.1',
                 'url': 'https://example.org/paper', 'abstract': 'A full abstract. ' * 35,
                 'abstract_source': 'openalex', 'evidence_quote': 'A full abstract.',
                 'evidence_verified': True}
        search_citation = self.driver.execute_script('''
          document.querySelector('#paper-list').append(paperCard(arguments[0],0,{}));
          document.querySelector('#result-section').hidden=false;
          return citationText(arguments[0]);
        ''', paper)
        self.driver.get(self.base + '/chat')
        self.wait.until(lambda d: d.execute_script('return typeof paperRow==="function";'))
        chat_citation = self.driver.execute_script('''
          document.querySelector('#turns').append(paperRow(arguments[0],0));
          return citationText(arguments[0]);
        ''', paper)
        self.assertEqual(search_citation, chat_citation)
        for author in paper['authors']:
            self.assertIn(author, chat_citation)
        self.assertIn('https://doi.org/10.1234/test.1', chat_citation)
        self.click('#turns .paper-details > summary')
        detail = self.driver.find_element(By.CSS_SELECTOR, '#turns .paper-details')
        for text in (paper['abstract'].strip(), 'Fourth Author', paper['doi'], 'OpenAlex', 'fixture-id'):
            self.assertIn(text, detail.text)
        self.assertEqual(len(detail.find_elements(By.TAG_NAME, 'script')), 0)
        self.assertEqual(detail.find_element(By.TAG_NAME, 'a').get_attribute('href'), 'https://doi.org/10.1234/test.1')
        self.driver.execute_script('''
          window.__copied=null; Object.defineProperty(navigator,'clipboard',{
            configurable:true,value:{writeText:async text=>{window.__copied=text;}}});
        ''')
        self.click('#turns [data-copy-citation]')
        self.wait.until(lambda d: d.execute_script('return window.__copied;'))
        self.assertEqual(self.driver.execute_script('return window.__copied;'), chat_citation)
        fallback = self.driver.execute_script('return citationText({title:"Unsafe DOI",doi:"javascript:alert(1)",url:"https://example.org/safe"});')
        self.assertIn('https://example.org/safe', fallback)
        self.assertNotIn('javascript:', fallback)

    def test_long_source_identity_wraps_inside_search_and_chat_at_390(self):
        # Real Semantic Scholar identifiers are long unbroken strings. The old
        # short-ID fixtures missed metadata that widened the 390px document.
        paper={'id':'s2:0026b3fac540145483d1baff3d9633ec5e871c50',
               'title':'Bringing a Personal Point of View: Evaluating Dynamic 3D Gaussian Splatting for Egocentric Scene Reconstruction',
               'authors':['First Author','Second Author','Third Author'],
               'year':2026,'venue':'arXiv.org','sources':['semantic_scholar'],
               'url':'https://example.test/long-id-fixture',
               'abstract':'A full abstract describing the reconstruction evaluation.',
               'evidence_quote':'A full abstract describing the reconstruction evaluation.',
               'evidence_verified':True}
        result={'status':'complete','mode':'live','papers':[paper],
                'request':{'topic':'UI metadata wrapping fixture','target_count':1},
                'metrics':{},'trace':[],'turn':1}
        self.search()
        for page in ('search','chat'):
            if page=='chat':
                self.driver.get(self.base+'/chat')
                self.wait.until(lambda d:d.execute_script('return typeof paperRow==="function";'))
            self.driver.execute_script('renderResult(arguments[0]);',result)
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
                {'width':390,'height':900,'deviceScaleFactor':1,'mobile':False})
            selector='.paper-meta' if page=='search' else '.p-meta'
            for theme in ('light','dark'):
                with self.subTest(page=page,theme=theme):
                    self.set_theme(theme)
                    self.driver.execute_async_script('const done=arguments[arguments.length-1];'
                        'requestAnimationFrame(()=>requestAnimationFrame(done));')
                    geometry=self.driver.execute_script('''
                      const e=document.querySelector(arguments[0]),r=e.getBoundingClientRect();
                      return {width:innerWidth,documentWidth:document.documentElement.scrollWidth,
                        clientWidth:document.documentElement.clientWidth,left:r.left,right:r.right,
                        scrollWidth:e.scrollWidth,metaWidth:e.clientWidth,text:e.textContent};
                    ''',selector)
                    self.assertEqual(geometry['width'],390)
                    self.assertLessEqual(geometry['documentWidth'],geometry['clientWidth'])
                    self.assertGreaterEqual(geometry['left'],0)
                    self.assertLessEqual(geometry['right'],390)
                    self.assertLessEqual(geometry['scrollWidth'],geometry['metaWidth'])
                    self.assertIn(paper['id'],geometry['text'])

    def test_activity_continue_returns_to_same_saved_chat_without_new_turn(self):
        self.driver.get(self.base + '/chat?demo=followup')
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        session = self.driver.find_element(By.ID, 'session-id').text
        turns = len(self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        self.driver.get(self.base + '/activity?session=' + session)
        self.wait.until(lambda d: d.find_element(By.ID, 'activity-continue').is_displayed())
        self.click('#activity-continue')
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        self.assertEqual(self.driver.find_element(By.ID, 'session-id').text, session)
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, '#turns .turn')), turns)

    def test_research_coverage_marks_gaps_and_qualifies_model_classification(self):
        self.search()
        self.driver.execute_script('''
          const result={status:'partial',mode:'live',papers:[{id:'one',title:'Method A',
            abstract:'Geometry prediction.',evidence_quote:'Geometry prediction.',evidence_verified:true,
            url:'https://example.org/one'}],request:{topic:'3D'},metrics:{},
            research_coverage:{status:'partial',scope:'broad',facets:[{id:'geometry',label:'Geometry <img src=x>'},
              {id:'dynamic',label:'Dynamic surfaces'}],covered:['geometry'],missing:['dynamic'],
              assignments:[{paper_id:'one',facet_ids:['geometry'],evidence_quote:'Geometry prediction.',verification:'quote_match_only'}]}};
          renderResult(result);
        ''')
        coverage = self.driver.find_element(By.CSS_SELECTOR, '#result-section > .research-coverage')
        self.assertIn('Dynamic surfaces · 仍有缺口', coverage.text)
        self.assertIn('Geometry <img src=x> · 已关联论文', coverage.text)
        self.assertIn('模型分类', coverage.text)
        self.assertIn('不代表领域穷尽', coverage.text)
        self.assertEqual(coverage.find_elements(By.TAG_NAME, 'img'), [])
        self.click('.research-coverage details > summary')
        self.assertIn('Geometry prediction.', coverage.text)
        self.assertIn('仍是模型判断', coverage.text)

    def test_interrupted_search_requires_explicit_resume_and_preserves_partial_results(self):
        self.search()
        self.driver.execute_script('''
          const base=window.fetch.bind(window);
          window.__resumePosts=0; window.__resumed=false;
          const result={status:'partial',mode:'live',papers:[{id:'kept',title:'Saved paper',authors:['Author'],
            abstract:'Saved evidence',evidence_quote:'Saved evidence',evidence_verified:true}],
            candidates:[{id:'candidate'}],request:{topic:'kept research'},metrics:{},trace:[]};
          window.fetch=(url, options={})=>{
            if(String(url).endsWith('/resume')) {
              window.__resumePosts++;
              if(window.__resumePosts===1) return Promise.resolve(new Response(JSON.stringify({error:'Resume transport unavailable'}),{status:503}));
              window.__resumed=true;
              return Promise.resolve(new Response(JSON.stringify({id:state.job}),{status:202}));
            }
            if(String(url)==='/api/jobs/'+state.job) return Promise.resolve(new Response(JSON.stringify(
              window.__resumed ? {state:'done',trace:[],result} : {state:'needs_retry',trace:[],result,
                recovery:{can_resume:true,message:'Recorded calls can be reused',completed_calls:2,uncertain_calls:0}}
            )));
            return base(url,options);
          };
          state.job='a'.repeat(32);state.started=Date.now();setBusy(true);poll();
        ''')
        self.wait.until(lambda d: d.find_element(By.ID, 'job-recovery').is_displayed())
        self.assertIn('Saved paper', self.driver.find_element(By.ID, 'paper-list').text)
        self.assertIn('2', self.driver.find_element(By.ID, 'job-recovery-progress').text)
        self.assertEqual(self.driver.execute_script('return window.__resumePosts;'), 0)
        self.assertFalse(self.driver.execute_script('return state.busy;'))
        self.click('#resume-search')
        self.wait.until(lambda d: '恢复未能启动' in d.find_element(By.ID, 'job-recovery-message').text)
        self.assertIn('Saved paper', self.driver.find_element(By.ID, 'paper-list').text)
        self.assertTrue(self.driver.find_element(By.ID, 'job-recovery').is_displayed())
        self.click('#resume-search')
        self.wait.until(lambda d: d.execute_script('return window.__resumed && !state.busy;'))
        self.assertEqual(self.driver.execute_script('return window.__resumePosts;'), 2)
        self.assertFalse(self.driver.find_element(By.ID, 'job-recovery').is_displayed())
        self.assertIn('Saved paper', self.driver.find_element(By.ID, 'paper-list').text)

    def test_switching_mode_preserves_interrupted_search_for_explicit_recovery(self):
        self.search()
        fixture = """
          const original=window.fetch.bind(window); window.__resumePosts=0;
          window.fetch=(url,options={})=>{
            const path=String(url);
            if(path.endsWith('/resume')) { window.__resumePosts++; return Promise.resolve(new Response('{}')); }
            if(path==='/api/jobs/'+'c'.repeat(32)) return Promise.resolve(new Response(JSON.stringify({
              state:'needs_retry',trace:[],result:null,
              recovery:{can_resume:true,message:'Saved interrupted task',completed_calls:1,uncertain_calls:0}})));
            return original(url,options);
          };
        """
        self.inject(fixture)
        self.driver.execute_script(fixture + "state.job='c'.repeat(32); state.started=Date.now(); setBusy(true); poll();")
        self.wait.until(lambda d: d.find_element(By.ID, 'job-recovery').is_displayed())
        self.click('#demo-tab')
        self.assertTrue(self.driver.find_element(By.ID, 'detached-recovery-note').is_displayed())
        self.assertIsNone(self.driver.execute_script('return state.job'))
        self.assertEqual(self.driver.execute_script('return window.__resumePosts'), 0)
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config) && !state.busy'))
        self.assertTrue(self.driver.find_element(By.ID, 'detached-recovery-note').is_displayed())
        self.click('#open-interrupted-search')
        self.wait.until(lambda d: d.find_element(By.ID, 'job-recovery').is_displayed())
        self.assertEqual(self.driver.execute_script('return state.mode'), 'live')
        self.assertEqual(self.driver.execute_script('return window.__resumePosts'), 0)
        self.assertIn('Saved interrupted task', self.driver.find_element(By.ID, 'job-recovery-message').text)

    def test_unknown_inflight_call_explains_recovery_without_auto_retry(self):
        self.search()
        self.driver.execute_script('''
          const base=window.fetch.bind(window);
          window.fetch=(url,options={})=>String(url)==='/api/jobs/'+state.job
            ? Promise.resolve(new Response(JSON.stringify({state:'needs_retry',trace:[],result:null,
              recovery:{can_resume:false,reason:'unknown_external_outcome',message:'外部调用结果未知，不能自动重发',
                completed_calls:1,uncertain_calls:1}}))) : base(url,options);
          state.job='b'.repeat(32);state.started=Date.now();setBusy(true);poll();
        ''')
        self.wait.until(lambda d: d.find_element(By.ID, 'job-recovery').is_displayed())
        self.assertFalse(self.driver.find_element(By.ID, 'resume-search').is_displayed())
        self.assertIn('未知', self.driver.find_element(By.ID, 'job-recovery-message').text)
        self.assertFalse(self.driver.find_element(By.ID, 'running-state').is_displayed())
        self.assertEqual(self.driver.execute_script('return window.__posts;'), [])
        self.click('#edit-interrupted-search')
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'topic')


if __name__ == '__main__':
    unittest.main()
