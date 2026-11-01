"""Strict v3 ID-only synthesis wire, with deterministic source quote expansion."""
from copy import deepcopy
import json
import unittest

from common import GuideError
from research_brief import (ABSTRACT_LIMIT, BRIEF_PROMPT_VERSION, MAX_EVIDENCE_PER_PAPER,
                            _papers, _validate, evidence_bank, synthesize, validated_cached_brief)
from tests.brief_fixture import wire_output
from tests.test_research_brief import LLM, PAPERS, TASK, valid_output
from tools import CostMeter


class EvidenceBankTests(unittest.TestCase):
    def test_exact_offsets_preserve_unicode_math_case_and_long_sentence(self):
        text = 'We use $\\alpha_2$ with “Quoted Text” and 99.9% coverage. '
        text += '长句原文包含大小写ABC以及 LaTeX $\\sigma$。'
        text += 'A' * 733 + '. Tail sentence reports a specific limitation.'
        papers = _papers([{**PAPERS[0], 'abstract': text}])
        bank, gaps = evidence_bank(papers)
        self.assertTrue(bank)
        self.assertEqual(gaps, [])
        self.assertEqual((bank, gaps), evidence_bank(papers))
        for span in bank:
            self.assertEqual(span['quote'], text[span['start']:span['end']])
            self.assertGreaterEqual(len(' '.join(span['quote'].split())), 12)
            self.assertLessEqual(len(span['quote']), 350)
            self.assertLessEqual(len(span['evidence_id']), 40)
        self.assertTrue(any('99.9%' in span['quote'] for span in bank))
        self.assertTrue(any('“Quoted Text”' in span['quote'] for span in bank))
        changed = _papers([{**PAPERS[0], 'abstract': text.upper()}])
        self.assertNotEqual(bank[0]['evidence_id'], evidence_bank(changed)[0][0]['evidence_id'])

    def test_model_only_selects_ids_and_server_expands_unchanged_source_quotes(self):
        llm = LLM()
        result = synthesize(llm, TASK, PAPERS)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(len(llm.calls), 1)
        packet, schema = llm.calls[0][2:]
        self.assertTrue(all('abstract' not in paper for paper in packet['papers']))
        self.assertNotIn('evidence', schema['properties'])
        self.assertEqual(schema['properties']['evidence_ids']['maxItems'], 15)
        bank = {item['evidence_id']: item for item in packet['evidence_bank']}
        for evidence in result['evidence']:
            self.assertEqual(evidence['quote'], bank[evidence['evidence_id']]['quote'])
            self.assertEqual(evidence['paper_id'], bank[evidence['evidence_id']]['paper_id'])
        self.assertTrue(validated_cached_brief(result, TASK, PAPERS, ''))

    def test_single_paper_discards_invalid_cross_paper_theme_but_keeps_grounded_notes(self):
        raw = valid_output()
        raw['evidence'] = raw['evidence'][:2]
        raw['paper_notes'] = raw['paper_notes'][:1]
        raw['themes'][0]['evidence_ids'] = ['E1a', 'E1b']
        raw['open_questions'][0]['evidence_ids'] = ['E1a']
        for hypothesis in raw['research_hypotheses']:
            hypothesis['evidence_ids'] = ['E1a']
        raw['reading_order'] = raw['reading_order'][:1]
        llm = LLM(raw)
        task = {**TASK, 'target_count': 1}
        result = synthesize(llm, task, PAPERS[:1], model='fixture')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0][3]['properties']['themes']['maxItems'], 0)
        self.assertIn('return themes as an empty array', llm.calls[0][1])
        self.assertEqual(result['themes'], [])
        self.assertEqual(result['paper_notes'][0]['contribution']['evidence'][0]['paper_id'], 'P1')
        self.assertIn({'section': 'themes', 'reason': 'insufficient_distinct_paper_evidence',
                       'discarded_model_themes': 1}, result['gaps'])
        self.assertTrue(validated_cached_brief(result, task, PAPERS[:1], 'fixture'))

    def test_multiple_selected_papers_with_only_one_usable_abstract_cannot_form_theme(self):
        raw = valid_output()
        raw['evidence'] = raw['evidence'][:2]
        raw['paper_notes'] = raw['paper_notes'][:1]
        raw['themes'][0]['evidence_ids'] = ['E1a', 'E1b']
        raw['open_questions'] = []
        raw['research_hypotheses'] = []
        raw['reading_order'] = raw['reading_order'][:1]
        papers = [PAPERS[0], {**PAPERS[1], 'abstract': '', 'metadata_only': True}]
        llm = LLM(raw)
        result = synthesize(llm, {**TASK, 'target_count': 2}, papers)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(llm.calls[0][3]['properties']['themes']['maxItems'], 0)
        self.assertEqual(result['themes'], [])
        self.assertTrue(any(gap['reason'] == 'insufficient_distinct_paper_evidence'
                            for gap in result['gaps']))

    def test_single_source_does_not_relax_other_evidence_checks(self):
        raw = valid_output()
        raw['evidence'] = raw['evidence'][:2]
        raw['paper_notes'] = raw['paper_notes'][:1]
        raw['themes'] = []
        raw['open_questions'] = []
        raw['research_hypotheses'] = []
        raw['reading_order'] = raw['reading_order'][:1]
        raw['paper_notes'][0]['method']['evidence_ids'] = ['invented-id']
        result = synthesize(LLM(raw), {**TASK, 'target_count': 1}, PAPERS[:1])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'unknown_or_missing_evidence_reference')
        self.assertEqual(result['paper_notes'], [])

    def test_tiny_trailing_source_fragment_is_retained_in_context(self):
        text = 'This source sentence establishes an explicit research contribution. z'
        bank, gaps = evidence_bank(_papers([{**PAPERS[0], 'abstract': text}]))
        self.assertEqual(gaps, [])
        self.assertEqual(bank[-1]['end'], len(text))
        self.assertEqual(bank[-1]['quote'], text)

    def test_generated_quote_fields_are_rejected_even_when_text_exists_in_source(self):
        class FreeQuoteLLM:
            def generate(self, stage, prompt, data, schema, validate):
                value = wire_output(valid_output(), data)
                value['evidence'] = [{'quote': PAPERS[0]['abstract']}]
                validate(value)
                return value
        result = synthesize(FreeQuoteLLM(), TASK, PAPERS)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'invalid_brief_structure')
        self.assertEqual(result['paper_notes'], [])

    def test_unknown_bank_id_and_wrong_paper_have_specific_safe_diagnostics(self):
        class UnknownLLM:
            def generate(self, stage, prompt, data, schema, validate):
                value = wire_output(valid_output(), data)
                value['evidence_ids'][0] = 'not-supplied-by-harness'
                validate(value)
                return value
        result = synthesize(UnknownLLM(), TASK, PAPERS)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'unknown_evidence_bank_id')
        self.assertIn('evidence_id', result['failure_detail'])

        class WrongPaperLLM:
            def generate(self, stage, prompt, data, schema, validate):
                value = wire_output(valid_output(), data)
                other = next(item['evidence_id'] for item in data['evidence_bank']
                             if item['paper_id'] == 'P2')
                value['paper_notes'][0]['method']['evidence_ids'] = [other]
                validate(value)
                return value
        result = synthesize(WrongPaperLLM(), TASK, PAPERS)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['paper_notes'][0]['method'])
        self.assertTrue(any(gap.get('reason') == 'evidence_from_other_paper'
                            for gap in result['gaps']))

    def test_claim_only_reference_to_a_real_bank_span_is_expanded_not_rejected(self):
        captured = {}
        class UndeclaredLLM:
            def generate(self, stage, prompt, data, schema, validate):
                value = wire_output(valid_output(), data)
                cited = value['themes'][0]['evidence_ids'][1]
                captured['cited'] = cited
                captured['bank'] = {item['evidence_id']: item for item in data['evidence_bank']}
                value['evidence_ids'] = [identity for identity in value['evidence_ids']
                                         if identity != cited]
                validate(value)
                return value
        result = synthesize(UndeclaredLLM(), TASK, PAPERS)
        self.assertIn(result['status'], ('ready', 'partial'))
        self.assertNotIn(captured['cited'], [])
        materialized = {item['evidence_id']: item for item in result['evidence']}
        self.assertIn(captured['cited'], materialized)
        self.assertEqual(materialized[captured['cited']]['quote'],
                         captured['bank'][captured['cited']]['quote'])
        self.assertTrue(validated_cached_brief(result, TASK, PAPERS, ''))

    def test_fabricated_claim_reference_still_fails_after_one_repair(self):
        class FabricatedLLM:
            def generate(self, stage, prompt, data, schema, validate):
                value = wire_output(valid_output(), data)
                value['themes'][0]['evidence_ids'] = ['E-not-in-bank']
                validate(value)
                return value
        result = synthesize(FabricatedLLM(), TASK, PAPERS)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'unknown_or_missing_evidence_reference')

    def test_omitted_bank_spans_are_explicit_and_no_unbounded_output_pool_is_allowed(self):
        papers = deepcopy(PAPERS)
        papers[0]['abstract'] += ' Short independent fact.' * 600
        bank, gaps = evidence_bank(_papers(papers))
        self.assertEqual(sum(item['paper_id'] == 'P1' for item in bank), MAX_EVIDENCE_PER_PAPER)
        self.assertTrue(any(gap['reason'] == 'evidence_bank_truncated' for gap in gaps))
        self.assertLessEqual(len(bank), MAX_EVIDENCE_PER_PAPER * len(papers))
        self.assertTrue(all(item['end'] <= ABSTRACT_LIMIT for item in bank))
        output = valid_output()
        class TooManyLLM:
            def generate(self, stage, prompt, data, schema, validate):
                value = wire_output(output, data)
                value['evidence_ids'] = [item['evidence_id'] for item in data['evidence_bank'][:16]]
                validate(value)
                return value
        self.assertEqual(synthesize(TooManyLLM(), TASK, papers)['status'], 'failed')

    def test_old_cache_version_is_not_silently_upgraded_and_tampered_quotes_fail(self):
        result = synthesize(LLM(), TASK, PAPERS)
        old = deepcopy(result)
        old['prompt_version'] = 'research-brief-v2'
        self.assertIsNone(validated_cached_brief(old, TASK, PAPERS, ''))
        tampered = deepcopy(result)
        tampered['evidence'][0]['quote'] = 'Invented source text is never fuzzy matched.'
        self.assertIsNone(validated_cached_brief(tampered, TASK, PAPERS, ''))
        self.assertEqual(BRIEF_PROMPT_VERSION, 'research-brief-v4-single-source-boundary')

    def test_too_short_source_skips_without_a_request_or_reservation(self):
        llm, meter = LLM(), CostMeter()
        result = synthesize(llm, TASK, [{**PAPERS[0], 'abstract': 'Too short.'}], meter)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['reason'], 'no_usable_evidence_spans')
        self.assertEqual(llm.calls, [])
        self.assertEqual(meter.entries, [])

    def test_strict_legacy_quote_validator_still_rejects_fabrication(self):
        value = valid_output()
        value['evidence'][0]['quote'] = 'An invented research finding has been verified.'
        with self.assertRaises(GuideError):
            _validate(value, _papers(PAPERS))


if __name__ == '__main__':
    unittest.main()
