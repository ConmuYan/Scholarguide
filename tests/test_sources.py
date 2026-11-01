"""Offline tests for the multi-source layer.

Every provider is driven through a fake ``_fetch`` (or a patched provider
method), so this module never touches the network. Fixtures mirror the public
response shapes documented by each service.
"""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config, GuideError, Paper
from source import AcademicSource, PROVIDERS, classify_source_error, merge_papers


def source(source_name='arxiv', **kwargs):
    config = Config(source=source_name, cache=False, **kwargs)
    return AcademicSource(config, Budget(60))


def as_json(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


S2 = {
    'data': [{
        'paperId': 's2-1', 'title': 'A RAG Evaluation Benchmark',
        'abstract': 'We introduce a benchmark for retrieval augmented generation.',
        'url': 'https://www.semanticscholar.org/paper/s2-1',
        'year': 2024, 'venue': 'ACL',
        'authors': [{'name': 'Ada Lovelace'}],
        'externalIds': {'DOI': '10.1234/rag.1', 'ArXiv': '2401.00001'},
        'openAccessPdf': {'url': 'https://example.org/rag.pdf'},
    }],
}
DBLP = {
    'result': {'hits': {'hit': [{
        'info': {
            'key': 'conf/acl/Test24', 'title': 'A RAG Evaluation Benchmark.',
            'venue': 'ACL', 'year': '2024',
            'authors': {'author': [{'text': 'Ada Lovelace'}, {'text': 'Alan Turing'}]},
            'doi': '10.1234/rag.1', 'ee': 'https://example.org/rag.pdf',
            'url': 'https://dblp.org/rec/conf/acl/Test24',
        }
    }]}}
}
OPENREVIEW = {
    'notes': [
        {'forum': 'accepted', 'content': {
            'title': {'value': 'Accepted RAG Paper'},
            'abstract': {'value': 'We study retrieval augmented generation.'},
            'venue': {'value': 'ICLR 2025 Poster'},
            'pdf': {'value': '/pdf/accepted.pdf'},
        }},
        {'forum': 'rejected', 'content': {
            'title': {'value': 'Rejected RAG Paper'},
            'abstract': {'value': 'This should not be recommended.'},
            'venue': {'value': 'Submitted to ICLR 2025'},
        }},
    ]
}
ACL_HTML = b'''<!doctype html><html><head>
<meta name="citation_title" content="ACL RAG Paper">
<meta name="citation_author" content="Ada Lovelace">
<meta name="citation_author" content="Alan Turing">
<meta name="citation_conference_title" content="Proceedings of ACL 2024">
<meta name="citation_publication_date" content="2024/08">
<meta name="citation_pdf_url" content="https://aclanthology.org/2024.acl-long.1.pdf">
<meta name="citation_doi" content="10.18653/v1/2024.acl-long.1">
</head><body><div class="card-body acl-abstract"><h5>Abstract</h5>
<span>We introduce a retrieval augmented generation benchmark for ACL papers.</span>
</div></body></html>'''
ACL_INDEX = {'items': [{
    'id': '2024.acl-long.1', 'title': 'ACL RAG Paper',
    'authors': ['Ada Lovelace'], 'year': 2024, 'booktitle': 'Proceedings of ACL 2024',
    'url': 'https://aclanthology.org/2024.acl-long.1/',
}]}
DEEPSEEK = {'results': [
    {'title': 'RAG paper on arXiv', 'url': 'https://arxiv.org/abs/2401.00001',
     'summary': 'A paper about retrieval augmented generation.',
     'snippet': 'RAG', 'site_name': 'arXiv', 'authority': 3, 'rerank_score': 2.8,
     'date': '2024-01-01T00:00:00Z'},
    {'title': 'Unrelated blog', 'url': 'https://example.com/blog',
     'summary': 'Not a paper.', 'snippet': '', 'site_name': 'Example', 'authority': 0},
]}


class ProviderParsingTests(unittest.TestCase):
    def test_semantic_scholar_parses_abstract_doi_and_oa(self):
        src = source('semantic_scholar')
        with patch.object(src, '_fetch', return_value=as_json(S2)):
            papers = src._semantic_scholar('rag', 5)
        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertEqual(paper.doi, '10.1234/rag.1')
        self.assertEqual(paper.arxiv_id, '2401.00001')
        self.assertEqual(paper.venue, 'ACL')
        self.assertEqual(paper.open_access_url, 'https://example.org/rag.pdf')
        self.assertFalse(paper.metadata_only)
        self.assertEqual(paper.abstract_source, 'semantic_scholar')

    def test_semantic_scholar_falls_back_to_bulk_on_429(self):
        src = source('semantic_scholar')
        with patch.object(src, '_json', side_effect=[GuideError('api.semanticscholar.org: HTTP 429'),
                                                     S2]):
            papers = src._semantic_scholar('rag', 5)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].doi, '10.1234/rag.1')

    def test_openalex_venue_and_year_filters_are_sent(self):
        src = source('openalex')
        with patch.object(src, '_venue_id', return_value='S4210176548'),              patch.object(src, '_json', return_value={'results': []}) as request:
            src._openalex('3D generation', 5, 'CVPR', 2024, 2025)
        url = request.call_args[0][0]
        self.assertIn('primary_location.source.id%3AS4210176548', url)
        self.assertIn('publication_year%3A2024-2025', url)

    def test_dblp_is_metadata_only_and_keeps_authors(self):
        src = source('dblp')
        with patch.object(src, '_fetch', return_value=as_json(DBLP)):
            papers = src._dblp('rag', 5)
        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertTrue(paper.metadata_only)
        self.assertEqual(paper.abstract, '')
        self.assertEqual(paper.authors, ['Ada Lovelace', 'Alan Turing'])
        self.assertEqual(paper.open_access_url, 'https://example.org/rag.pdf')

    def test_openreview_skips_rejected_and_keeps_pdf(self):
        src = source('openreview')
        with patch.object(src, '_fetch', return_value=as_json(OPENREVIEW)):
            papers = src._openreview('rag', 5)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].id, 'openreview:accepted')
        self.assertEqual(papers[0].open_access_url, 'https://openreview.net/pdf/accepted.pdf')
        self.assertFalse(papers[0].metadata_only)

    def test_acl_search_fetches_page_abstract(self):
        src = source('acl_anthology')

        def fake_fetch(url, **kwargs):
            if 'verbatim' in url:
                return as_json(ACL_INDEX)
            return ACL_HTML

        with patch.object(src, '_fetch', side_effect=fake_fetch):
            papers = src._acl_anthology('rag', 5)
        self.assertEqual(len(papers), 1)
        self.assertIn('retrieval augmented generation benchmark', papers[0].abstract)
        self.assertEqual(papers[0].doi, '10.18653/v1/2024.acl-long.1')
        self.assertEqual(papers[0].open_access_url, 'https://aclanthology.org/2024.acl-long.1.pdf')

    def test_deepseek_strips_cloudflare_query_from_doi(self):
        src = source('openai', deepseek_search_key='test-key')
        payload = {'results': [{'title': 'ACM RAG paper',
            'url': 'https://dl.acm.org/doi/10.1609/aaai.v38i1.12345?__cf_chl_tk=abc',
            'summary': 'A paper about RAG.', 'site_name': 'ACM', 'authority': 3}]}
        with patch.object(src, '_fetch', return_value=as_json(payload)):
            papers = src._deepseek_search('rag', 3)
        self.assertEqual(papers[0].doi, '10.1609/aaai.v38i1.12345')

    def test_deepseek_keeps_only_academic_results_and_caps_calls(self):
        src = source('openai', deepseek_search_key='test-key')
        with patch.object(src, '_fetch', return_value=as_json(DEEPSEEK)):
            papers = src._deepseek_search('rag', 5)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].arxiv_id, '2401.00001')
        self.assertTrue(papers[0].metadata_only)
        self.assertEqual(papers[0].snippet, 'A paper about retrieval augmented generation.')
        with self.assertRaises(GuideError):
            src._deepseek_search('rag again', 5)


