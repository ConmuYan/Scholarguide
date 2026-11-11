import unittest
from common import Paper, Request
from source import merge_papers
from workflow import select_papers, coverage

class InfluenceRankingTests(unittest.TestCase):
    def test_crossref_requires_matching_published_conference_identity(self):
        from source import AcademicSource
        from research_policy import matched_publication
        source = AcademicSource.__new__(AcademicSource)
        message = {'DOI': '10.1109/CVPR52733.2024.00715',
                   'title': ['VidToMe: Video Token Merging for Zero-Shot Video Editing'],
                   'container-title': ['2024 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)'],
                   'type': 'proceedings-article', 'published': {'date-parts': [[2024, 6, 16]]}}
        source._json = lambda *args, **kwargs: {'message': message}
        paper = Paper(id='lune:x', title=message['title'][0], abstract='', url='',
                      doi=message['DOI'], venue='CVPR', year=2024)
        verified = source.verify_crossref_publication(paper)
        self.assertTrue(matched_publication(verified, {'venue': 'CVPR', 'year_from': 2024,
                                                       'year_to': 2024, 'publication_type': 'conference',
                                                       'accepted_only': True}))
        for field, bad in [('title', ['Different']), ('type', 'posted-content'),
                           ('container-title', ['ICCV']), ('published', {'date-parts': [[2025]]}),
                           ('DOI', '10.1109/other')]:
            old = message[field]
            message[field] = bad
            self.assertIs(source.verify_crossref_publication(paper), paper)
            message[field] = old

    def test_exact_openalex_conference_publication_verification(self):
        from types import SimpleNamespace
        from source import AcademicSource
        from research_policy import matched_publication
        source = AcademicSource.__new__(AcademicSource)
        source.providers = ['openalex']
        source.config = SimpleNamespace(openalex_key='')
        source.available = lambda provider: True
        work = {'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/10.1109/example',
                'title': 'An Example', 'publication_year': 2025, 'type': 'conference-paper',
                'primary_location': {'is_published': True, 'version': 'publishedVersion',
                                     'source': {'type': 'conference', 'display_name': 'CVPR'}}}
        source._json = lambda *args, **kwargs: work
        paper = Paper(id='lune:x', title='An Example', abstract='abstract', url='',
                      doi='10.1109/example', venue='CVPR', year=2025)
        verified = source.verify_openalex_publication(paper)
        self.assertTrue(matched_publication(verified, {'venue': 'CVPR', 'year_from': 2025,
                                                      'year_to': 2025, 'publication_type': 'conference',
                                                      'accepted_only': True}))
        work['title'] = 'A Different Paper'
        self.assertIs(source.verify_openalex_publication(paper), paper)
        work['title'] = paper.title
        work['primary_location']['version'] = 'acceptedVersion'
        self.assertIs(source.verify_openalex_publication(paper), paper)

    def test_recovers_only_a_literal_known_conference_whitelist(self):
        from workflow import _restore_explicit_conferences
        field = 'Eligible conferences: ICLR, ICML, NeurIPS, ACL, EMNLP, AAAI'
        plan = {'venue': '', 'unsupported': [field, 'code released']}
        _restore_explicit_conferences(plan, 'Only use the whitelist.\n' + field + '\nCount: 5')
        self.assertEqual(plan['venue'], 'ICLR, ICML, NeurIPS, ACL, EMNLP, AAAI')
        self.assertEqual(plan['unsupported'], ['code released'])
        conflict = {'venue': 'CVPR', 'unsupported': [field]}
        _restore_explicit_conferences(conflict, field)
        self.assertEqual(conflict, {'venue': 'CVPR', 'unsupported': [field]})
        unknown = {'venue': '', 'unsupported': ['Eligible conferences: ACL, ImaginaryVenue']}
        _restore_explicit_conferences(unknown, 'Eligible conferences: ACL, ImaginaryVenue')
        self.assertEqual(unknown['venue'], '')
        self.assertTrue(unknown['unsupported'])
        duplicate = {'venue': '', 'unsupported': [field]}
        _restore_explicit_conferences(duplicate, field + '\n' + field)
        self.assertEqual(duplicate['venue'], '')
        video = 'ACM MM, CVPR, ICCV, ICLR, ICML, NeurIPS, SIGGRAPH'
        plan = {'venue': '', 'unsupported': [video, 'code released']}
        _restore_explicit_conferences(plan, 'Eligible conferences: ' + video)
        self.assertEqual(plan['venue'], video)
        self.assertEqual(plan['unsupported'], ['code released'])
        asia = {'venue': '', 'unsupported': ['SIGGRAPH Asia']}
        _restore_explicit_conferences(asia, 'Eligible conferences: SIGGRAPH Asia')
        self.assertEqual(asia['venue'], '')
        self.assertEqual(asia['unsupported'], ['SIGGRAPH Asia'])

    def test_benchmark_significance_is_qualified_only_in_paired_ranking_request(self):
        from workflow import _calibrate_paired_significance
        plan = {'ranking': 'citations'}
        text = 'Recommend the most influential and research-significant papers.'
        self.assertEqual(_calibrate_paired_significance(plan, text, ['research-significant', 'code released']), ['code released'])
        self.assertEqual(_calibrate_paired_significance(plan, 'Find research-significant papers.', ['research-significant']), ['research-significant'])
        corrected = {'ranking': 'relevance'}
        self.assertEqual(_calibrate_paired_significance(corrected, text, ['research-significant']), [])
        self.assertEqual(corrected['ranking'], 'citations')

    def test_ranking_and_missing_counts(self):
        papers = [Paper(id='a', title='A', abstract='evidence', url='', citation_count=0, citation_source='openalex'),
                  Paper(id='b', title='B', abstract='evidence', url='', citation_count=50, citation_source='openalex'),
                  Paper(id='c', title='C', abstract='evidence', url='')]
        assessments = {p.id: {'relevance': 2, 'evidence_verified': True, 'category_status': 'unknown', 'paper_type': 'research'} for p in papers}
        request = Request(topic='agents', target_count=3, reading_goal='general', ranking='citations')
        result = select_papers(papers, assessments, request)
        self.assertEqual([p['id'] for p in result], ['b', 'a', 'c'])
        self.assertEqual(coverage(result, request)[0]['requirement'], 'citation_counts')

    def test_citations_follow_verified_shared_identity(self):
        a = Paper(id='arxiv:1', title='Same', abstract='long abstract', url='', arxiv_id='2401.12345')
        b = Paper(id='openalex:W1', title='Same', abstract='abstract', url='', arxiv_id='2401.12345', citation_count=42, citation_source='openalex')
        self.assertEqual(merge_papers([a,b])[0].citation_count, 42)
        b.arxiv_id = ''
        self.assertIsNone(merge_papers([a,b])[0].citation_count)

    def test_rejects_unknown_rank(self):
        with self.assertRaises(Exception):
            Request.from_dict({'topic': 'test', 'ranking': 'fabricated'})

    def test_exact_doi_citation_enrichment_and_bounded_calls(self):
        from types import SimpleNamespace
        from common import Budget
        from source import AcademicSource
        source = AcademicSource.__new__(AcademicSource)
        source.store = None
        source.config = SimpleNamespace(openalex_key='')
        source.budget = Budget(60)
        source.providers = ['openalex']
        source.available = lambda name: True
        calls = []
        def fetch(url, *, provider):
            calls.append(url)
            return {'id':'https://openalex.org/W123', 'doi':'https://doi.org/10.1234/foo', 'cited_by_count':12}
        source._json = fetch
        papers = [Paper(id='a',title='A',abstract='',url='',doi='10.1234/foo'),
                  Paper(id='b',title='B',abstract='',url='',doi='https://doi.org/10.1234/foo'),
                  Paper(id='c',title='C',abstract='',url='',doi='10.1234/different'),
                  Paper(id='d',title='D',abstract='',url='',doi='10.1234/foo',citation_count=7)]
        result, stats = source.enrich_openalex_citations(papers, max_lookups=2)
        self.assertEqual([p.citation_count for p in result], [12,12,None,7])
        self.assertEqual(stats, {'attempted':2, 'updated':2, 'unresolved':1})
        self.assertEqual(len(calls),2)
        self.assertIn('10.1234%2Ffoo',calls[0])
        self.assertIsNone(papers[0].citation_count)
        self.assertEqual(result[0].citation_source,'openalex')

    def test_enrichment_requires_authorized_provider(self):
        from types import SimpleNamespace
        from common import Budget, GuideError
        from source import AcademicSource
        source = AcademicSource.__new__(AcademicSource)
        source.store = None
        source.config = SimpleNamespace(openalex_key='')
        source.budget = Budget(60)
        source.providers = ['lune']
        with self.assertRaises(GuideError): source.enrich_openalex_citations([])


