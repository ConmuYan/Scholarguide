"""Pure failure-diagnosis tests: explain misses and propose explicit recovery."""
import unittest

from diagnosis import diagnose
from agent_tools import ToolObservation
from common import Config
from retrieval import RetrievalRouter


class VenueNormalizationTests(unittest.TestCase):
    def test_embedded_year_is_split_into_year_filter(self):
        from common import Request
        request = Request.from_dict({'topic': '3D generation', 'venue': 'CVPR 2026'})
        self.assertEqual(request.venue, 'CVPR')
        self.assertEqual((request.year_from, request.year_to), (2026, 2026))

    def test_explicit_year_fields_remain_authoritative(self):
        from common import Request
        request = Request.from_dict({'topic': '3D generation', 'venue': 'CVPR 2025',
                                     'year_from': 2024, 'year_to': 2026})
        self.assertEqual(request.venue, 'CVPR')
        self.assertEqual((request.year_from, request.year_to), (2024, 2026))


class DiagnosisTests(unittest.TestCase):
    def test_complete_has_no_recovery(self):
        result = diagnose(
            request={'target_count': 5, 'topic': 'RAG'},
            gaps=[], trace=[{'action': 'search'}], stop_reason='requirements_met')
        self.assertEqual(result['code'], 'ok')
        self.assertEqual(result['actions'], [])

    def test_constraint_gap_explains_and_offers_relaxations(self):
        request = {'target_count': 5, 'venue': 'CVPR', 'year_from': 2026, 'year_to': 2026,
                   'category': 'research', 'minimum_category_count': 3}
        gaps = [
            {'requirement': 'paper_count', 'required': 5, 'found': 0, 'missing': 5},
            {'requirement': 'constraints', 'value': 'venue=CVPR · years=2026-2026',
             'required': 5, 'found': 0, 'missing': 5},
            {'requirement': 'mix', 'required': 5, 'found': 0, 'missing': 5},
        ]
        trace = [{'action': 'search', 'query': 'VGGT CVPR 2026',
                  'error': 'search exceeded 15.0s; OpenAlex could not resolve venue "CVPR".; '
                           'DBLP returned an HTML challenge (bot protection).'},
                 {'action': 'search', 'error': 'OpenAlex could not resolve venue "CVPR".'}]
        result = diagnose(request=request, gaps=gaps, trace=trace,
                          candidates=[{'id': 'openreview:x', 'title': 'VGGT-Ω'}],
                          stop_reason='budget_exhausted')
        self.assertEqual(result['code'], 'constraints_unmet')
        self.assertIn('已交付 0 篇', result['summary'])
        self.assertIn('OpenAlex', result['summary'])
        providers = [x['provider'] for x in result['source_errors']]
        self.assertIn('OpenAlex', providers)
        self.assertIn('DBLP', providers)
        codes = {x['code'] for x in result['source_errors']}
        self.assertIn('venue_resolve', codes)
        self.assertIn('bot_challenge', codes)
        self.assertIn('source_health', result)
        self.assertIn('relax_year', [x['code'] for x in result['actions']])
        self.assertIn('relax_venue', [x['code'] for x in result['actions']])
        self.assertIn('new_session', [x['code'] for x in result['actions']])
        self.assertEqual(result['near_misses'][0]['title'], 'VGGT-Ω')
        self.assertNotIn('api_key', str(result))

    def test_lower_target_is_not_offered_below_supported_minimum(self):
        result = diagnose(
            request={'target_count': 3, 'venue': 'CVPR', 'year_from': 2026, 'year_to': 2026},
            gaps=[{'requirement': 'paper_count', 'required': 3, 'found': 1, 'missing': 2},
                  {'requirement': 'constraints', 'value': 'venue=CVPR · years=2026-2026',
                   'required': 3, 'found': 1, 'missing': 2}],
            trace=[], stop_reason='budget_exhausted')
        codes = [x['code'] for x in result['actions']]
        self.assertNotIn('lower_target', codes)
        self.assertIn('accept_partial', codes)

    def test_source_degradation_without_candidates(self):
        result = diagnose(
            request={'target_count': 3, 'topic': 'RAG'}, gaps=[
                {'requirement': 'paper_count', 'required': 3, 'found': 0, 'missing': 3}],
            trace=[{'action': 'search_failed',
                    'error': 'Semantic Scholar returned 429 rate limited'}],
            stop_reason='model_or_tool_error')
        self.assertEqual(result['code'], 'source_degraded')
        self.assertIn('Semantic Scholar', result['summary'])
        self.assertTrue(any(x['code'] == 'retry_source' for x in result['actions']))

    def test_workflow_result_carries_diagnosis(self):
        from demo import demo_run
        result = demo_run('insufficient')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['diagnosis']['code'], 'quota_unmet')
        self.assertTrue(result['diagnosis']['actions'])

    def test_quota_gap_is_distinct_from_source_failure(self):
        result = diagnose(
            request={'target_count': 5, 'category': 'survey', 'minimum_category_count': 1},
            gaps=[{'requirement': 'survey', 'required': 1, 'found': 0, 'missing': 1},
                  {'requirement': 'paper_count', 'required': 5, 'found': 3, 'missing': 2}],
            trace=[], stop_reason='requirements_unmet')
        self.assertEqual(result['code'], 'quota_unmet')
        self.assertIn('类别', result['summary'])


if __name__ == '__main__':
    unittest.main()


class RouterTimeoutDiagnosisTests(unittest.TestCase):
    class Source:
        providers = ['arxiv', 'openalex']

    class Tools:
        def search(self, *args, **kwargs):
            return ToolObservation('search', False, 'timeout',
                                   error='search timed out after 0.01s (worker did not cancel)',
                                   retryable=True, worker_waited=True)

    def test_real_router_timeout_reaches_timeout_code(self):
        router = RetrievalRouter(Config(source='multi'), self.Source(), tools=self.Tools())
        result = router.retrieve({'topic': 'RAG', 'target_count': 3}, 'RAG',
                                 allow_refine=False)
        self.assertTrue(any('timed out' in error for error in result.errors))
        diagnosis = diagnose(request={'target_count': 3}, gaps=[
            {'requirement': 'paper_count', 'required': 3, 'found': 0, 'missing': 3}],
            trace=[{'action': 'search', 'error': '; '.join(result.errors)}],
            stop_reason='model_or_tool_error')
        self.assertIn('timeout', [item['code'] for item in diagnosis['source_errors']])
