"""Offline regression tests through the research delivery path."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from research_specialist import MAX_FINDINGS, claim_critic, make_ticket
from tests.test_research_runtime import ResearchRuntimeTests, paper
from research_runtime import attach_brief


class EvidenceRegressionTests(ResearchRuntimeTests):
    def setUp(self):
        super().setUp()
        self.chat_ = self.chat(task={'topic': 'fixture'})
        self.papers = [paper('P1').to_dict(), paper('P2').to_dict()]
        self.rows = [{'evidence_id': 'E1', 'paper_id': 'P1', 'quote': self.papers[0]['abstract'].split('. ')[0] + '.'},
                     {'evidence_id': 'E2', 'paper_id': 'P2', 'quote': self.papers[1]['abstract'].split('. ')[0] + '.'}]
        from research_brief import ABSTRACT_LIMIT
        for row, item in zip(self.rows, self.papers):
            row['abstract_sha256'] = hashlib.sha256(item['abstract'][:ABSTRACT_LIMIT].encode()).hexdigest()
        self.brief = {'status': 'ready', 'reason': 'generated', 'evidence': deepcopy(self.rows),
                      'paper_notes': [{'paper_id': 'P1', 'contribution': {
                          'text': 'Claim', 'kind': 'abstract_reported', 'evidence': [deepcopy(self.rows[0])]}}],
                      'themes': [], 'open_questions': [], 'research_hypotheses': [], 'gaps': []}

    def deliver(self, brief, papers=None):
        with patch('research_runtime.synthesize', return_value=deepcopy(brief)):
            return attach_brief(self.chat_, papers or self.papers)

    def test_forged_and_missing_references_are_removed(self):
        for mode in ('paper', 'quote', 'empty_quote', 'missing', 'duplicate', 'missing_row', 'other_paper'):
            with self.subTest(mode=mode):
                brief = deepcopy(self.brief)
                claim = brief['paper_notes'][0]['contribution']
                if mode == 'paper': claim['evidence'][0].update(paper_id='P2', quote=self.rows[1]['quote'])
                if mode == 'quote': claim['evidence'][0]['quote'] = 'retrieval method P1'
                if mode == 'empty_quote': claim['evidence'][0]['quote'] = ''
                if mode == 'missing': claim['evidence'] = []
                if mode == 'duplicate': brief['evidence'].append(deepcopy(self.rows[0]))
                if mode == 'missing_row': brief['evidence'] = []
                if mode == 'other_paper': claim['evidence'] = [deepcopy(self.rows[1])]
                result = self.deliver(brief)
                self.assertIsNone(result['paper_notes'][0]['contribution'])
                self.assertEqual(result['status'], 'partial')

    def test_valid_inferences_and_hypotheses_are_retained(self):
        brief = deepcopy(self.brief)
        brief['paper_notes'][0]['model_inference'] = {'text': 'May apply', 'kind': 'model_inference', 'evidence': [deepcopy(self.rows[0])]}
        brief['research_hypotheses'] = [{'hypothesis': 'May work', 'kind': 'hypothesis_not_tested', 'experiment_status': 'proposed_not_run', 'evidence': [deepcopy(self.rows[0])]}]
        result = self.deliver(brief)
        self.assertIsNotNone(result['paper_notes'][0]['contribution'])
        self.assertIsNotNone(result['paper_notes'][0]['model_inference'])
        self.assertEqual(len(result['research_hypotheses']), 1)

    def test_changed_abstract_and_overflow_fail_closed(self):
        changed = deepcopy(self.papers)
        changed[0]['abstract'] = 'Changed abstract.'
        self.assertIsNone(self.deliver(self.brief, changed)['paper_notes'][0]['contribution'])
        brief = deepcopy(self.brief)
        brief['themes'] = [{'text': str(i), 'evidence': []} for i in range(MAX_FINDINGS + 3)]
        result = self.deliver(brief)
        self.assertEqual(result['themes'], [])
        self.assertEqual(result['status'], 'partial')

    def test_exception_fails_closed_without_logging_private_exception(self):
        with patch('research_specialist.evidence_critic', side_effect=RuntimeError('private secret text')):
            result = self.deliver(self.brief)
        self.assertIsNone(result['paper_notes'][0]['contribution'])
        self.assertEqual(result['status'], 'partial')
        self.assertNotIn('private secret text', json.dumps(self.store.events(self.chat_.session_id)))

    def test_stale_snapshot_and_metadata_only_are_rejected(self):
        brief = deepcopy(self.brief)
        brief['evidence'][0]['abstract_sha256'] = 'stale'
        self.assertIsNone(self.deliver(brief)['paper_notes'][0]['contribution'])
        papers = deepcopy(self.papers)
        papers[0]['metadata_only'] = True
        self.assertIsNone(self.deliver(self.brief, papers)['paper_notes'][0]['contribution'])

    def test_history_is_preserved_when_review_fails(self):
        self.chat_._event('fixture_history', {'value': 'prior'})
        before = deepcopy(self.store.events(self.chat_.session_id))
        with patch('research_specialist.evidence_critic', side_effect=RuntimeError('private')):
            self.deliver(self.brief)
        after = self.store.events(self.chat_.session_id)
        self.assertEqual(after[:len(before)], before)

    def test_reference_cannot_impersonate_another_paper_in_theme(self):
        brief = deepcopy(self.brief)
        forged = deepcopy(self.rows[0])
        forged.update(paper_id='P2', quote=self.rows[1]['quote'])
        brief['themes'] = [{'text': 'Cross-paper claim', 'evidence': [forged]}]
        self.assertEqual(self.deliver(brief)['themes'], [])

    def test_malformed_claim_and_rejected_review_fail_closed(self):
        from research_specialist import SpecialistResult
        brief = deepcopy(self.brief)
        brief['paper_notes'][0]['method'] = 'unsupported factual statement'
        self.assertIsNone(self.deliver(brief)['paper_notes'][0]['method'])
        with patch('research_specialist.evidence_critic', return_value=SpecialistResult(
                ticket_id='fixture', status='rejected', failure_code='abstract_snapshot_changed')):
            result = self.deliver(self.brief)
        self.assertIsNone(result['paper_notes'][0]['contribution'])
        self.assertEqual(result['status'], 'partial')

    def test_shared_reference_does_not_remove_valid_claim_or_duplicate_note(self):
        brief = deepcopy(self.brief)
        good = {'text': 'valid', 'kind': 'model_inference', 'evidence': [deepcopy(self.rows[0])]}
        bad = deepcopy(good)
        bad['evidence'][0]['quote'] = 'forged'
        brief['themes'] = [good, bad]
        brief['paper_notes'].append({'paper_id': 'P1', 'contribution': deepcopy(bad)})
        result = self.deliver(brief)
        self.assertEqual(result['themes'], [good])
        self.assertIsNotNone(result['paper_notes'][0]['contribution'])
        self.assertIsNone(result['paper_notes'][1]['contribution'])

    def test_finding_identity_is_checked_after_deepcopy(self):
        from research_specialist import evidence_critic, apply_evidence_findings, session_ticket, SpecialistError
        brief = deepcopy(self.brief)
        brief['paper_notes'][0]['contribution']['evidence'][0]['quote'] = 'forged'
        result = evidence_critic(session_ticket('evidence_critic', self.chat_, self.papers), self.papers, brief)
        self.assertIsNone(apply_evidence_findings(deepcopy(brief), result)[0]['paper_notes'][0]['contribution'])
        brief['paper_notes'][0]['contribution']['text'] = 'changed'
        with self.assertRaises(SpecialistError):
            apply_evidence_findings(brief, result)

    def test_quote_beyond_snapshot_and_missing_hash_are_rejected(self):
        from research_brief import ABSTRACT_LIMIT
        papers = deepcopy(self.papers)
        papers[0]['abstract'] = 'x' * ABSTRACT_LIMIT + ' hidden quote beyond limit'
        brief = deepcopy(self.brief)
        row = {'evidence_id': 'E1', 'paper_id': 'P1', 'quote': 'hidden quote beyond limit',
               'abstract_sha256': hashlib.sha256(papers[0]['abstract'][:ABSTRACT_LIMIT].encode()).hexdigest()}
        brief['evidence'] = [row]
        brief['paper_notes'][0]['contribution']['evidence'] = [deepcopy(row)]
        self.assertIsNone(self.deliver(brief, papers)['paper_notes'][0]['contribution'])
        brief = deepcopy(self.brief)
        del brief['evidence'][0]['abstract_sha256']
        self.assertIsNone(self.deliver(brief)['paper_notes'][0]['contribution'])

    def test_duplicate_references_and_invalid_labels_are_rejected(self):
        for mode in ('duplicate', 'inference', 'hypothesis_kind', 'hypothesis_status'):
            with self.subTest(mode=mode):
                brief = deepcopy(self.brief)
                note = brief['paper_notes'][0]
                if mode == 'duplicate':
                    note['contribution']['evidence'].append(deepcopy(self.rows[0]))
                elif mode == 'inference':
                    note['model_inference'] = {'text': 'x', 'kind': 'abstract_reported', 'evidence': [deepcopy(self.rows[0])]}
                else:
                    brief['research_hypotheses'] = [{'hypothesis': 'x', 'kind': 'hypothesis_not_tested', 'experiment_status': 'proposed_not_run', 'evidence': [deepcopy(self.rows[0])]}]
                    brief['research_hypotheses'][0]['kind' if mode == 'hypothesis_kind' else 'experiment_status'] = 'verified'
                result = self.deliver(brief)
                if mode == 'duplicate': self.assertIsNone(result['paper_notes'][0]['contribution'])
                elif mode == 'inference': self.assertIsNone(result['paper_notes'][0]['model_inference'])
                else: self.assertEqual(result['research_hypotheses'], [])

    def test_malformed_fallback_preserves_skipped_status(self):
        brief = {'status': 'skipped', 'reason': 'offline_demo', 'paper_notes': 1, 'gaps': 1}
        with patch('research_specialist.evidence_critic', side_effect=RuntimeError('private')):
            result = self.deliver(brief)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['reason'], 'offline_demo')
        self.assertEqual(result['paper_notes'], [])
        self.assertEqual(result['gaps'], [{'section': 'evidence', 'reason': 'evidence_review_unavailable'}])

    def test_legacy_deep_critic_never_dispatches(self):
        class LLM:
            def generate(self, *args, **kwargs):
                raise AssertionError('must not dispatch')
        ticket = make_ticket('claim_critic', max_requests=1, max_output_tokens=100, deadline=1)
        result = claim_critic(ticket, [{'claim_id': 'C1'}], llm=LLM(), deep=True)
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(result.usage.get('model_calls', 0), 0)
