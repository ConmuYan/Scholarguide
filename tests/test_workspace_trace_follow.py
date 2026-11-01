"""Regression contracts for the workspace trace panel and the Activity view.

Two defects this file pins down:

1. `/search` put the "open the full trace in Activity" link *below* a sticky
   panel. Once a live run produced a long trace, the panel grew taller than the
   viewport, slid over the link, and the link became unreachable.
2. Opening Activity mid-run showed a frozen snapshot: the page fetched the
   record once and never followed it, so a running search or chat turn looked
   stuck even though the workspace was still working.

Both are checked in a real browser against an isolated service.
"""
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import test_experience_upgrade as _harness
try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    By = None


LONG_TRACE = """
  const list = document.getElementById('trace-list');
  list.replaceChildren();
  for (let i = 1; i <= 26; i += 1) {
    const li = document.createElement('li');
    li.className = i === 26 ? 'running' : 'done';
    const dot = document.createElement('span'); dot.className = 'dot';
    const body = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = '步骤 ' + i + ' · 检索与核对';
    const note = document.createElement('small');
    note.textContent = '候选 42 · 新增 17 · 摘要核对 6 篇，判定可推荐 4 篇';
    body.append(title, note);
    li.append(dot, body);
    list.append(li);
  }
  document.getElementById('trace-count').textContent = '27 步';
"""


class WorkspaceTracePanelTests(unittest.TestCase):
    setUpClass = classmethod(_harness.ExperienceUpgradeTests.setUpClass.__func__)
    tearDownClass = classmethod(_harness.ExperienceUpgradeTests.tearDownClass.__func__)
    setUp = _harness.ExperienceUpgradeTests.setUp
    tearDown = _harness.ExperienceUpgradeTests.tearDown
    click = _harness.ExperienceUpgradeTests.click

    def test_activity_link_stays_clickable_with_a_long_trace(self):
        for width in (1440, 390):
            with self.subTest(width=width):
                self.driver.set_window_size(width, 900)
                self.driver.get(self.base + '/search?demo=followup&case=activity-link')
                WebDriverWait(self.driver, 15).until(lambda d: d.execute_script(
                    'return typeof state !== "undefined" && !state.configLoading && document.querySelectorAll("#trace-list li").length > 0;'))
                self.driver.execute_script(LONG_TRACE)
                # Smooth scrolling is the default on these pages; re-assert the
                # target position while layout settles, then wait until the link
                # really is inside the viewport before hit-testing.
                def link_in_view(driver):
                    return driver.execute_script("""
                      const link = document.getElementById('open-activity-link');
                      link.scrollIntoView({block: 'center', behavior: 'instant'});
                      const r = link.getBoundingClientRect();
                      return r.top >= 0 && r.bottom <= innerHeight;
                    """)
                WebDriverWait(self.driver, 20).until(link_in_view)
                state = self.driver.execute_script("""
                  const link = document.getElementById('open-activity-link');
                  const rect = link.getBoundingClientRect();
                  const hit = document.elementFromPoint(
                    Math.round(rect.left + rect.width / 2),
                    Math.round(rect.top + rect.height / 2));
                  const card = document.querySelector('.trace-card');
                  return {inside: rect.bottom <= card.getBoundingClientRect().bottom + 0.5,
                          hit: hit ? (hit.id || hit.tagName) : 'none',
                          cardHeight: Math.round(card.getBoundingClientRect().height),
                          viewport: innerHeight};
                """)
                self.assertTrue(state['inside'], state)
                self.assertIn(state['hit'], ('open-activity-link', 'A'), state)
                # The service allows one job at a time. Let this demo settle
                # before the next width/test starts another one.
                WebDriverWait(self.driver, 30).until(
                    lambda d: d.execute_script('return !state.busy'))

    def test_the_trace_list_scrolls_instead_of_growing_the_panel(self):
        self.driver.set_window_size(1440, 900)
        self.driver.get(self.base + '/search?demo=followup&case=trace-scroll')
        WebDriverWait(self.driver, 15).until(lambda d: d.execute_script(
            'return typeof state !== "undefined" && !state.configLoading && document.querySelectorAll("#trace-list li").length > 0;'))
        self.driver.execute_script(LONG_TRACE)
        WebDriverWait(self.driver, 10).until(lambda d: d.execute_script(
            'return document.querySelectorAll("#trace-list li").length >= 26;'))
        state = self.driver.execute_script("""
          const card = document.querySelector('.trace-card');
          const list = document.getElementById('trace-list');
          return {cardHeight: Math.round(card.getBoundingClientRect().height),
                  viewport: innerHeight,
                  listScrolls: list.scrollHeight > list.clientHeight + 1,
                  overflow: getComputedStyle(list).overflowY};
        """)
        self.assertLessEqual(state['cardHeight'], state['viewport'], state)
        self.assertTrue(state['listScrolls'], state)
        self.assertIn(state['overflow'], ('auto', 'scroll'), state)