class CitationPersistenceTests(unittest.TestCase):
    def test_migrates_old_database_and_roundtrips_chat_and_api(self):
        import sqlite3, tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from state import StateStore, SCHEMA_PATH
        from session import ChatSession
        from api_v1 import public_result
        from api_schema import openapi_document
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            old_schema = SCHEMA_PATH.read_text(encoding='utf8').replace('  citation_count INTEGER,\n  citation_source TEXT NOT NULL DEFAULT \'\',\n  citation_observed_at TEXT NOT NULL DEFAULT \'\',\n', '')
            db = sqlite3.connect(path)
            db.executescript(old_schema)
            db.execute("INSERT INTO papers (paper_id,content_hash,title,abstract,fetched_at) VALUES ('legacy','hash','Old','Abstract','then')")
            db.commit(); db.close()
            store = StateStore(path)
            self.assertIsNone(store.con.execute("SELECT citation_count FROM papers WHERE paper_id='legacy'").fetchone()[0])
            sid = store.create_session()
            data = {'id':'p1', 'title':'Test', 'abstract':'an abstract', 'url':'', 'citation_count':42, 'citation_source':'openalex', 'citation_observed_at':'2026-01-01'}
            store.add_candidate(sid, data)
            store.register_paper({k:v for k,v in data.items() if not k.startswith('citation_')})
            reconstructed = ChatSession._papers(SimpleNamespace(store=store, session_id=sid), ['p1'])[0]
            self.assertEqual((reconstructed.citation_count,reconstructed.citation_source,reconstructed.citation_observed_at),(42,'openalex','2026-01-01'))
            public = public_result({'papers':[reconstructed.to_dict()]})['papers'][0]
            self.assertEqual(public['citation_count'],42)
            self.assertIn('ranking', openapi_document()['components']['schemas']['Filters']['properties'])
            store.close()

    def test_quota_preserves_final_citation_order(self):
        papers = [Paper(id='a',title='A',abstract='x',url='',citation_count=90), Paper(id='b',title='B',abstract='x',url='',citation_count=10)]
        ass = {'a': {'relevance':2,'evidence_verified':True,'category_status':'no'},'b': {'relevance':2,'evidence_verified':True,'category_status':'yes'}}
        request = Request(topic='x', target_count=2,category='survey', minimum_category_count=1,ranking='citations')
        self.assertEqual([p['id'] for p in select_papers(papers,ass,request)], ['a','b'])

