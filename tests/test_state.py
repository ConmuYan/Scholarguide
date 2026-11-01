"""State-layer tests: keys, provenance, atomicity and cost conservation.

No network access. Every test uses a temporary SQLite database.
"""
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state import (
    InvariantError,
    StateStore,
    assert_assessment_evidence,
    assert_reuse_key,
    content_hash,
    normalize_text,
    semantic_key,
)


PAPER = {
    'paper_id': '2401.00001',
    'title': 'Retrieval Augmented Generation for Evaluation',
    'abstract': 'We introduce a benchmark that evaluates retrieval augmented generation systems.',
    'url': 'https://arxiv.org/abs/2401.00001',
    'year': 2024,
    'venue': 'arXiv',
    'source': 'arxiv',
}


class KeyTests(unittest.TestCase):
    def test_content_hash_ignores_whitespace_and_case(self):
        a = content_hash('A  Paper', 'Abstract   text')
        b = content_hash('a paper', 'abstract text')
        self.assertEqual(a, b)

    def test_content_hash_changes_with_abstract(self):
        self.assertNotEqual(content_hash('T', 'A'), content_hash('T', 'A revised'))

    def test_semantic_key_covers_every_relevant_field(self):
        base = semantic_key('rag', 'learn', 'evaluation', 'deepseek-flash', 'assess-v2')
        self.assertNotEqual(base, semantic_key('rag', 'experiment', 'evaluation', 'deepseek-flash', 'assess-v2'))
        self.assertNotEqual(base, semantic_key('rag', 'learn', 'survey', 'deepseek-flash', 'assess-v2'))
        self.assertNotEqual(base, semantic_key('rag', 'learn', 'evaluation', 'other-model', 'assess-v2'))
        self.assertNotEqual(base, semantic_key('rag', 'learn', 'evaluation', 'deepseek-flash', 'assess-v3'))
        self.assertEqual(base, semantic_key(' rag ', 'LEARN', 'Evaluation', 'deepseek-flash', 'assess-v2'))

    def test_normalize_text(self):
        self.assertEqual(normalize_text('  a \n b  '), 'a b')


