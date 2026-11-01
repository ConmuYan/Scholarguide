"""Semantics the frozen v2 spec requires beyond the happy path.

Covers: exclusion / recency / K in selection, purpose reaching the assessor,
phase budgets, ASK pause, failure retention, unsatisfiable tasks, and the
invariant assertions promised by docs/DESIGN_INVARIANTS.md.
"""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Config, GuideError, Paper, Request
from controller import (
    BudgetView,
    ControlState,
    FactCounts,
    PolicyError,
    assert_action_allowed,
    assert_budget_conservation,
    assert_threshold_source,
    partial_reason,
)
from reuse import assert_delivery_consistent, verify_delivery
from session import ChatSession, FeedbackBudgetExhausted
from state import InvariantError, StateStore, semantic_key
from workflow import coverage, select_papers

QUOTE = 'This paper introduces a benchmark for retrieval augmented generation evaluation.'


def make_paper(idx, year=2020):
    return Paper(id=f'p{idx}', title=f'Paper {idx}', abstract=QUOTE,
                 url=f'https://arxiv.org/abs/2401.0000{idx}', year=year)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.papers = [make_paper(1, 2019), make_paper(2, 2024), make_paper(3, 2022),
                       make_paper(4, 2021)]
        self.assessments = {p.id: {'relevance': 2, 'category_status': 'yes',
                                   'evidence_verified': True, 'evidence_quote': QUOTE,
                                   'reason': 'fixture'} for p in self.papers}

    def test_excluded_papers_never_appear(self):
        request = Request.from_dict({'topic': 'rag', 'target_count': 4,
                                     'excluded_paper_ids': ['p2']})
        selected = select_papers(self.papers, self.assessments, request)
        self.assertNotIn('p2', [p['id'] for p in selected])
        self.assertEqual(len(selected), 3)

    def test_prefer_recent_orders_within_same_relevance(self):
        request = Request.from_dict({'topic': 'rag', 'target_count': 4, 'prefer_recent': True})
        selected = select_papers(self.papers, self.assessments, request)
        self.assertEqual([p['id'] for p in selected], ['p2', 'p3', 'p4', 'p1'])
        default = select_papers(self.papers, self.assessments,
                                Request.from_dict({'topic': 'rag', 'target_count': 4}))
        self.assertEqual([p['id'] for p in default], ['p1', 'p2', 'p3', 'p4'])

    def test_venue_and_year_are_hard_constraints(self):
        papers = [
            Paper(id='cvpr24', title='CVPR 2024', abstract=QUOTE,
                  url='https://example.org/1', year=2024,
                  venue='IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)'),
            Paper(id='cvpr23', title='Old CVPR', abstract=QUOTE,
                  url='https://example.org/2', year=2023, venue='CVPR'),
            Paper(id='iccv24', title='ICCV 2024', abstract=QUOTE,
                  url='https://example.org/3', year=2024, venue='ICCV'),
        ]
        assessed = {p.id: {'relevance': 2, 'category_status': 'yes',
                           'evidence_verified': True, 'evidence_quote': QUOTE,
                           'reason': 'fixture'} for p in papers}
        request = Request.from_dict({'topic': '3D generation', 'target_count': 3,
                                     'venue': 'CVPR', 'year_from': 2024, 'year_to': 2025})
        selected = select_papers(papers, assessed, request)
        self.assertEqual([p['id'] for p in selected], ['cvpr24'])
        gaps = coverage(selected, request)
        self.assertTrue(any(g['requirement'] == 'constraints' for g in gaps))
        with self.assertRaises(GuideError):
            Request.from_dict({'topic': '3D', 'year_from': 2026, 'year_to': 2024})

    def test_target_count_accepts_one_to_ten(self):
        for target in range(1, 11):
            request = Request.from_dict({'topic': 'rag', 'target_count': target})
            self.assertEqual(request.target_count, target)
            self.assertLessEqual(len(select_papers(self.papers, self.assessments, request)), target)
        with self.assertRaises(GuideError):
            Request.from_dict({'topic': 'rag', 'target_count': 0})
        with self.assertRaises(GuideError):
            Request.from_dict({'topic': 'rag', 'target_count': 11})


