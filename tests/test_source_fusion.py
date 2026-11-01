"""Offline product regressions for bounded, ranked source fusion."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import Budget, Config, Paper
from retrieval import RetrievalRouter
from source import AcademicSource


class FusionQualityTests(unittest.TestCase):
    def test_later_provider_is_ranked_before_assessment_cap_and_survives_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(source='arxiv,openalex', cache=True, cache_dir=Path(directory))
            source = AcademicSource(config, Budget(60))
            early = [Paper(f'a{i}', f'Unrelated image paper {i}', 'Image segmentation.',
                           f'https://example.org/a{i}', sources=['arxiv']) for i in range(10)]
            later = Paper('best', 'Retrieval augmented generation benchmark',
                          'A benchmark for retrieval augmented generation.',
                          'https://example.org/best', sources=['openalex'])
            router = RetrievalRouter(config, source)
            with patch.object(source, '_search_one', side_effect=[early, [later]]) as search:
                live = router.retrieve({'topic': 'retrieval augmented generation'},
                                       'retrieval augmented generation', limit=10,
                                       allow_refine=False)
                cached = router.retrieve({'topic': 'retrieval augmented generation'},
                                         'retrieval augmented generation', limit=10,
                                         allow_refine=False)
            self.assertEqual(search.call_count, 2)  # one request per provider, then cache
            for retrieval in (live, cached):
                self.assertEqual(retrieval.papers[0].id, 'best')
                self.assertEqual(len(retrieval.papers), 10)
                self.assertEqual(len(retrieval.batches[0].papers), 11)

    def test_provider_pool_remains_bounded_before_ranking(self):
        source = AcademicSource(Config(source='arxiv,openalex', cache=False), Budget(60))
        rows = {name: [Paper(f'{name}:{i}', f'{name} paper {i}', 'Abstract',
                             f'https://example.org/{name}/{i}') for i in range(20)]
                for name in ('arxiv', 'openalex')}
        with patch.object(source, '_search_one', side_effect=lambda name, *args: rows[name]):
            batch = source.search_batch('RAG', limit=3)
        self.assertEqual(len(batch.papers), 6)
        self.assertEqual([run.returned for run in batch.runs], [3, 3])

    def test_venue_filter_can_join_preprint_evidence_to_publication_metadata(self):
        source = AcademicSource(Config(source='arxiv,dblp', cache=False), Budget(60))
        preprint = Paper('arxiv:1', 'A RAG paper', 'A genuine preprint abstract.',
                         'https://arxiv.org/abs/1', doi='10.1234/rag', year=2023,
                         venue='arXiv', sources=['arxiv'], abstract_source='arxiv')
        publication = Paper('dblp:1', 'A RAG paper', '', 'https://example.org/paper',
                            doi='10.1234/rag', year=2024, venue='ACL',
                            sources=['dblp'], metadata_only=True)
        with patch.object(source, '_search_one', side_effect=[[preprint], [publication]]):
            batch = source.search_batch('RAG', filters={'venue': 'ACL', 'year_from': 2024})
        self.assertEqual(len(batch.papers), 1)
        paper = batch.papers[0]
        self.assertEqual((paper.venue, paper.year), ('ACL', 2024))
        self.assertEqual(paper.abstract, preprint.abstract)
        self.assertEqual(paper.abstract_source, 'arxiv')
        self.assertEqual(set(paper.sources), {'arxiv', 'dblp'})
        self.assertFalse(paper.metadata_only)


if __name__ == '__main__':
    unittest.main()
