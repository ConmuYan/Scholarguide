"""Reuse-planning tests: what survives an intent change, and what must be recomputed."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reuse import plan_reuse, record_reuse, verify_delivery
from state import StateStore, semantic_key

MODEL = 'deepseek-flash'
PROMPT = 'assess-v2'
QUOTE = 'We introduce a benchmark that evaluates retrieval augmented generation systems.'
PAPER = {
    'paper_id': '2401.00001',
    'title': 'Retrieval Augmented Generation for Evaluation',
    'abstract': QUOTE,
    'year': 2024,
    'venue': 'arXiv',
}

TASK = {'topic': 'rag', 'purpose': 'learn', 'category': 'evaluation'}


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / 'state.sqlite3')
        self.sid = self.store.create_session()
        self.store.save_task(self.sid, 1, TASK, source_message='find rag papers')
        self.store.add_candidate(self.sid, PAPER, 'rag')
        self.ref = self.store.register_paper(PAPER)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _assess(self, task=None, *, relevance=2, model=MODEL, prompt=PROMPT, paper=None,
                verified=True, quote=QUOTE):
        task = task or TASK
        paper = paper or PAPER
        ref = self.store.register_paper(paper)
        semantic = semantic_key(task['topic'], task.get('purpose', ''),
                                task.get('category', 'none'), model, prompt)
        self.store.put_assessment(
            self.sid, paper_id=ref['paper_id'], content=ref['content_hash'], semantic=semantic,
            computed={'content_hash': ref['content_hash'], 'semantic_key': semantic,
                      'model': model, 'prompt_version': prompt},
            relevance=relevance, category_status='yes', evidence_quote=quote,
            evidence_verified=verified,
        )
        return semantic

    def test_unchanged_task_reuses_everything(self):
        self._assess()
        plan = plan_reuse(self.store, self.sid, TASK, model=MODEL, prompt_version=PROMPT)
        self.assertEqual(plan.reused_ids, ['2401.00001'])
        self.assertEqual(plan.stale_ids, [])
        self.assertEqual(plan.reuse_rate, 1.0)
        check = verify_delivery(self.store, self.sid, ['2401.00001'], TASK,
                                model=MODEL, prompt_version=PROMPT)
        self.assertTrue(check.ok)
        self.assertEqual(check.reason, '')

    def test_purpose_change_invalidates_and_blocks_delivery(self):
        self._assess()
        changed = dict(TASK, purpose='design an ablation')
        self.store.save_task(self.sid, 2, changed, source_message='now for experiments')
        plan = plan_reuse(self.store, self.sid, changed, model=MODEL, prompt_version=PROMPT)
        self.assertEqual(plan.stale_ids, ['2401.00001'])
        self.assertEqual(plan.items[0].reason, 'semantic_key_changed')
        check = verify_delivery(self.store, self.sid, ['2401.00001'], changed,
                                model=MODEL, prompt_version=PROMPT)
        self.assertFalse(check.ok)
        self.assertEqual(check.reason, 'stale_assessments')
        self.assertEqual(check.stale_reuse, 1)

    def test_abstract_change_invalidates_by_content_hash(self):
        self._assess()
        revised = dict(PAPER, abstract=QUOTE + ' The benchmark also reports cost.')
        self.store.register_paper(revised)
        self.store.add_candidate(self.sid, revised, 'rag')
        plan = plan_reuse(self.store, self.sid, TASK, model=MODEL, prompt_version=PROMPT)
        self.assertEqual(plan.stale_ids, ['2401.00001'])
        self.assertEqual(plan.items[0].reason, 'content_changed')

    def test_model_or_prompt_change_is_reported_separately(self):
        self._assess()
        plan = plan_reuse(self.store, self.sid, TASK, model='other-model', prompt_version=PROMPT)
        self.assertEqual(plan.items[0].reason, 'model_changed')
        plan = plan_reuse(self.store, self.sid, TASK, model=MODEL, prompt_version='assess-v3')
        self.assertEqual(plan.items[0].reason, 'prompt_changed')

    def test_never_assessed_is_missing(self):
        plan = plan_reuse(self.store, self.sid, TASK, model=MODEL, prompt_version=PROMPT)
        self.assertEqual(plan.items[0].reason, 'missing')
        self.assertEqual(plan.reuse_rate, 0.0)

    def test_delivery_rejects_unverified_evidence(self):
        self._assess(relevance=0, verified=True)
        check = verify_delivery(self.store, self.sid, ['2401.00001'], TASK,
                                model=MODEL, prompt_version=PROMPT)
        self.assertFalse(check.ok)
        self.assertEqual(check.reason, 'unsupported_evidence')

    def test_reuse_event_explains_every_stale_paper(self):
        self._assess()
        changed = dict(TASK, category='survey')
        plan = plan_reuse(self.store, self.sid, changed, model=MODEL, prompt_version=PROMPT)
        record_reuse(self.store, self.sid, plan, revision=2)
        event = self.store.events(self.sid)[-1]
        self.assertEqual(event['action'], 'reuse')
        self.assertEqual(event['payload']['reuse_rate'], 0.0)
        self.assertEqual(event['payload']['stale'][0]['reason'], 'semantic_key_changed')

    def test_reuse_does_not_depend_on_revision(self):
        self._assess()
        # A revision bump that changes nothing semantic must not invalidate anything.
        self.store.save_task(self.sid, 2, dict(TASK, K=4), source_message='K=4')
        plan = plan_reuse(self.store, self.sid, TASK, model=MODEL, prompt_version=PROMPT)
        self.assertEqual(plan.reuse_rate, 1.0)


if __name__ == '__main__':
    unittest.main()
