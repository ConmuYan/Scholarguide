"""Policy tests: what the agent is allowed to do next, and why.

These tests never call a model. They pin the gating rules that keep a model from
converging early, searching past its budget, or asking a question that cannot
change the next action.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import GuideError
from controller import (
    ACTIONS,
    BudgetView,
    ControlState,
    PolicyError,
    allowed_actions,
    ask_permitted,
    decide,
    fallback_action,
    partial_reason,
    validate_decision,
)
from tools import CostMeter


def state(**kw):
    base = dict(candidates=10, unassessed=0, stale=0, gaps=(),
                unresolved=(), budget=BudgetView(seconds_left=60, llm_left=10, searches_left=2))
    base.update(kw)
    return ControlState(**base)


QUOTA_GAP = {'requirement': 'evaluation', 'required': 2, 'found': 0, 'missing': 2}
COUNT_GAP = {'requirement': 'paper_count', 'required': 5, 'found': 3, 'missing': 2}


class AllowedActionsTests(unittest.TestCase):
    def test_gaps_and_budget_forbid_finish(self):
        s = state(gaps=(QUOTA_GAP,), unassessed=4)
        legal = allowed_actions(s)
        self.assertIn('ASSESS', legal)
        self.assertIn('SEARCH', legal)
        self.assertNotIn('FINISH', legal)

    def test_finish_allowed_when_requirements_met(self):
        s = state(gaps=(), unassessed=2)
        self.assertIn('FINISH', allowed_actions(s))

    def test_finish_allowed_when_no_productive_action_remains(self):
        s = state(gaps=(QUOTA_GAP,), unassessed=0, budget=BudgetView(0, 0, 0))
        self.assertEqual(allowed_actions(s), ('FINISH',))

    def test_assess_requires_pending_work(self):
        s = state(unassessed=0, stale=0)
        self.assertNotIn('ASSESS', allowed_actions(s))
        s = state(stale=2)
        self.assertIn('ASSESS', allowed_actions(s))

    def test_awaiting_user_blocks_every_action(self):
        s = state(awaiting_user=True, gaps=(QUOTA_GAP,))
        self.assertEqual(allowed_actions(s), ())
        with self.assertRaises(PolicyError):
            decide(object(), s)


class AskGatingTests(unittest.TestCase):
    def test_ask_requires_a_blocking_field(self):
        s = state(unresolved=({'field': 'purpose', 'blocking': True},))
        self.assertTrue(ask_permitted(s))
        self.assertIn('ASK', allowed_actions(s))

    def test_non_blocking_ambiguity_does_not_ask(self):
        s = state(unresolved=({'field': 'purpose', 'blocking': False},))
        self.assertFalse(ask_permitted(s))
        self.assertNotIn('ASK', allowed_actions(s))

    def test_ask_requires_an_affordable_search(self):
        s = state(unresolved=({'field': 'topic', 'blocking': True},),
                  budget=BudgetView(60, 10, 0))
        self.assertFalse(ask_permitted(s))

    def test_only_one_ask_per_phase(self):
        s = state(unresolved=({'field': 'purpose', 'blocking': True},), asks_left=0)
        self.assertFalse(ask_permitted(s))
        self.assertNotIn('ASK', allowed_actions(s))

    def test_unrelated_field_does_not_ask(self):
        s = state(unresolved=({'field': 'author', 'blocking': True},))
        self.assertFalse(ask_permitted(s))


class DecisionValidationTests(unittest.TestCase):
    def test_unknown_action_rejected(self):
        d = validate_decision({'action': 'DELETE', 'reason': 'x'}, state())
        self.assertFalse(d.accepted)
        self.assertEqual(d.error, 'unknown_action')

    def test_action_outside_allowed_rejected(self):
        s = state(gaps=(QUOTA_GAP,), unassessed=4)
        d = validate_decision({'action': 'FINISH', 'reason': 'done'}, s)
        self.assertFalse(d.accepted)
        self.assertEqual(d.error, 'not_allowed')
        self.assertEqual(d.fallback, 'ASSESS')

    def test_search_rejected_when_budget_is_spent(self):
        s = state(budget=BudgetView(60, 10, 0))
        self.assertNotIn('SEARCH', allowed_actions(s))
        d = validate_decision({'action': 'SEARCH', 'query': 'new angle'}, s)
        self.assertFalse(d.accepted)
        self.assertEqual(d.error, 'not_allowed')
        self.assertEqual(d.fallback, 'FINISH')

    def test_search_allowed_with_one_search_left(self):
        s = state(budget=BudgetView(60, 10, 1))
        d = validate_decision({'action': 'SEARCH', 'query': 'new angle'}, s)
        self.assertTrue(d.accepted)
        self.assertEqual(d.args, {'query': 'new angle'})

    def test_repeated_query_rejected(self):
        s = state(last_query='RAG evaluation')
        d = validate_decision({'action': 'SEARCH', 'query': '  rag   evaluation '}, s)
        self.assertFalse(d.accepted)
        self.assertEqual(d.error, 'repeated_query')

    def test_assess_requires_known_ids(self):
        s = state(unassessed=2, candidate_ids=('p1', 'p2'))
        d = validate_decision({'action': 'ASSESS', 'paper_ids': []}, s)
        self.assertFalse(d.accepted)
        self.assertEqual(d.error, 'no_paper_ids')
        d = validate_decision({'action': 'ASSESS', 'paper_ids': ['p9']}, s)
        self.assertFalse(d.accepted)
        self.assertEqual(d.error, 'unknown_paper_ids')
        d = validate_decision({'action': 'ASSESS', 'paper_ids': ['p1']}, s)
        self.assertTrue(d.accepted)

    def test_ask_requires_question_and_blocking_field(self):
        s = state(unresolved=({'field': 'purpose', 'blocking': True},))
        bad = validate_decision({'action': 'ASK', 'question': 'Which?'}, s)
        self.assertFalse(bad.accepted)
        self.assertEqual(bad.error, 'unknown_field')
        good = validate_decision({'action': 'ASK', 'question': 'Is this for a survey?',
                                  'affected_field': 'purpose'}, s)
        self.assertTrue(good.accepted)


class PartialReasonTests(unittest.TestCase):
    def test_complete_has_no_reason(self):
        self.assertEqual(partial_reason(state(gaps=())), '')

    def test_quota_unmet(self):
        s = state(gaps=(QUOTA_GAP,), candidates=10, budget=BudgetView(60, 10, 1))
        self.assertEqual(partial_reason(s), 'quota_unmet')

    def test_source_lacks_topic(self):
        s = state(gaps=(COUNT_GAP,), candidates=3, budget=BudgetView(60, 10, 1))
        self.assertEqual(partial_reason(s), 'source_lacks_topic')

    def test_budget_exhausted(self):
        s = state(gaps=(COUNT_GAP,), candidates=3, budget=BudgetView(0, 0, 0))
        self.assertEqual(partial_reason(s), 'budget_exhausted')
        s = state(gaps=(COUNT_GAP,), candidates=3, unassessed=0,
                  budget=BudgetView(30, 5, 0))
        self.assertTrue(partial_reason(s), 'budget_exhausted')


class DecideTests(unittest.TestCase):
    class LLM:
        def __init__(self, payload):
            self.payload = payload
            self.calls = []

        def generate(self, stage, prompt, data, schema, validate):
            self.calls.append({'stage': stage, 'data': data})
            validate(self.payload)
            return self.payload

    def test_decision_charges_one_action_unit(self):
        meter = CostMeter()
        llm = self.LLM({'action': 'SEARCH', 'query': 'rag benchmark', 'reason': 'need quota'})
        d = decide(llm, state(gaps=(QUOTA_GAP,)), {'task': {'topic': 'rag'}}, meter)
        self.assertTrue(d.accepted)
        self.assertEqual(d.action, 'SEARCH')
        self.assertEqual(meter.summary()['settled'], 1.0)
        self.assertEqual(meter.summary()['by_action']['action']['settled'], 1.0)

    def test_model_sees_the_allowed_set(self):
        llm = self.LLM({'action': 'FINISH', 'reason': 'done'})
        decide(llm, state(gaps=(QUOTA_GAP,), unassessed=1), {})
        self.assertEqual(llm.calls[0]['data']['allowed'], ['ASSESS', 'SEARCH'])

    def test_invalid_model_output_falls_back(self):
        llm = self.LLM({'action': 'FINISH', 'reason': 'done'})
        d = decide(llm, state(gaps=(QUOTA_GAP,), unassessed=1), {})
        self.assertFalse(d.accepted)
        self.assertEqual(d.fallback, 'ASSESS')


if __name__ == '__main__':
    unittest.main()


class RecordDecisionTests(unittest.TestCase):
    def test_rejection_is_recorded_as_invalid_action(self):
        import tempfile
        from pathlib import Path as _Path
        from state import StateStore
        from controller import record_decision

        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(_Path(tmp) / 's.sqlite3')
            sid = store.create_session()
            bad = validate_decision({'action': 'FINISH'}, state(gaps=(QUOTA_GAP,), unassessed=2))
            record_decision(store, sid, bad, revision=1)
            events = store.events(sid)
            self.assertEqual(events[0]['action'], 'invalid_action')
            self.assertEqual(events[0]['payload']['attempted'], 'FINISH')
            self.assertEqual(events[0]['payload']['fallback'], 'ASSESS')
            ok = validate_decision({'action': 'ASSESS', 'paper_ids': ['p1']},
                                   state(unassessed=2, candidate_ids=('p1',)))
            record_decision(store, sid, ok, revision=1)
            self.assertEqual(store.events(sid)[1]['action'], 'decision')
            self.assertEqual(store.events(sid)[1]['payload']['decision'], 'ASSESS')
            store.close()
