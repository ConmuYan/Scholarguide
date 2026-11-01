"""Intent schema compatibility and paper_type persistence."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import GuideError, Paper, Request
from state import StateStore, semantic_key
from workflow import grounded_assessments


QUOTE = 'This paper introduces a benchmark for retrieval augmented generation evaluation.'


class IntentSchemaTests(unittest.TestCase):
    def test_old_request_has_no_reading_goal(self):
        request = Request.from_dict({'topic': 'rag'})
        self.assertEqual(request.reading_goal, '')
        self.assertEqual(request.category, 'none')

    def test_auto_and_research_category(self):
        request = Request.from_dict({'topic': 'rag', 'reading_goal': 'auto',
                                     'category': 'research', 'minimum_category_count': 1})
        self.assertEqual(request.reading_goal, 'auto')
        self.assertEqual(request.category, 'research')
        with self.assertRaises(GuideError):
            Request.from_dict({'topic': 'rag', 'reading_goal': 'made_up'})
        with self.assertRaises(GuideError):
            Request.from_dict({'topic': 'rag', 'category': 'made_up'})

    def test_grounded_assessor_derives_paper_type(self):
        paper = Paper('p1', 'Bench', QUOTE, 'https://example.org/p1')
        checked = grounded_assessments([{'paper_id': 'p1', 'relevance': 2,
                                         'category_status': 'yes', 'paper_type': 'benchmark',
                                         'evidence_quote': QUOTE, 'reason': 'bench'}],
                                       [paper], 'evaluation')
        self.assertEqual(checked['p1']['paper_type'], 'benchmark')
        inferred = grounded_assessments([{'paper_id': 'p1', 'relevance': 2,
                                          'category_status': 'yes',
                                          'evidence_quote': QUOTE, 'reason': 'bench'}],
                                        [paper], 'evaluation')
        self.assertEqual(inferred['p1']['paper_type'], 'benchmark')

    def test_old_assessments_table_migrates_paper_type(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            con = sqlite3.connect(path)
            con.execute('''CREATE TABLE assessments (
                paper_id TEXT NOT NULL, content_hash TEXT NOT NULL, semantic_key TEXT NOT NULL,
                computed_with TEXT NOT NULL, relevance INTEGER NOT NULL,
                category_status TEXT NOT NULL, understanding_sentence TEXT,
                evidence_quote TEXT NOT NULL DEFAULT '', evidence_verified INTEGER NOT NULL DEFAULT 0,
                unknown_reason TEXT, model TEXT, prompt_version TEXT, computed_at TEXT NOT NULL,
                PRIMARY KEY (paper_id, content_hash, semantic_key))''')
            con.commit(); con.close()
            store = StateStore(path)
            try:
                columns = {row[1] for row in store.con.execute('PRAGMA table_info(assessments)')}
                self.assertIn('paper_type', columns)
            finally:
                store.close()

    def test_paper_type_is_persisted_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                session = store.create_session({})
                paper = Paper('p1', 'Bench', QUOTE, 'https://example.org/p1')
                store.add_candidate(session, paper.to_dict(), 'rag')
                ref = store.candidate_refs(session)[0]
                task = {'topic': 'rag', 'category': 'none'}
                semantic = semantic_key('rag', '', 'none', 'm', 'v')
                store.put_assessment(session, paper_id='p1', content=ref['content_hash'],
                                     semantic=semantic, computed={'content_hash': ref['content_hash'],
                                     'semantic_key': semantic, 'model': 'm', 'prompt_version': 'v'},
                                     relevance=2, category_status='yes', paper_type='benchmark',
                                     evidence_quote=QUOTE, evidence_verified=True)
                rows = store.valid_assessments(session, ['p1'], task=task, model='m', prompt_version='v')
                self.assertEqual(rows['p1']['paper_type'], 'benchmark')
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
