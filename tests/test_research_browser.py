"""Research product journeys in Chrome against temporary offline live fakes.

Screenshots under runs/core-20261020/research-browser are UI fixture evidence,
not real model quality, literature coverage or publication-verification evidence.
All provider settings, jobs and conversations are created in a temporary folder.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import base64
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
    from selenium.webdriver.support.ui import Select, WebDriverWait
except ImportError:
    webdriver = None

from activity import ActivityFeed
from agent_tools import ToolRegistry
from common import Budget, Config, source_names
from providers import ProviderStore
from retrieval import RetrievalRouter
from webapp import ChatService, Jobs, create_server
from scripts.verify_delivery import apply_browser_isolation
from workflow import run as run_workflow
from tests.test_research_runtime import OfflineLLM, OfflineSource, TASK, paper


INJECTION = '<img src=x onerror="window.__researchInjected=1">'
PREFERENCES = {'venue_scope': 'preferred_only', 'publication_type': 'conference',
               'accepted_only': True, 'presentation_types': ['oral', 'poster']}


def fixture_papers():
    items = []
    for index, presentation in enumerate(('oral', 'poster', 'oral'), 1):
        record = {'type': 'conference', 'status': 'accepted', 'venue_name': 'ICLR',
            'venue_id': 'ICLR.cc/2025/Conference', 'date': '2025',
            'presentation': presentation, 'verified': True, 'identity_verified': True,
            'evidence': [{'url': f'https://openreview.net/forum?id=OFFLINE_FIXTURE_{index}',
                          'source': 'openreview', 'field': 'decision'}]}
        item = paper(f'OFFLINE-P{index}', year=2025, venue='ICLR',
                     publication={**record, 'records': [deepcopy(record)]}, sources=['openreview'])
        item.title = f'[OFFLINE FIXTURE] Visual retrieval method {index}'
        items.append(item)
    return items


class BrowserLLM(OfflineLLM):
    def generate(self, stage, prompt, data, schema, validate):
        if stage == 'update':
            # A deterministic local updater; it is deliberately not evidence of
            # natural-language interpretation quality.
            self.budget.check_cancelled()
            guard = getattr(self.budget, 'request_guard', None)
            if guard:
                guard()
            self.budget.llm_calls += 1
            self.calls.append({'stage': stage, 'data': deepcopy(data)})
            patch = ({'purpose': data['message']} if data.get('task') else
                     {key: value for key, value in TASK.items() if key != 'query'})
            value = {'patch': patch, 'summary': 'Offline fixture task update',
                     'unresolved': [], 'unsupported': [], 'assumptions': []}
            validate(value)
            return value
        value = super().generate(stage, prompt, data, schema, validate)
        if stage == 'synthesis':
            value['research_hypotheses'][0]['hypothesis'] = f'Proposed {INJECTION} may improve query robustness.'
            validate(value)
        return value


class OfflineChatService(ChatService):
    """Keep production service persistence and HTTP paths with injected fakes."""
    def _bind(self, record):
        chat = record['chat']
        source = OfflineSource([fixture_papers()], providers=source_names(record['source']))
        llm = BrowserLLM()
        source.budget = llm.budget = chat.budget
        chat.source, chat.llm = source, llm
        chat.tools = ToolRegistry.from_source(source)
        chat.router = RetrievalRouter(chat.config, source, tools=chat.tools)

    def _new_chat(self, data):
        config = self.providers.config(Config(agent_runtime='legacy'))
        config = replace(config, source=data.get('source') or 'multi', research_mode=True)
        return config, OfflineSource([fixture_papers()], providers=source_names(config.source)), BrowserLLM()

    def _refresh_live_provider(self, record):
        config = self.providers.config(record['chat'].config)
        record['chat'].rebind_provider(config, record['chat'].llm)

    def import_run(self, result, run_id):
        session_id = super().import_run(result, run_id)
        self._bind(self.sessions[session_id])
        return session_id

    def _load_record(self, session_id):
        record = super()._load_record(session_id)
        if record:
            self._bind(record)
        return record


class ResearchBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get('SG_REQUIRE_BROWSER') == '1'
        if webdriver is None:
            if required:
                raise RuntimeError('Research browser journeys require requirements-dev.txt/Selenium.')
            raise unittest.SkipTest('Research browser journeys require Selenium.')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.providers = ProviderStore(cls.root / 'providers.json')
        cls.providers.upsert({'id': 'offline-research-browser',
            'name': 'OFFLINE BROWSER FIXTURE — NO API',
            'base_url': 'http://127.0.0.1:1', 'model': 'offline-fixture'}, activate=True)
        cls.service = OfflineChatService(cls.root / 'web', providers=cls.providers)
        cls.submissions = []

        def offline_runner(data, progress):
            if data['mode'] != 'live':
                raise AssertionError('This journey must exercise the research live branch with offline fakes.')
            cls.submissions.append({key: deepcopy(value) for key, value in data.items() if not key.startswith('_')})
            config = cls.providers.config(Config(agent_runtime='legacy', research_mode=True))
            config = replace(config, source=data['source'], cache=False)
            budget = Budget(60, cancel_token=data['_cancel_token'])
            source = OfflineSource([fixture_papers()], providers=source_names(data['source']))
            llm = BrowserLLM()
            source.budget = llm.budget = budget
            return run_workflow(data['request'], config, method='S',
                request_data={**TASK, 'purpose': 'OFFLINE BROWSER FIXTURE — no external inference'},
                request_constraints=data.get('request_constraints'),
                research_preferences=data.get('research_preferences'),
                goal_hint=data.get('reading_goal'), source=source, llm=llm,
                budget=budget, progress=progress)

        cls.jobs = Jobs(cls.root / 'runs', runner=offline_runner,
                        importer=cls.service.import_run, providers=cls.providers)
        cls.server = create_server(0, jobs=cls.jobs, chat=cls.service,
            activity=ActivityFeed(cls.root / 'runs'), providers=cls.providers)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        options = Options()
        apply_browser_isolation(options, cls.root / 'chrome-profile')
        for flag in ('--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars', '--window-size=1440,1100'):
            options.add_argument(flag)
        try:
            cls.driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)
            cls.service.close(); cls.tmp.cleanup()
            if required:
                raise RuntimeError(f'Required research WebDriver unavailable: {exc}') from exc
            raise unittest.SkipTest(f'Research WebDriver unavailable: {exc}')
        default_shots = ROOT / 'runs' / 'core-20261020' / 'research-browser' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') / 'screenshots'
        cls.screenshots = Path(os.environ.get('SG_RESEARCH_SCREENSHOTS_DIR') or default_shots)
        cls.screenshots.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)
        cls.service.close(); cls.tmp.cleanup()

    def setUp(self):
        self.driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
        self.driver.set_window_size(1440, 1100)
        self.driver.get(self.base + '/settings')
        self.driver.execute_script('localStorage.clear(); sessionStorage.clear();')
        self.driver.get(self.base + '/search')
        self.wait = WebDriverWait(self.driver, 20)
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config) && !state.busy && !state.polling'))

    def js(self, script, *args):
        return self.driver.execute_script(script, *args)

    def click(self, selector):
        self.js('arguments[0].click();', self.driver.find_element(By.CSS_SELECTOR, selector))

    def fill(self, identity, text):
        node = self.driver.find_element(By.ID, identity)
        node.clear()
        if text:
            node.send_keys(text)

    def select(self, identity, value):
        field = self.driver.find_element(By.ID, identity)
        for details in field.find_elements(By.XPATH, 'ancestor::details[not(@open)]'):
            details.find_element(By.TAG_NAME, 'summary').click()
        Select(self.driver.find_element(By.ID, identity)).select_by_value(value)

    def fetch(self, path):
        return self.driver.execute_async_script('''
          const done=arguments[arguments.length-1];
          fetch(arguments[0]).then(async response=>done({status:response.status,
              type:response.headers.get('content-type'), text:await response.text()}))
            .catch(error=>done({error:String(error)}));
        ''', path)

    def configure(self):
        self.fill('topic', '推荐3篇 visual retrieval — OFFLINE BROWSER FIXTURE 论文')
        self.click('#research-settings summary')
        self.select('venue-scope', 'preferred_only')
        self.select('publication-type', 'conference')
        self.click('#accepted-only')
        self.click('#presentation-oral')
        self.click('#presentation-poster')

    def submit(self):
        self.click('#submit-button')
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#result-section > .research-brief'))
        self.wait.until(lambda d: not d.execute_script('return state.busy || state.polling'))
        return self.js('return state.job')

    def to_chat(self):
        self.click('#continue-chat')
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .research-brief'))
        self.wait.until(lambda d: not d.execute_script('return state.busy'))
        return self.js('return state.sessionId')

    def assert_research_rendered(self, selector):
        brief = self.driver.find_element(By.CSS_SELECTOR, selector)
        self.assertIn('仅已选论文的摘要', brief.text)
        self.assertIn('尚未验证', brief.text)
        self.assertIn('尚未执行', brief.text)
        self.assertIn('不证明假设有效或新颖', brief.text)
        self.assertEqual(len(brief.find_elements(By.CSS_SELECTOR, '.research-hypothesis')), 3)
        self.assertIn(INJECTION, brief.find_element(By.CSS_SELECTOR, '.research-hypothesis h4').text)
        self.assertEqual(brief.find_elements(By.CSS_SELECTOR, 'img,script,svg,iframe'), [])
        self.assertIsNone(self.js('return window.__researchInjected'))
        for link in brief.find_elements(By.CSS_SELECTOR, 'a[target="_blank"]'):
            self.assertIn('noopener', link.get_attribute('rel'))

    def assert_publication(self, selector):
        rows = self.driver.find_elements(By.CSS_SELECTOR, selector + ' .publication-info')
        self.assertEqual(len(rows), 3)
        self.assertTrue(any('oral' in item.text for item in rows))
        self.assertTrue(any('poster' in item.text for item in rows))
        for item in rows:
            self.assertIn('已录用', item.text)
            link = item.find_element(By.CSS_SELECTOR, 'a')
            self.assertTrue(link.get_attribute('href').startswith('https://openreview.net/forum?id=OFFLINE_FIXTURE_'))
            self.assertIn('noopener', link.get_attribute('rel'))

    def test_preferences_use_real_post_and_survive_draft_reload(self):
        self.assertEqual(self.js("return document.querySelector('#source').value"), 'multi')
        self.configure()
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return Boolean(state.config) && !state.busy'))
        self.assertEqual(self.js("return document.querySelector('#venue-scope').value"), 'preferred_only')
        self.assertEqual(self.js("return document.querySelector('#publication-type').value"), 'conference')
        self.assertTrue(self.js("return document.querySelector('#accepted-only').checked"))
        self.assertTrue(self.js("return document.querySelector('#presentation-oral').checked"))
        self.assertTrue(self.js("return document.querySelector('#presentation-poster').checked"))
        self.assertIn('OFFLINE BROWSER FIXTURE', self.js("return document.querySelector('#topic').value"))
        self.submit()
        payload = self.submissions[-1]
        self.assertEqual(payload['research_preferences'], PREFERENCES)
        self.assertTrue(payload['research_mode'])
        self.assertEqual(payload['source'], 'multi')
        self.assertEqual(payload['request_constraints']['target_count'], 3)
        self.assertIn('oral或poster', payload['request'])

    def test_natural_copy_is_visible_and_does_not_stack_hidden_research_preferences(self):
        self.configure()
        self.click('#toggle-input')
        copied = self.js("return document.querySelector('#request').value")
        for text in ('仅考虑', '会议论文', '可核验', 'oral或poster'):
            self.assertIn(text, copied)
        self.assertFalse(self.driver.find_element(By.ID, 'research-settings').is_displayed())
        changed = 'OFFLINE FIXTURE: recommend three visual retrieval papers without venue or acceptance limits.'
        self.fill('request', changed)
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script('return state.natural && Boolean(state.config)'))
        self.assertEqual(self.js("return document.querySelector('#request').value"), changed)
        self.submit()
        payload = self.submissions[-1]
        self.assertEqual(payload['request'], changed)
        self.assertNotIn('research_preferences', payload)
        self.assertNotIn('request_constraints', payload)
        self.assertEqual(self.js("return state.lastResult.request.accepted_only"), False)

    def test_search_serves_real_renderer_and_exports_grounded_brief(self):
        response = self.fetch('/research.js')
        self.assertEqual(response['status'], 200)
        self.assertIn('javascript', response['type'])
        self.assertIn('ScholarGuideResearch', response['text'])
        self.assertTrue(self.js("return performance.getEntriesByType('resource').some(e=>new URL(e.name).pathname==='/research.js')"))
        self.configure()
        self.submit()
        self.assert_research_rendered('#result-section > .research-brief')
        self.assert_publication('#paper-list')
        data = self.fetch(self.driver.find_element(By.ID, 'download-json').get_attribute('href'))
        markdown = self.fetch(self.driver.find_element(By.ID, 'download-md').get_attribute('href'))
        self.assertEqual(data['status'], 200)
        self.assertEqual(markdown['status'], 200)
        brief = json.loads(data['text'])['research_brief']
        self.assertEqual(len(brief['paper_notes']), 3)
        self.assertEqual(brief['source_level'], 'abstract_only')
        self.assertEqual(brief['research_hypotheses'][0]['kind'], 'hypothesis_not_tested')
        self.assertIn('query robustness', markdown['text'])
        self.assertIn('&lt;img', markdown['text'])
        self.assertNotIn(INJECTION, markdown['text'])
        self.assertIn('待验证的研究假设', markdown['text'])
        self.assertIn('拟议实验，不是论文已经完成的实验', markdown['text'])

    def test_chat_refresh_and_followup_preserve_first_turn_brief_and_exports(self):
        self.configure()
        self.submit()
        session_id = self.to_chat()
        self.assert_research_rendered('#turns .turn:first-child .research-brief')
        self.assert_publication('#turns .turn:first-child')
        prefix = f'/api/chat/sessions/{session_id}/turns/1/'
        first_json, first_md = self.fetch(prefix + 'result.json'), self.fetch(prefix + 'report.md')
        self.assertEqual(first_json['status'], 200)
        self.assertEqual(first_md['status'], 200)
        self.assertIn('query robustness', first_md['text'])
        first = json.loads(first_json['text'])
        text = self.driver.find_element(By.CSS_SELECTOR, '#turns .turn:first-child .research-brief').text
        self.driver.refresh()
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#turns .research-brief') and not d.execute_script('return state.busy'))
        self.assertEqual(self.js('return state.sessionId'), session_id)
        self.js("setDraft('OFFLINE FIXTURE follow-up: compare implementation effort using the same papers.');")
        self.click('#send')
        self.wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .turn')) == 2 and
                        not d.execute_script('return state.busy'))
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, '#turns .research-brief')), 2)
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '#turns .turn:first-child .research-brief').text, text)
        self.assertEqual(self.fetch(prefix + 'result.json')['text'], first_json['text'])
        self.assertEqual(self.fetch(prefix + 'report.md')['text'], first_md['text'])
        second = json.loads(self.fetch(f'/api/chat/sessions/{session_id}/turns/2/result.json')['text'])
        self.assertNotEqual(second['research_brief']['input_key'], first['research_brief']['input_key'])
        self.assertIn('OFFLINE FIXTURE follow-up', second['request']['purpose'])
        self.driver.refresh()
        self.wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '#turns .research-brief')) == 2)
        self.assertEqual(len(self.service.snapshot(session_id)['history']), 2)

    def capture(self, name, width):
        self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
            {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
        self.assertEqual(self.js('return window.innerWidth'), width)
        self.js('''
          if(!document.querySelector('#offline-fixture-banner')){
            const banner=document.createElement('div'); banner.id='offline-fixture-banner';
            banner.textContent='OFFLINE BROWSER FIXTURE · Scripted local inputs · Not model/research quality evidence';
            Object.assign(banner.style,{position:'fixed',bottom:'0',left:'0',right:'0',zIndex:'9999',
              padding:'8px 12px',background:'#fff1bd',color:'#473300',fontSize:'12px',textAlign:'center'});
            document.body.append(banner);
          }
          window.scrollTo(0,0);
        ''')
        self.assertLessEqual(self.js('return document.documentElement.scrollWidth'), width + 1)
        for selector in ('.research-brief', '.publication-info', '.research-hypothesis'):
            bounds = self.js('return [...document.querySelectorAll(arguments[0])].map(e=>{const r=e.getBoundingClientRect();return [r.left,r.right]})', selector)
            self.assertTrue(bounds, selector)
            for left, right in bounds:
                self.assertGreaterEqual(left, -1, selector)
                self.assertLessEqual(right, width + 1, selector)
        metrics = self.driver.execute_cdp_cmd('Page.getLayoutMetrics', {})
        height = metrics['cssContentSize']['height']
        image = self.driver.execute_cdp_cmd('Page.captureScreenshot',
            {'format': 'png', 'captureBeyondViewport': True,
             'clip': {'x': 0, 'y': 0, 'width': width, 'height': height, 'scale': 1}})
        png = base64.b64decode(image['data'])
        self.assertEqual(int.from_bytes(png[16:20], 'big'), width)
        (self.screenshots / f'{name}-{width}.png').write_bytes(png)
        self.js('''
          document.documentElement.style.scrollBehavior='auto';
          const top=window.scrollY+document.querySelector('.research-brief').getBoundingClientRect().top-90;
          window.scrollTo({top, behavior:'instant'});
        ''')
        self.wait.until(lambda d: d.execute_script("return document.querySelector('.research-brief').getBoundingClientRect().top < 100"))
        focused = self.driver.execute_cdp_cmd('Page.captureScreenshot',
            {'format': 'png', 'captureBeyondViewport': False})
        (self.screenshots / f'{name}-research-{width}.png').write_bytes(base64.b64decode(focused['data']))

    def test_research_results_fit_390_and_1440_with_fixture_screenshots(self):
        self.configure()
        self.submit()
        self.capture('search', 1440)
        self.capture('search', 390)
        self.to_chat()
        self.capture('chat', 1440)
        self.capture('chat', 390)


if __name__ == '__main__':
    unittest.main()
