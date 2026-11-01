"""Offline regressions for active-time recovery and hard runtime budgets."""
from pathlib import Path
import tempfile
import unittest

from common import BudgetExceeded, Config, GuideError
from controller import Decision
from demo import ChatDemoLLM, DemoSource
from session import ChatSession, MAX_LLM_REQUESTS, TURN_SECONDS
from state import StateStore


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class RuntimeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / 'state.sqlite3')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def chat(self, *, clock=None, llm=None, source=None, session_id=None, batch_size=2,
             task_timeout=600):
        chat = ChatSession(Config(agent_runtime='langgraph', batch_size=batch_size,
                                  task_timeout=task_timeout),
                           self.store, session_id=session_id,
                           clock=clock or Clock(), source=source or DemoSource('satisfied'),
                           llm=llm or ChatDemoLLM())
        if not chat.task():
            chat.store.save_task(chat.session_id, 1, {
                'topic': 'retrieval augmented generation', 'target_count': 5,
                'category': 'none', 'minimum_category_count': 0})
            chat._new_phase(first=True)
        return chat

    def test_reflection_cannot_bypass_any_hard_budget(self):
        for exhausted in ('actions_left', 'llm_left', 'searches_left',
                          'searches_left_phase', 'candidates_left'):
            with self.subTest(exhausted=exhausted):
                chat = self.chat()
                limits = chat.limits()
                limits[exhausted] = 0
                chat._save_limits(limits)
                def forbidden():
                    self.fail('refinement ran after a hard budget was exhausted')
                chat._refine_query = forbidden
                out = chat.agent_runtime.critic({})
                self.assertEqual(out['stage'], 'decide')
                self.assertFalse(self.store.query_history(chat.session_id))

    def test_reflection_rechecks_time_after_refinement(self):
        clock = Clock()
        chat = self.chat(clock=clock)
        def slow_refine():
            clock.now += TURN_SECONDS + 1
            return 'new query'
        chat._refine_query = slow_refine
        out = chat.agent_runtime.critic({})
        self.assertEqual(out['stage'], 'decide')
        self.assertFalse(self.store.query_history(chat.session_id))

    def test_executor_refuses_search_without_phase_or_session_allowance(self):
        for exhausted in ('searches_left', 'searches_left_phase', 'candidates_left'):
            with self.subTest(exhausted=exhausted):
                source = DemoSource('satisfied')
                chat = self.chat(source=source)
                limits = chat.limits()
                limits[exhausted] = 0
                chat._save_limits(limits)
                with self.assertRaises(BudgetExceeded):
                    chat.apply_decision(Decision('SEARCH', {'query': 'new'}, True))
                self.assertEqual(source.calls, 0)

    def test_search_counts_distinct_new_candidates_only(self):
        source = DemoSource('satisfied')
        paper = source.papers[0]
        source.search = lambda query, limit=10: [paper, paper, source.papers[1]]
        chat = self.chat(source=source)
        chat._do_search('first query')
        self.assertEqual(len(self.store.candidate_refs(chat.session_id)), 2)
        self.assertEqual(chat.limits()['candidates_left'], 38)
        source.search = lambda query, limit=10: [paper, source.papers[2], paper]
        chat._do_search('second query')
        self.assertEqual(len(self.store.candidate_refs(chat.session_id)), 3)
        self.assertEqual(chat.limits()['candidates_left'], 37)
        self.assertEqual([row['new_count'] for row in self.store.query_history(chat.session_id)], [2, 1])

    def test_executor_rejects_any_previous_normalized_query(self):
        chat = self.chat()
        chat._do_search('First   query')
        chat._do_search('Second query')
        chat._new_phase(first=False)
        before = chat.source.calls
        with self.assertRaisesRegex(GuideError, 'Repeated query'):
            chat._do_search('  FIRST query  ')
        self.assertEqual(chat.source.calls, before)

    def test_assessment_stops_before_next_batch_and_keeps_completed_batch(self):
        chat = self.chat()
        chat._do_search('initial')
        limits = chat.limits()
        limits['llm_left'] = 1
        chat._save_limits(limits)
        with self.assertRaises(BudgetExceeded):
            chat._do_assess([ref['paper_id'] for ref in self.store.candidate_refs(chat.session_id)])
        self.assertEqual(chat.limits()['llm_left'], 0)
        self.assertEqual(len(chat._assessments(chat.task())), 2)

    def test_final_decision_request_does_not_authorize_extra_assessment_requests(self):
        chat = self.chat()
        chat._do_search('initial')
        limits = chat.limits()
        limits['llm_left'] = 1
        chat._save_limits(limits)
        decision, _ = chat.next_decision()
        self.assertEqual(decision.action, 'ASSESS')
        self.assertEqual(chat.limits()['llm_left'], 0)
        with self.assertRaises(BudgetExceeded):
            chat.apply_decision(decision)
        self.assertEqual(len(chat._assessments(chat.task())), 0)

    def test_failed_requests_are_charged_without_resetting_phase(self):
        class FailingLLM:
            def generate(self, *args):
                chat.budget.llm_calls += 2  # malformed response plus one repair
                raise GuideError('invalid response')
        chat = self.chat(llm=FailingLLM())
        before = chat.limits()['llm_left']
        with self.assertRaisesRegex(GuideError, 'invalid response'):
            chat.update_task('update intent')
        self.assertEqual(chat.limits()['llm_left'], before - 2)

    def test_clarification_wait_excludes_idle_time_from_transport_deadline(self):
        clock = Clock()
        chat = self.chat(clock=clock)
        clock.now += 31
        chat._pause_clock()
        before = chat.limits()
        chat.budget.started -= chat.config.task_timeout + 10000
        clock.now += 10000
        chat._resume_clock()
        self.assertAlmostEqual(chat._seconds_left(), TURN_SECONDS - 31)
        self.assertGreater(chat.budget.remaining(), 88)
        self.assertLessEqual(chat.budget.remaining(), 89)
        self.assertEqual(chat.limits()['llm_left'], before['llm_left'])
        self.assertEqual(chat.limits()['actions_left'], before['actions_left'])

    def test_active_phase_restore_preserves_remaining_time_with_new_clock_origin(self):
        clock = Clock()
        chat = self.chat(clock=clock)
        clock.now += 37
        chat._consume(actions_left=2, llm_left=3)
        restored = self.chat(clock=Clock(5), session_id=chat.session_id)
        self.assertAlmostEqual(restored._seconds_left(), TURN_SECONDS - 37)
        self.assertEqual(restored.limits()['actions_left'], 4)
        self.assertEqual(restored.limits()['llm_left'], MAX_LLM_REQUESTS - 3)
        self.assertGreater(restored.budget.remaining(), 82)

    def test_feedback_gets_new_phase_request_allowance_but_answer_does_not(self):
        chat = self.chat()
        chat._consume(llm_left=20)
        chat._pause_clock()
        chat._resume_clock()
        self.assertEqual(chat.limits()['llm_left'], 4)
        chat._new_phase(first=False)
        self.assertEqual(chat.limits()['llm_left'], MAX_LLM_REQUESTS)
        self.assertIsNone(chat.capabilities()['feedback_phases_left'])
        self.assertTrue(chat.can_send())

    def test_short_configured_timeout_caps_the_whole_phase(self):
        clock = Clock()
        chat = self.chat(clock=clock, task_timeout=20)
        self.assertEqual(chat._seconds_left(), 20)
        clock.now += 12
        chat._sync_http_deadline()
        self.assertGreater(chat.budget.remaining(), 7)
        self.assertLessEqual(chat.budget.remaining(), 8)
        clock.now += 9
        with self.assertRaises(BudgetExceeded):
            chat.update_task('change intent')

    def test_tool_error_skips_reflection_and_routes_to_delivery(self):
        chat = self.chat()
        out = chat.agent_runtime.critic({'stop_reason': 'model_or_tool_error'})
        self.assertEqual(out['stage'], 'finalize')
        self.assertEqual(chat.agent_runtime._after_critic(out | {
            'stop_reason': 'model_or_tool_error'}), 'finalize')


if __name__ == '__main__':
    unittest.main()
