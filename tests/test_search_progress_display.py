"""Search progress is driven by observed events, not a fake timer.

Uses the existing isolated local-service fixture and synthetic worker only.
"""
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import test_experience_upgrade as harness
from demo import demo_run
from selenium.webdriver.common.by import By


class SearchProgressDisplayTests(unittest.TestCase):
    setUpClass = classmethod(harness.ExperienceUpgradeTests.setUpClass.__func__)
    tearDownClass = classmethod(harness.ExperienceUpgradeTests.tearDownClass.__func__)
    setUp = harness.ExperienceUpgradeTests.setUp
    tearDown = harness.ExperienceUpgradeTests.tearDown
    set_theme = harness.ExperienceUpgradeTests.set_theme
    click = harness.ExperienceUpgradeTests.click

    def active(self):
        return self.driver.execute_script("return document.querySelector('#stagebar .stage.active')?.dataset.stage || null")

    def stage(self, expected):
        self.wait.until(lambda d: self.active() == expected)
        caption = self.driver.find_element(By.ID, 'stage-caption').text
        self.assertIn('最近可识别阶段', caption)
        self.assertIn('不代表完成比例', caption)

    def test_real_event_sequence_can_revisit_search_and_does_not_guess(self):
        gates = [threading.Event() for _ in range(4)]
        arrived = threading.Event()

        def runner(data, progress):
            progress(dict(action='tool_start', category='model', tool='model.research_plan'))
            arrived.set()
            for gate, event in zip(gates, [
                dict(action='search', query='synthetic query'),
                dict(action='assess', assessed=2, eligible=1),
                dict(action='refine', query='synthetic follow-up'),
                dict(action='synthesis', status='preparing'),
            ]):
                if not gate.wait(30):
                    raise RuntimeError('synthetic test gate timed out')
                progress(event)
            return demo_run('satisfied', 'A')

        with patch.object(self.jobs, 'runner', runner):
            try:
                self.driver.get(self.base + '/search?demo=satisfied')
                self.assertTrue(arrived.wait(8))
                self.stage('plan')
                self.assertEqual(self.driver.execute_script("return [...document.querySelectorAll('#stagebar .seen')].length"), 0)
                # Inspect the same synthetic running state at desktop and mobile sizes.
                for width in (1440, 390, 320):
                    self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                        'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
                    for theme in ('light', 'dark'):
                        self.set_theme(theme)
                        overflow = self.driver.execute_script('return document.documentElement.scrollWidth-document.documentElement.clientWidth')
                        self.assertLessEqual(overflow, 1, (width, theme))
                        output = os.environ.get('SG_PROGRESS_SHOTS')
                        if output and width in (1440, 390):
                            Path(output).mkdir(parents=True, exist_ok=True)
                            self.driver.execute_script("document.querySelector('.results-card').scrollIntoView({block:'start'})")
                            self.driver.find_element(By.CSS_SELECTOR, '.results-card').screenshot(str(Path(output) / f'search-progress-{width}-{theme}.png'))
                            self.driver.execute_script("document.documentElement.style.scrollBehavior='auto'; document.querySelector('#stagebar').scrollIntoView({block:'center',behavior:'instant'})")
                            self.driver.find_element(By.ID, 'stagebar').screenshot(str(Path(output) / f'search-stages-{width}-{theme}.png'))
                for index, expected in enumerate(('search', 'assess', 'search', 'done')):
                    gates[index].set()
                    if index == 3:
                        # The worker can finish before the browser observes the
                        # final event; synthesis mapping is also tested below.
                        break
                    self.stage(expected)
                self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#paper-list .paper'))
            finally:
                for gate in gates:
                    gate.set()

    def test_trace_merge_and_terminal_status_do_not_invent_phases(self):
        self.driver.get(self.base + '/search')
        result = self.driver.execute_script('''
          const all=mergeSearchTrace(
            [{seq:1,action:'plan'}, {seq:2,action:'search'}, {seq:3,action:'assess'}],
            [{seq:1,action:'plan'}, {seq:2,action:'search'}]);
          renderTrace([...all, {seq:4,action:'error'}]);
          const active=document.querySelector('#stagebar .stage.active')?.dataset.stage;
          const seen=[...document.querySelectorAll('#stagebar .seen')].map(n=>n.dataset.stage);
          renderTrace([{seq:1,action:'synthesis',status:'preparing'}]);
          const synthesis=document.querySelector('#stagebar .stage.active')?.dataset.stage;
          const invented=[...document.querySelectorAll('#stagebar .seen')].length;
          renderTrace([]);
          return {count:all.length, active, seen, synthesis, invented,
                  cleared:!document.querySelector('#stagebar .stage.active')};
        ''')
        self.assertEqual(result, {'count': 3, 'active': 'assess', 'seen': ['plan', 'search'],
                                  'synthesis': 'done', 'invented': 0, 'cleared': True})

    def test_resume_resubscribes_and_legacy_trace_uses_snapshots(self):
        self.driver.get(self.base + '/search')
        result = self.driver.execute_async_script('''
          const done=arguments[0], savedApi=api, savedSubscribe=window.ScholarGuideTrace.subscribe;
          const savedTimeout=window.setTimeout;
          let calls=0, closed=0, response;
          window.setTimeout=()=>0;
          window.ScholarGuideTrace.subscribe=()=>{calls++;return {close(){closed++}}};
          api=async()=>response;
          (async()=>{
            state.job='a'.repeat(32); state.busy=true; state.streamJob=null; state.stream=null;
            response={state:'running',trace:[{seq:1,action:'plan'}]}; await poll();
            response={state:'needs_retry',trace:[{seq:1,action:'plan'}],recovery:{}}; await poll();
            state.busy=true; response={state:'running',trace:[{seq:1,action:'plan'}]}; await poll();
            const resumed=calls;
            state.job='b'.repeat(32); response={state:'running',trace:[{action:'plan'}]}; await poll();
            const legacyAdded=calls-resumed;
            done({resumed,legacyAdded,closed});
          })().catch(e=>done({error:String(e)})).finally(()=>{
            api=savedApi;window.setTimeout=savedTimeout;window.ScholarGuideTrace.subscribe=savedSubscribe;
            state.stream?.close();state.stream=null;state.busy=false;
          });
        ''')
        self.assertEqual(result.get('resumed'), 2, result)
        self.assertEqual(result.get('legacyAdded'), 0, result)
        self.assertGreaterEqual(result.get('closed', 0), 2, result)

    def test_failed_new_task_restores_previous_trace_and_activity_link(self):
        self.driver.get(self.base + '/search')
        result = self.driver.execute_script('''
          const prior=arguments[0], a='a'.repeat(32), b='b'.repeat(32);
          state.job=b; state.previousResult={job:a,result:prior,started:1,
            trace:[{seq:1,action:'search',query:'prior trace'}]};
          renderTrace([{seq:1,action:'search',query:'different task'}]);
          restorePreviousResult();
          return {job:state.job,trace:document.querySelector('#trace-list').textContent,
                  link:document.querySelector('#open-activity-link').getAttribute('href')};
        ''', demo_run('satisfied', 'A'))
        self.assertEqual(result['job'], 'a' * 32)
        self.assertIn('prior trace', result['trace'])
        self.assertNotIn('different task', result['trace'])
        self.assertEqual(result['link'], '/activity?run=' + 'a' * 32)
