"""Router tests: source selection, query variants, ranking and budget edges.

The fake source implements the same ``search_batch`` contract as
``AcademicSource`` but records calls and returns deterministic batches.
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Config, GuideError, Paper
from retrieval import RetrievalRouter
from source import DEFAULT_MULTI, SearchBatch, SourceRun
from tools import CostMeter
from demo import DemoLLM
from workflow import run as run_workflow


def paper(idx, title='RAG paper', abstract='retrieval augmented generation',
          year=2024, venue='ACL', authority=0.8, oa='', sources=None, metadata=False):
    return Paper(f'p{idx}', title, abstract, 'https://example.org/p' + str(idx),
                 year=year, venue=venue, authority=authority, open_access_url=oa,
                 sources=sources or ['arxiv'], source=(sources or ['arxiv'])[0],
                 abstract_source='arxiv' if abstract else '', metadata_only=metadata)


class FakeSource:
    def __init__(self, batches=None, providers=None, available=('deepseek',)):
        self.providers = list(providers or DEFAULT_MULTI)
        self.batches = list(batches or [])
        self.available_names = set(available)
        self.calls = []
        self.deepseek_calls = 0

    def available(self, name):
        return name in self.available_names

    def search_batch(self, query, limit=10, providers=None, filters=None):
        names = list(providers or self.providers)
        self.calls.append({'query': query, 'providers': names, 'filters': filters})
        if names == ['deepseek']:
            self.deepseek_calls += 1
        if self.batches:
            items = self.batches.pop(0)
            if isinstance(items, Exception):
                raise items
        else:
            items = []
        runs = [SourceRun(name, 'ok' if items else 'empty', len(items)) for name in names]
        return SearchBatch(query, names, list(items), runs)


def router(source, source_name='multi', **kwargs):
    return RetrievalRouter(Config(source=source_name, cache=False, **kwargs), source)


class PlanTests(unittest.TestCase):
    def test_single_source_never_expands(self):
        src = FakeSource(providers=['arxiv'])
        plan = router(src, 'arxiv').plan({'topic': 'rag'}, 'rag')
        self.assertTrue(plan.single_source)
        self.assertEqual(plan.providers, ['arxiv'])
        self.assertEqual(plan.queries, ['rag'])
        self.assertFalse(plan.deepseek_allowed)

    def test_topic_only_uses_scholarly_core(self):
        src = FakeSource(available=())
        plan = router(src).plan({'topic': 'retrieval augmented generation'}, 'retrieval augmented generation')
        self.assertEqual(plan.providers, ['arxiv', 'openalex', 'semantic_scholar'])
        self.assertNotIn('acl_anthology', plan.providers)
        self.assertNotIn('dblp', plan.providers)
        self.assertEqual(len(plan.queries), 1)

    def test_saved_priority_orders_preset_sources(self):
        class PriorityStore:
            @staticmethod
            def enabled_sources():
                return ['openalex', 'lune', 'semantic_scholar', 'arxiv']
        src = FakeSource(providers=['lune', 'arxiv', 'openalex', 'semantic_scholar'],
                         available=('lune',))
        src.store = PriorityStore()
        plan = router(src).plan({'topic': 'retrieval augmented generation'},
                                'retrieval augmented generation')
        self.assertEqual(plan.providers[:3], ['openalex', 'lune', 'semantic_scholar'])

    def test_evaluation_intent_adds_query_variant_and_venue_oa(self):
        src = FakeSource(available=())
        plan = router(src).plan({'topic': 'RAG', 'category': 'evaluation',
                                 'minimum_category_count': 2}, 'RAG')
        self.assertIn('openreview', plan.providers)
        self.assertIn('acl_anthology', plan.providers)
        self.assertEqual(len(plan.queries), 2)
        self.assertIn('evaluation benchmark', plan.queries[1])
        self.assertTrue(any('category' in reason for reason in plan.reasons))

    def test_explicit_venue_intent_adds_dblp_openreview_acl(self):
        src = FakeSource(available=())
        plan = router(src).plan({'topic': 'ACL retrieval augmented generation'}, 'ACL RAG')
        for name in ('dblp', 'openreview', 'acl_anthology'):
            self.assertIn(name, plan.providers)

    def test_deepseek_is_reserved_for_gap(self):
        src = FakeSource(available=('deepseek',))
        plan = router(src).plan({'topic': 'rag'}, 'rag')
        self.assertTrue(plan.deepseek_allowed)
        self.assertNotIn('deepseek', plan.providers)


class RankingTests(unittest.TestCase):
    def test_relevance_authority_recency_and_oa_win(self):
        src = FakeSource()
        r = router(src)
        good = paper(1, 'Retrieval augmented generation evaluation benchmark',
                     'We introduce a benchmark for retrieval augmented generation.',
                     year=2025, venue='ACL', authority=0.9, oa='https://example.org/p.pdf')
        old = paper(2, 'Retrieval augmented generation in 2015',
                    'retrieval augmented generation', year=2015, venue='arXiv',
                    authority=0.4)
        unrelated = paper(3, 'Image segmentation with transformers',
                          'We segment images.', year=2025, venue='CVPR', authority=0.7)
        ordered, scores = r.rank([old, unrelated, good], {'topic': 'RAG evaluation'}, 'RAG evaluation')
        self.assertEqual(ordered[0].id, 'p1')
        self.assertGreater(scores['p1'], scores['p2'])
        self.assertGreater(scores['p1'], scores['p3'])

    def test_metadata_only_is_not_counted_as_coverage(self):
        src = FakeSource()
        r = router(src)
        papers = [paper(i, metadata=True, abstract='') for i in range(5)]
        self.assertFalse(r._coverage(papers, {'target_count': 5}))
        papers[0].abstract = 'real abstract'
        self.assertFalse(r._coverage(papers, {'target_count': 5}))


class ExecutionTests(unittest.TestCase):
    def test_coverage_stops_after_one_batch(self):
        src = FakeSource(batches=[[paper(i) for i in range(5)]])
        result = router(src).retrieve({'topic': 'rag', 'target_count': 5}, 'rag', meter=CostMeter())
        self.assertEqual(len(src.calls), 1)
        self.assertEqual(result.cost, 1.0)
        self.assertEqual(len(result.papers), 5)

    def test_gap_refines_once_then_uses_deepseek_once(self):
        src = FakeSource(batches=[[], [], [paper(9, 'RAG from web')]], available=('deepseek',))
        task = {'topic': 'RAG', 'category': 'evaluation', 'minimum_category_count': 2,
                'target_count': 5}
        result = router(src).retrieve(task, 'RAG', meter=CostMeter())
        self.assertEqual(len(src.calls), 3)
        self.assertEqual(src.calls[0]['providers'], ['arxiv', 'openalex', 'semantic_scholar',
                                                     'openreview', 'acl_anthology'])
        self.assertEqual(src.calls[2]['providers'], ['deepseek'])
        self.assertEqual(src.deepseek_calls, 1)
        self.assertEqual(result.cost, 3.0)
        self.assertEqual(result.papers[0].id, 'p9')

    def test_empty_result_is_not_an_exception(self):
        src = FakeSource(batches=[[], []], available=())
        result = router(src).retrieve({'topic': 'rag', 'category': 'evaluation',
                                       'target_count': 5}, 'rag', meter=CostMeter())
        self.assertEqual(result.papers, [])
        self.assertEqual(result.cost, 2.0)

    def test_provider_failure_degrades_and_records_error(self):
        src = FakeSource(batches=[GuideError('S2 429')], available=())
        result = router(src).retrieve({'topic': 'rag', 'target_count': 5}, 'rag', meter=CostMeter())
        self.assertEqual(result.papers, [])
        self.assertEqual(result.cost, 0.0)
        self.assertIn('S2 429', result.errors[0])

    def test_venue_and_year_filters_are_forwarded_and_enforced(self):
        src = FakeSource(batches=[[paper(1, venue='CVPR', year=2024),
                                   paper(2, venue='ICCV', year=2024)]], available=())
        task = {'topic': '3D generation', 'target_count': 5, 'venue': 'CVPR',
                'year_from': 2024, 'year_to': 2025}
        result = router(src).retrieve(task, '3D generation CVPR 2024 2025', meter=CostMeter())
        self.assertEqual(src.calls[0]['filters']['venue'], 'CVPR')
        self.assertEqual([p.id for p in result.papers], ['p1'])

    def test_single_pass_workflow_uses_router_for_multi_source(self):
        src = FakeSource(batches=[[paper(i) for i in range(1, 7)]], available=())
        result = run_workflow('Recommend five RAG papers including one evaluation paper.',
                              Config(source='multi', cache=False), 'S', source=src, llm=DemoLLM())
        self.assertEqual(result['config']['source'], 'multi')
        self.assertTrue(src.calls)
        self.assertEqual(src.calls[0]['providers'], ['arxiv', 'openalex', 'semantic_scholar',
                                                     'openreview', 'acl_anthology'])
        self.assertIn(result['status'], ('complete', 'partial'))

    def test_router_dry_run_has_no_historical_dataset_dependency(self):
        tasks = [{'turns': [{'message': 'Find RAG papers', 'gold': {'topic': 'RAG', 'category': category, 'target_count': 5}},
                            {'message': 'Refine the reading list', 'gold': {'topic': 'RAG', 'category': category, 'target_count': 5}}]}
                 for category in ('none', 'evaluation', 'survey')]
        total = 0.0
        for task in tasks:
            for turn in task['turns']:
                src = FakeSource(batches=[[paper(i) for i in range(5)]], available=())
                task_view = turn.get('gold') or {'topic': task['turns'][0]['gold']['topic'],
                                                 'category': 'none', 'target_count': 5}
                result = router(src).retrieve(task_view, turn['message'], meter=CostMeter())
                self.assertLessEqual(result.cost, 1.0)
                self.assertTrue(result.plan.reasons)
                self.assertTrue(result.observation()['batches'])
                total += result.cost
        self.assertLessEqual(total, 24.0)

    def test_observation_contains_reasons_and_sources(self):
        src = FakeSource(batches=[[paper(1)]], available=())
        result = router(src).retrieve({'topic': 'rag', 'target_count': 1}, 'rag', meter=CostMeter())
        obs = result.observation()
        self.assertTrue(obs['plan']['reasons'])
        self.assertEqual(obs['batches'][0]['providers'][0], 'arxiv')
        self.assertEqual(obs['selected'][0]['id'], 'p1')


if __name__ == '__main__':
    unittest.main()
