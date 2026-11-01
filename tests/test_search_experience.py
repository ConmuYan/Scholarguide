"""Search journeys in a real browser, using temporary offline-only services."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import Select, WebDriverWait
except ImportError:
    webdriver = None

from activity import ActivityFeed
from common import Budget, Config
from demo import demo_run
from providers import ProviderStore
from webapp import ChatService, Jobs, create_server
from scripts.verify_delivery import apply_browser_isolation


class SearchExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get('SG_REQUIRE_BROWSER') == '1'
        if webdriver is None:
            if required:
                raise RuntimeError('Search journeys require Selenium.')
            raise unittest.SkipTest('Search journeys require Selenium.')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.providers = ProviderStore(cls.root / 'providers.json')
        cls.providers.upsert({'id': 'offline-search-test', 'name': 'Offline search fixture',
                              'base_url': 'http://127.0.0.1:1', 'model': 'no-paid-api'}, activate=True)
        cls.chat = ChatService(cls.root / 'web', providers=cls.providers)

        def offline_only(data, progress):
            if data['mode'] != 'demo':
                raise AssertionError('Browser tests must never issue a live model request.')
            return demo_run(data['scenario'], data['method'], progress=progress,
                            config=cls.providers.config(Config()),
                            budget=Budget(60, cancel_token=data['_cancel_token']))

        cls.jobs = Jobs(cls.root / 'runs', runner=offline_only,
                        importer=cls.chat.import_run, providers=cls.providers)
        cls.server = create_server(0, jobs=cls.jobs, chat=cls.chat,
                                   activity=ActivityFeed(cls.root / 'runs'), providers=cls.providers)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        options = Options()
        apply_browser_isolation(options, cls.root / 'chrome-profile')
        for flag in ('--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=1440,1100'):
            options.add_argument(flag)
        try:
            cls.driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)
            cls.chat.close(); cls.tmp.cleanup()
            if required:
                raise RuntimeError(f'Required browser unavailable: {exc}') from exc
            raise unittest.SkipTest(f'Browser unavailable: {exc}')

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)
        cls.chat.close(); cls.tmp.cleanup()

    def setUp(self):
        self.driver.set_window_size(1440, 1100)
        # Clear on a page without Search polling, so a late completed-job
        # callback cannot repopulate sessionStorage between clear and reload.
        self.driver.get(self.base + '/settings')
        self.driver.execute_script('sessionStorage.clear(); localStorage.clear();')
        self.driver.get(self.base + '/search')
        self.wait = WebDriverWait(self.driver, 20)
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config);'))
        self.driver.execute_script('''
          window.__copied = null;
          Object.defineProperty(navigator, 'clipboard', {configurable:true,
            value:{writeText: async value => {window.__copied = value;}}});
        ''')

    def click(self, selector):
        self.driver.execute_script('arguments[0].click();', self.driver.find_element(By.CSS_SELECTOR, selector))

    def demo(self, scenario='followup'):
        self.click('#demo-tab')
        self.driver.execute_script('document.querySelector("#scenario").value=arguments[0];', scenario)
        self.click('#submit-button')
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#paper-list article'))
        self.wait.until(lambda d: not d.execute_script('return state.busy;'))
        return self.driver.execute_script('return state.job;')

    def open_control(self, identity):
        self.driver.execute_script('for(let node=document.getElementById(arguments[0])?.parentElement;node;node=node.parentElement) if(node.tagName==="DETAILS") node.open=true;', identity)

    def select(self, identity, value):
        self.open_control(identity)
        Select(self.driver.find_element(By.ID, identity)).select_by_value(value)

    def fill(self, identity, value):
        self.open_control(identity)
        node = self.driver.find_element(By.ID, identity)
        node.clear()
        if value:
            node.send_keys(value)

    def capture_submissions(self):
        """Observe real browser POST bodies while preventing any live work."""
        self.driver.execute_script('''
          window.__searchRequests=[];
          const original=window.fetch.bind(window);
          window.fetch=(url, options={})=>{
            if(String(url)==='/api/jobs' && options.method==='POST'){
              window.__searchRequests.push(JSON.parse(options.body));
              return Promise.resolve(new Response(JSON.stringify({error:'Captured offline; no API call'}),
                {status:503,headers:{'Content-Type':'application/json'}}));
            }
            return original(url, options);
          };
        ''')

    def captured_payload(self):
        self.click('#submit-button')
        self.wait.until(lambda d: d.execute_script('return window.__searchRequests.length > 0;'))
        self.wait.until(lambda d: not d.execute_script('return state.busy;'))
        return self.driver.execute_script('return window.__searchRequests.at(-1);')

    def preview(self):
        details = self.driver.find_element(By.ID, 'request-preview')
        self.assertTrue(details.is_displayed())
        if not details.get_attribute('open'):
            self.click('#request-preview summary')
        return self.driver.find_element(By.ID, 'request-preview-text').text

    def test_default_scope_is_unrestricted_and_post_has_no_placeholder_constraints(self):
        self.capture_submissions()
        self.assertFalse(self.driver.find_elements(By.ID, 'target-count'))
        self.assertFalse(self.driver.find_element(By.ID, 'minimum-field').is_displayed())
        for identity in ('venue', 'year-from', 'year-to'):
            node = self.driver.find_element(By.ID, identity)
            self.assertEqual(node.get_attribute('value'), '')
            self.assertIn('不限', node.get_attribute('placeholder'))
        self.fill('topic', 'graph neural networks')
        scope = self.driver.find_element(By.ID, 'search-scope-summary')
        self.assertTrue(scope.is_displayed())
        self.assertIn('5', scope.text)
        self.assertIn('不限', scope.text)
        self.assertNotIn('2024', scope.text)
        self.assertNotIn('2025', scope.text)
        preview = self.preview()
        payload = self.captured_payload()
        self.assertEqual(payload['request_constraints'], {
            'category': 'none', 'minimum_category_count': 0,
            'venue': '', 'year_from': None, 'year_to': None,
        })
        self.assertIn(payload['request'], preview)
        self.assertIn('graph neural networks', payload['request'])
        self.assertNotIn('2024', payload['request'])
        self.assertNotIn('2025', payload['request'])

    def test_chosen_count_category_and_range_match_actual_post_and_preview(self):
        self.capture_submissions()
        self.fill('topic', '推荐3篇 medical retrieval augmented generation 论文')
        self.select('category', 'evaluation')
        self.assertTrue(self.driver.find_element(By.ID, 'minimum-field').is_displayed())
        self.select('minimum', '2')
        self.fill('venue', 'ACL')
        self.fill('year-from', '2022')
        self.fill('year-to', '2025')
        self.select('reading-goal', 'comparison')
        preview = self.preview()
        scope = self.driver.find_element(By.ID, 'search-scope-summary').text
        for value in ('3', '2', 'ACL', '2022', '2025'):
            self.assertIn(value, scope)
        payload = self.captured_payload()
        self.assertEqual(payload['request_constraints'], {
            'target_count': 3, 'category': 'evaluation', 'minimum_category_count': 2,
            'venue': 'ACL', 'year_from': 2022, 'year_to': 2025,
        })
        self.assertEqual(payload['reading_goal'], 'comparison')
        self.assertIn(payload['request'], preview)
        self.assertRegex(payload['request'], r'3\s*篇')
        self.assertIn('ACL', payload['request'])
        self.assertIn('2022', payload['request'])
        self.assertIn('2025', payload['request'])

    def test_quota_above_requested_count_is_rejected_without_silent_changes(self):
        self.capture_submissions()
        self.fill('topic', '推荐3篇 RAG evaluation 论文')
        self.select('category', 'evaluation')
        self.select('minimum', '5')
        self.assertEqual(self.driver.find_element(By.ID, 'minimum').get_attribute('value'), '5')
        self.assertIn('3篇', self.driver.find_element(By.ID, 'topic').get_attribute('value'))
        self.assertTrue(self.driver.find_element(By.ID, 'submit-button').get_attribute('disabled'))
        self.assertTrue(self.driver.find_element(By.ID, 'input-feedback').text.strip())
        self.driver.find_element(By.ID, 'topic').send_keys(Keys.CONTROL, Keys.ENTER)
        self.assertEqual(self.driver.execute_script('return window.__searchRequests.length;'), 0)
        self.assertIsNone(self.driver.execute_script('return state.job;'))
        self.select('category', 'none')
        self.assertFalse(self.driver.find_element(By.ID, 'minimum-field').is_displayed())
        payload = self.captured_payload()
        self.assertEqual(payload['request_constraints']['target_count'], 3)
        self.assertEqual(payload['request_constraints']['minimum_category_count'], 0)

    def test_guided_to_natural_copies_valid_requirements_and_does_not_stack_filters(self):
        self.capture_submissions()
        self.fill('topic', '推荐3篇 scientific question answering 论文')
        self.select('category', 'survey')
        self.select('minimum', '2')
        self.fill('venue', 'ACL')
        self.fill('year-from', '2021')
        self.fill('year-to', '2024')
        guided_preview = self.preview()
        self.click('#toggle-input')
        natural = self.driver.find_element(By.ID, 'request')
        copied_request = natural.get_attribute('value')
        self.assertIn(copied_request, guided_preview)
        self.assertIn('scientific question answering', copied_request)
        for value in ('ACL', '2021', '2024'):
            self.assertIn(value, copied_request)
        self.assertRegex(copied_request, r'3\s*篇')
        self.assertFalse(self.driver.find_elements(By.ID, 'target-count'))
        self.assertRegex(self.driver.find_element(By.ID, 'topic-help').text, r'未叠加|不叠加|不.*叠加|仅.*完整需求')
        self.fill('request', '推荐 4 篇 scientific question answering 论文，会议和年份不限。')
        preview = self.preview()
        payload = self.captured_payload()
        self.assertNotIn('request_constraints', payload)
        self.assertEqual(payload['request'], natural.get_attribute('value'))
        self.assertIn(payload['request'], preview)
        self.click('#toggle-input')
        self.assertIn('3篇', self.driver.find_element(By.ID, 'topic').get_attribute('value'))
        self.assertEqual(self.driver.find_element(By.ID, 'venue').get_attribute('value'), 'ACL')
        self.assertEqual(self.driver.find_element(By.ID, 'minimum').get_attribute('value'), '2')

    def test_reading_goal_override_and_examples_are_explicit_without_running(self):
        self.capture_submissions()
        self.open_control('reading-goal')
        examples_help = self.driver.find_element(By.ID, 'examples-help').text
        self.assertRegex(examples_help, r'(仅|只).*填[入充]')
        self.assertRegex(examples_help, r'编辑|修改|搜索|发送')
        self.assertRegex(self.driver.find_element(By.ID, 'goal-help').text, r'综述|搭配|类型')
        self.click('[data-example="evaluation"]')
        self.assertEqual(self.driver.find_element(By.ID, 'live-tab').get_attribute('aria-pressed'), 'true')
        self.assertEqual(self.driver.find_element(By.ID, 'demo-tab').get_attribute('aria-pressed'), 'false')
        self.assertEqual(self.driver.execute_script('return window.__searchRequests.length;'), 0)
        self.assertIsNone(self.driver.execute_script('return state.job;'))
        self.select('reading-goal', 'orientation')
        self.assertRegex(self.driver.find_element(By.ID, 'goal-help').text, r'覆盖|优先')
        preview = self.preview()
        self.assertIn('入门', preview)
        self.assertRegex(preview, r'覆盖|优先')
        payload = self.captured_payload()
        self.assertEqual(payload['reading_goal'], 'orientation')
        self.assertNotIn('request_constraints', payload)
        self.assertEqual(payload['mode'], 'live')

    def test_draft_restores_count_and_range_across_reload_and_live_demo_switch(self):
        self.fill('topic', '推荐4篇 language model safety 论文')
        self.select('category', 'survey')
        self.select('minimum', '2')
        self.fill('venue', 'ACL')
        self.fill('year-from', '2020')
        self.fill('year-to', '2023')
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config);'))
        self.assertEqual(self.driver.find_element(By.ID, 'topic').get_attribute('value'), '推荐4篇 language model safety 论文')
        expected = {'category': 'survey', 'minimum': '2',
                    'venue': 'ACL', 'year-from': '2020', 'year-to': '2023'}
        for identity, value in expected.items():
            self.assertEqual(self.driver.find_element(By.ID, identity).get_attribute('value'), value)
        self.click('#demo-tab')
        self.click('#live-tab')
        for identity, value in expected.items():
            self.assertEqual(self.driver.find_element(By.ID, identity).get_attribute('value'), value)
        self.capture_submissions()
        payload = self.captured_payload()
        self.assertEqual(payload['request_constraints'], {
            'target_count': 4, 'category': 'survey', 'minimum_category_count': 2,
            'venue': 'ACL', 'year_from': 2020, 'year_to': 2023,
        })

    def test_delayed_config_does_not_replace_a_source_selected_during_startup(self):
        injection = self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
              (() => {
                const original=window.fetch.bind(window);
                const pending=[];
                window.__configDeferred=false;
                window.__startupSearchPosts=0;
                window.__releaseSearchConfig=()=>{
                  for(const release of pending.splice(0)) release();
                };
                window.fetch=(url, options)=>{
                  if(String(url)==='/api/jobs' && options?.method==='POST') window.__startupSearchPosts++;
                  if(String(url)==='/api/config'){
                    window.__configDeferred=true;
                    return new Promise((resolve,reject)=>{
                      pending.push(()=>original(url, options).then(resolve,reject));
                    });
                  }
                  return original(url, options);
                };
              })();
            ''',
        })
        try:
            self.driver.execute_script('sessionStorage.clear();')
            self.driver.get(self.base + '/search')
            self.wait.until(lambda d: d.execute_script('return window.__configDeferred === true;'))
            self.assertIsNone(self.driver.execute_script('return state.config;'))
            self.click('#source-settings > summary')
            self.select('source', 'openreview')
            self.fill('topic', 'language model reasoning')
            self.assertEqual(self.driver.find_element(By.ID, 'source').get_attribute('value'), 'openreview')
            self.driver.execute_script('window.__releaseSearchConfig();')
            self.wait.until(lambda d: d.execute_script('return Boolean(state.config);'))
            # The research default is separate from the environment source;
            # the user's explicit selection must survive either configuration.
            self.assertEqual(self.driver.execute_script('return state.config.research_default_source;'), 'multi')
            self.assertEqual(self.driver.find_element(By.ID, 'source').get_attribute('value'), 'openreview')
            self.assertEqual(self.driver.execute_script('return inputPayload().source;'), 'openreview')
            self.assertEqual(self.driver.execute_script('return window.__startupSearchPosts;'), 0)
            self.assertIsNone(self.driver.execute_script('return state.job;'))
        finally:
            self.driver.execute_script('window.__releaseSearchConfig?.();')
            self.driver.execute_cdp_cmd('Page.removeScriptToEvaluateOnNewDocument', {
                'identifier': injection['identifier'],
            })

    def test_examples_are_editable_and_never_submit_a_search(self):
        self.assertEqual(self.driver.find_element(By.ID, 'source').get_attribute('value'), 'multi')
        self.click('[data-example="evaluation"]')
        request = self.driver.find_element(By.ID, 'request')
        self.assertTrue(request.is_displayed())
        self.assertIn('设计实验', request.get_attribute('value'))
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'request')
        self.assertFalse(self.driver.find_element(By.ID, 'category').is_displayed())
        self.assertFalse(self.driver.find_element(By.ID, 'submit-button').get_attribute('disabled'))
        self.assertIsNone(self.driver.execute_script('return state.job;'))
        request.send_keys(' 仅考虑医学场景。')
        self.click('#request-preview summary')
        self.assertIn('医学场景', self.driver.find_element(By.ID, 'request-preview-text').text)

    def test_composing_a_chinese_query_does_not_submit_on_control_enter(self):
        self.driver.find_element(By.ID, 'topic').send_keys('RAG evaluation')
        self.driver.execute_script("window.__searchPosts=0; const original=window.fetch.bind(window); window.fetch=(...args)=>{if(String(args[0])==='/api/jobs' && args[1]?.method==='POST') window.__searchPosts++; return original(...args);}; document.querySelector('#topic').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',ctrlKey:true,isComposing:true,bubbles:true,cancelable:true}));")
        self.assertEqual(self.driver.execute_script('return window.__searchPosts'), 0)
        self.assertIsNone(self.driver.execute_script('return state.job'))

    def test_readiness_explains_online_setup_and_demo_stays_available(self):
        self.driver.execute_script("state.config={ready:false,message:'请配置当前模型'}; syncForm();")
        self.assertIn('配置', self.driver.find_element(By.ID, 'search-readiness').text)
        self.assertTrue(self.driver.find_element(By.ID, 'submit-button').get_attribute('disabled'))
        self.assertTrue(self.driver.find_element(By.ID, 'readiness-actions').is_displayed())
        self.click('#readiness-demo')
        self.assertFalse(self.driver.find_element(By.ID, 'submit-button').get_attribute('disabled'))
        self.assertIn('固定 RAG', self.driver.find_element(By.ID, 'search-readiness').text)
        self.assertIsNone(self.driver.execute_script('return state.job;'))
        self.click('#submit-button')
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#paper-list article'))
        self.assertEqual(self.driver.execute_script('return state.lastResult.metrics.llm_calls;'), 0)
        self.assertIn('离线演示', self.driver.find_element(By.ID, 'outcome').text)

    def test_invalid_years_block_keyboard_submission_and_preserve_draft(self):
        self.driver.find_element(By.ID, 'topic').send_keys('RAG evaluation')
        self.open_control('year-from')
        self.driver.find_element(By.ID, 'year-from').send_keys('2025')
        self.driver.find_element(By.ID, 'year-to').send_keys('2023')
        self.assertIn('不能晚于', self.driver.find_element(By.ID, 'input-feedback').text)
        self.assertTrue(self.driver.find_element(By.ID, 'submit-button').get_attribute('disabled'))
        self.driver.find_element(By.ID, 'topic').send_keys(Keys.CONTROL, Keys.ENTER)
        self.assertIsNone(self.driver.execute_script('return state.job;'))
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'year-from')
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config);'))
        self.assertEqual(self.driver.find_element(By.ID, 'topic').get_attribute('value'), 'RAG evaluation')
        self.assertEqual(self.driver.find_element(By.ID, 'year-from').get_attribute('value'), '2025')
        end = self.driver.find_element(By.ID, 'year-to')
        end.clear(); end.send_keys('2026')
        self.assertFalse(self.driver.find_element(By.ID, 'submit-button').get_attribute('disabled'))
        self.click('#request-preview summary')
        self.assertIn('2025-2026', self.driver.find_element(By.ID, 'request-preview-text').text)

    def test_filter_keeps_original_ranks_and_copy_includes_full_list_and_locators(self):
        self.demo()
        self.driver.execute_script('''
          const result = structuredClone(state.lastResult);
          result.papers[0].sources=['arxiv'];
          result.papers.slice(1).forEach(p => p.sources=['openalex']);
          renderResult(result);
        ''')
        self.click('#source-filter [data-source="openalex"]')
        indices = self.driver.find_elements(By.CSS_SELECTOR, '#paper-list .paper-index')
        self.assertEqual(indices[0].text, '02')
        self.assertEqual(len(indices), 4)
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '[data-source="openalex"]').get_attribute('aria-pressed'), 'true')
        self.assertIn('完整的 5 篇', self.driver.find_element(By.ID, 'filter-feedback').text)
        self.click('#copy-reading-list')
        self.wait.until(lambda d: d.execute_script('return Boolean(window.__copied);'))
        copied = self.driver.execute_script('return window.__copied;')
        all_papers = self.driver.execute_script('return state.lastResult.papers;')
        for paper in all_papers:
            self.assertIn(paper['title'], copied)
        self.assertIn('https://', copied)
        self.assertIn('离线演示', copied)
        self.click('#paper-list [data-copy-citation]')
        citation = self.driver.execute_script('return window.__copied;')
        self.assertIn(all_papers[1]['title'], citation)
        self.assertIn('https://', citation)
        self.assertIn('已复制', self.driver.find_element(By.ID, 'copy-status').get_attribute('textContent'))

    def test_switching_mode_clears_result_and_trace_without_deleting_history(self):
        job = self.demo()
        count = len(self.jobs.items)
        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '#trace-list li'))
        self.click('#live-tab')
        self.assertTrue(self.driver.find_element(By.ID, 'empty-state').is_displayed())
        self.assertFalse(self.driver.find_element(By.ID, 'result-section').is_displayed())
        self.assertFalse(self.driver.find_element(By.ID, 'downloads').is_displayed())
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list article'), [])
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#trace-list li'), [])
        self.assertIsNone(self.driver.execute_script('return state.lastResult;'))
        self.assertEqual(len(self.jobs.items), count)
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config) && !state.busy;'))
        self.assertEqual(self.driver.execute_script('return state.mode;'), 'live')
        self.assertTrue(self.driver.find_element(By.ID, 'empty-state').is_displayed())
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list article'), [])
        self.assertIn(job, self.jobs.items)

    def test_late_trace_from_previous_mode_cannot_repopulate_the_panel(self):
        self.driver.execute_script("""
          window.ScholarGuideTrace.subscribe = (url, options) => {
            window.__oldTraceEvent=options.onEvent; return {close(){}};
          };
        """)
        self.demo()
        self.click('#live-tab')
        self.driver.execute_script("window.__oldTraceEvent({seq:999,action:'search',query:'obsolete trace'});")
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#trace-list li'), [])
        self.assertEqual(self.driver.find_element(By.ID, 'trace-count').get_attribute('textContent'), '空闲')

    def test_live_input_edit_clears_old_output_without_starting_another_search(self):
        original = self.jobs.runner
        self.addCleanup(setattr, self.jobs, 'runner', original)
        def fixture_live(data, progress):
            result = demo_run('followup', data['method'], progress=progress,
                              config=self.providers.config(Config()),
                              budget=Budget(60, cancel_token=data['_cancel_token']))
            result['mode'] = 'live'
            return result
        self.jobs.runner = fixture_live
        self.fill('topic', 'first topic')
        self.click('#submit-button')
        self.wait.until(lambda d: d.execute_script('return state.lastResult && !state.busy;'))
        job = self.driver.execute_script('return state.job')
        count = len(self.jobs.items)
        self.driver.find_element(By.ID, 'topic').send_keys(' changed')
        self.assertTrue(self.driver.find_element(By.ID, 'empty-state').is_displayed())
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list article'), [])
        self.assertEqual(len(self.jobs.items), count)
        self.assertIn(job, self.jobs.items)
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config) && !state.busy'))
        self.assertIn('changed', self.driver.find_element(By.ID, 'topic').get_attribute('value'))
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list article'), [])

    def test_editing_scenario_clears_old_output_but_keeps_the_job(self):
        job = self.demo()
        count = len(self.jobs.items)
        self.select('scenario', 'satisfied')
        self.assertTrue(self.driver.find_element(By.ID, 'empty-state').is_displayed())
        self.assertFalse(self.driver.find_element(By.ID, 'result-section').is_displayed())
        self.assertEqual(len(self.jobs.items), count)
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config) && !state.busy;'))
        self.assertEqual(self.driver.find_element(By.ID, 'scenario').get_attribute('value'), 'satisfied')
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list article'), [])
        self.assertIn(job, self.jobs.items)

    def test_reload_does_not_restore_a_legacy_job_from_the_wrong_mode(self):
        job = self.demo()
        self.driver.execute_script("""
          const draft=JSON.parse(sessionStorage.getItem('scholarguide-search-draft'));
          draft.mode='live'; sessionStorage.setItem('scholarguide-search-draft', JSON.stringify(draft));
        """)
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config) && !state.busy;'))
        self.assertEqual(self.driver.execute_script('return state.mode;'), 'live')
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, '#paper-list article'), [])
        self.assertTrue(self.driver.find_element(By.ID, 'empty-state').is_displayed())
        self.assertIn(job, self.jobs.items)

    def test_demo_deep_link_starts_its_requested_scenario_after_an_old_result(self):
        old_job = self.demo('followup')
        self.driver.execute_script("""
          const old=JSON.parse(sessionStorage.getItem('scholarguide-job'));
          old.mode='live'; sessionStorage.setItem('scholarguide-job', JSON.stringify(old));
          const draft=JSON.parse(sessionStorage.getItem('scholarguide-search-draft'));
          draft.mode='live'; sessionStorage.setItem('scholarguide-search-draft', JSON.stringify(draft));
        """)
        self.driver.get(self.base + '/search?demo=insufficient')
        self.wait.until(lambda d: d.execute_script('return state.lastResult && !state.busy && state.job !== arguments[0];', old_job))
        self.assertEqual(self.driver.execute_script('return state.mode;'), 'demo')
        self.assertEqual(self.driver.find_element(By.ID, 'scenario').get_attribute('value'), 'insufficient')
        self.assertIn(old_job, self.jobs.items)
        new_job = self.driver.execute_script('return state.job;')
        count = len(self.jobs.items)
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return state.lastResult && !state.busy;'))
        self.assertEqual(self.driver.execute_script('return state.job;'), new_job)
        self.assertEqual(len(self.jobs.items), count)

    def test_completed_demo_survives_reload_and_hands_same_task_to_chat(self):
        job = self.demo()
        count = len(self.jobs.items)
        link = self.driver.find_element(By.ID, 'continue-chat').get_attribute('href')
        self.assertIn('/chat?session=', link)
        self.assertTrue(self.driver.find_element(By.ID, 'task-continuity').is_displayed())
        self.driver.refresh()
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#paper-list article'))
        self.assertEqual(self.driver.execute_script('return state.job;'), job)
        self.assertEqual(len(self.jobs.items), count)
        self.assertEqual(self.driver.find_element(By.ID, 'continue-chat').get_attribute('href'), link)
        self.click('#continue-chat')
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .turn'))
        self.assertEqual(self.driver.find_element(By.ID, 'session-id').text, link.rsplit('=', 1)[1])

    def test_exports_are_actual_saved_artifacts_with_complete_list(self):
        job = self.demo()
        payload = self.driver.execute_async_script('''
          const done=arguments[0];
          Promise.all(['report.md','result.json','references.bib'].map(async name=>{
            const response=await fetch('/api/jobs/'+state.job+'/'+name);
            return {name,status:response.status,body:await response.text()};
          })).then(done);
        ''')
        self.assertEqual([entry['status'] for entry in payload], [200, 200, 200])
        raw = json.loads(next(item['body'] for item in payload if item['name'] == 'result.json'))
        self.assertEqual(len(raw['papers']), 5)
        markdown = next(item['body'] for item in payload if item['name'] == 'report.md')
        bibtex = next(item['body'] for item in payload if item['name'] == 'references.bib')
        for paper in raw['papers']:
            self.assertIn(paper['title'], markdown)
        self.assertEqual(bibtex.count('@misc{'), 5)
        self.assertIn(job, self.driver.find_element(By.ID, 'download-bib').get_attribute('href'))

    def test_failed_replacement_submission_keeps_previous_results(self):
        job = self.demo()
        first = self.driver.find_element(By.CSS_SELECTOR, '#paper-list h3').text
        self.driver.execute_script('''
          const original = window.fetch.bind(window);
          window.fetch = (url, options={}) => String(url)==='/api/jobs' && options.method==='POST'
            ? Promise.resolve(new Response(JSON.stringify({error:'离线故障注入'}),
                {status:503,headers:{'Content-Type':'application/json'}}))
            : original(url, options);
        ''')
        self.click('#submit-button')
        self.wait.until(lambda d: d.find_element(By.ID, 'form-error-row').is_displayed())
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '#paper-list h3').text, first)
        self.assertEqual(self.driver.execute_script('return state.job;'), job)
        self.assertTrue(self.driver.find_element(By.ID, 'downloads').is_displayed())

    def test_stop_requests_once_and_waits_for_same_job_result(self):
        self.driver.execute_script('''
          window.__cancelPosts=0;
          const original=window.fetch.bind(window);
          window.fetch=(url, options={})=>{
            if(String(url).endsWith('/cancel')){
              window.__cancelPosts+=1;
              return Promise.resolve(new Response(JSON.stringify({accepted:true,cancel_requested:true}),
                {status:200,headers:{'Content-Type':'application/json'}}));
            }
            if(String(url)==='/api/jobs/'+state.job) return Promise.resolve(new Response(
              JSON.stringify({state:'running',trace:[],cancel_requested:true,cancellation_state:'stopping'}),
              {status:200,headers:{'Content-Type':'application/json'}}));
            return original(url,options);
          };
          state.mode='demo'; state.job='f'.repeat(32); state.started=Date.now();
          document.querySelector('#running-state').hidden=false; setBusy(true);
        ''')
        self.click('#cancel-search')
        self.wait.until(lambda d: d.execute_script('return window.__cancelPosts === 1;'))
        self.assertTrue(self.driver.find_element(By.ID, 'cancel-search').get_attribute('disabled'))
        self.assertTrue(self.driver.execute_script('return state.busy;'))
        self.assertIn('正在停止', self.driver.find_element(By.ID, 'cancel-search').text)
        self.assertEqual(self.driver.execute_script('return state.job;'), 'f' * 32)
        self.driver.execute_script('state.job=null; setBusy(false);')


if __name__ == '__main__':
    unittest.main()