class FakeSource:
    mode = 'demo'

    def __init__(self, papers=None):
        self.papers = papers if papers is not None else [make_paper(i) for i in range(1, 7)]
        self.calls = []

    def search(self, query, limit=10):
        self.calls.append(query)
        return self.papers[:limit]


class ScriptedLLM:
    """Returns scripted payloads per stage; records every assess payload."""
    mode = 'demo'

    def __init__(self, *, update_patch=None, action='FINISH', fail_stage=None,
                 assess_payload=None, unsupported=None, assumptions=None,
                 ask_on_assumption=False):
        self.update_patch = update_patch or {}
        self.action = action
        self.fail_stage = fail_stage
        self.assess_payload = assess_payload or {}
        self.search_count = 0
        self.last_assess_payload = {}
        self.last_update_payload = {}
        self.stages = []
        self.unsupported = unsupported or []
        self.assumptions = assumptions or []
        self.ask_on_assumption = ask_on_assumption

    def generate(self, stage, prompt, data, schema, validate):
        self.stages.append(stage)
        if stage == self.fail_stage:
            raise GuideError('simulated model failure')
        if stage == 'update':
            self.last_update_payload = data
            patch = dict(self.update_patch)
            task = data.get('task') or {}
            message = str(data.get('message') or '')
            if not task.get('topic') and 'topic' not in patch:
                patch['topic'] = 'retrieval augmented generation'
            if 'third paper' in message and data.get('last_recommendations'):
                patch['excluded_paper_ids'] = [data['last_recommendations'][2]['paper_id']]
            out = {'patch': patch, 'summary': 'scripted',
                   'assumptions': self.assumptions or
                                 [{'field': 'prefer_recent', 'text': 'fixture assumption'}],
                   'unsupported': self.unsupported, 'unresolved': []}
        elif stage == 'action':
            allowed = data['allowed']
            observation = data.get('observation') or {}
            pending = observation.get('pending_ids') or []
            if self.ask_on_assumption and 'ASK' in allowed:
                out = {'action': 'ASK', 'question': 'Is the purpose a survey?',
                       'affected_field': 'purpose', 'reason': 'assumption blocks selection'}
            elif self.action == 'SEARCH' and 'SEARCH' in allowed:
                self.search_count += 1
                out = {'action': 'SEARCH', 'query': f'rag angle {self.search_count}',
                       'reason': 'scripted search'}
            elif 'ASSESS' in allowed and pending:
                out = {'action': 'ASSESS', 'paper_ids': pending[:10], 'reason': 'scripted assess'}
            elif 'FINISH' in allowed:
                out = {'action': 'FINISH', 'reason': 'scripted finish'}
            else:
                out = {'action': 'FINISH', 'reason': 'nothing legal'}
        elif stage == 'refine':
            self.search_count += 1
            out = {'query': f'refined angle {self.search_count}'}
        elif stage == 'assess':
            rows = []
            for paper in data['papers']:
                rows.append({'paper_id': paper['paper_id'], 'relevance': 2,
                             'category_status': self.assess_payload.get('category_status', 'yes'),
                             'evidence_quote': QUOTE,
                             'reason': self.assess_payload.get('reason', 'scripted reason')})
            out = {'assessments': rows}
            self.last_assess_payload = data
        else:
            raise AssertionError(stage)
        validate(out)
        return out


class SessionSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / 'state.sqlite3')
        self.config = Config(provider='openai', model='fake-model', source='arxiv')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def chat(self, llm=None, source=None):
        return ChatSession(self.config, self.store, source=source or FakeSource(), llm=llm)

    def test_purpose_reaches_the_assessor(self):
        llm = ScriptedLLM(update_patch={'purpose': 'design an ablation'})
        chat = self.chat(llm)
        chat.start(message='Find five RAG papers for designing an ablation.')
        self.assertEqual(llm.last_assess_payload['purpose'], 'design an ablation')

    def test_phase_budget_caps_first_turn_at_two_searches(self):
        llm = ScriptedLLM(action='SEARCH')
        source = FakeSource(papers=[])
        chat = self.chat(llm, source)
        result = chat.start(request_data={'topic': 'rag', 'target_count': 5})
        self.assertEqual(len(source.calls), 4)
        self.assertEqual(result['status'], 'no_results')
        self.assertEqual(chat.limits()['searches_left_phase'], 0)

    def test_feedback_phase_allows_one_new_search(self):
        llm = ScriptedLLM()
        chat = self.chat(llm)
        chat.start(request_data={'topic': 'rag', 'target_count': 5})
        llm2 = ScriptedLLM(action='SEARCH')
        chat.llm = llm2
        chat.send('Actually include a survey too.')
        self.assertEqual(chat.limits()['searches_left_phase'], 0)
        self.assertLessEqual(len(chat.source.calls), 5)  # first discovery plus one bounded new turn

    def test_ask_pauses_clock_and_keeps_actions(self):
        llm = ScriptedLLM()
        chat = self.chat(llm)
        chat.start(message='Find RAG papers for me.')
        # Force a programmatic clarification by removing the topic.
        task = chat.task()
        task.pop('topic', None)
        chat.store.save_task(chat.session_id, chat.revision + 1, task)
        result = chat.turn()
        self.assertEqual(result['status'], 'awaiting_user')
        self.assertEqual(result['stop_reason'], 'missing_topic')
        paused = chat.limits()['paused_seconds_left']
        self.assertGreaterEqual(paused, 0)
        before_actions = chat.limits()['actions_left']
        resumed = chat.send('The topic is retrieval augmented generation.')
        self.assertEqual(resumed['status'], 'complete')
        self.assertLess(chat.limits()['actions_left'], before_actions)  # not reset to 6

    def test_model_failure_returns_partial_with_existing_candidates(self):
        llm = ScriptedLLM(fail_stage='assess')
        chat = self.chat(llm)
        result = chat.start(request_data={'topic': 'rag', 'target_count': 5})
        self.assertEqual(result['status'], 'no_results')
        self.assertEqual(result['stop_reason'], 'model_or_tool_error')
        self.assertTrue(result['warnings'])
        self.assertGreater(len(result['candidates']), 0)  # search results are kept

    def test_action_failure_is_retained_as_partial(self):
        llm = ScriptedLLM(fail_stage='action')
        chat = self.chat(llm)
        result = chat.start(request_data={'topic': 'rag', 'target_count': 5})
        self.assertEqual(result['stop_reason'], 'model_or_tool_error')
        self.assertIn(result['status'], ('partial', 'no_results'))

    def test_invalid_constraints_do_not_crash_update(self):
        llm = ScriptedLLM(update_patch={'minimum_category_count': 5, 'target_count': 3})
        chat = self.chat(llm)
        with self.assertRaises(GuideError):
            chat.start(message='Recommend three RAG papers with five evaluation papers.')
        self.assertEqual(chat.revision, 0)
        self.assertFalse(chat.source.calls)

    def test_unsatisfiable_task_is_reported_not_crashed(self):
        llm = ScriptedLLM()
        chat = self.chat(llm)
        chat.start(request_data={'topic': 'rag', 'target_count': 5})
        task = chat.task()
        task['minimum_category_count'] = 5
        task['target_count'] = 3
        task['category'] = 'evaluation'
        chat.store.save_task(chat.session_id, chat.revision + 1, task)
        result = chat.turn()
        self.assertEqual(result['stop_reason'], 'unsatisfiable')
        self.assertIn(result['status'], ('no_results', 'partial'))

    def test_feedback_receives_last_recommendations_and_excludes(self):
        llm = ScriptedLLM()
        chat = self.chat(llm)
        first = chat.start(request_data={'topic': 'rag', 'target_count': 5})
        ordered = [p['id'] for p in first['papers']]
        self.assertEqual(len(ordered), 5)
        result = chat.send('Drop the third paper, keep the rest.')
        self.assertEqual([r['index'] for r in llm.last_update_payload['last_recommendations']],
                         [1, 2, 3, 4, 5])
        self.assertEqual(llm.last_update_payload['last_recommendations'][2]['paper_id'],
                         ordered[2])
        self.assertNotIn(ordered[2], [p['id'] for p in result['papers']])
        self.assertIn('excluded_paper_ids', result['request'])

    def test_unsupported_requirement_blocks_completion(self):
        llm = ScriptedLLM(unsupported=['keep the two evaluation papers'])
        chat = self.chat(llm)
        result = chat.start(message='Recommend RAG papers. keep the two evaluation papers.')
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(result['stop_reason'], 'unsupported_requirements')
        self.assertEqual(result['unsupported'], ['keep the two evaluation papers'])

    def test_blocking_assumption_is_surfaced_and_can_trigger_ask(self):
        llm = ScriptedLLM(assumptions=[{'field': 'purpose', 'text': 'maybe a survey'}],
                          ask_on_assumption=True)
        chat = self.chat(llm)
        result = chat.start(message='Find RAG papers for me.')
        self.assertEqual(result['status'], 'awaiting_user')
        state = chat.control_state()
        self.assertTrue(any(item.get('assumption') for item in state.unresolved))

    def test_feedback_phase_limit(self):
        llm = ScriptedLLM()
        chat = self.chat(llm)
        chat.start(request_data={'topic': 'rag', 'target_count': 5})
        chat.send('Keep the same topic, prefer recent work.')
        chat.send('Still the same topic.')
        result = chat.send('One more change.')
        self.assertTrue(chat.can_send())
        self.assertIsNone(chat.capabilities()['feedback_phases_left'])
        self.assertGreaterEqual(result['revision'], 4)

    def test_zero_llm_budget_stops_before_decision(self):
        llm = ScriptedLLM()
        chat = self.chat(llm)
        chat.store.update_budget(chat.session_id, {'llm_left': 0})
        result = chat.start(request_data={'topic': 'rag', 'target_count': 5})
        self.assertEqual(result['stop_reason'], 'llm_budget_exhausted')
        self.assertNotIn('action', llm.stages)

    def test_origins_and_assumptions_are_persisted(self):
        llm = ScriptedLLM(update_patch={'prefer_recent': True})
        chat = self.chat(llm)
        chat.start(message='Find four recent RAG papers.')
        task = chat.task()
        self.assertIn('prefer_recent', task['requirement_origins'])
        self.assertEqual(task['requirement_origins']['prefer_recent']['message_id'], 'msg-1')
        self.assertFalse(task['requirement_origins']['prefer_recent']['verified'])
        self.assertEqual(task['assumptions'][0]['text'], 'fixture assumption')


