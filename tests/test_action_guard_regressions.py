"""Action authorization regressions using only local, deterministic doubles."""
from types import SimpleNamespace
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from common import Config, GuideError
from controller import (BudgetView, ControlState, Decision, PolicyError, allowed_actions,
                        fallback_action, validate_decision, validate_issued_decision)
from demo import ChatDemoLLM, DemoSource
from session import ChatSession
from state import StateStore


GAP = {'requirement': 'paper_count', 'missing': 1}


def control(**changes):
    fields = {
        'gaps': (GAP,),
        'budget': BudgetView(seconds_left=60, llm_left=5, searches_left=1),
    }
    fields.update(changes)
    return ControlState(**fields)


class FallbackDouble:
    def __init__(self, state, *, query='', stale_ids=()):
        self.state = state
        self.query = query
        self.stale_ids = list(stale_ids)

    def _refine_query(self):
        return self.query

    def _plan(self):
        return SimpleNamespace(stale_ids=self.stale_ids)

    def control_state(self):
        return self.state


class ExecutorDouble:
    _apply_decision_native_guarded = ChatSession._apply_decision_native_guarded

    def __init__(self, state):
        self.state = state
        self.budget = SimpleNamespace(check_cancelled=lambda: None)
        self.executed = []

    def control_state(self):
        return self.state

    def _native_operation_open(self):
        return False

    def limits(self):
        return {'searches_left': 1, 'searches_left_phase': 1,
                'candidates_left': 10, 'llm_left': 5}

    def _seconds_left(self):
        return 60

    def _finish(self):
        self.executed.append('FINISH')

    def _do_search(self, *args, **kwargs):
        self.executed.append('SEARCH')

    def _do_assess(self, *args, **kwargs):
        self.executed.append('ASSESS')

    def _ask(self, *args):
        self.executed.append('ASK')

    def _result(self, *args, **kwargs):
        return {}

    def select(self):
        return []


class AskFieldGuardTests(unittest.TestCase):
    def test_ask_must_target_a_current_blocking_unresolved_field(self):
        state = control(unresolved=({'field': 'purpose', 'blocking': True},
                                    {'field': 'topic', 'blocking': False}))
        for field in ('topic', 'category', 'minimum_category_count'):
            with self.subTest(field=field):
                decision = validate_decision({'action': 'ASK', 'question': 'Which?',
                                              'affected_field': field}, state)
                self.assertFalse(decision.accepted)
        accepted = validate_decision({'action': 'ASK', 'question': 'Which purpose?',
                                      'affected_field': 'purpose'}, state)
        self.assertTrue(accepted.accepted)

    def test_only_boolean_true_can_mark_an_ambiguity_as_blocking(self):
        for value in ('false', 'true', 1, None):
            with self.subTest(value=value):
                state = control(unresolved=({'field': 'purpose', 'blocking': value},))
                self.assertNotIn('ASK', allowed_actions(state))

    def test_multiple_current_blocking_fields_are_each_eligible(self):
        state = control(unresolved=({'field': 'purpose', 'blocking': True},
                                    {'field': 'topic', 'blocking': True}))
        for field in ('purpose', 'topic'):
            with self.subTest(field=field):
                self.assertTrue(validate_decision({'action': 'ASK', 'question': 'Which?',
                                                   'affected_field': field}, state).accepted)


class ForcedFallbackGuardTests(unittest.TestCase):
    def assert_no_illegal_action(self, decision, state):
        if decision is not None:
            self.assertIn(decision.action, allowed_actions(state))
            checked = validate_decision({'action': decision.action, **decision.args}, state)
            self.assertTrue(checked.accepted, checked.error)

    def test_failed_search_refinement_cannot_fall_through_to_illegal_finish(self):
        state = control()
        self.assertNotIn('FINISH', allowed_actions(state))
        decision = ChatSession._forced(FallbackDouble(state), 'SEARCH', state)
        self.assert_no_illegal_action(decision, state)

    def test_repeated_refinement_is_not_a_valid_fallback(self):
        state = control(last_query='existing query')
        decision = ChatSession._forced(FallbackDouble(state, query=' Existing   Query '),
                                       'SEARCH', state)
        self.assert_no_illegal_action(decision, state)

    def test_assessment_fallback_cannot_include_unknown_paper_ids(self):
        state = control(stale=1, candidate_ids=('p1',))
        decision = ChatSession._forced(FallbackDouble(state, stale_ids=('unknown',)),
                                       'ASSESS', state)
        self.assert_no_illegal_action(decision, state)

    def test_legal_finish_remains_available(self):
        state = control(gaps=())
        decision = ChatSession._forced(FallbackDouble(state), 'FINISH', state)
        self.assertEqual(decision.action, 'FINISH')
        self.assert_no_illegal_action(decision, state)