class MergeAndRoutingTests(unittest.TestCase):
    def test_cross_source_merge_prefers_abstract_and_combines_provenance(self):
        src = source('multi')
        a = Paper('dblp:x', 'A RAG Evaluation Benchmark', '', 'https://dblp.org/x',
                  doi='10.1234/rag.1', venue='ACL', sources=['dblp'], metadata_only=True)
        b = Paper('s2:y', 'A RAG Evaluation Benchmark', 'Long abstract.',
                  'https://semanticscholar.org/y', doi='10.1234/rag.1',
                  sources=['semantic_scholar'], abstract_source='semantic_scholar')
        with patch.object(src, '_arxiv', return_value=[]), \
             patch.object(src, '_semantic_scholar', return_value=[b]), \
             patch.object(src, '_dblp', return_value=[a]):
            batch = src.search_batch('rag', 5, providers=['arxiv', 'semantic_scholar', 'dblp'])
        self.assertEqual(len(batch.papers), 1)
        paper = batch.papers[0]
        self.assertEqual(paper.abstract, 'Long abstract.')
        self.assertEqual(set(paper.sources), {'dblp', 'semantic_scholar'})
        self.assertFalse(paper.metadata_only)
        self.assertEqual([r.status for r in batch.runs], ['empty', 'ok', 'ok'])

    def test_multi_source_degrades_when_one_provider_fails(self):
        src = source('multi')
        good = Paper('arxiv:1', 'Good paper', 'Abstract', 'https://arxiv.org/abs/1')
        with patch.object(src, '_arxiv', return_value=[good]), \
             patch.object(src, '_semantic_scholar', side_effect=GuideError('S2 429')):
            result = src.search_batch('rag', 5, providers=['arxiv', 'semantic_scholar'])
        self.assertEqual(len(result.papers), 1)
        self.assertEqual(result.runs[1].status, 'error')
        self.assertIn('S2 429', result.runs[1].error)

    def test_missing_deepseek_key_is_skipped_not_crashed(self):
        src = source('deepseek')
        papers = src.search('rag')
        self.assertEqual(papers, [])
        self.assertEqual(src.last_batch.runs[0].status, 'skipped')

    def test_unknown_source_is_rejected_by_config(self):
        with self.assertRaises(GuideError):
            Config(source='made_up').validate()

    def test_single_source_failure_still_raises_for_workflow(self):
        src = source('arxiv')
        with patch.object(src, '_arxiv', side_effect=GuideError('arXiv down')):
            with self.assertRaises(GuideError):
                src.search('rag')


