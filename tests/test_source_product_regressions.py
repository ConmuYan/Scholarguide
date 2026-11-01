"""Source correctness and concurrency: no live services or researcher datasets."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from common import Budget, Config, Paper
from execution_events import bind_events, operation
from retrieval import RetrievalRouter
from source import AcademicSource, SearchBatch, SourceRun, merge_papers
from source_store import SourceStore


class SourceProductRegressionTests(unittest.TestCase):
    def test_invalid_normalized_ids_never_merge_different_native_records(self):
        rows = [Paper('lune:first', 'Different first work', 'Abstract first', '', doi='bad-a', arxiv_id='bad-a'),
                Paper('lune:second', 'Different second work', 'Abstract second', '', doi='bad-b', arxiv_id='bad-b')]
        self.assertEqual(len(merge_papers(rows)), 2)

    def test_lune_bibliographic_filters_do_not_invent_acceptance_or_presentation(self):
        from mcp_lune import to_paper
        from research_policy import publication_matches
        paper = to_paper({'paper_id':'actual-native-id', 'title':'An indexed work',
                          'conference':'ACL', 'year':2025, 'abstract':'A source abstract.',
                          'doi':'10.1234/work'})
        self.assertTrue(publication_matches(paper, {'venue':'ACL','year_from':2025,'year_to':2025}))
        self.assertFalse(publication_matches(paper, {'venue':'ACL','accepted_only':True}))
        self.assertFalse(publication_matches(paper, {'presentation_types':['oral']}))
        self.assertEqual(paper.publication['status'], 'unknown')
        self.assertTrue(paper.url.startswith('https://doi.org/'))

    def test_native_lune_id_and_real_abstract_survive_standard_source_path(self):
        source = AcademicSource(Config(source='lune', cache=False), Budget(10))
        native = Paper('lune:real-native', 'A paper', 'An actual source abstract.', '', sources=['lune'])
        with patch.object(source, 'available', return_value=True), patch.object(source, '_search_one', return_value=[native]) as adapter:
            result = source.search_batch('RAG', providers=['lune'])
        self.assertEqual([p.id for p in result.papers], ['lune:real-native'])
        self.assertEqual(adapter.call_count, 1)

    def test_source_cache_preserves_counts_without_changing_live_health(self):
        with tempfile.TemporaryDirectory() as directory:
            source = AcademicSource(Config(source='arxiv,openalex', cache=True, cache_dir=Path(directory)), Budget(10))
            paper = Paper('p1','Work','Actual abstract','https://example.test/p', sources=['arxiv'])
            with patch.object(source,'_search_one',side_effect=[[paper],[]]):
                source.search_batch('RAG')
            source.record_run(SourceRun('openalex','error',error='HTTP 429',code='rate_limited'))
            cached=source.search_batch('RAG')
            self.assertEqual([(r.name,r.returned) for r in cached.runs],[('arxiv',1),('openalex',0)])
            self.assertEqual(source.health_snapshot()['openalex']['last_code'],'rate_limited')

    def test_independent_primary_sources_overlap_but_publish_on_parent(self):
        barrier=threading.Barrier(2)
        parent=threading.get_ident()
        observed=[]
        class Source:
            providers=['arxiv','openalex']
            store=object()  # Product scheduler is injected; fixtures without it remain serial.
            budget=Budget(10)
            def available(self,name): return True
            def search_batch(self, query, limit=10, providers=None, filters=None):
                name=providers[0]
                with operation('search',source=name):
                    barrier.wait(timeout=2)
                    time.sleep(.05)
                    paper=Paper(name,name+' RAG','Actual abstract','https://example.test/'+name,sources=[name])
                    return SearchBatch(query,providers,[paper],[SourceRun(name,'ok',1)])
        source=Source()
        router=RetrievalRouter(Config(source='arxiv,openalex',research_mode=True,cache=False),source)
        with bind_events(lambda action,payload:observed.append((threading.get_ident(),action,payload))):
            result=router.retrieve({'topic':'RAG','target_count':2},'RAG')
        self.assertEqual(len(result.papers),2)
        self.assertEqual({row[0] for row in observed},{parent})
        self.assertEqual(sum(action=='tool_start' for _,action,_ in observed),2)
        self.assertEqual(sum(action=='tool_end' for _,action,_ in observed),2)


if __name__=='__main__':
    unittest.main()
