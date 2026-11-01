"""Offline research-policy and directed-retrieval acceptance cases.

These fixtures deliberately separate a work's several publication events so a
rejected submission cannot borrow another venue's acceptance or publication year.
No service credentials, network requests or experiment artifacts are used.
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, BudgetExceeded, Config, GuideError, Paper, RunCancelled
from research_policy import (CATALOG_VERSION, curated_venue_catalog, journal_issns_for_venue, matched_publication,
                             matches_requested_venue, normalize_issn, publication_matches,
                             publication_records, publication_violations,
                             research_selection_key, venue_identity)
from retrieval import RetrievalRouter
from source import DEFAULT_MULTI, SearchBatch, SourceRun
from tools import CostMeter


def record(venue='ICLR', *, kind='conference', status='accepted', year=2025,
           presentation='poster', verified=True, venue_id='', issn=None, evidence=None):
    source = 'openreview' if kind == 'conference' else 'openalex' if kind == 'journal' else 'arxiv'
    url = {'openreview': 'https://openreview.net/forum?id=fixture-record',
           'openalex': 'https://api.openalex.org/works/Wfixture',
           'arxiv': 'https://arxiv.org/abs/fixture-record'}[source]
    return {'type': kind, 'status': status, 'presentation': presentation,
            'venue_name': venue, 'venue_id': venue_id, 'date': str(year) if year else '',
            'issn': list(issn or []), 'verified': verified,
            'evidence': evidence if evidence is not None else [
                {'source': source, 'url': url, 'field': 'status', 'value': status}]}


def paper(idx, *, title=None, abstract='retrieval augmented generation', year=2025,
          sources=None, records=None, authority=0.0):
    sources = sources or ['arxiv']
    publication = {'records': deepcopy(records)} if records is not None else {}
    urls = {'arxiv': 'https://arxiv.org/abs/fixture-' + str(idx),
            'openalex': 'https://openalex.org/Wfixture-' + str(idx),
            'openreview': 'https://openreview.net/forum?id=fixture-' + str(idx),
            'acl_anthology': 'https://aclanthology.org/fixture-' + str(idx)}
    return Paper(f'p{idx}', title or f'Retrieval augmented generation method {idx}', abstract,
                 urls.get(sources[0], 'https://arxiv.org/abs/fixture-' + str(idx)), year=year,
                 sources=sources, source=sources[0],
                 abstract_source=sources[0] if abstract else '', metadata_only=not bool(abstract),
                 authority=authority, publication=publication)


class FakeSource:
    def __init__(self, batches=None, providers=None, unavailable=(), budget=None,
                 after_call=None):
        self.providers = list(providers if providers is not None else DEFAULT_MULTI)
        self.batches = list(batches or [])
        self.unavailable = set(unavailable)
        self.budget = budget
        self.after_call = after_call
        self.calls = []

    def available(self, name):
        return name not in self.unavailable

    def search_batch(self, query, limit=10, providers=None, filters=None):
        names = list(providers or self.providers)
        self.calls.append({'query': query, 'providers': names, 'filters': deepcopy(filters),
                           'limit': limit})
        items = self.batches.pop(0) if self.batches else []
        if isinstance(items, Exception):
            raise items
        if self.after_call:
            self.after_call(self)
        runs = [SourceRun(name, 'ok' if items else 'empty', len(items)) for name in names]
        return SearchBatch(query, names, list(items), runs)


def router(source, *, research=True):
    return RetrievalRouter(Config(source='multi', cache=False, research_mode=research), source)


class PublicationPolicyTests(unittest.TestCase):
    def test_exact_venue_aliases_do_not_promote_workshops_or_other_acronyms(self):
        formal = ('Proceedings of the 62nd Annual Meeting of the Association for '
                  'Computational Linguistics (Volume 1: Long Papers)')
        self.assertEqual(venue_identity(formal), 'ACL')
        self.assertEqual(venue_identity('Proceedings of ACL 2024'), 'ACL')
        self.assertEqual(venue_identity('IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)'), 'CVPR')
        for other in ('NAACL', 'Findings of ACL', 'ACL Workshop', 'SIGMOD Record'):
            p = paper(1, records=[record(other)])
            expected = 'SIGMOD' if other == 'SIGMOD Record' else 'ACL'
            self.assertFalse(matches_requested_venue(p, expected), other)

    def test_official_openreview_edition_id_resolves_unknown_display_heading(self):
        p = paper(1, records=[record('', venue_id='ICLR.cc/2025/Conference')])
        self.assertTrue(matches_requested_venue(p, 'ICLR'))
        p.publication['records'][0]['venue_id'] = 'ICLR.cc/2025/Workshop'
        self.assertFalse(matches_requested_venue(p, 'ICLR'))

    def test_journal_issn_exact_match_and_verified_catalog_mapping(self):
        p = paper(1, records=[record('ACM Computing Surveys', kind='journal', status='published',
                                    issn=['0360-0300', '1557-7341'])])
        self.assertEqual(normalize_issn('0360-0300'), '03600300')
        self.assertTrue(matches_requested_venue(p, '03600300'))
        self.assertFalse(matches_requested_venue(p, '0360'))
        self.assertFalse(matches_requested_venue(p, '0360-0301'))
        self.assertEqual(journal_issns_for_venue('CSUR'), ['03600300', '15577341'])
        p.publication['records'][0]['venue_name'] = 'Unresolved display name'
        self.assertTrue(matches_requested_venue(p, 'ACM Computing Surveys'))
        self.assertTrue(publication_matches(p, {'venue_scope': 'preferred_only'}))

    def test_artificial_intelligence_review_alias_and_official_issns_are_exact(self):
        expected = ['02692821', '15737462']
        for name in ('Artificial Intelligence Review', 'AI Review'):
            self.assertEqual(venue_identity(name), 'Artificial Intelligence Review')
            self.assertEqual(journal_issns_for_venue(name), expected)
            for issn in ('0269-2821', '1573-7462'):
                p = paper(1, records=[record('Unresolved display name', kind='journal',
                                            status='published', issn=[issn])])
                self.assertTrue(publication_matches(p, {'venue': name, 'publication_type': 'journal'}))
            csur = paper(2, records=[record('ACM Computing Surveys', kind='journal',
                                           status='published', issn=['0360-0300'])])
            self.assertFalse(matches_requested_venue(csur, name))
        catalog = curated_venue_catalog()
        air = next(item for item in catalog['venues'] if item['name'] == 'Artificial Intelligence Review')
        self.assertEqual(air['issn'], expected)

    def test_rejected_iclr_cannot_borrow_neurips_acceptance(self):
        p = paper(1, records=[record('ICLR', status='rejected'), record('NeurIPS')])
        task = {'venue': 'ICLR', 'accepted_only': True}
        self.assertFalse(publication_matches(p, task))
        self.assertIn('acceptance_unverified', publication_violations(p, task))
        self.assertTrue(publication_matches(p, {'venue': 'NeurIPS', 'accepted_only': True}))

    def test_presentation_and_year_must_match_same_publication(self):
        p = paper(1, year=2026, records=[record('ICLR', year=2024, presentation='poster'),
                                       record('NeurIPS', year=2026, presentation='oral')])
        task = {'venue': 'ICLR', 'accepted_only': True, 'presentation_types': ['oral'],
                'year_from': 2026, 'year_to': 2026}
        self.assertFalse(publication_matches(p, task))
        self.assertTrue(publication_matches(p, {'venue': 'ICLR', 'accepted_only': True,
                                               'year_from': 2024, 'year_to': 2024}))
        p.publication['records'][0]['date'] = ''
        self.assertFalse(publication_matches(p, {'venue': 'ICLR', 'year_from': 2026}))

    def test_unknown_conference_status_does_not_prove_acceptance(self):
        for state in ('unknown', 'submitted', 'conditionalaccept', 'rejected', 'withdrawn', 'desk_rejected'):
            p = paper(1, records=[record(status=state)])
            self.assertFalse(publication_matches(p, {'accepted_only': True}), state)
        self.assertTrue(publication_matches(paper(1, records=[record(status='published')]),
                                            {'accepted_only': True}))

    def test_presentation_requires_acceptance_even_for_unvalidated_task_dict(self):
        p = paper(1, records=[record(status='submitted', presentation='oral')])
        self.assertFalse(publication_matches(p, {'presentation_types': ['oral']}))

    def test_verified_record_needs_traceable_evidence_and_identity(self):
        for evidence in ([], [{'url': 'javascript:alert(1)'}], [{'url': 'https:///invalid'}]):
            self.assertFalse(publication_matches(paper(1, records=[record(evidence=evidence)]),
                                                 {'accepted_only': True}))
        p = paper(1, records=[record()])
        p.publication['records'][0]['identity_verified'] = False
        self.assertFalse(publication_matches(p, {'accepted_only': True}))

    def test_summary_cannot_supply_an_extra_event(self):
        p = paper(1, records=[record('ICLR', status='rejected')])
        p.publication.update(record('ICLR'))
        self.assertFalse(publication_matches(p, {'accepted_only': True}))
        p.publication['records'] = []
        self.assertEqual(publication_records(p), [])

    def test_preferred_is_soft_and_preferred_only_is_inspectable_hard_pool(self):
        outside = paper(1, records=[record('A specialist venue')])
        self.assertTrue(publication_matches(outside, {'venue_scope': 'preferred'}))
        self.assertFalse(publication_matches(outside, {'venue_scope': 'preferred_only'}))
        catalog = curated_venue_catalog()
        self.assertEqual(catalog['version'], CATALOG_VERSION)
        self.assertIn('not an objective ranking', catalog['notice'])
        self.assertTrue(any(item['name'] == 'JMLR' for item in catalog['venues']))

    def test_plain_recency_constraint_preserves_preprint_discovery(self):
        p = paper(1, year=2026)
        self.assertTrue(publication_matches(p, {'year_from': 2025, 'year_to': 2026}))
        self.assertFalse(publication_matches(p, {'year_to': 2025}))
        self.assertFalse(publication_matches(p, {'publication_type': 'conference'}))

    def test_matched_display_publication_uses_same_version_and_is_independent_copy(self):
        p = paper(1, year=2026, records=[record('ICLR', year=2024), record('NeurIPS', year=2026)])
        original = deepcopy(p.publication)
        matched = matched_publication(p, {'venue': 'ICLR', 'accepted_only': True,
                                         'year_from': 2024, 'year_to': 2024})
        self.assertEqual((matched['venue_name'], matched['date']), ('ICLR', '2024'))
        matched['evidence'][0]['url'] = 'https://example.org/changed'
        self.assertEqual(p.publication, original)
        self.assertIsNone(matched_publication(p, {'venue': 'ACL', 'accepted_only': True}))

    def test_matched_display_without_publication_constraint_prefers_verified_acceptance(self):
        p = paper(1, records=[record('ICLR', status='rejected', year=2026),
                              record('NeurIPS', year=2025)])
        self.assertEqual(matched_publication(p, {})['venue_name'], 'NeurIPS')
        self.assertIsNone(matched_publication(p, {'year_from': 2027}))
        self.assertIsNone(matched_publication(paper(2), {}))


class SemanticOrderingTests(unittest.TestCase):
    def test_preferred_venue_can_break_comparable_fit_band_without_overriding_relevance(self):
        indexed = paper(1, year=2026)
        accepted = paper(2, year=2025, records=[record('NeurIPS',year=2025)])
        task = {'venue_scope':'preferred','prefer_recent':True}
        self.assertLess(research_selection_key(accepted,{'relevance':2,'fit_score':95},task),
                        research_selection_key(indexed,{'relevance':2,'fit_score':99},task))
        self.assertLess(research_selection_key(indexed,{'relevance':2,'fit_score':95},task),
                        research_selection_key(accepted,{'relevance':1,'fit_score':100},task))

    def test_semantic_relevance_and_fit_precede_authority_venue_and_recency(self):
        old = paper(1, year=2020, authority=0.1)
        prestigious = paper(2, year=2026, authority=1.0, records=[record('NeurIPS', year=2026)])
        task = {'venue_scope': 'preferred', 'prefer_recent': True}
        self.assertLess(research_selection_key(old, {'relevance': 2, 'fit_score': 65}, task),
                        research_selection_key(prestigious, {'relevance': 1, 'fit_score': 100}, task))
        self.assertLess(research_selection_key(old, {'relevance': 2, 'fit_score': 95}, task),
                        research_selection_key(prestigious, {'relevance': 2, 'fit_score': 60}, task))

    def test_fit_default_and_type_preference_are_secondary(self):
        a, b = paper(1), paper(2)
        task = {'prefer_recent': True}
        self.assertEqual(research_selection_key(a, {'relevance': 2}, task)[:2], (-2, -50))
        self.assertEqual(research_selection_key(a, {'relevance': 2, 'fit_score': float('nan')}, task)[:2], (-2, -50))
        self.assertLess(research_selection_key(a, {'relevance': 2, 'fit_score': 80, 'paper_type': 'method'}, task, 'survey'),
                        research_selection_key(b, {'relevance': 2, 'fit_score': 79, 'paper_type': 'survey'}, task, 'survey'))

    def test_recency_uses_publication_that_satisfies_hard_constraints(self):
        a = paper(1, year=2026, records=[record('ICLR', year=2024), record('NeurIPS', year=2026)])
        b = paper(2, year=2025, records=[record('ICLR', year=2025)])
        task = {'venue': 'ICLR', 'accepted_only': True, 'prefer_recent': True}
        assessment = {'relevance': 2, 'fit_score': 80}
        self.assertLess(research_selection_key(b, assessment, task), research_selection_key(a, assessment, task))


class DirectedPlanTests(unittest.TestCase):
    def test_venue_and_publication_kind_direct_primary_source(self):
        cases = [({'venue': 'ACL'}, 'acl_anthology'), ({'venue': 'ICLR'}, 'openreview'),
                 ({'publication_type': 'journal'}, 'openalex'),
                 ({'publication_type': 'preprint'}, 'arxiv'),
                 ({'publication_type': 'conference', 'accepted_only': True}, 'openreview')]
        for task, expected in cases:
            plan = router(FakeSource()).plan(task, 'RAG')
            self.assertEqual(plan.steps[0]['provider'], expected, task)
            self.assertTrue(plan.research)
            self.assertLessEqual(len(plan.steps), 2)

    def test_venue_filters_are_precise_and_each_step_is_one_source(self):
        plan = router(FakeSource()).plan({'venue': 'ICLR', 'accepted_only': True,
                                         'year_from': 2024, 'year_to': 2025,
                                         'category': 'evaluation'}, 'RAG')
        self.assertEqual(plan.steps[0]['filters']['openreview_venue_ids'],
                         ['ICLR.cc/2024/Conference', 'ICLR.cc/2025/Conference'])
        self.assertEqual(plan.steps[0]['provider'], 'openreview')
        self.assertNotEqual(plan.steps[0]['query'], plan.steps[1]['query'])
        journal = router(FakeSource()).plan({'venue': 'CSUR', 'publication_type': 'journal'}, 'RAG')
        self.assertEqual(journal.steps[0]['filters']['journal_issns'], ['03600300', '15577341'])

    def test_artificial_intelligence_review_directs_exact_journal_search_without_csur_leakage(self):
        for name in ('Artificial Intelligence Review', 'AI Review'):
            plan = router(FakeSource()).plan({'venue': name, 'publication_type': 'journal',
                                              'category': 'survey'}, 'large language model agents')
            self.assertEqual(plan.steps[0]['provider'], 'openalex')
            self.assertEqual(plan.steps[0]['filters']['venue'], name)
            self.assertEqual(plan.steps[0]['filters']['journal_issns'], ['02692821', '15737462'])
            self.assertIn('Artificial Intelligence Review', plan.steps[0]['purpose'])
            self.assertNotIn('03600300', plan.steps[0]['filters']['journal_issns'])
        unrestricted = router(FakeSource()).plan({'publication_type': 'journal', 'category': 'survey'}, 'RAG')
        self.assertEqual(unrestricted.steps[0]['provider'], 'openalex')
        self.assertEqual(unrestricted.steps[0]['filters']['venue'], '')
        self.assertNotIn('journal_issns', unrestricted.steps[0]['filters'])

    def test_model_source_hints_intersect_config_and_availability_without_fallback(self):
        source = FakeSource(providers=['arxiv', 'openreview'], unavailable=['openreview'])
        result = router(source).retrieve({'topic': 'RAG'}, 'RAG',
                                         providers=['not_configured', 'openreview'])
        self.assertEqual(source.calls, [])
        self.assertEqual(result.papers, [])
        self.assertTrue(any('not configured' in item for item in result.errors))
        self.assertTrue(any('unavailable' in item for item in result.errors))
        self.assertTrue(any('No configured' in item for item in result.errors))

    def test_explicit_source_allowlist_is_not_silently_expanded(self):
        source = FakeSource()
        plan = router(source).plan({'venue': 'ICLR', 'category': 'evaluation'}, 'RAG',
                                   providers=['semantic_scholar'], intent='accepted evaluation')
        self.assertEqual(plan.providers, ['semantic_scholar'])
        self.assertEqual(plan.steps[0]['intent'], 'accepted evaluation')

    def test_no_automatic_paid_discovery_and_explicit_deepseek_is_observable(self):
        source = FakeSource(providers=['deepseek'])
        self.assertEqual(router(source).plan({}, 'RAG').steps[0]['provider'], 'deepseek')
        plan = router(source).plan({}, 'RAG', providers=['deepseek'])
        self.assertEqual(plan.providers, ['deepseek'])
        self.assertTrue(plan.deepseek_allowed)
        self.assertIn('snippets do not satisfy abstract evidence', plan.steps[0]['purpose'])

    def test_legacy_router_contract_remains_unchanged(self):
        source = FakeSource()
        plan = router(source, research=False).plan({'category': 'evaluation'}, 'RAG')
        self.assertEqual(plan.providers, ['arxiv', 'openalex', 'semantic_scholar',
                                         'openreview', 'acl_anthology'])
        self.assertEqual(plan.queries, ['RAG', 'RAG evaluation benchmark'])
        self.assertNotIn('research', plan.as_dict())


class DirectedExecutionTests(unittest.TestCase):
    def task(self):
        return {'topic': 'retrieval augmented generation', 'category': 'evaluation', 'target_count': 3}

    def test_raw_target_count_cannot_cancel_complementary_search_or_claim_semantics(self):
        irrelevant = [paper(i, title=f'Image segmentation {i}', abstract='pixels and masks') for i in range(3)]
        relevant = paper(9, sources=['openalex'])
        source = FakeSource([irrelevant, [relevant]])
        meter = CostMeter()
        result = router(source).retrieve(self.task(), 'retrieval augmented generation', limit=3, meter=meter)
        self.assertEqual(len(source.calls), 2)
        self.assertEqual([item['providers'] for item in source.calls], [['arxiv'], ['openalex']])
        self.assertNotEqual(source.calls[0]['query'], source.calls[1]['query'])
        self.assertEqual(result.papers[0].id, 'p9')
        self.assertEqual(result.cost, 2.0)
        self.assertEqual(meter.summary()['settled'], result.cost)
        obs = result.observation()
        self.assertIsNone(obs['semantic_satisfaction'])
        self.assertEqual(obs['raw_candidates'], 4)
        self.assertEqual(len(obs['batches']), 2)
        self.assertTrue(all(entry.detail['provider'] for entry in meter.entries))

    def test_provider_failure_refunds_and_preserves_other_planned_source(self):
        source = FakeSource([GuideError('429 rate limited'), [paper(9, sources=['openalex'])]])
        meter = CostMeter()
        result = router(source).retrieve(self.task(), 'RAG', meter=meter)
        self.assertEqual([item.id for item in result.papers], ['p9'])
        self.assertIn('429', result.errors[0])
        self.assertEqual(meter.summary()['reserved'], 3.0)
        self.assertEqual(meter.summary()['settled'], 2.0)
        self.assertEqual(meter.summary()['refunded'], 1.0)
        self.assertEqual(result.observation()['retrieval_gap'], 'usable_candidate_shortfall')

    def test_cancellation_between_steps_retains_successful_primary_results(self):
        budget = Budget(60)
        source = FakeSource([[paper(1)], [paper(2)]], budget=budget,
                            after_call=lambda source: source.budget.cancel_token.request())
        meter = CostMeter()
        result = router(source).retrieve(self.task(), 'RAG', meter=meter)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual([item.id for item in result.papers], ['p1'])
        self.assertEqual(meter.summary()['settled'], 1.0)

    def test_already_cancelled_run_has_no_source_call_or_reservation(self):
        budget = Budget(60)
        budget.cancel_token.request()
        source = FakeSource([[paper(1)]], budget=budget)
        meter = CostMeter()
        result = router(source).retrieve(self.task(), 'RAG', meter=meter)
        self.assertEqual(source.calls, [])
        self.assertEqual(result.papers, [])
        self.assertEqual(meter.summary()['reserved'], 0.0)

    def test_budget_exhaustion_between_steps_retains_successful_primary_results(self):
        class StepBudget(Budget):
            checks = 0

            def remaining(self):
                self.checks += 1
                if self.checks > 1:
                    raise BudgetExceeded('test budget exhausted')
                return 10

        source = FakeSource([[paper(1)], [paper(2)]], budget=StepBudget(60))
        meter = CostMeter()
        result = router(source).retrieve(self.task(), 'RAG', meter=meter)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual([item.id for item in result.papers], ['p1'])
        self.assertEqual(result.errors, ['test budget exhausted'])
        self.assertEqual(meter.summary()['reserved'], 1.0)

    def test_cancellation_during_second_step_refunds_only_that_step(self):
        source = FakeSource([[paper(1)], RunCancelled()])
        meter = CostMeter()
        result = router(source).retrieve(self.task(), 'RAG', meter=meter)
        self.assertEqual([item.id for item in result.papers], ['p1'])
        self.assertEqual(meter.summary()['settled'], 1.0)
        self.assertEqual(meter.summary()['refunded'], 1.0)

    def test_single_pass_caller_cannot_trigger_unapproved_second_step(self):
        source = FakeSource([[paper(1)], [paper(2)]])
        result = router(source).retrieve(self.task(), 'RAG', allow_refine=False)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(len(result.plan.steps), 2)

    def test_hard_publication_constraints_filter_candidates_but_keep_raw_observation(self):
        source = FakeSource([[paper(1, records=[record(status='rejected')]),
                              paper(2, records=[record()])]])
        task = {'topic': 'RAG', 'venue': 'ICLR', 'accepted_only': True, 'year_from': 2025}
        result = router(source).retrieve(task, 'RAG')
        self.assertEqual([item.id for item in result.papers], ['p2'])
        self.assertEqual(result.observation()['raw_candidates'], 2)
        self.assertTrue(source.calls[0]['filters']['accepted_only'])

    def test_directed_search_uses_registry_adapter_with_exact_filters(self):
        from types import SimpleNamespace

        class Registry:
            def __init__(self):
                self.calls = []

            def search(self, query, limit, **kwargs):
                self.calls.append((query, limit, kwargs))
                return SimpleNamespace(ok=True, data=SearchBatch(query, kwargs['providers'],
                    [paper(1, records=[record()])], [SourceRun('openreview', 'ok', 1)]))

        source, registry = FakeSource(), Registry()
        r = RetrievalRouter(Config(source='multi', cache=False, research_mode=True), source, tools=registry)
        task = {'venue': 'ICLR', 'accepted_only': True, 'year_from': 2025, 'year_to': 2025}
        result = r.retrieve(task, 'RAG', providers=['openreview'])
        self.assertEqual(source.calls, [])
        self.assertEqual(len(registry.calls), 1)
        self.assertEqual(registry.calls[0][2]['providers'], ['openreview'])
        self.assertEqual(registry.calls[0][2]['filters']['openreview_venue_ids'], ['ICLR.cc/2025/Conference'])
        self.assertEqual(result.cost, 1.0)
        self.assertEqual([item.id for item in result.papers], ['p1'])

    def test_candidate_rank_expands_aliases_deduplicates_titles_and_preserves_near_source(self):
        source = FakeSource()
        papers = [paper(1), paper(2), paper(3, sources=['openalex']),
                  paper(4, title='Image segmentation', abstract='vision pixels', authority=1.0),
                  paper(5, title='Retrieval augmented generation method 1', sources=['semantic_scholar'])]
        ranked, scores = router(source).rank(papers, {'topic': 'RAG'}, 'RAG')
        self.assertGreater(scores['p1'], scores['p4'])
        self.assertEqual(ranked[0].id, 'p1')
        self.assertEqual(ranked[1].sources, ['openalex'])
        self.assertEqual(sum(item.title == papers[0].title for item in ranked), 1)
        self.assertEqual(ranked[-1].id, 'p4')

    def test_candidate_ties_keep_verified_publication_and_requested_recency(self):
        old = paper(1, year=2020)
        current = paper(9, year=2026)
        published = paper(8, year=2025, records=[record('ACL')])
        ranked, _ = router(FakeSource()).rank([old, current, published],
                    {'topic': 'RAG', 'prefer_recent': True, 'venue_scope': 'preferred'}, 'RAG')
        self.assertEqual([item.id for item in ranked], ['p8', 'p9', 'p1'])


if __name__ == '__main__':
    unittest.main()