class TransportTests(unittest.TestCase):
    def test_429_is_retried_once(self):
        src = source('semantic_scholar')
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _): return b'{"ok": true}'
        error = HTTPError('https://example.org', 429, 'rate limited',
                          {'Retry-After': '0'}, None)
        with patch('source.urlopen', side_effect=[error, Response()]) as opener, \
             patch.object(src.budget, 'wait'):
            body = src._fetch('https://example.org', provider='semantic_scholar')
        self.assertEqual(body, b'{"ok": true}')
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(src.budget.search_http_calls, 2)

    def test_arxiv_429_pauses_without_an_immediate_retry(self):
        src = source('arxiv')
        error = HTTPError('https://export.arxiv.org', 429, 'Rate exceeded.', {}, None)
        with patch('source.urlopen', side_effect=[error]) as opener:
            with self.assertRaises(GuideError):
                src._fetch('https://export.arxiv.org/api/query', provider='arxiv')
        self.assertEqual(opener.call_count, 1)
        self.assertGreater(src.cooldown['arxiv'], time.monotonic() + 800)

    def test_arxiv_rate_exceeded_body_is_not_parsed_as_results(self):
        src = source('arxiv')
        with patch.object(src, '_request_bytes', return_value=b'Rate exceeded.'):
            with self.assertRaises(GuideError):
                src._fetch('https://export.arxiv.org/api/query', provider='arxiv')
        self.assertGreater(src.cooldown['arxiv'], time.monotonic() + 800)
        self.assertEqual(classify_source_error('arXiv rate exceeded; paused'), 'rate_limited')

    def test_timeout_is_retried_once(self):
        src = source('arxiv')
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _): return b'ok'
        with patch('source.urlopen', side_effect=[TimeoutError('slow'), Response()]) as opener,              patch.object(src.budget, 'wait'):
            body = src._fetch('https://example.org', provider='arxiv')
        self.assertEqual(body, b'ok')
        self.assertEqual(opener.call_count, 2)

    def test_begin_turn_resets_deepseek_cap(self):
        src = source('openai', deepseek_search_key='test-key')
        with patch.object(src, '_fetch', return_value=as_json(DEEPSEEK)):
            for _ in range(4):
                src._deepseek_search('rag', 2)
            with self.assertRaises(GuideError):
                src._deepseek_search('rag', 2)
            src.begin_turn()
            self.assertEqual(len(src._deepseek_search('rag', 2)), 1)

    def test_paper_schema_round_trips_new_fields_through_cache_shape(self):
        paper = Paper('arxiv:1', 'Title', 'Abstract', 'https://arxiv.org/abs/1',
                      venue='ACL', open_access_url='https://example.org/p.pdf',
                      retrieved_at='2026-01-01T00:00:00Z', authority=0.8,
                      sources=['arxiv'], abstract_source='arxiv')
        loaded = Paper(**paper.to_dict())
        self.assertEqual(loaded.venue, 'ACL')
        self.assertEqual(loaded.open_access_url, 'https://example.org/p.pdf')
        self.assertEqual(loaded.retrieved_at, '2026-01-01T00:00:00Z')
        self.assertEqual(loaded.authority, 0.8)


if __name__ == '__main__':
    unittest.main()