if __name__ == '__main__': unittest.main()

class CitationEnrichmentPipelineTests(unittest.TestCase):
    def test_enrichment_bounded_before_assessment(self):
        from dataclasses import replace
        from tests.test_search_constraints import Planner, QUOTE
        from common import Config
        from workflow import run
        class FakeSource:
            mode='demo'; providers=['openalex']
            def __init__(self): self.calls=[]
            def search(self, query, limit=10):
                return [Paper(str(i),'Paper '+str(i),QUOTE,'',doi='10.1234/'+str(i)) for i in range(12)]
            def enrich_openalex_citations(self, papers, *, max_lookups):
                self.calls.append(max_lookups)
                return [replace(p,citation_count=100-int(p.id),citation_source='openalex') for p in papers], {'attempted':len(papers),'updated':len(papers),'unresolved':0}
        source=FakeSource()
        result=run('x',Config(batch_size=5),'S',source=source,llm=Planner(),request_data={'topic':'test','query':'test','target_count':5,'ranking':'citations'})
        self.assertEqual(source.calls,[10])
        self.assertEqual(next(x for x in result['trace'] if x['action']=='citation_enrichment')['updated'],10)
        self.assertEqual([p['citation_count'] for p in result['papers']], [100,99,98,97,96])

    def test_unresolved_paid_failure_blocks_enrichment(self):
        from tests.test_search_constraints import Planner
        from common import Config, GuideError
        from workflow import run
        class FailedSource:
            mode='demo'; providers=['lune','openalex']
            def __init__(self): self.calls=0
            def search(self,query,limit=10):
                error=GuideError('unknown paid outcome'); error.remote_outcome='unknown'; error.retry_safe=False; raise error
            def enrich_openalex_citations(self,*args,**kwargs): self.calls+=1; raise AssertionError('must not run')
        source=FailedSource()
        result=run('x',Config(),'S',source=source,llm=Planner(),request_data={'topic':'test','query':'test','ranking':'citations'})
        self.assertEqual(source.calls,0)
        self.assertEqual(result['status'],'failed')