class ExecutionGuardTests(unittest.TestCase):
    def assert_rejected_without_execution(self, state, decision):
        chat = ExecutorDouble(state)
        with self.assertRaises(PolicyError):
            ChatSession.apply_decision(chat, decision)
        self.assertEqual(chat.executed, [])

    def test_forbidden_finish_is_rechecked_before_execution(self):
        self.assert_rejected_without_execution(control(), Decision('FINISH', {}, True))

    def test_invalid_or_unaccepted_search_cannot_execute(self):
        state = control()
        for decision in (Decision('SEARCH', {'query': ''}, True),
                         Decision('SEARCH', {'query': 'new'}, False),
                         Decision('UNKNOWN', {}, True)):
            with self.subTest(decision=decision):
                self.assert_rejected_without_execution(state, decision)

    def test_ask_for_nonblocking_field_cannot_execute(self):
        state = control(unresolved=({'field': 'purpose', 'blocking': True},))
        self.assert_rejected_without_execution(
            state, Decision('ASK', {'question': 'Which topic?', 'affected_field': 'topic'}, True))

    def test_action_in_arguments_cannot_override_the_issued_action(self):
        self.assert_rejected_without_execution(
            control(), Decision('FINISH', {'action': 'SEARCH', 'query': 'new query'}, True))

    def test_forced_reassessment_requires_program_authorization(self):
        state = control(stale=1, candidate_ids=('p1',))
        self.assert_rejected_without_execution(
            state, Decision('ASSESS', {'paper_ids': ['p1'], 'force': True}, True))

    def test_unaccepted_decision_is_rejected_by_shared_validator(self):
        checked = validate_issued_decision(Decision('SEARCH', {'query': 'new'}, False), control())
        self.assertFalse(checked.accepted)

    def test_no_actions_has_no_fallback(self):
        self.assertIsNone(fallback_action(control(awaiting_user=True)))

    def test_budget_lookup_failure_cannot_be_ignored(self):
        chat = ExecutorDouble(control())
        def broken_limits():
            raise GuideError('budget state unavailable')
        chat.limits = broken_limits
        with self.assertRaises(GuideError):
            ChatSession.apply_decision(chat, Decision('SEARCH', {'query': 'new query'}, True))
        self.assertEqual(chat.executed, [])

    def test_missing_budget_keys_cannot_authorize_execution(self):
        chat = ExecutorDouble(control())
        chat.limits = lambda: {'searches_left': 1}
        with self.assertRaises(PolicyError):
            ChatSession.apply_decision(chat, Decision('SEARCH', {'query': 'new query'}, True))
        self.assertEqual(chat.executed, [])

    def test_missing_budget_interface_cannot_authorize_execution(self):
        chat = ExecutorDouble(control())
        chat.limits = None
        with self.assertRaises(PolicyError):
            ChatSession.apply_decision(chat, Decision('SEARCH', {'query': 'new query'}, True))
        self.assertEqual(chat.executed, [])


