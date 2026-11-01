"""Curated-source recall and conservative publication facts, without network.

The source payloads include a real metadata shape seen in the live smoke run;
synthetic paper titles exercise general routing rather than named-paper recall.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config, GuideError
from research_policy import publication_matches, publication_preference
from retrieval import RetrievalRouter
from source import AcademicSource
from tests.test_retrieval_upgrade import RecordingSource, candidate


CVPR_DISPLAY = ('IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings - '
                'IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings')


def source(provider='openalex', **kwargs):
    return AcademicSource(Config(source=provider, research_mode=True, cache=False, **kwargs), Budget(60))


def work(number=1, name='CVPR', source_id='S101', kind='conference'):
    return {'id':f'https://openalex.org/W{number}', 'title':f'A representation learning method {number}',
        'doi':f'https://doi.org/10.1234/method.{number}', 'type':'conference-paper' if kind=='conference' else 'article',
        'publication_year':2025, 'publication_date':'2025-06-01',
        'abstract_inverted_index':{'We':[0], 'learn':[1], 'representations.':[2]},
        'primary_location':{'version':'publishedVersion', 'is_published':True,
            'source':{'id':f'https://openalex.org/{source_id}', 'display_name':name, 'type':kind,
                      'issn':['1234-5678']}}}


class CuratedRouterTests(unittest.TestCase):
    def router(self, batch_source):
        return RetrievalRouter(Config(source='multi', research_mode=True, cache=False), batch_source, today=2026)

    def test_soft_venue_pool_is_source_side_and_does_not_rewrite_topic_or_hard_venue(self):
        task={'topic':'representation learning','preferred_venues':['CVPR','ICCV','TPAMI'],
              'venue_scope':'preferred','year_from':2024,'year_to':2026}
        plan=self.router(RecordingSource()).plan(task,'representation learning')
        self.assertEqual(plan.providers,['openalex','semantic_scholar'])
        self.assertEqual(plan.steps[0]['query'],'representation learning')
        self.assertEqual(plan.steps[0]['filters']['preferred_venues'],['CVPR','ICCV','TPAMI'])
        self.assertEqual(plan.steps[0]['filters']['venue'],'')
        self.assertTrue(plan.fallback_steps[0]['soft_preference_fallback'])
        self.assertNotIn('preferred_venues',plan.fallback_steps[0]['filters'])
        self.assertEqual(plan.as_dict()['max_calls'],3)

    def test_explicit_venue_and_preprint_intent_override_soft_planner_venues(self):
        for task in ({'venue':'ICLR','accepted_only':True,'preferred_venues':['CVPR']},
                     {'publication_type':'preprint','preferred_venues':['CVPR']}):
            plan=self.router(RecordingSource()).plan(task,'representation learning')
            self.assertNotIn('preferred_venues',plan.steps[0]['filters'])
            self.assertEqual(plan.steps[0]['provider'],'openreview' if task.get('venue') else 'arxiv')

    def test_pool_is_catalogue_bounded_and_does_not_allow_query_injection(self):
        plan=self.router(RecordingSource()).plan({'preferred_venues':[
            'NIPS','NeurIPS','Unreviewed Symposium','CVPR|arxiv','AI Review']},'representation learning')
        self.assertEqual(plan.steps[0]['filters']['preferred_venues'],['NeurIPS','Artificial Intelligence Review'])
        self.assertTrue(plan.notes)

    def test_pool_empty_or_untrusted_venue_results_use_one_authorized_broad_fallback(self):
        outsider=candidate('outside',venue='Unreviewed Symposium')
        recovered=candidate('broad',venue='A specialist journal')
        source=RecordingSource([[outsider],[],[recovered]])
        result=self.router(source).retrieve({'preferred_venues':['CVPR','TPAMI'],
            'year_from':2025,'year_to':2025,'accepted_only':True},'representation learning')
        self.assertEqual([p.id for p in result.papers],['broad'])
        self.assertEqual(len(source.calls),3)
        self.assertTrue(result.executions[-1]['soft_preference_fallback'])
        self.assertTrue(all(call['filters']['accepted_only'] for call in source.calls))
        self.assertTrue(all(call['filters']['year_from']==2025 for call in source.calls))

    def test_good_curated_abstracts_keep_the_complement_and_avoid_broad_fallback(self):
        source=RecordingSource([[candidate(str(i),venue='CVPR') for i in range(5)],[],[]])
        result=self.router(source).retrieve({'preferred_venues':['CVPR'],'target_count':5},'representation learning')
        self.assertEqual(len(source.calls),2)
        self.assertEqual(len(result.papers),5)
        self.assertFalse(any(row.get('soft_preference_fallback') for row in result.executions))

    def test_objective_pool_shortfall_uses_only_the_existing_third_call_allowance(self):
        src=RecordingSource([[candidate('a',venue='CVPR')],[],[candidate('b',venue='Specialist Conference')]])
        result=self.router(src).retrieve({'preferred_venues':['CVPR'],'target_count':5},'representation learning')
        self.assertEqual(len(src.calls),3)
        self.assertEqual(result.executions[-1]['trigger'],'usable_candidate_shortfall')
        self.assertEqual(result.plan.as_dict()['max_calls'],3)
        self.assertIsNone(result.observation()['semantic_satisfaction'])

    def test_single_provider_can_soft_fallback_without_expanding_its_allowlist(self):
        src=RecordingSource([[],[candidate('specialist',venue='Specialist Conference')]],providers=['openalex'])
        result=RetrievalRouter(Config(source='openalex',research_mode=True,cache=False),src).retrieve(
            {'preferred_venues':['CVPR']},'representation learning')
        self.assertEqual([call['provider'] for call in src.calls],['openalex','openalex'])
        self.assertEqual(len(result.papers),1)
        src=RecordingSource([[],[candidate('specialist')]])
        self.router(src).retrieve({'preferred_venues':['CVPR']},'representation learning',providers=['semantic_scholar'])
        self.assertEqual([call['provider'] for call in src.calls],['semantic_scholar','semantic_scholar'])

    def test_openreview_pool_editions_are_small_and_observable(self):
        plan=self.router(RecordingSource()).plan({'preferred_venues':['ICLR','NeurIPS','ICML'],
            'year_from':2024,'year_to':2026,'prefer_recent':True},'representation learning')
        official=next(step for step in plan.steps if step['provider']=='openreview')
        self.assertEqual(len(official['filters']['openreview_venue_ids']),3)
        self.assertTrue(all('/2026/Conference' in value for value in official['filters']['openreview_venue_ids']))
        self.assertTrue(any('edition' in note for note in plan.notes))


class CuratedSourceTests(unittest.TestCase):
    def test_openalex_uses_one_or_filtered_works_request_and_cached_source_resolution(self):
        src=source()
        calls=[]
        names={'CVPR':('S101',CVPR_DISPLAY,'conference'),'TPAMI':('S202','IEEE Transactions on Pattern Analysis and Machine Intelligence','journal')}
        def response(url,**kwargs):
            calls.append(url)
            q=parse_qs(urlsplit(url).query)
            if '/sources?' in url:
                identifier,name,kind=names[q['search'][0]]
                return {'results':[{'id':f'https://openalex.org/{identifier}','display_name':name,
                                    'type':kind,'works_count':100}]}
            return {'results':[work(name=CVPR_DISPLAY),work(2,'IEEE Transactions on Pattern Analysis and Machine Intelligence','S202','journal'),work(3,'Unreviewed Symposium','S303')]}
        with patch.object(src,'_json',side_effect=response):
            papers=src.search_batch('representation learning',5,providers=['openalex'],
                filters={'preferred_venues':['CVPR','TPAMI'],'year_from':2024}).papers
            src.search_batch('another research topic',5,providers=['openalex'],filters={'preferred_venues':['CVPR','TPAMI']})
        works_calls=[parse_qs(urlsplit(url).query) for url in calls if '/works?' in url]
        self.assertEqual(len(works_calls),2)
        self.assertIn('primary_location.source.id:S101|S202',works_calls[0]['filter'][0])
        self.assertIn('publication_year:>2023',works_calls[0]['filter'][0])
        self.assertEqual(len([url for url in calls if '/sources?' in url]),2)
        self.assertEqual([p.id for p in papers],['openalex:W1','openalex:W2'])
        self.assertEqual(publication_preference(papers[0],{'venue_scope':'preferred'}),3)
        self.assertTrue(publication_matches(papers[0],{'venue':'CVPR','accepted_only':True}))
        self.assertEqual(papers[0].publication['records'][0]['evidence'][0]['value']['display_name'],CVPR_DISPLAY)

    def test_openalex_partial_resolution_uses_subset_but_never_silently_broadens(self):
        src=source()
        with patch.object(src,'_venue_id',side_effect=[GuideError('could not resolve ECCV'),'S101']), \
             patch.object(src,'_json',return_value={'results':[work()]}) as request:
            src._openalex('representation learning',4,constraints={'preferred_venues':['ECCV','CVPR']})
        query=parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertIn('primary_location.source.id:S101',query['filter'][0])
        self.assertEqual(src.preference_resolution['openalex']['resolved_venues'],['CVPR'])
        self.assertEqual(src.preference_resolution['openalex']['unresolved_venues'],['ECCV'])
        with patch.object(src,'_venue_id',side_effect=GuideError('not found')), \
             patch.object(src,'_json') as request:
            with self.assertRaisesRegex(GuideError,'preferred venue pool'):
                src._openalex('representation learning',4,constraints={'preferred_venues':['ECCV','CVPR']})
        request.assert_not_called()

    def test_semantic_scholar_venue_parameter_is_an_or_list_and_leaks_are_excluded(self):
        src=source('semantic_scholar')
        rows=[{'paperId':'a','url':'https://semanticscholar.org/paper/a','title':'A representation method','abstract':'We learn representations.',
               'year':2025,'venue':'CVPR','publicationVenue':{'name':'CVPR','type':'conference'},'publicationTypes':['Conference']},
              {'paperId':'b','url':'https://semanticscholar.org/paper/b','title':'Another representation method','abstract':'We learn features.',
               'year':2025,'venue':'arXiv.org','publicationVenue':{'name':'arXiv.org'},'publicationTypes':['JournalArticle']}]
        with patch.object(src,'_json',return_value={'data':rows}) as request:
            papers=src.search_batch('representation learning',5,providers=['semantic_scholar'],
                filters={'preferred_venues':['CVPR','ICLR']}).papers
        self.assertEqual(parse_qs(urlsplit(request.call_args.args[0]).query)['venue'],['CVPR,ICLR'])
        self.assertEqual([p.id for p in papers],['s2:a'])
        self.assertFalse(publication_matches(papers[0],{'accepted_only':True}))

    def test_repository_metadata_cannot_become_a_published_journal_from_journalarticle(self):
        src=source('semantic_scholar')
        row={'paperId':'preprint','title':'A dynamic representation method','abstract':'We evaluate representations.',
            'year':2026,'externalIds':{'ArXiv':'2604.23803','DOI':'10.48550/arXiv.2604.23803'},
            'venue':'arXiv.org','publicationTypes':['JournalArticle'],
            'publicationVenue':{'id':'1901e811-ee72-4b20-8f7e-de08cd395a10','name':'arXiv.org',
                'alternate_names':['ArXiv'],'issn':'2331-8422','url':'https://arxiv.org'},
            'journal':{'name':'ArXiv','volume':'abs/2604.23803'}}
        with patch.object(src,'_json',return_value={'data':[row]}):
            paper=src._semantic_scholar('representation learning',1)[0]
        self.assertEqual(paper.publication['type'],'preprint')
        self.assertEqual(paper.publication['status'],'unknown')
        self.assertFalse(publication_matches(paper,{'publication_type':'journal'}))
        self.assertFalse(publication_matches(paper,{'accepted_only':True}))
        self.assertTrue(publication_matches(paper,{'publication_type':'preprint'}))
        self.assertEqual(paper.publication['evidence'][1]['value'],['JournalArticle'])

    def test_real_journal_with_an_arxiv_version_remains_journal(self):
        src=source('semantic_scholar')
        row={'paperId':'published','title':'A representation survey','abstract':'We review representations.',
            'year':2025,'externalIds':{'ArXiv':'2501.12345','DOI':'10.1007/s10462-025-10000-1'},
            'venue':'Artificial Intelligence Review','publicationTypes':['JournalArticle','Review'],
            'publicationVenue':{'name':'Artificial Intelligence Review','type':'journal','issn':'1573-7462'},
            'journal':{'name':'Artificial Intelligence Review'}}
        with patch.object(src,'_json',return_value={'data':[row]}):
            paper=src._semantic_scholar('representation learning',1)[0]
        self.assertTrue(publication_matches(paper,{'publication_type':'journal','accepted_only':True}))

    def test_research_cache_changes_without_invalidating_frozen_legacy_key(self):
        src=source('arxiv')
        src.config.research_mode=False
        old=src._cache_path('representation learning',3,['arxiv'],{})
        src.config.research_mode=True
        new=src._cache_path('representation learning',3,['arxiv'],{})
        self.assertNotEqual(old,new)
        src.config.research_mode=False
        self.assertEqual(old,src._cache_path('representation learning',3,['arxiv'],{}))

    def test_legacy_s2_does_not_reuse_old_repository_as_journal_cache_or_delete_it(self):
        with tempfile.TemporaryDirectory() as directory:
            src=AcademicSource(Config(source='semantic_scholar',research_mode=False,
                cache=True,cache_dir=Path(directory)),Budget(60))
            # Historical v8 key for this exact query/provider/limit/filter tuple.
            old=Path(directory)/'98ef0376ddbc4241ef9e0c17192aa8a831e7496e55f84d923209dc9027c759fe.json'
            poisoned=candidate('cached')
            old.write_text(json.dumps({'stored_at':time.time(),'papers':[poisoned.to_dict()]}),encoding='utf-8')
            row={'paperId':'fresh','url':'https://arxiv.org/abs/2604.23803','title':'A representation method',
                 'abstract':'We learn representations.','year':2026,'venue':'arXiv.org',
                 'externalIds':{'ArXiv':'2604.23803','DOI':'10.48550/arxiv.2604.23803'},
                 'publicationVenue':{'name':'arXiv.org','issn':'2331-8422'},
                 'publicationTypes':['JournalArticle'],'journal':{'name':'ArXiv'}}
            with patch.object(src,'_json',return_value={'data':[row]}) as request:
                papers=src.search_batch('representation learning',3,providers=['semantic_scholar'],filters={}).papers
            self.assertEqual(request.call_count,1)
            self.assertEqual(papers[0].publication['type'],'preprint')
            self.assertEqual(src.budget.cache_hits,0)
            self.assertTrue(old.exists())


if __name__=='__main__':
    unittest.main()