class ActivityFollowTests(unittest.TestCase):
    setUpClass = classmethod(_harness.ExperienceUpgradeTests.setUpClass.__func__)
    tearDownClass = classmethod(_harness.ExperienceUpgradeTests.tearDownClass.__func__)
    setUp = _harness.ExperienceUpgradeTests.setUp
    tearDown = _harness.ExperienceUpgradeTests.tearDown

    JOB = 'e' * 32

    def _install_stub(self):
        self.driver.get(self.base + '/activity')
        WebDriverWait(self.driver, 20).until(
            lambda d: d.execute_script('return typeof state === "object" && !!state.index'))
        self.driver.execute_script("""
          const jobId = arguments[0];
          window.__stub = {reads: 0, done: false, steps: 3};
          const real = window.fetch.bind(window);
          const snapshot = () => {
            window.__stub.reads += 1;
            if (!window.__stub.done) window.__stub.steps = Math.min(window.__stub.steps + 2, 24);
            const trace = Array.from({length: window.__stub.steps}, (_, i) => ({
              step: i + 1,
              action: i % 3 === 0 ? 'search' : (i % 3 === 1 ? 'assess' : 'check'),
              query: 'retrieval augmented generation ' + (i + 1),
            }));
            return {id: jobId, state: window.__stub.done ? 'done' : 'running',
                    created_at: new Date().toISOString(), mode: 'demo', trace: trace,
                    result: null, error: null, cancel_requested: false,
                    cancellation_state: '',
                    request: {topic: 'retrieval augmented generation evaluation'}};
          };
          window.fetch = (url, options) => {
            const path = String(url);
            if (path.includes('/api/activity/runs/' + jobId)) {
              if (!window.__stub.done) {
                return Promise.resolve(new Response(
                  JSON.stringify({error: '任务还没有可下载的结果。'}),
                  {status: 404, headers: {'Content-Type': 'application/json'}}));
              }
              return Promise.resolve(new Response(JSON.stringify({
                kind: 'run', id: jobId,
                result: {status: 'complete', mode: 'demo', method: 'A',
                         request: {topic: 'stub run'}, papers: [],
                         metrics: {elapsed_seconds: 1.2},
                         trace: Array.from({length: window.__stub.steps},
                                           (_, i) => ({step: i + 1, action: 'check'}))},
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (path.includes('/api/jobs/' + jobId)) {
              return Promise.resolve(new Response(JSON.stringify(snapshot()),
                {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return real(url, options);
          };
        """, self.JOB)

    def test_a_running_job_is_followed_and_then_settles(self):
        self._install_stub()
        self.driver.execute_script("select({id: arguments[0], kind: 'run'});", self.JOB)
        WebDriverWait(self.driver, 10).until(
            lambda d: not d.find_element(By.ID, 'activity-detail').get_attribute('hidden'))
        arrival = self.driver.execute_script("""
          return {pill: !document.getElementById('activity-follow').hidden,
                  text: document.getElementById('activity-follow-text').textContent,
                  events: document.querySelectorAll('#activity-timeline > li').length,
                  status: document.getElementById('activity-status').textContent};
        """)
        self.assertTrue(arrival['pill'], arrival)
        self.assertIn('跟踪', arrival['text'])
        self.assertEqual(arrival['status'], '运行中')
        self.assertGreater(arrival['events'], 0)

        # The poll must pick up new events without the reader reloading.
        WebDriverWait(self.driver, 15).until(
            lambda d: d.execute_script(
                "return document.querySelectorAll('#activity-timeline > li').length")
            > arrival['events'])
        growing = self.driver.execute_script("""
          return {events: document.querySelectorAll('#activity-timeline > li').length,
                  ticks: state.follow ? state.follow.ticks : 0};
        """)
        self.assertGreaterEqual(growing['ticks'], 1, growing)

        # Finish the job: the page must fetch the final record and stop following.
        self.driver.execute_script("window.__stub.done = true;")
        WebDriverWait(self.driver, 20).until(
            lambda d: d.execute_script(
                "return !state.follow && document.getElementById('activity-status').textContent"
                " === '完成'"))
        self.assertTrue(self.driver.execute_script(
            "return document.getElementById('activity-follow').hidden"))

    def test_switching_records_stops_the_follow_loop(self):
        self._install_stub()
        self.driver.execute_script("select({id: arguments[0], kind: 'run'});", self.JOB)
        WebDriverWait(self.driver, 10).until(
            lambda d: d.execute_script('return Boolean(state.follow)'))
        self.driver.execute_script("document.getElementById('tab-sessions').click();")
        self.assertFalse(self.driver.execute_script('return Boolean(state.follow)'))
        self.assertTrue(self.driver.execute_script(
            "return document.getElementById('activity-follow').hidden"))

    def test_hiding_a_followed_record_stops_updates_and_clears_detail(self):
        self._install_stub()
        self.driver.execute_script("""
          state.kind = 'runs';
          state.index = {sessions: [], runs: [{id: arguments[0], kind:'run',
            title:'running fixture', mode:'demo'}]};
          select({id: arguments[0], kind:'run'});
        """, self.JOB)
        WebDriverWait(self.driver, 10).until(lambda d: d.execute_script('return Boolean(state.follow)'))
        self.driver.execute_script("""
          const field = document.getElementById('activity-mode');
          field.value = 'live'; field.dispatchEvent(new Event('change', {bubbles:true}));
        """)
        self.assertFalse(self.driver.execute_script('return Boolean(state.follow)'))
        self.assertTrue(self.driver.find_element(By.ID, 'activity-placeholder').is_displayed())
        self.assertFalse(self.driver.find_element(By.ID, 'activity-detail').is_displayed())

    def test_the_action_filter_survives_a_live_refresh(self):
        self._install_stub()
        self.driver.execute_script("select({id: arguments[0], kind: 'run'});", self.JOB)
        WebDriverWait(self.driver, 10).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, '#activity-filters .act-filter'))
        chosen = self.driver.execute_script("""
          const chip = [...document.querySelectorAll('#activity-filters .act-filter')]
            .find(node => node.dataset.group === 'assess');
          if (!chip) return null;
          chip.click();
          return state.actionFilter;
        """)
        if chosen is None:
            self.skipTest('fixture produced no assess events to filter')
        WebDriverWait(self.driver, 15).until(
            lambda d: d.execute_script('return state.follow ? state.follow.ticks : 0') >= 1)
        self.assertEqual(self.driver.execute_script('return state.actionFilter'), chosen)


if __name__ == '__main__':
    unittest.main()