class AwaitingUserGuardTests(unittest.TestCase):
    def test_waiting_session_neither_proposes_nor_executes_search(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                source = DemoSource()
                chat = ChatSession(Config(), store, source=source, llm=ChatDemoLLM())
                store.save_task(chat.session_id, 1, {
                    'topic': 'RAG', 'target_count': 5, 'category': 'none',
                    'minimum_category_count': 0})
                chat._new_phase(first=True)
                store.set_status(chat.session_id, 'awaiting_user')
                store.append_event(chat.session_id, 'ask', {'question': 'Which purpose?'}, revision=1)
                self.assertTrue(chat.control_state().awaiting_user)
                decision, reason = chat.next_decision()
                self.assertIsNone(decision)
                self.assertEqual(reason, 'awaiting_user')
                result = chat._partial(reason)
                self.assertEqual(result['status'], 'awaiting_user')
                self.assertEqual(result['question'], 'Which purpose?')
                self.assertEqual(store.get_session(chat.session_id)['status'], 'awaiting_user')
                with self.assertRaises(PolicyError):
                    chat.apply_decision(Decision('SEARCH', {'query': 'new query'}, True))
                self.assertEqual(source.calls, 0)
            finally:
                store.close()

    def test_resuming_waiting_turn_never_starts_research_or_finishes_session(self):
        class NeverLLM:
            mode = 'live'
            calls = 0
            def generate(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError('waiting session invoked the model')
        class NeverSource:
            mode = 'live'
            calls = 0
            def search(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError('waiting session invoked a source')
        for runtime in ('legacy', 'langgraph'):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                store = StateStore(Path(directory) / 'state.sqlite3')
                try:
                    llm, source = NeverLLM(), NeverSource()
                    chat = ChatSession(Config(research_mode=True, agent_runtime=runtime), store,
                                       source=source, llm=llm)
                    store.save_task(chat.session_id, 1, {
                        'topic': 'RAG', 'target_count': 5, 'category': 'none',
                        'minimum_category_count': 0})
                    chat._new_phase(first=True)
                    store.set_status(chat.session_id, 'awaiting_user')
                    store.append_event(chat.session_id, 'ask', {'question': 'Which purpose?'}, revision=1)
                    result = chat.turn()
                    self.assertEqual(result['status'], 'awaiting_user')
                    self.assertEqual(result['question'], 'Which purpose?')
                    self.assertEqual(store.get_session(chat.session_id)['status'], 'awaiting_user')
                    self.assertEqual((llm.calls, source.calls), (0, 0))
                finally:
                    store.close()


class ForcedAuthorizationRevisionTests(unittest.TestCase):
    def test_reassessment_authorization_does_not_cross_task_revision(self):
        from test_deep_efficiency_specialist import DeepEfficiencyTests
        fixture = DeepEfficiencyTests('test_explicit_reassessment_is_the_only_channel_that_repeats_a_valid_judgement')
        fixture.setUp()
        try:
            chat = fixture.prepared()
            chat.request_reassessment(['P1'])
            decision, _ = chat.next_decision()
            self.assertTrue(decision.args.get('force'))
            original_assessments = chat.llm.count('assess')
            fixture.store.save_task(chat.session_id, chat.revision + 1,
                                    {**chat.task(), 'purpose': 'a different task'})
            with self.assertRaises(PolicyError):
                chat.apply_decision(decision)
            self.assertEqual(chat.llm.count('assess'), original_assessments)
        finally:
            fixture.tearDown()

    def test_normal_assessment_does_not_consume_forced_authorization(self):
        from test_deep_efficiency_specialist import DeepEfficiencyTests
        fixture = DeepEfficiencyTests('test_explicit_reassessment_is_the_only_channel_that_repeats_a_valid_judgement')
        fixture.setUp()
        try:
            chat = fixture.prepared()
            chat.request_reassessment(['P1'])
            forced, _ = chat.next_decision()
            before = chat.llm.count('assess')
            chat.apply_decision(Decision('ASSESS', {'paper_ids': ['P1']}, True))
            self.assertEqual(chat.llm.count('assess'), before)
            self.assertIn('P1', chat._forced_authorizations)
            chat.apply_decision(forced)
            self.assertEqual(chat.llm.count('assess'), before + 1)
        finally:
            fixture.tearDown()

    def test_nonresearch_reassessment_flows_through_the_controller(self):
        from test_deep_efficiency_specialist import DeepEfficiencyTests, OfflineSource, TASK, paper
        fixture = DeepEfficiencyTests('test_explicit_reassessment_is_the_only_channel_that_repeats_a_valid_judgement')
        fixture.setUp()
        try:
            papers = [paper('P1'), paper('P2')]
            chat = fixture.chat(OfflineSource([papers]), task=TASK, research=False)
            chat._do_search('visual retrieval')
            chat._do_assess(['P1'])
            before = chat.llm.count('assess')
            self.assertEqual(chat.request_reassessment(['P1']), ['P1'])
            decision, stop = chat.next_decision()
            self.assertEqual(stop, '')
            self.assertEqual(decision.action, 'ASSESS')
            self.assertTrue(decision.args.get('force'))
            chat.apply_decision(decision)
            self.assertEqual(chat.llm.count('assess'), before + 1)
        finally:
            fixture.tearDown()

    def test_reassessment_requests_merge_without_dropping_the_first_request(self):
        from test_deep_efficiency_specialist import DeepEfficiencyTests
        fixture = DeepEfficiencyTests('test_explicit_reassessment_is_the_only_channel_that_repeats_a_valid_judgement')
        fixture.setUp()
        try:
            chat = fixture.prepared()
            self.assertEqual(chat.request_reassessment(['P1']), ['P1'])
            self.assertEqual(chat.request_reassessment(['P2']), ['P1', 'P2'])
            decision, _ = chat.next_decision()
            self.assertEqual(decision.args['paper_ids'], ['P1', 'P2'])
            self.assertTrue(decision.args.get('force'))
        finally:
            fixture.tearDown()

    def test_nonresearch_reuses_valid_assessment_without_forcing(self):
        from test_deep_efficiency_specialist import DeepEfficiencyTests, OfflineSource, TASK, paper
        fixture = DeepEfficiencyTests('test_explicit_reassessment_is_the_only_channel_that_repeats_a_valid_judgement')
        fixture.setUp()
        try:
            papers = [paper('P1'), paper('P2')]
            chat = fixture.chat(OfflineSource([papers]), task=TASK, research=False)
            chat._do_search('visual retrieval')
            chat._do_assess(['P1'])
            before = chat.llm.count('assess')
            chat._do_assess(['P1'])
            self.assertEqual(chat.llm.count('assess'), before)
        finally:
            fixture.tearDown()

    def test_pending_request_is_not_authorized_after_revision_changes(self):
        from test_deep_efficiency_specialist import DeepEfficiencyTests
        fixture = DeepEfficiencyTests('test_explicit_reassessment_is_the_only_channel_that_repeats_a_valid_judgement')
        fixture.setUp()
        try:
            chat = fixture.prepared()
            chat.request_reassessment(['P1'])
            fixture.store.save_task(chat.session_id, chat.revision + 1,
                                    {**chat.task(), 'purpose': 'a different task'})
            decision, _ = chat.next_decision()
            self.assertTrue(decision is None or not decision.args.get('force'))
        finally:
            fixture.tearDown()


class FallbackIntegrationTests(unittest.TestCase):
    def test_no_safe_fallback_is_reported_without_false_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                chat = ChatSession(Config(), store, source=DemoSource(), llm=ChatDemoLLM())
                store.save_task(chat.session_id, 1, {
                    'topic': 'RAG', 'target_count': 5, 'category': 'none',
                    'minimum_category_count': 0})
                chat._new_phase(first=True)
                chat._refine_query = lambda: ''
                rejection = Decision('FINISH', {}, False, attempted='SEARCH',
                                     error='empty_query', fallback='SEARCH')
                with patch('session.decide', return_value=rejection):
                    decision, reason = chat.next_decision()
                self.assertIsNone(decision)
                self.assertEqual(reason, 'no_legal_action')
                self.assertTrue(any(event['action'] == 'invalid_action'
                                    for event in store.events(chat.session_id)))
                self.assertEqual(chat._partial(reason)['stop_reason'], 'no_legal_action')
            finally:
                store.close()

    def test_policy_rejection_has_distinct_partial_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                chat = ChatSession(Config(), store, source=DemoSource(), llm=ChatDemoLLM())
                store.save_task(chat.session_id, 1, {
                    'topic': 'RAG', 'target_count': 5, 'category': 'none',
                    'minimum_category_count': 0})
                chat._new_phase(first=True)
                reason = chat._error_stop_reason(PolicyError('expired decision'))
                self.assertEqual(reason, 'policy_rejected')
                self.assertEqual(chat._partial(reason)['stop_reason'], 'policy_rejected')
            finally:
                store.close()

    def test_actual_fallback_decision_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                chat = ChatSession(Config(), store, source=DemoSource(), llm=ChatDemoLLM())
                store.save_task(chat.session_id, 1, {
                    'topic': 'RAG', 'target_count': 5, 'category': 'none',
                    'minimum_category_count': 0})
                chat._new_phase(first=True)
                chat._refine_query = lambda: 'refined research query'
                rejected = Decision('FINISH', {}, False, attempted='SEARCH',
                                    error='empty_query', fallback='SEARCH')
                with patch('session.decide', return_value=rejected):
                    decision, _ = chat.next_decision()
                self.assertEqual(decision.action, 'SEARCH')
                events = store.events(chat.session_id)
                self.assertTrue(any(event['action'] == 'invalid_action' for event in events))
                self.assertTrue(any(event['action'] == 'decision'
                                    and event['payload']['context']['decision_source'] == 'program_fallback'
                                    and event['payload']['decision'] == 'SEARCH' for event in events))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
