"""Contracts for the deep upgrade: fast paths, bounded context, specialist tickets.

Everything here is offline. The fakes reuse the production source/model
interfaces, so the fast paths, the reuse rules and the specialist contract are
exercised without HTTP, without credentials and without touching real history.
"""
from copy import deepcopy
from dataclasses import asdict, fields
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config, GuideError, Request
from llm import JsonLLM
from research_specialist import (MAX_CLAIM_CRITIC_OUTPUT_TOKENS, MAX_CLAIM_CRITIC_SECONDS,
                                  SpecialistError, abstract_hashes, assert_result_matches,
                                  claim_critic, coverage_specialist, evidence_critic,
                                  make_ticket, session_ticket)
from session import ChatSession
from state import StateStore

from tests.test_research_runtime import (OfflineLLM, OfflineSource, TASK, brief_output,
                                         paper)


def _key_count(value, key):
    if isinstance(value, dict):
        return sum((1 if name == key else 0) + _key_count(item, key)
                   for name, item in value.items())
    if isinstance(value, list):
        return sum(_key_count(item, key) for item in value)
    return 0


class DeepEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / 'state.sqlite3')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def chat(self, source=None, llm=None, *, task=None, research=True):
        source = source or OfflineSource()
        llm = llm or OfflineLLM()
        config = Config(research_mode=research, agent_runtime='legacy',
                        source=','.join(source.providers), model='offline-fixture', cache=False)
        chat = ChatSession(config, self.store, source=source, llm=llm)
        source.budget = llm.budget = chat.budget
        if task is not None:
            self.store.save_task(chat.session_id, chat.revision + 1,
                                 {**dict(task), **asdict(Request.from_dict(dict(task)))})
            chat._new_phase(first=True)
        return chat

    def prepared(self, *, papers=None, task=None):
        papers = papers or [paper(f'P{i}') for i in range(1, 4)]
        source = OfflineSource([papers])
        chat = self.chat(source, task=task or TASK)
        chat._do_search('visual retrieval')
        chat._do_assess([item.id for item in papers])
        return chat

    # -- fast finish ------------------------------------------------------
    def test_fast_finish_skips_the_model_but_consumes_the_action_allowance(self):
        chat = self.prepared()
        before = chat.llm.count('action')
        allowance = chat.limits()['actions_left']
        decision, stop = chat.next_decision()
        self.assertEqual(stop, '')
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.action, 'FINISH')
        self.assertEqual(decision.reason, 'requirements_verified_by_program')
        self.assertEqual(chat.llm.count('action'), before)
        self.assertEqual(chat.limits()['actions_left'], allowance - 1)
        event = [row for row in chat.store.events(chat.session_id)
                 if row['action'] == 'decision'][-1]
        self.assertEqual(event['payload']['decision'], 'FINISH')
        self.assertEqual(event['payload']['context']['decision_source'], 'program')

    def test_fast_finish_fails_closed_for_a_stale_judgement(self):
        chat = self.prepared()
        self.store.save_task(chat.session_id, chat.revision + 1,
                             {**dict(TASK), 'topic': 'visual retrieval changed',
                              **asdict(Request.from_dict({**dict(TASK),
                                                         'topic': 'visual retrieval changed'}))})
        chat._new_phase(first=False)
        decision, _ = chat.next_decision()
        self.assertNotEqual(decision.action, 'FINISH')

    def test_fast_finish_fails_closed_for_an_excluded_paper(self):
        papers = [paper(f'P{i}') for i in range(1, 4)]
        task = {**TASK, 'excluded_paper_ids': ['P1']}
        source = OfflineSource([papers])
        chat = self.chat(source, task=task)
        chat._do_search('visual retrieval')
        chat._do_assess([item.id for item in papers])
        selected = [item['id'] for item in chat.select()]
        self.assertNotIn('P1', selected)
        decision, _ = chat.next_decision()
        self.assertNotEqual(decision.action, 'FINISH')

    def test_fast_finish_fails_closed_for_an_unmet_publication_constraint(self):
        from reuse import verify_delivery
        chat = self.prepared()
        task = dict(TASK)
        task['accepted_only'] = True
        self.store.save_task(chat.session_id, chat.revision + 1,
                             {**task, **asdict(Request.from_dict(task))})
        chat._new_phase(first=False)
        ids = [row['paper_id'] for row in chat.store.candidate_refs(chat.session_id)]
        checked = verify_delivery(chat.store, chat.session_id, ids, chat.task(),
                                  model=chat.model, prompt_version=chat.prompt_version,
                                  research=True)
        self.assertFalse(checked.ok)
        self.assertEqual(checked.reason, 'publication_constraints_unmet')
        decision, _ = chat.next_decision()
        self.assertNotEqual(decision.action, 'FINISH')

    def test_fast_finish_fails_closed_for_a_coverage_gap(self):
        from model_identity import research_model_identity
        from research_landscape import task_key
        chat = self.prepared()
        plan = {'status': 'ready', 'scope': 'landscape', 'preferred_venues': [],
                'facets': [{'id': 'alpha', 'label': 'Alpha', 'query': 'alpha query'},
                           {'id': 'beta', 'label': 'Beta', 'query': 'beta query'}]}
        plan['task_key'] = task_key(chat.task(), research_model_identity(chat.config))
        chat._event('research_plan', {'plan': plan})
        self.assertTrue(chat.gaps())
        decision, _ = chat.next_decision()
        self.assertNotEqual(decision.action, 'FINISH')

    def test_a_cancelled_session_never_takes_the_fast_path(self):
        chat = self.prepared()
        chat.request_stop()
        decision, stop = chat.next_decision()
        self.assertIsNone(decision)
        self.assertEqual(stop, 'user_cancelled')

    # -- assess ------------------------------------------------------------
    def test_assess_request_is_deduplicated_and_intersected_with_the_stale_set(self):
        papers = [paper('P1'), paper('P2'), paper('P3')]
        source = OfflineSource([papers])
        chat = self.chat(source, task=TASK)
        chat._do_search('visual retrieval')
        chat._do_assess(['P1'])
        self.assertEqual(chat.llm.count('assess'), 1)
        chat._do_assess(['P1', 'P1', 'P2'])
        reuse = [row for row in chat.store.events(chat.session_id)
                 if row['action'] == 'assessment_reuse']
        self.assertTrue(reuse)
        self.assertEqual(reuse[-1]['payload']['paper_ids'], ['P1'])
        assessed = [row for row in chat.store.events(chat.session_id)
                    if row['action'] == 'assess'][-1]['payload']
        self.assertEqual(assessed['assessed'], 1)
        self.assertEqual(chat.llm.count('assess'), 2)

    def test_explicit_reassessment_is_the_only_channel_that_repeats_a_valid_judgement(self):
        chat = self.prepared()
        self.assertEqual(chat.llm.count('assess'), 1)
        self.assertEqual(chat.request_reassessment(['P1']), ['P1'])
        decision, _ = chat.next_decision()
        self.assertEqual(decision.action, 'ASSESS')
        self.assertTrue(decision.args.get('force'))
        self.assertEqual(chat.llm.count('action'), 0)
        chat.apply_decision(decision)
        self.assertEqual(chat.llm.count('assess'), 2)
        forced = [row for row in chat.store.events(chat.session_id)
                  if row['action'] == 'assessment_forced']
        self.assertEqual(forced[-1]['payload']['paper_ids'], ['P1'])

    def test_the_model_cannot_ask_for_a_forced_reassessment(self):
        from controller import ControlState, BudgetView, validate_decision
        state = ControlState(candidates=2, candidate_ids=('P1', 'P2'), stale=1,
                             budget=BudgetView(seconds_left=60, llm_left=4, searches_left=2))
        decision = validate_decision({'action': 'ASSESS', 'paper_ids': ['P1'],
                                      'force': True, 'reason': 'again'}, state)
        self.assertTrue(decision.accepted)
        self.assertNotIn('force', decision.args)

    def test_assessment_stops_when_the_requirements_are_already_met(self):
        papers = [paper(f'P{i}') for i in range(1, 7)]
        source = OfflineSource([papers])
        chat = self.chat(source, task=TASK)
        chat._do_search('visual retrieval')
        chat._do_assess([item.id for item in papers])
        stopped = [row for row in chat.store.events(chat.session_id)
                   if row['action'] == 'assessment_complete']
        self.assertTrue(stopped)
        self.assertEqual(stopped[-1]['payload']['reason'],
                         'requirements_and_planned_coverage_met')
        self.assertLess(chat.llm.count('assess'), 3)

    # -- bounded model-facing context --------------------------------------
    def test_model_facing_context_is_bounded_in_bytes_and_candidate_count(self):
        from research_runtime import MAX_CONTEXT_BYTES, research_context
        oversized = 'x' * 400000
        papers = [paper(f'P{i}', publication={'type': oversized, 'status': oversized,
                                              'presentation': oversized}) for i in range(1, 6)]
        source = OfflineSource([papers])
        chat = self.chat(source, task=TASK)
        chat._do_search('visual retrieval')
        chat._do_assess([item.id for item in papers])
        context = research_context(chat)
        budget = context['context_budget']
        self.assertLessEqual(budget['bytes'], MAX_CONTEXT_BYTES)
        self.assertEqual(budget['limit'], MAX_CONTEXT_BYTES)
        self.assertLessEqual(budget['candidates'], len(papers))
        for candidate in context['candidates']:
            for value in candidate['publication'].values():
                self.assertLessEqual(len(str(value)), 120)

    def test_the_only_legal_search_action_uses_the_coverage_specialist(self):
        from model_identity import research_model_identity
        from research_landscape import task_key
        chat = self.prepared()
        plan = {'status': 'ready', 'scope': 'landscape', 'preferred_venues': [],
                'facets': [{'id': 'alpha', 'label': 'Alpha', 'query': 'alpha query'},
                           {'id': 'beta', 'label': 'Beta', 'query': 'beta query'}]}
        plan['task_key'] = task_key(chat.task(), research_model_identity(chat.config))
        chat._event('research_plan', {'plan': plan})
        decision, stop = chat.next_decision()
        self.assertEqual(stop, '')
        self.assertEqual(decision.action, 'SEARCH')
        self.assertEqual(decision.reason, 'coverage_specialist')
        self.assertEqual(decision.args['intent'], 'fill_gap')
        self.assertEqual(decision.args['query'], 'alpha query')
        ticket = [row for row in chat.store.events(chat.session_id)
                  if row['action'] == 'specialist_ticket'][-1]
        self.assertEqual(ticket['payload']['status'], 'ok')
        self.assertEqual(ticket['payload']['model_calls'], 0)
        self.assertEqual(ticket['payload']['gap_ids'], ['alpha'])
        self.assertEqual(chat.llm.count('action'), 0)

    def test_the_decision_payload_sends_the_task_and_the_gaps_once(self):
        from controller import BudgetView, ControlState, decide
        from copy import deepcopy as copy

        class PayloadLLM:
            mode = 'live'
            last_usage: dict = {}

            def __init__(self):
                self.payload = None

            def generate(self, stage, prompt, data, schema, validate):
                self.payload = copy(data)
                value = {'action': 'FINISH', 'reason': 'Requirements are met.'}
                validate(value)
                return value

        llm = PayloadLLM()
        state = ControlState(candidates=2, candidate_ids=('P1', 'P2'), stale=1,
                             gaps=({'requirement': 'paper_count', 'missing': 1},),
                             budget=BudgetView(seconds_left=60, llm_left=4, searches_left=2))
        observation = {'task': {'topic': 'visual retrieval'},
                       'gaps': [{'requirement': 'paper_count', 'missing': 1}],
                       'pending_ids': ['P1'],
                       'research_context': {'candidates': [{'paper_id': 'P1'}]}}
        decide(llm, state, observation)
        data = llm.payload
        self.assertEqual(_key_count(data, 'task'), 1)
        self.assertEqual(_key_count(data, 'gaps'), 1)
        self.assertEqual(data['task'], {'topic': 'visual retrieval'})
        self.assertEqual(data['gaps'], [{'requirement': 'paper_count', 'missing': 1}])
        self.assertNotIn('task', data['observation'])
        self.assertNotIn('gaps', data['observation'])
        self.assertIn('research_context', data['observation'])

    def test_stage_metrics_report_the_measured_payload_bytes(self):
        config = Config(model='offline-fixture', cache=False)
        llm = JsonLLM(config, Budget(60))
        llm._call = lambda messages, schema: json.dumps({'action': 'FINISH'})
        data = {'task': {'topic': 'visual retrieval'}, 'observation': {}}
        llm.generate('action', 'prompt', data, {'type': 'object'}, lambda value: None)
        expected = len(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        self.assertEqual(llm.events[-1]['payload_bytes'], expected)
        metrics = llm.stage_metrics()
        self.assertEqual(metrics['action']['calls'], 1)
        self.assertEqual(metrics['action']['payload_bytes_total'], expected)
        self.assertGreaterEqual(metrics['action']['elapsed_seconds'], 0.0)

    def test_the_synthesis_stage_reports_its_input_summary(self):
        chat = self.prepared()
        chat._deliver('complete', 'requirements_met', chat.select())
        events = [row for row in chat.store.events(chat.session_id)
                  if row['action'] == 'synthesis']
        self.assertTrue(events)
        summary = events[-1]['payload'].get('input_summary')
        self.assertIsNotNone(summary)
        self.assertGreater(summary['utf8_bytes'], 0)
        self.assertIn('packet_version', summary)


class SpecialistContractTests(unittest.TestCase):
    def setUp(self):
        self.papers = [{'id': 'P1', 'abstract': 'We introduce a retrieval method for images.'},
                       {'id': 'P2', 'abstract': 'We evaluate the method on public benchmarks.'}]
        self.ticket = make_ticket(
            'evidence_critic', task_revision=3, task_key='task-key',
            selection_key='selection-key', model_identity='model-id',
            paper_ids=['P1', 'P2'], trigger='before_delivery',
            abstract_sha256=abstract_hashes(self.papers))

    def test_the_ticket_carries_every_contract_field_and_is_frozen(self):
        expected = {'ticket_id', 'role', 'task_revision', 'task_key', 'selection_key',
                    'model_identity', 'paper_ids', 'evidence_ids', 'abstract_sha256',
                    'trigger', 'deadline', 'max_requests', 'max_output_tokens'}
        self.assertEqual({field.name for field in fields(self.ticket)}, expected)
        with self.assertRaises(AttributeError):
            self.ticket.paper_ids = ()

    def test_a_deterministic_ticket_may_not_request_a_model_call(self):
        with self.assertRaises(SpecialistError):
            make_ticket('evidence_critic', max_requests=1)

    def test_the_evidence_critic_rejects_unknown_cross_paper_and_stale_snapshots(self):
        brief = {'evidence': [
            {'evidence_id': 'E1', 'paper_id': 'P1', 'quote': 'We introduce a retrieval method for images.'},
            {'evidence_id': 'E2', 'paper_id': 'P2', 'quote': 'We evaluate the method on public benchmarks.'}],
            'paper_notes': [
                {'paper_id': 'P1', 'contribution': {'text': 'A method.', 'kind': 'abstract_reported', 'evidence': [
                    {'evidence_id': 'E1', 'paper_id': 'P1', 'quote': 'We introduce a retrieval method for images.'}]}},
                {'paper_id': 'P2', 'contribution': {'text': 'Other paper text.', 'kind': 'abstract_reported', 'evidence': [
                    {'evidence_id': 'E1', 'paper_id': 'P1', 'quote': 'We introduce a retrieval method for images.'}]}}],
            'themes': [], 'open_questions': [], 'research_hypotheses': [],
            'reading_order': [], 'gaps': []}
        result = evidence_critic(self.ticket, self.papers, brief)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.usage['model_calls'], 0)
        reasons = {finding['reason'] for finding in result.findings}
        self.assertIn('cross_paper_evidence', reasons)

        unknown = deepcopy(brief)
        unknown['paper_notes'][0]['contribution']['evidence'] = [
            {'evidence_id': 'E9', 'paper_id': 'P1', 'quote': 'We introduce a retrieval method for images.'}]
        result = evidence_critic(self.ticket, self.papers, unknown)
        self.assertIn('unknown_evidence_id',
                      {finding['reason'] for finding in result.findings})

        changed = deepcopy(self.papers)
        changed[0]['abstract'] = 'A completely different abstract.'
        result = evidence_critic(self.ticket, changed, brief)
        self.assertEqual(result.status, 'rejected')
        self.assertEqual(result.failure_code, 'abstract_snapshot_changed')

    def test_the_evidence_critic_removes_only_the_rejected_claim(self):
        from research_specialist import apply_evidence_findings
        brief = {'evidence': [
            {'evidence_id': 'E1', 'paper_id': 'P1', 'quote': 'A quote that is not in any abstract.'}],
            'paper_notes': [{'paper_id': 'P1', 'contribution': {'text': 'A method.', 'kind': 'abstract_reported', 'evidence': [
                {'evidence_id': 'E1', 'paper_id': 'P1', 'quote': 'A quote that is not in any abstract.'}]}}],
            'themes': [], 'open_questions': [], 'research_hypotheses': [],
            'reading_order': [], 'gaps': []}
        papers = [dict(self.papers[0])]
        ticket = make_ticket('evidence_critic', task_revision=1, task_key='k',
                             selection_key='s', model_identity='m', paper_ids=['P1'],
                             abstract_sha256=abstract_hashes(papers),
                             trigger='before_delivery')
        result = evidence_critic(ticket, papers, brief)
        self.assertEqual(result.status, 'ok')
        self.assertIn('quote_not_in_abstract',
                      {finding['reason'] for finding in result.findings})
        updated, gaps = apply_evidence_findings(brief, result)
        self.assertIsNone(updated['paper_notes'][0]['contribution'])
        self.assertTrue(gaps)
        self.assertEqual(gaps[0]['reason'], 'claim_removed_by_evidence_critic')

    def test_the_coverage_specialist_plans_one_facet_and_adds_no_search_round(self):
        landscape = {'scope': 'landscape', 'status': 'ready', 'facets': [
            {'id': 'alpha', 'label': 'Alpha', 'query': 'alpha query'},
            {'id': 'beta', 'label': 'Beta', 'query': 'beta query'}]}
        selected = [{'id': 'P1'}]
        covered = dict(landscape)
        covered['assignments'] = [{'paper_id': 'P1', 'facet_ids': ['alpha']}]
        ticket = make_ticket('coverage_specialist', task_revision=1, task_key='k',
                             selection_key='s', model_identity='m', paper_ids=['P1'],
                             trigger='planned_facet_missing')
        result = coverage_specialist(ticket, covered, selected, [], searches_left=2)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.usage['model_calls'], 0)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0]['facet_id'], 'beta')
        self.assertEqual(result.findings[0]['query'], 'beta query')
        self.assertEqual(result.gap_ids, ('beta',))

        blocked = coverage_specialist(ticket, covered, selected, [], searches_left=0)
        self.assertEqual(blocked.status, 'skipped')
        self.assertEqual(blocked.failure_code, 'no_affordable_search_round')

    def test_the_coverage_specialist_never_repeats_a_previous_query(self):
        landscape = {'scope': 'landscape', 'status': 'ready', 'facets': [
            {'id': 'alpha', 'label': 'Alpha', 'query': 'alpha query'}]}
        ticket = make_ticket('coverage_specialist', task_revision=1, task_key='k',
                             selection_key='s', model_identity='m', trigger='planned_facet_missing')
        result = coverage_specialist(ticket, landscape, [], ['alpha query'], searches_left=1)
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(result.failure_code, 'no_uncovered_facet')

    def test_the_claim_critic_is_disabled_unless_deep_mode_is_requested(self):
        ticket = make_ticket('claim_critic', task_revision=1, task_key='k',
                             selection_key='s', model_identity='m', paper_ids=['P1'],
                             evidence_ids=['E1'], trigger='unresolved_claim',
                             deadline=10, max_requests=1, max_output_tokens=600)
        result = claim_critic(ticket, [{'claim_id': 'C1', 'text': 'x'}], llm=None)
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(result.failure_code, 'deep_mode_disabled')
        self.assertEqual(result.usage.get('model_calls', 0), 0)

    def test_the_claim_critic_is_bounded_by_one_request_and_its_ticket(self):
        with self.assertRaises(SpecialistError):
            make_ticket('claim_critic', max_requests=2)
        with self.assertRaises(SpecialistError):
            make_ticket('claim_critic', max_output_tokens=MAX_CLAIM_CRITIC_OUTPUT_TOKENS + 1)
        with self.assertRaises(SpecialistError):
            make_ticket('claim_critic', deadline=MAX_CLAIM_CRITIC_SECONDS + 1)

        calls = []

        class FakeLLM:
            last_usage = {'input_tokens': 10, 'output_tokens': 5}

            def generate(self, stage, prompt, data, schema, validate):
                calls.append(stage)
                value = {'verdicts': [{'claim_id': 'C1', 'verdict': 'qualify',
                                       'reason': 'partial support',
                                       'evidence_ids': ['E1']}]}
                validate(value)
                return value

        ticket = make_ticket('claim_critic', task_revision=1, task_key='k',
                             selection_key='s', model_identity='m', paper_ids=['P1'],
                             evidence_ids=['E1'], trigger='unresolved_claim',
                             deadline=10, max_requests=1, max_output_tokens=600)
        result = claim_critic(ticket, [{'claim_id': 'C1', 'text': 'x'}], llm=FakeLLM(),
                              deep=True)
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(result.failure_code, 'legacy_critic_disabled')
        self.assertEqual(calls, [])
        self.assertEqual(result.usage['model_calls'], 0)

    def test_the_claim_critic_rejects_unknown_identities(self):
        class FakeLLM:
            last_usage = {}

            def generate(self, stage, prompt, data, schema, validate):
                value = {'verdicts': [{'claim_id': 'C9', 'verdict': 'retain', 'reason': 'x'}]}
                validate(value)
                return value

        ticket = make_ticket('claim_critic', task_revision=1, task_key='k',
                             selection_key='s', model_identity='m', paper_ids=['P1'],
                             evidence_ids=['E1'], trigger='unresolved_claim',
                             deadline=10, max_requests=1, max_output_tokens=600)
        result = claim_critic(ticket, [{'claim_id': 'C1', 'text': 'x'}], llm=FakeLLM(),
                              deep=True)
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(result.failure_code, 'legacy_critic_disabled')
        self.assertEqual(result.usage['model_calls'], 0)

    def test_a_result_from_another_revision_selection_or_model_is_rejected(self):
        from research_specialist import SpecialistResult
        result = SpecialistResult(ticket_id=self.ticket.ticket_id, status='ok')
        with self.assertRaises(SpecialistError):
            assert_result_matches(self.ticket, result, revision=99)
        with self.assertRaises(SpecialistError):
            assert_result_matches(self.ticket, result, model_identity='other-model')
        with self.assertRaises(SpecialistError):
            assert_result_matches(self.ticket, result, papers=self.papers)
        other = SpecialistResult(ticket_id='other-ticket', status='ok')
        with self.assertRaises(SpecialistError):
            assert_result_matches(self.ticket, other)

    def test_a_session_ticket_binds_the_current_task_selection_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / 'state.sqlite3')
            try:
                source = OfflineSource()
                llm = OfflineLLM()
                config = Config(research_mode=True, agent_runtime='legacy',
                                source='arxiv', model='offline-fixture', cache=False)
                chat = ChatSession(config, store, source=source, llm=llm)
                source.budget = llm.budget = chat.budget
                store.save_task(chat.session_id, chat.revision + 1,
                                {**dict(TASK), **asdict(Request.from_dict(dict(TASK)))})
                ticket = session_ticket('evidence_critic', chat, [], trigger='before_delivery')
                self.assertEqual(ticket.role, 'evidence_critic')
                self.assertTrue(ticket.task_key)
                self.assertTrue(ticket.selection_key)
                self.assertTrue(ticket.model_identity)
                self.assertEqual(ticket.task_revision, chat.revision)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
