"""Offline contracts for synthesis, provenance, proposals, reuse and safe DOM."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from common import Budget, GuideError
from research_brief import BRIEF_PROMPT_VERSION, brief_key, synthesize
from tools import CostMeter
from tests.brief_fixture import wire_output


TASK = {'topic': 'visual retrieval', 'purpose': 'design a controlled experiment',
        'reading_goal': 'frontier', 'target_count': 3, 'category': 'none',
        'minimum_category_count': 0, 'venue': '', 'year_from': None, 'year_to': None}
PAPERS = [
    {'id': 'P1', 'title': 'Method Alpha', 'url': 'https://example.test/alpha', 'year': 2025,
     'venue': 'CVPR', 'sources': ['arxiv'], 'abstract_source': 'arxiv',
     'abstract': 'We introduce Alpha using visual retrieval. Our approach is limited to static scenes.'},
    {'id': 'P2', 'title': 'Method Beta', 'url': 'https://example.test/beta', 'year': 2025,
     'venue': 'CVPR', 'sources': ['openalex'], 'abstract_source': 'openalex',
     'abstract': 'We introduce Beta using semantic retrieval. Our approach requires labeled queries.'},
    {'id': 'P3', 'title': 'Method Gamma', 'url': 'https://example.test/gamma', 'year': 2024,
     'venue': 'CVPR', 'sources': ['arxiv'], 'abstract_source': 'arxiv',
     'abstract': 'We introduce Gamma using hybrid retrieval. Our approach currently supports only images.'},
]


def valid_output():
    evidence, notes = [], []
    for index, paper in enumerate(PAPERS, 1):
        contribution, limitation = paper['abstract'].split('. ', 1)
        evidence.extend([
            {'evidence_id': f'E{index}a', 'paper_id': paper['id'], 'quote': contribution + '.'},
            {'evidence_id': f'E{index}b', 'paper_id': paper['id'], 'quote': limitation},
        ])
        notes.append({'paper_id': paper['id'],
            'contribution': {'text': f"Introduces {paper['title']}", 'evidence_ids': [f'E{index}a']},
            'method': {'text': 'Uses retrieval as stated in the abstract.', 'evidence_ids': [f'E{index}a']},
            'author_limitation': {'text': 'The authors state a restricted scope.', 'evidence_ids': [f'E{index}b']},
            'model_inference': None})
    return {'evidence': evidence, 'paper_notes': notes,
        'themes': [{'title': 'Retrieval variants', 'text': 'The selected abstracts describe different retrieval inputs.',
                    'evidence_ids': ['E1a', 'E2a']}],
        'open_questions': [{'question': 'Would the retrieval inputs behave differently under a shared protocol?',
                            'evidence_ids': ['E1a', 'E2a']}],
        'research_hypotheses': [{
            'hypothesis': f'Proposed retrieval variant {name} may reduce sensitivity to input changes.',
            'rationale': 'The abstracts motivate comparing different retrieval inputs.',
            'experiment': 'Propose a paired comparison with the same queries and compute budget.',
            'baseline': 'A fixed retrieval input, proposed as a control.',
            'measure': 'Proposed error and query sensitivity measures.',
            'falsifier': 'No consistent improvement under the shared protocol would refute this.',
            'evidence_ids': ['E1a', 'E2a'],
        } for name in ('A', 'B', 'C')],
        'reading_order': [{'paper_id': paper['id'], 'reason': 'Suggested order for comparing retrieval inputs.'}
                          for paper in PAPERS]}


class LLM:
    def __init__(self, output=None, error=None):
        self.output = valid_output() if output is None else output
        self.error = error
        self.calls = []

    def generate(self, stage, prompt, data, schema, validate):
        self.calls.append((stage, prompt, deepcopy(data), deepcopy(schema)))
        if self.error:
            raise self.error
        value = wire_output(deepcopy(self.output), data)
        validate(value)
        return value


class ResearchBriefTests(unittest.TestCase):
    def test_single_bounded_stage_types_claims_and_keeps_inputs_immutable(self):
        llm, meter = LLM(), CostMeter()
        before = deepcopy((TASK, PAPERS))
        result = synthesize(llm, TASK, PAPERS, meter, model='fixture-model')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0][0], 'synthesis')
        self.assertEqual((TASK, PAPERS), before)
        self.assertEqual(result['input_key'], brief_key(TASK, PAPERS, 'fixture-model'))
        self.assertEqual(result['source_level'], 'abstract_only')
        self.assertEqual(result['verification'], 'quote_match_only_not_semantic_proof')
        self.assertEqual(meter.summary()['by_action']['synthesis']['settled'], 1.0)
        note = result['paper_notes'][0]
        self.assertEqual(note['contribution']['kind'], 'abstract_reported')
        self.assertEqual(note['contribution']['evidence'][0]['paper_id'], 'P1')
        self.assertEqual(note['author_limitation']['kind'], 'author_reported_limitation')
        self.assertTrue(all(item['kind'] == 'hypothesis_not_tested' and
                            item['experiment_status'] == 'proposed_not_run'
                            for item in result['research_hypotheses']))
        self.assertEqual(result['themes'][0]['kind'], 'model_inference')

    def test_dynamic_schema_has_only_selected_paper_ids(self):
        llm = LLM()
        synthesize(llm, TASK, PAPERS)
        schema = llm.calls[0][3]
        for section in ('paper_notes', 'reading_order'):
            self.assertEqual(schema['properties'][section]['items']['properties']['paper_id']['enum'], ['P1', 'P2', 'P3'])
        self.assertNotIn('evidence', schema['properties'])
        self.assertEqual(schema['properties']['evidence_ids']['items']['enum'],
                         [item['evidence_id'] for item in llm.calls[0][2]['evidence_bank']])

    def test_unknown_ids_fabricated_quotes_and_unused_bad_evidence_fail_closed(self):
        for change in ('unknown-paper', 'fake-quote', 'case-changed-quote', 'tiny-quote'):
            raw = valid_output()
            if change == 'unknown-paper': raw['evidence'][0]['paper_id'] = 'P99'
            elif change == 'fake-quote': raw['evidence'][0]['quote'] = 'This nonexistent result has been confirmed.'
            elif change == 'case-changed-quote': raw['evidence'][0]['quote'] = raw['evidence'][0]['quote'].upper()
            else: raw['evidence'][0]['quote'] = 'We'
            with self.subTest(change=change):
                meter, llm = CostMeter(), LLM(raw)
                result = synthesize(llm, TASK, PAPERS, meter)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['paper_notes'], [])
                self.assertEqual(meter.summary()['settled'], 0.0)
                self.assertEqual(len(llm.calls), 1)

    def test_repeated_selection_is_bookkeeping_and_never_becomes_a_failure(self):
        raw = valid_output()
        raw['evidence'].append(deepcopy(raw['evidence'][0]))
        result = synthesize(LLM(raw), TASK, PAPERS)
        self.assertIn(result['status'], ('ready', 'partial'))
        ids = [item['evidence_id'] for item in result['evidence']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_foreign_quote_drops_the_claim_but_keeps_the_rest_of_the_brief(self):
        raw = valid_output()
        raw['paper_notes'][0]['method']['evidence_ids'] = ['E2a']
        result = synthesize(LLM(raw), TASK, PAPERS)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['paper_notes'][0]['method'])
        self.assertTrue(any(gap.get('reason') == 'evidence_from_other_paper'
                            for gap in result['gaps']))
        self.assertTrue(all(ref['paper_id'] == note['paper_id']
                            for note in result['paper_notes'] for field in ('contribution', 'method')
                            for ref in ((note[field] or {}).get('evidence') or [])))

    def test_claim_must_cite_its_own_paper_and_known_evidence(self):
        for refs in (['unknown-evidence'], []):
            raw = valid_output()
            raw['paper_notes'][0]['method']['evidence_ids'] = refs
            with self.subTest(refs=refs):
                self.assertEqual(synthesize(LLM(raw), TASK, PAPERS)['status'], 'failed')
        raw = valid_output()
        raw['paper_notes'][0]['method']['evidence_ids'] = ['E1a', 'E1a']
        repeated = synthesize(LLM(raw), TASK, PAPERS)
        self.assertIn(repeated['status'], ('ready', 'partial'))
        self.assertEqual(len(repeated['paper_notes'][0]['method']['evidence']), 1)

    def test_generic_quote_cannot_support_invented_factual_numbers(self):
        raw = valid_output()
        raw['paper_notes'][0]['contribution']['text'] = 'Achieves 99.9% accuracy with 8 GPUs.'
        self.assertEqual(synthesize(LLM(raw), TASK, PAPERS)['status'], 'failed')

    def test_author_limitation_is_unknown_when_source_has_no_explicit_scope_cue(self):
        raw = valid_output()
        raw['paper_notes'][0]['author_limitation'] = {
            'text': 'The method may not generalize.', 'evidence_ids': ['E1a']}
        result = synthesize(LLM(raw), TASK, PAPERS)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['paper_notes'][0]['author_limitation'])
        self.assertTrue(any(gap['section'] == 'author_limitation' for gap in result['gaps']))

    def test_inference_is_separate_from_author_reported_limitation(self):
        raw = valid_output()
        raw['paper_notes'][0]['author_limitation'] = None
        raw['paper_notes'][0]['model_inference'] = {
            'text': 'Dynamic scenes may need a separate study.', 'evidence_ids': ['E1b']}
        result = synthesize(LLM(raw), TASK, PAPERS)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['paper_notes'][0]['model_inference']['kind'], 'model_inference')
        self.assertIsNone(result['paper_notes'][0]['author_limitation'])

    def test_background_problem_is_not_laundered_into_authors_own_limitation(self):
        papers, raw = deepcopy(PAPERS), valid_output()
        background = 'Existing methods are limited by static representations.'
        papers[0]['abstract'] += ' ' + background
        raw['evidence'][1]['quote'] = background
        result = synthesize(LLM(raw), TASK, papers)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['paper_notes'][0]['author_limitation'])

    def test_cross_paper_theme_needs_distinct_papers_and_cannot_rank_performance(self):
        for refs, text in ((['E1a', 'E1b'], 'Different retrieval inputs.'),
                           (['E1a', 'E2a'], 'Alpha outperforms Beta.'),
                           (['E1a', 'E2a'], '实验结果证明 Alpha 优于 Beta。')):
            raw = valid_output()
            raw['themes'][0].update(evidence_ids=refs, text=text)
            with self.subTest(text=text, refs=refs):
                self.assertEqual(synthesize(LLM(raw), TASK, PAPERS)['status'], 'failed')

    def test_hypotheses_are_not_allowed_to_claim_the_experiment_already_ran(self):
        for field, claim in (('hypothesis', 'Experiments show this hypothesis is true.'),
                             ('experiment', '实验结果表明这一方案有效。')):
            raw = valid_output()
            raw['research_hypotheses'][0][field] = claim
            with self.subTest(field=field):
                self.assertEqual(synthesize(LLM(raw), TASK, PAPERS)['status'], 'failed')

    def test_missing_experiment_control_measure_or_falsifier_is_rejected(self):
        for field in ('baseline', 'measure', 'falsifier'):
            raw = valid_output()
            raw['research_hypotheses'][0][field] = ''
            with self.subTest(field=field):
                self.assertEqual(synthesize(LLM(raw), TASK, PAPERS)['status'], 'failed')

    def test_fewer_grounded_hypotheses_degrades_without_filling_with_fiction(self):
        raw = valid_output()
        raw['research_hypotheses'] = raw['research_hypotheses'][:1]
        result = synthesize(LLM(raw), TASK, PAPERS)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['research_hypotheses']), 1)
        gap = next(item for item in result['gaps'] if item['section'] == 'research_hypotheses')
        self.assertEqual((gap['found'], gap['requested']), (1, 3))

    def test_duplicate_hypotheses_cannot_fill_the_requested_three(self):
        raw = valid_output()
        raw['research_hypotheses'][1] = deepcopy(raw['research_hypotheses'][0])
        self.assertEqual(synthesize(LLM(raw), TASK, PAPERS)['status'], 'failed')

    def test_reading_order_rejects_unknown_and_duplicate_ids(self):
        for identity in ('P1', 'P99'):
            raw = valid_output()
            raw['reading_order'][1]['paper_id'] = identity
            with self.subTest(identity=identity):
                self.assertEqual(synthesize(LLM(raw), TASK, PAPERS)['status'], 'failed')

    def test_missing_reading_order_and_notes_are_explicit_partial_results(self):
        raw = valid_output()
        raw['reading_order'] = raw['reading_order'][:2]
        raw['paper_notes'] = raw['paper_notes'][:2]
        result = synthesize(LLM(raw), TASK, PAPERS)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['reading_order']), 2)
        self.assertEqual(len(result['paper_notes']), 3)
        self.assertEqual(result['paper_notes'][-1]['paper_id'], 'P3')
        self.assertIsNone(result['paper_notes'][-1]['method'])

    def test_metadata_only_and_empty_selection_skip_without_a_model_or_cost(self):
        for papers in ([], [{**PAPERS[0], 'metadata_only': True}], [{**PAPERS[0], 'abstract': ''}]):
            llm, meter = LLM(), CostMeter()
            with self.subTest(papers=papers):
                result = synthesize(llm, TASK, papers, meter)
                self.assertEqual(result['status'], 'skipped')
                self.assertEqual(llm.calls, [])
                self.assertEqual(meter.entries, [])

    def test_source_conflict_and_input_truncation_are_disclosed(self):
        papers = deepcopy(PAPERS)
        papers[0]['provenance_conflict'] = True
        papers[1]['abstract'] += ' More source text.' * 1000
        llm = LLM()
        result = synthesize(llm, TASK, papers)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any(item['reason'] == 'conflicting_source_abstracts' for item in result['gaps']))
        self.assertTrue(any(item['reason'] == 'abstract_input_truncated' for item in result['gaps']))
        packet = llm.calls[0][2]
        self.assertNotIn('abstract', packet['papers'][1])
        self.assertTrue(all(item['quote'] in papers[1]['abstract'][:7000]
                            for item in packet['evidence_bank'] if item['paper_id'] == 'P2'))

    def test_cancelled_or_expired_budget_does_not_start_a_model_request(self):
        cancelled, expired = Budget(60), Budget(0)
        cancelled.cancel_token.request()
        for budget, reason in ((cancelled, 'user_cancelled'), (expired, 'budget_exhausted')):
            llm, meter = LLM(), CostMeter()
            with self.subTest(reason=reason):
                result = synthesize(llm, TASK, PAPERS, meter, budget)
                self.assertEqual(result['status'], 'skipped')
                self.assertEqual(result['reason'], reason)
                self.assertEqual(llm.calls, [])
                self.assertEqual(meter.entries, [])

    def test_model_failure_is_bounded_and_does_not_expose_raw_provider_errors(self):
        llm, meter = LLM(error=GuideError('provider error with secret-do-not-echo')), CostMeter()
        result = synthesize(llm, TASK, PAPERS, meter)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(llm.calls), 1)
        self.assertNotIn('secret-do-not-echo', json.dumps(result))
        self.assertEqual(meter.summary()['refunded'], 1.0)

    def test_invalid_input_and_unbounded_selection_do_not_start_a_request(self):
        for task, papers in (([], PAPERS), (TASK, PAPERS * 2), (TASK, [PAPERS[0], PAPERS[0]])):
            llm = LLM()
            with self.subTest(task=task, papers=papers):
                self.assertEqual(synthesize(llm, task, papers)['status'], 'failed')
                self.assertEqual(llm.calls, [])

    def test_cache_key_tracks_semantics_selection_evidence_and_metadata(self):
        original = brief_key(TASK, PAPERS, 'model-a')
        for field, value in (('topic', 'another topic'), ('purpose', 'another purpose'),
                             ('reading_goal', 'orientation'), ('target_count', 4), ('venue', 'ACL')):
            self.assertNotEqual(original, brief_key({**TASK, field: value}, PAPERS, 'model-a'))
        for field, value in (('abstract', PAPERS[0]['abstract'].upper()), ('venue', 'ACL'),
                             ('year', 2026), ('abstract_source', 'another source'),
                             ('provenance_conflict', True)):
            papers = deepcopy(PAPERS)
            papers[0][field] = value
            self.assertNotEqual(original, brief_key(TASK, papers, 'model-a'))
        self.assertNotEqual(original, brief_key(TASK, PAPERS[::-1], 'model-a'))
        self.assertNotEqual(original, brief_key(TASK, PAPERS[:2], 'model-a'))
        self.assertNotEqual(original, brief_key(TASK, PAPERS, 'model-b'))
        self.assertNotEqual(original, brief_key(TASK, PAPERS, 'model-a', BRIEF_PROMPT_VERSION + '-next'))

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the safe DOM renderer check.')
    def test_shared_renderer_uses_safe_dom_and_separates_proposals_from_facts(self):
        result = synthesize(LLM(), TASK, PAPERS)
        result['paper_notes'][0]['contribution']['text'] = '<img src=x onerror=alert(1)>'
        papers = deepcopy(PAPERS)
        papers[0]['url'] = 'javascript:alert(1)'
        script = r'''
          const fs = require('fs'), vm = require('vm');
          const packet = JSON.parse(fs.readFileSync(0, 'utf8'));
          class Element {
            constructor(tag){this.tag=tag;this.children=[];this.attributes={};this.dataset={};this._text='';}
            set textContent(value){this._text=String(value);}
            get textContent(){return this._text+this.children.map(x=>x.textContent||'').join(' ');}
            set innerHTML(value){throw new Error('innerHTML is forbidden');}
            append(...nodes){this.children.push(...nodes);}
            setAttribute(name,value){this.attributes[name]=String(value);}
          }
          const context={window:{addEventListener(){}},URL,Map,Set,Object,Array,Number,
            document:{createElement:tag=>new Element(tag)}};
          vm.runInNewContext(fs.readFileSync(packet.path,'utf8'),context);
          const root=context.window.ScholarGuideResearch.render(packet.brief,{papers:packet.papers});
          const walk=n=>[n,...n.children.flatMap(walk)];
          const all=walk(root);
          if(all.some(n=>n.href && !/^https?:/.test(n.href)))throw new Error('unsafe URL');
          if(!root.textContent.includes('<img src=x onerror=alert(1)>'))throw new Error('text was not preserved');
          for(const label of ['尚未验证','拟议实验','模型推论','不证明','摘要']){
            if(!root.textContent.includes(label))throw new Error('missing boundary: '+label);
          }
          if(!all.some(n=>n.href==='https://example.test/beta'))throw new Error('real source link missing');
          process.stdout.write('safe');
        '''
        renderer = Path(__file__).resolve().parents[1] / 'web' / 'research.js'
        completed = subprocess.run([shutil.which('node'), '-e', script], input=json.dumps({
            'path': str(renderer), 'brief': result, 'papers': papers}),
            text=True, encoding='utf-8', capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, 'safe')


if __name__ == '__main__':
    unittest.main()
