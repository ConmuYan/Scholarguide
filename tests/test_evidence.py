"""Evidence, provenance and conflict tests for the multi-source path."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Paper, Request
from source import merge_papers
from state import StateStore
from workflow import grounded_assessments, select_papers

QUOTE = 'We introduce a benchmark for retrieval augmented generation evaluation.'


def paper(**overrides):
    data = dict(id='p1', title='RAG Evaluation', abstract=QUOTE,
                url='https://example.org/p1', year=2024, venue='ACL',
                doi='10.1234/rag', sources=['semantic_scholar'],
                abstract_source='semantic_scholar', authority=0.8,
                open_access_url='https://example.org/p1.pdf')
    data.update(overrides)
    return Paper(**data)


class GroundingTests(unittest.TestCase):
    def test_exact_quote_is_verified(self):
        rows = [{'paper_id': 'p1', 'relevance': 2, 'category_status': 'yes',
                 'evidence_quote': QUOTE, 'reason': 'It is an evaluation benchmark.'}]
        checked = grounded_assessments(rows, [paper()], 'evaluation')
        self.assertTrue(checked['p1']['evidence_verified'])

    def test_paraphrase_is_rejected_and_downgraded(self):
        rows = [{'paper_id': 'p1', 'relevance': 2, 'category_status': 'yes',
                 'evidence_quote': 'This paper is a benchmark.', 'reason': 'It is one.'}]
        checked = grounded_assessments(rows, [paper()], 'evaluation')
        self.assertFalse(checked['p1']['evidence_verified'])
        self.assertEqual(checked['p1']['relevance'], 0)

    def test_metadata_only_is_not_selected(self):
        rows = [{'paper_id': 'p1', 'relevance': 2, 'category_status': 'yes',
                 'evidence_quote': '', 'reason': 'looks relevant'}]
        candidate = paper(abstract='', metadata_only=True)
        checked = grounded_assessments(rows, [candidate], 'none')
        selected = select_papers([candidate], checked, Request(topic='RAG', target_count=5))
        self.assertEqual(selected, [])

    def test_conflict_flag_is_added_to_assessment_warning(self):
        rows = [{'paper_id': 'p1', 'relevance': 2, 'category_status': 'yes',
                 'evidence_quote': QUOTE, 'reason': 'It is an evaluation benchmark.'}]
        checked = grounded_assessments(rows, [paper(provenance_conflict=True)], 'evaluation')
        self.assertTrue(checked['p1']['provenance_conflict'])
        self.assertIn('Cross-source', checked['p1']['warning'])


class ProvenanceTests(unittest.TestCase):
    def test_cross_source_disagreement_is_flagged(self):
        a = paper(id='doi:10.1234/rag', abstract=QUOTE)
        b = paper(id='s2:x', abstract='A completely different abstract about image segmentation.')
        merged = merge_papers([a, b])
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0].provenance_conflict)

    def test_state_persists_sources_oa_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                item = paper(sources=['dblp', 'semantic_scholar'], metadata_only=False)
                session_id = store.create_session({})
                store.add_candidate(session_id, item.to_dict(), 'rag')
                row = store.candidate_papers(session_id)[0]
                self.assertEqual(row['venue'], 'ACL')
                self.assertEqual(row['open_access_url'], 'https://example.org/p1.pdf')
                self.assertEqual(row['sources_json'], '["dblp", "semantic_scholar"]')
                self.assertEqual(row['abstract_source'], 'semantic_scholar')
            finally:
                store.close()

    def test_old_cache_without_new_fields_still_loads(self):
        old = {'id': 'arxiv:1', 'title': 'T', 'abstract': 'A', 'url': 'u',
               'sources': ['arxiv'], 'fetched_at': '2026-01-01'}
        item = Paper(**old)
        self.assertEqual(item.venue, '')
        self.assertEqual(item.retrieved_at, '2026-01-01')
        self.assertFalse(item.provenance_conflict)


if __name__ == '__main__':
    unittest.main()