class InvariantAssertionTests(unittest.TestCase):
    def test_state_rejects_quote_not_in_abstract(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / 's.sqlite3')
            sid = store.create_session()
            paper = {'paper_id': 'p1', 'title': 'T', 'abstract': 'A totally different abstract.'}
            ref = store.register_paper(paper)
            semantic = semantic_key('rag', '', 'none', 'm', 'v1')
            with self.assertRaises(InvariantError):
                store.put_assessment(
                    sid, paper_id='p1', content=ref['content_hash'], semantic=semantic,
                    computed={'content_hash': ref['content_hash'], 'semantic_key': semantic,
                              'model': 'm', 'prompt_version': 'v1'},
                    relevance=2, category_status='yes',
                    evidence_quote='this quote is absent from the paper', evidence_verified=True)
            store.close()

    def test_delivery_assertion_refuses_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / 's.sqlite3')
            sid = store.create_session()
            check = verify_delivery(store, sid, [], {'topic': 'rag'}, model='m', prompt_version='v1')
            self.assertTrue(check.ok)
            with self.assertRaises(Exception):
                assert_delivery_consistent(type(check)(ok=False, stale_ids=('p1',),
                                                       unsupported_ids=(), reason='stale_assessments',
                                                       plan=check.plan))
            store.close()

    def test_controller_assertions(self):
        state = ControlState(gaps=({'requirement': 'x'},), stale=1,
                             budget=BudgetView(60, 10, 2))
        with self.assertRaises(PolicyError):
            assert_action_allowed('FINISH', state)
        assert_action_allowed('ASSESS', state)
        with self.assertRaises(PolicyError):
            assert_threshold_source(0.42)
        assert_threshold_source(FactCounts(verified=3, quota_covered=1, candidates=5))
        with self.assertRaises(PolicyError):
            assert_budget_conservation([{'reserved': 2, 'settled': 1, 'refunded': 0}])
        assert_budget_conservation([{'reserved': 2, 'settled': 1, 'refunded': 1}])

    def test_partial_reason_includes_unsatisfiable(self):
        state = ControlState(gaps=({'requirement': 'paper_count'},), unsatisfiable=True,
                             budget=BudgetView(60, 10, 2))
        self.assertEqual(partial_reason(state), 'unsatisfiable')


if __name__ == '__main__':
    unittest.main()