class InvariantTests(unittest.TestCase):
    def test_i1_rejects_unverified_relevance(self):
        with self.assertRaises(InvariantError):
            assert_assessment_evidence(1, False, '')
        with self.assertRaises(InvariantError):
            assert_assessment_evidence(2, True, 'too short')
        assert_assessment_evidence(0, False, '')
        assert_assessment_evidence(1, True, 'this exact quote exists in the abstract')

    def test_i2_rejects_key_drift(self):
        stored = {'content_hash': 'c1', 'semantic_key': 's1', 'model': 'm', 'prompt_version': 'p'}
        assert_reuse_key(stored, dict(stored))
        with self.assertRaises(InvariantError):
            assert_reuse_key(stored, {**stored, 'semantic_key': 's2'})
        with self.assertRaises(InvariantError):
            assert_reuse_key(stored, {**stored, 'model': 'other'})


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = StateStore(self.path)
        self.sid = self.store.create_session({'seconds': 120, 'llm_calls': 24})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _semantic(self, task, model='deepseek-flash', prompt='assess-v2'):
        return semantic_key(task['topic'], task.get('purpose', ''), task.get('category', 'none'),
                            model, prompt)

    def _put(self, task, relevance=2, model='deepseek-flash', prompt='assess-v2'):
        ref = self.store.register_paper(PAPER)
        semantic = self._semantic(task, model, prompt)
        self.store.put_assessment(
            self.sid, paper_id=ref['paper_id'], content=ref['content_hash'], semantic=semantic,
            computed={'content_hash': ref['content_hash'], 'semantic_key': semantic,
                      'model': model, 'prompt_version': prompt},
            relevance=relevance, category_status='yes',
            evidence_quote='We introduce a benchmark that evaluates retrieval augmented generation systems.',
            evidence_verified=True,
        )
        return ref, semantic

    def test_session_roundtrip_and_reopen(self):
        self.store.save_task(self.sid, 1, {'topic': 'rag'}, source_message='find rag papers',
                             change_summary='initial')
        self.store.close()
        self.store = StateStore(self.path)
        current = self.store.current_task(self.sid)
        self.assertEqual(current['revision'], 1)
        self.assertEqual(current['task']['topic'], 'rag')
        self.assertEqual(self.store.get_session(self.sid)['status'], 'active')

    def test_task_versions_are_idempotent_and_monotonic(self):
        self.store.save_task(self.sid, 1, {'topic': 'rag'})
        self.store.save_task(self.sid, 1, {'topic': 'rag'})
        count = self.store.con.execute('SELECT COUNT(*) AS n FROM task_versions WHERE session_id = ?',
                                       (self.sid,)).fetchone()['n']
        self.assertEqual(count, 1)
        self.store.save_task(self.sid, 2, {'topic': 'rag', 'purpose': 'experiment'})
        self.assertEqual(self.store.current_revision(self.sid), 2)
        with self.assertRaises(InvariantError):
            self.store.save_task(self.sid, 1, {'topic': 'stale'})
        self.assertEqual(self.store.current_task(self.sid)['task']['purpose'], 'experiment')

    def test_paper_registration_is_idempotent(self):
        first = self.store.register_paper(PAPER)
        second = self.store.register_paper({**PAPER, 'title': '  Retrieval   Augmented Generation for Evaluation '})
        self.assertEqual(first, second)
        count = self.store.con.execute('SELECT COUNT(*) AS n FROM papers').fetchone()['n']
        self.assertEqual(count, 1)

    def test_candidate_registration_is_idempotent(self):
        self.store.add_candidate(self.sid, PAPER, 'rag evaluation')
        self.store.add_candidate(self.sid, PAPER, 'rag evaluation')
        count = self.store.con.execute('SELECT COUNT(*) AS n FROM candidates WHERE session_id = ?',
                                       (self.sid,)).fetchone()['n']
        self.assertEqual(count, 1)

    def test_assessment_requires_verified_quote_before_write(self):
        ref = self.store.register_paper(PAPER)
        semantic = self._semantic({'topic': 'rag'})
        with self.assertRaises(InvariantError):
            self.store.put_assessment(
                self.sid, paper_id=ref['paper_id'], content=ref['content_hash'], semantic=semantic,
                computed={'content_hash': ref['content_hash'], 'semantic_key': semantic,
                          'model': 'deepseek-flash', 'prompt_version': 'assess-v2'},
                relevance=1, category_status='yes', evidence_quote='', evidence_verified=False,
            )
        count = self.store.con.execute('SELECT COUNT(*) AS n FROM assessments').fetchone()['n']
        self.assertEqual(count, 0)

    def test_assessment_upsert_keeps_one_row(self):
        task = {'topic': 'rag'}
        ref, semantic = self._put(task, relevance=1)
        self.store.put_assessment(
            self.sid, paper_id=ref['paper_id'], content=ref['content_hash'], semantic=semantic,
            computed={'content_hash': ref['content_hash'], 'semantic_key': semantic,
                      'model': 'deepseek-flash', 'prompt_version': 'assess-v2'},
            relevance=2, category_status='yes',
            evidence_quote='evaluates retrieval augmented generation systems',
            evidence_verified=True,
        )
        row = self.store.get_assessment(ref['paper_id'], ref['content_hash'], semantic)
        self.assertEqual(row['relevance'], 2)
        count = self.store.con.execute('SELECT COUNT(*) AS n FROM assessments').fetchone()['n']
        self.assertEqual(count, 1)

    def test_reuse_is_decided_by_semantic_key_not_revision(self):
        task = {'topic': 'rag', 'purpose': 'learn', 'category': 'evaluation'}
        ref, semantic = self._put(task)
        self.store.save_task(self.sid, 1, task)
        self.store.add_candidate(self.sid, PAPER, 'rag')

        same = self.store.valid_assessments(self.sid, [ref['paper_id']], task=task,
                                            model='deepseek-flash', prompt_version='assess-v2')
        self.assertIn(ref['paper_id'], same)

        # Changing the purpose must invalidate the judgement even though the paper is untouched.
        new_task = {'topic': 'rag', 'purpose': 'design an experiment', 'category': 'evaluation'}
        self.store.save_task(self.sid, 2, new_task)
        changed = self.store.valid_assessments(self.sid, [ref['paper_id']], task=new_task,
                                               model='deepseek-flash', prompt_version='assess-v2')
        self.assertNotIn(ref['paper_id'], changed)
        self.assertEqual(self.store.stale_paper_ids(self.sid, [ref['paper_id']], task=new_task,
                                                    model='deepseek-flash',
                                                    prompt_version='assess-v2'),
                         [ref['paper_id']])
        self.assertNotEqual(semantic, self._semantic(new_task))

    def test_events_are_atomic_with_state(self):
        self.store.save_task(self.sid, 1, {'topic': 'rag'})
        with self.assertRaises(RuntimeError):
            with self.store.tx():
                self.store.save_task(self.sid, 2, {'topic': 'changed'})
                self.store.append_event(self.sid, 'search', {'query': 'rag'}, revision=2)
                raise RuntimeError('simulate crash before commit')
        self.assertEqual(self.store.current_revision(self.sid), 1)
        self.assertEqual(self.store.events(self.sid), [])

        with self.store.tx():
            seq = self.store.append_event(self.sid, 'search', {'query': 'rag'}, revision=1)
        self.assertEqual(seq, 1)
        self.assertEqual(len(self.store.events(self.sid)), 1)
        seq2 = self.store.append_event(self.sid, 'assess', {'n': 3}, revision=1)
        self.assertEqual(seq2, 2)

    def test_budget_ledger_conserves_cost(self):
        ledger = self.store.reserve(self.sid, 1, 'search', 2.0, {'query': 'rag'})
        self.store.settle(ledger, 1.5)
        cost = self.store.session_cost(self.sid)
        self.assertEqual({k: cost[k] for k in ('reserved', 'settled', 'refunded')},
                         {'reserved': 2.0, 'settled': 1.5, 'refunded': 0.5})
        self.assertEqual(cost['by_action']['search']['settled'], 1.5)
        with self.assertRaises(InvariantError):
            self.store.settle(ledger, 3.0)
        with self.assertRaises(InvariantError):
            self.store.reserve(self.sid, 2, 'assess', -1)

    def test_candidate_requires_registered_paper(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.con.execute(
                'INSERT INTO candidates(session_id, paper_id, content_hash, first_seen_at) '
                'VALUES (?, ?, ?, ?)', (self.sid, 'missing', 'nope', 'now'))
            self.store.con.commit()
        self.store.con.rollback()


if __name__ == '__main__':
    unittest.main()
