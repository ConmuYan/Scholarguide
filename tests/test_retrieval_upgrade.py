"""Product routing regressions: source coverage is independent of query aliases.

All requests use recorded in-memory batches. No academic API or model is called.
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, BudgetExceeded, Config, GuideError, Paper
from retrieval import RetrievalRouter
from source import DEFAULT_MULTI, SearchBatch, SourceRun
from tools import CostMeter


def candidate(name, *, abstract='Multi-view scene reconstruction with geometric reasoning.',
              venue='CVPR', verified=True, status='published', year=2025):
    return Paper(name, 'Scene reconstruction ' + name, abstract, 'https://openalex.org/' + name,
        year=year, venue=venue, sources=['openalex'], source='openalex',
        abstract_source='openalex' if abstract else '', metadata_only=not bool(abstract),
        publication={'records': [{'venue_name': venue, 'type': 'conference',
            'status': status, 'date': str(year), 'verified': verified,
            'evidence': [{'source': 'openalex', 'url': 'https://api.openalex.org/works/' + name,
                          'field': 'type', 'value': 'conference'}]}]})


class RecordingSource:
    def __init__(self, batches=(), *, providers=DEFAULT_MULTI, unavailable=(), budget=None,
                 after_call=None):
        self.providers, self.batches = list(providers), list(batches)
        self.unavailable, self.budget = set(unavailable), budget
        self.calls, self.after_call = [], after_call

    def available(self, name):
        return name not in self.unavailable

    def search_batch(self, query, limit=10, providers=None, filters=None):
        provider = providers[0]
        self.calls.append({'provider': provider, 'query': query, 'filters': deepcopy(filters)})
        items = self.batches.pop(0) if self.batches else []
        if self.after_call:
            self.after_call(self)
        if isinstance(items, Exception):
            raise items
        return SearchBatch(query, providers, items,
                           [SourceRun(provider, 'ok' if items else 'empty', len(items))])


def router(source, source_name='multi'):
    return RetrievalRouter(Config(source=source_name, research_mode=True, cache=False), source,
                           today=2026)


class SourceCoverageRegressionTests(unittest.TestCase):
    def test_plain_recent_topic_uses_complementary_source_without_needing_alias(self):
        for topic in ('3D scene reconstruction', 'RAG'):
            with self.subTest(topic=topic):
                source = RecordingSource([[candidate('W1')], [candidate('W2')]])
                result = router(source).retrieve({'topic': topic, 'reading_goal': 'frontier',
                    'prefer_recent': True}, topic)
                self.assertEqual([c['provider'] for c in source.calls], ['arxiv', 'openalex', 'semantic_scholar'])
                self.assertEqual(source.calls[0]['query'], source.calls[1]['query'])
                self.assertEqual(len(result.plan.steps), 2)

    def test_cvpr_acceptance_uses_metadata_coverage_instead_of_unrelated_venue_pools(self):
        source = RecordingSource([[candidate('W1')], [candidate('W2')]])
        result = router(source).retrieve({'topic': '3D reconstruction', 'venue': 'CVPR',
            'publication_type': 'conference', 'accepted_only': True}, '3D reconstruction')
        self.assertEqual([c['provider'] for c in source.calls], ['openalex', 'semantic_scholar', 'dblp'])
        self.assertEqual([p.id for p in result.papers], ['W1', 'W2'])
        self.assertEqual(result.plan.fallback_steps[0]['provider'], 'dblp')

    def test_known_venue_families_and_journals_have_directed_coverage(self):
        cases = [('ICLR', 'conference', ['openreview', 'openalex']),
                 ('NeurIPS', 'conference', ['openreview', 'openalex']),
                 ('ICML', 'conference', ['openreview', 'openalex']),
                 ('EMNLP', 'conference', ['acl_anthology', 'openalex']),
                 ('AI Review', 'journal', ['openalex', 'semantic_scholar']),
                 ('ECCV', 'conference', ['openalex', 'semantic_scholar'])]
        for venue, kind, expected in cases:
            with self.subTest(venue=venue):
                plan = router(RecordingSource()).plan({'venue': venue,
                    'publication_type': kind, 'accepted_only': True}, 'representation learning')
                self.assertEqual(plan.providers, expected)
                self.assertEqual(len(plan.steps), 2)

    def test_empty_calls_have_one_bounded_observable_fallback_and_never_spray_all_sources(self):
        source = RecordingSource()
        result = router(source).retrieve({'topic': '3D scene reconstruction', 'venue': 'CVPR',
            'accepted_only': True}, '3D scene reconstruction')
        self.assertEqual([c['provider'] for c in source.calls], ['openalex', 'semantic_scholar', 'dblp'])
        self.assertEqual(result.observation()['retrieval_execution'][-1]['trigger'], 'empty_candidates')
        self.assertEqual(result.observation()['plan']['max_calls'], 3)
        self.assertTrue(all(c['filters']['accepted_only'] for c in source.calls))

    def test_abstract_gap_uses_fallback_without_counting_snippet_as_evidence(self):
        metadata = candidate('W1', abstract='')
        metadata.doi = '10.1234/verified-work'
        metadata.snippet = 'A tempting search snippet is not an abstract.'
        preprint = candidate('arxiv:2501.12345')
        preprint.doi, preprint.arxiv_id = metadata.doi, '2501.12345'
        preprint.sources, preprint.source, preprint.abstract_source = ['arxiv'], 'arxiv', 'arxiv'
        preprint.publication = {}
        preprint.year = 2024
        source = RecordingSource([[metadata], [], [preprint]])
        result = router(source).retrieve({'venue': 'CVPR', 'accepted_only': True}, 'scene reconstruction')
        self.assertEqual(len(source.calls), 3)
        self.assertEqual(source.calls[-1]['provider'], 'arxiv')
        self.assertEqual(source.calls[-1]['query'], metadata.title)
        self.assertEqual(source.calls[-1]['filters'], {})
        self.assertEqual(result.observation()['retrieval_execution'][-1]['trigger'], 'missing_abstracts')
        self.assertEqual(len(result.papers), 1)
        self.assertTrue(result.papers[0].abstract)
        self.assertEqual(result.papers[0].id, metadata.id)
        self.assertEqual(result.papers[0].year, 2025)
        self.assertEqual(result.papers[0].publication, metadata.publication)
        self.assertEqual(result.papers[0].abstract_source, 'arxiv')
        self.assertEqual(result.observation()['retrieval_execution'][-1]['identity_matches'][0]['identity'], 'doi')

    def test_title_enrichment_needs_complete_authors_and_never_transfers_publication_records(self):
        metadata = candidate('W1', abstract='')
        metadata.title = 'Reliable surface reconstruction from sparse multi-view observations'
        metadata.authors = ['Alice Researcher', 'Bob Scientist']
        preprint = candidate('arxiv:2501.12345')
        preprint.title, preprint.authors = metadata.title, list(reversed(metadata.authors))
        preprint.publication = {}
        source = RecordingSource([[metadata], [], [preprint]])
        result = router(source).retrieve({'venue': 'CVPR', 'accepted_only': True}, 'scene reconstruction')
        self.assertEqual(result.papers[0].abstract, preprint.abstract)
        self.assertEqual(result.papers[0].publication, metadata.publication)
        self.assertEqual(result.observation()['retrieval_execution'][-1]['identity_matches'][0]['identity'],
                         'strict_title_authors')

    def test_matching_arxiv_identity_can_enrich_a_published_version_with_a_revised_title(self):
        metadata = candidate('W1', abstract='')
        metadata.arxiv_id = 'https://arxiv.org/abs/2501.12345v2'
        preprint = candidate('arxiv:2501.12345')
        preprint.arxiv_id = '2501.12345'
        preprint.title = 'The earlier title of the same reconstruction work'
        preprint.publication = {}
        source = RecordingSource([[metadata], [], [preprint]])
        result = router(source).retrieve({'venue': 'CVPR', 'accepted_only': True}, 'scene reconstruction')
        self.assertTrue(result.papers[0].abstract)
        self.assertEqual(result.papers[0].title, metadata.title)
        self.assertEqual(result.papers[0].publication, metadata.publication)
        self.assertEqual(result.executions[-1]['identity_matches'][0]['identity'], 'arxiv_id')

    def test_enrichment_rejects_unknown_work_title_collisions_and_identifier_conflicts(self):
        for case in ('unknown', 'title_only', 'author_mismatch', 'doi_conflict', 'arxiv_conflict'):
            with self.subTest(case=case):
                metadata = candidate('W1', abstract='')
                metadata.title = 'Reliable surface reconstruction from sparse multi-view observations'
                metadata.authors, metadata.doi = ['Alice Researcher'], '10.1234/formal'
                metadata.arxiv_id = '2501.12345'
                incoming = candidate('unrelated')
                incoming.title, incoming.authors = metadata.title, list(metadata.authors)
                incoming.publication = {}
                if case == 'unknown':
                    incoming.title = 'An unrelated scene reconstruction method'
                elif case == 'title_only':
                    incoming.authors = []
                elif case == 'author_mismatch':
                    incoming.authors = ['Someone Else']
                elif case == 'doi_conflict':
                    incoming.doi = '10.1234/different'
                elif case == 'arxiv_conflict':
                    incoming.arxiv_id = '2502.98765'
                source = RecordingSource([[metadata], [], [incoming]])
                result = router(source).retrieve({'venue': 'CVPR', 'accepted_only': True}, 'scene reconstruction')
                self.assertEqual([p.id for p in result.papers], [metadata.id])
                self.assertFalse(result.papers[0].abstract)
                self.assertEqual(result.papers[0].publication, metadata.publication)
                self.assertEqual(result.retrieval_gap, 'missing_abstracts')
                self.assertEqual(result.executions[-1]['enriched_candidates'], 0)

    def test_missing_abstract_does_not_use_metadata_only_or_unapproved_provider(self):
        source = RecordingSource([[candidate('W1', abstract='')], [], [candidate('unused')]],
                                 providers=['openalex', 'semantic_scholar', 'dblp'])
        result = router(source, 'openalex,semantic_scholar,dblp').retrieve(
            {'venue': 'CVPR', 'accepted_only': True}, 'scene reconstruction')
        self.assertEqual([c['provider'] for c in source.calls], ['openalex', 'semantic_scholar'])
        self.assertEqual(result.retrieval_gap, 'missing_abstracts')
        self.assertTrue(any('No authorized abstract-capable fallback' in e for e in result.errors))

    def test_source_failure_then_unverified_records_falls_back_without_loosening_constraints(self):
        source = RecordingSource([GuideError('HTTP 429 rate limited'),
                                  [candidate('unverified', verified=False)], [candidate('formal')]])
        meter = CostMeter()
        result = router(source).retrieve({'venue': 'CVPR', 'accepted_only': True,
            'year_from': 2025, 'year_to': 2025}, 'scene reconstruction', meter=meter)
        self.assertEqual([p.id for p in result.papers], ['formal'])
        self.assertEqual(result.observation()['retrieval_execution'][-1]['trigger'], 'publication_evidence_gap')
        self.assertIn('429', result.errors[0])
        self.assertEqual(meter.summary()['settled'], 2)
        self.assertEqual(meter.summary()['refunded'], 1)
        self.assertTrue(all(c['filters']['venue'] == 'CVPR' for c in source.calls))

    def test_candidate_shortfall_uses_bounded_fallback_without_semantic_claim(self):
        source = RecordingSource([[candidate('W1')], [], [candidate('W2')]])
        result = router(source).retrieve({'venue': 'CVPR', 'accepted_only': True,
            'target_count': 5}, 'scene reconstruction')
        self.assertEqual(len(source.calls), 3)
        self.assertEqual(result.observation()['retrieval_execution'][-1]['status'], 'ok')
        self.assertEqual(result.observation()['retrieval_gap'], 'usable_candidate_shortfall')
        self.assertIsNone(result.observation()['semantic_satisfaction'])

    def test_single_source_and_explicit_allowlist_never_escape_authorized_sources(self):
        single = RecordingSource(providers=['arxiv'])
        result = router(single, 'arxiv').retrieve({'venue': 'CVPR'}, 'scene reconstruction')
        self.assertEqual([c['provider'] for c in single.calls], ['arxiv'])
        self.assertEqual(result.plan.fallback_steps, [])
        multi = RecordingSource()
        result = router(multi).retrieve({'venue': 'CVPR'}, 'scene reconstruction',
            providers=['semantic_scholar', 'openalex'])
        self.assertEqual([c['provider'] for c in multi.calls], ['semantic_scholar', 'openalex'])
        self.assertEqual(result.plan.fallback_steps, [])

    def test_single_pass_retains_plan_but_never_executes_complement_or_fallback(self):
        source = RecordingSource()
        result = router(source).retrieve({'venue': 'CVPR'}, 'scene reconstruction', allow_refine=False)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(len(result.plan.steps), 2)
        self.assertEqual(len(result.plan.fallback_steps), 1)

    def test_health_changes_after_planning_skip_unavailable_source_and_try_bounded_fallback(self):
        source = RecordingSource(after_call=lambda s: s.unavailable.add('semantic_scholar'))
        result = router(source).retrieve({'venue': 'CVPR'}, 'scene reconstruction')
        self.assertEqual([c['provider'] for c in source.calls], ['openalex', 'dblp'])
        execution = result.observation()['retrieval_execution']
        self.assertEqual(execution[1]['status'], 'unavailable')
        self.assertEqual(execution[2]['trigger'], 'empty_candidates')

    def test_budget_and_cancellation_gate_fallback_before_cost_reservation(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                budget = Budget(60)
                def stop_after_two(s):
                    if len(s.calls) == 2:
                        if cancel:
                            budget.cancel_token.request()
                        else:
                            budget.started -= 100
                source = RecordingSource(budget=budget, after_call=stop_after_two)
                meter = CostMeter()
                router(source).retrieve({'venue': 'CVPR'}, 'scene reconstruction', meter=meter)
                self.assertEqual(len(source.calls), 2)
                self.assertEqual(meter.summary()['reserved'], 2)


if __name__ == '__main__':
    unittest.main()
