"""Behavior tests for budgets, evidence, selection and the formerly broken lookup path."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Budget, Config, GuideError, Paper, Request
from demo import DemoLLM, DemoSource, demo_run
from llm import JsonLLM
from source import AcademicSource, merge_papers, normalize_doi
from vendor.gptr_arxiv import ArxivSearch, arxiv_id
from workflow import grounded_assessments, run


class WorkflowTests(unittest.TestCase):
    def test_followup_satisfies_final_list_not_just_pool(self):
        result = demo_run('followup')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['metrics']['search_rounds'], 2)
        self.assertEqual(len(result['papers']), 5)
        self.assertEqual(sum(p['category_status'] == 'yes' for p in result['papers']), 2)
        self.assertEqual(len({p['id'] for p in result['papers']}), 5)
        self.assertEqual(result['mode'], 'demo')
        self.assertEqual(result['metrics']['llm_calls'], 0)

    def test_explicit_goal_hint_overrides_planner(self):
        result = run('Recommend five RAG papers including one evaluation paper.', Config(),
                     'S', source=DemoSource(), llm=DemoLLM(), goal_hint='frontier')
        self.assertEqual(result['request']['reading_goal'], 'frontier')
        self.assertTrue(any(step.get('action') == 'intent' for step in result['trace']))

    def test_sufficient_stops_after_first_search(self):
        result = demo_run('satisfied')
        self.assertEqual(result['metrics']['search_rounds'], 1)
        self.assertEqual(result['status'], 'complete')

    def test_single_pass_is_real_ablation(self):
        result = demo_run('followup', 'S')
        self.assertEqual(result['metrics']['search_rounds'], 1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['gaps'][0]['requirement'], 'evaluation')

    def test_insufficient_never_invents_papers_or_loops(self):
        result = demo_run('insufficient')
        self.assertEqual(len(result['papers']), 2)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['metrics']['search_rounds'], 2)

    def test_network_failure_is_visible_and_bounded(self):
        class FailedSource(DemoSource):
            def search(self, query, limit=10):
                self.calls += 1
                raise GuideError('API temporarily unavailable')
        src = FailedSource()
        result = run('test', Config(), source=src, llm=DemoLLM())
        self.assertEqual(src.calls, 2)
        self.assertEqual(result['status'], 'no_results')
        self.assertEqual(len(result['warnings']), 2)

    def test_failure_after_first_round_preserves_partial_results(self):
        class BrokenRefine(DemoLLM):
            def generate(self, stage, *args):
                if stage == 'refine':
                    raise GuideError('Model API unavailable')
                return super().generate(stage, *args)
        result = run('test', Config(), source=DemoSource(), llm=BrokenRefine())
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['papers']), 5)
        self.assertIn('Model API', result['stop_reason'])

    def test_unsupported_request_is_not_silently_relaxed(self):
        src = DemoSource()
        result = run('papers with code', Config(), source=src, llm=DemoLLM(),
                     request_data={'topic': 'RAG', 'unsupported': ['with code']})
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(src.calls, 0)

    def test_expired_budget_stops_before_search(self):
        budget = Budget(-1)
        result = run('test', Config(), source=DemoSource(), llm=DemoLLM(), budget=budget)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['metrics']['search_rounds'], 0)

    def test_fabricated_quote_is_excluded(self):
        p = Paper('id', 'Title', 'This is the real abstract text.', 'https://example.com')
        rows = [{'paper_id': 'id', 'relevance': 2, 'category_status': 'yes',
                 'evidence_quote': 'The paper released public source code.', 'reason': 'It has code.'}]
        checked = grounded_assessments(rows, [p], 'evaluation')['id']
        self.assertFalse(checked['evidence_verified'])
        self.assertEqual(checked['relevance'], 0)
        self.assertEqual(checked['category_status'], 'unknown')

    def test_credential_reference_is_explicit_and_never_serialized(self):
        import os
        from common import resolve_api_key
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'auth.json'
            path.write_text(json.dumps({'deepseek': {'type':'api_key','key':'test-secret-value'}}))
            with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': directory}):
                self.assertEqual(resolve_api_key('@pi:deepseek'), 'test-secret-value')
                with self.assertRaises(GuideError):
                    resolve_api_key('@pi:missing')
            config = Config(api_key='test-secret-value')
            self.assertNotIn('test-secret-value', json.dumps(config.public()))
            self.assertNotIn('test-secret-value', repr(config))

    def test_llm_json_repair_once(self):
        llm = JsonLLM(Config(), Budget(10))
        with patch.object(llm, '_call', side_effect=['not json', '{"query":"RAG benchmark"}']) as call:
            output = llm.generate('refine', '', {}, {}, lambda x: None)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(output['query'], 'RAG benchmark')
        with patch.object(llm, '_call', return_value='not json') as call:
            with self.assertRaises(GuideError):
                llm.generate('refine', '', {}, {}, lambda x: None)
        self.assertEqual(call.call_count, 2)


class SourceTests(unittest.TestCase):
    def test_arxiv_id_normalization(self):
        self.assertEqual(arxiv_id('https://arxiv.org/pdf/2005.11401v3.pdf'), '2005.11401')
        self.assertEqual(arxiv_id('arxiv:hep-th/9901001v2'), 'hep-th/9901001')
        self.assertEqual(arxiv_id('https://evil.example/2005.11401'), '')
        self.assertEqual(normalize_doi('https://random.example/paper'), '')

    def test_id_lookup_uses_id_list_and_rejects_wrong_identity(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>https://arxiv.org/abs/2311.09476v1</id><title>Wrong paper</title>
        <summary>Abstract</summary></entry></feed>'''
        seen = []
        client = ArxivSearch('', lambda url: seen.append(url) or xml)
        self.assertEqual(client.lookup('2005.11401'), [])
        self.assertIn('id_list=2005.11401', seen[0])
        self.assertNotIn('search_query', seen[0])

    def test_doi_arxiv_bridge_and_cross_year_version_dedup(self):
        a = Paper('doi:10.1234/test', 'Same title', 'Abstract', 'url', year=2024,
                  doi='10.1234/test', sources=['openalex'], from_query=['q1'])
        b = Paper('arxiv:2401.12345', 'Same title!', 'Longer abstract', 'url', year=2025,
                  arxiv_id='2401.12345v2', sources=['arxiv'], from_query=['q2'])
        c = Paper('third', 'Same title', 'Abstract', 'url', doi='10.1234/test', arxiv_id='2401.12345',
                  sources=['crossref'], from_query=['q3'])
        merged = merge_papers([a, b, c])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].from_query, ['q1', 'q2', 'q3'])
        self.assertEqual(a.from_query, ['q1'])  # originals must not be mutated

    def test_distinct_known_ids_are_not_merged_by_title(self):
        a = Paper('arxiv:2401.11111', 'Same title', '', '', arxiv_id='2401.11111')
        b = Paper('arxiv:2401.22222', 'Same title', '', '', arxiv_id='2401.22222')
        self.assertEqual(len(merge_papers([a, b])), 2)

    def test_cache_is_explicit_and_bypassed_for_online_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(cache_dir=Path(directory))
            budget = Budget(20)
            source = AcademicSource(config, budget)
            p = Paper('arxiv:2401.12345', 'Title', 'Abstract', 'https://arxiv.org/abs/2401.12345')
            with patch.object(source, '_arxiv', return_value=[p]) as fetch:
                source.search('query')
                source.search('query')
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(budget.cache_hits, 1)
                config.cache = False
                source.search('query')
                self.assertEqual(fetch.call_count, 2)


if __name__ == '__main__':
    unittest.main()
