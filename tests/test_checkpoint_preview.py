from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from common import Config
from demo import demo_run
from recovery import atomic_json
from webapp import Jobs


class CheckpointPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job = 'a' * 32
        checkpoints = []
        def progress(event, checkpoint=None):
            if event['action'] == 'assess':
                checkpoints.append(deepcopy(checkpoint))
        progress.accepts_checkpoint = True
        demo_run('satisfied', config=Config(batch_size=2, research_mode=False), progress=progress)
        self.checkpoint = checkpoints[0]

    def snapshot(self, checkpoint=None, runner=True, calls=None, data=None):
        directory = self.root / self.job
        atomic_json(directory / 'job.json', {'input': data or {'mode': 'demo', 'method': 'A'},
            'recoverable_runner': runner, 'job': {'id': self.job, 'state': 'running',
            'trace': [], 'result': None, 'checkpoint': deepcopy(checkpoint if checkpoint is not None else self.checkpoint)}})
        atomic_json(directory / 'calls.json', {'version': 1, 'calls': calls if calls is not None else []})
        return Jobs(self.root).snapshot(self.job)

    def test_verified_checkpoint_and_pending_are_independent(self):
        for calls in ([], [{'state': 'pending'}]):
            with self.subTest(calls=calls):
                value = self.snapshot(calls=calls)
                self.assertEqual(value['recovery']['can_resume'], not bool(calls))
                self.assertEqual(value['result']['status'], 'partial')
                self.assertEqual(value['result']['stop_reason'], 'process_interrupted')
                self.assertEqual(len(value['result']['papers']), 2)
                self.assertGreater(len(value['result']['candidates']), 2)

    def test_tampered_checkpoint_fails_closed(self):
        def quote(c): c['assessments'][c['papers'][0]['id']]['evidence_quote'] = 'invented evidence that is not present'
        def abstract(c): c['papers'][0]['abstract'] += ' altered'
        def publication(c): c['papers'][0]['publication'] = {'status': 'accepted'}
        def duplicate(c): c['candidates'].append(deepcopy(c['candidates'][0]))
        def request(c): c['request']['target_count'] = 0
        def short_quote(c): c['assessments'][c['papers'][0]['id']]['evidence_quote'] = 'short'
        def empty_reason(c): c['assessments'][c['papers'][0]['id']]['reason'] = '  '
        def selected_evidence(c): c['papers'][0]['evidence_quote'] = 'forged evidence'
        def duplicate_selected(c): c['papers'].append(deepcopy(c['papers'][0]))
        def unknown_assessment(c): c['assessments']['forged'] = deepcopy(next(iter(c['assessments'].values())))
        def too_many(c): c['candidates'] = c['candidates'] * 5
        def year_constraint(c): c['request']['year_from'] = 2099
        def venue_constraint(c): c['request']['venue'] = 'Nonexistent Venue'
        def accepted_constraint(c): c['request']['accepted_only'] = True
        def excluded(c): c['request']['excluded_paper_ids'] = [c['papers'][0]['id']]
        for mutate in (quote, abstract, publication, duplicate, request, short_quote,
                       empty_reason, selected_evidence, duplicate_selected, unknown_assessment, too_many,
                       year_constraint, venue_constraint, accepted_constraint, excluded):
            with self.subTest(mutate=mutate.__name__):
                cp = deepcopy(self.checkpoint)
                mutate(cp)
                value = self.snapshot(cp)
                self.assertFalse((value.get('result') or {}).get('papers'))
                self.assertTrue(value['recovery']['checkpoint_available'])
                self.assertTrue((self.root / self.job / 'job.json').exists())

    def test_live_source_constraint_is_enforced(self):
        cp = deepcopy(self.checkpoint)
        cp['config']['source'] = 'arxiv'
        selected_id = cp['papers'][0]['id']
        candidate = next(p for p in cp['candidates'] if p['id'] == selected_id)
        candidate['sources'] = ['lune']
        candidate['source'] = 'lune'
        cp['papers'][0]['sources'] = ['lune']
        cp['papers'][0]['source'] = 'lune'
        value = self.snapshot(cp, data={'mode': 'live', 'source': 'arxiv', 'research_mode': False})
        self.assertFalse((value.get('result') or {}).get('papers'))

    def test_journal_candidates_are_projected_and_source_restricted(self):
        calls = [{'state': 'done', 'kind': 'source.search', 'output': {'papers': [
            {'id': 'allowed', 'title': 'Allowed', 'source': 'arxiv', 'sources': ['arxiv'],
             'secret': 'synthetic-private', 'publication': {'status': 'unknown',
             'records': [{'status': 'unknown', 'secret': 'synthetic-private'}]}},
            {'id': 'denied', 'title': 'Denied', 'source': 'lune', 'sources': ['lune']},
        ]}}]
        value = self.snapshot(calls=calls, data={'mode': 'live', 'source': 'arxiv', 'research_mode': False})
        candidates = value['result']['candidates']
        allowed = next(paper for paper in candidates if paper.get('id') == 'allowed')
        self.assertNotIn('synthetic-private', repr(allowed))
        self.assertFalse(any(paper.get('id') == 'denied' for paper in candidates))

    def test_checkpoint_nested_metadata_is_never_reflected(self):
        cp = deepcopy(self.checkpoint)
        paper_id = cp['papers'][0]['id']
        candidate = next(p for p in cp['candidates'] if p['id'] == paper_id)
        candidate['publication']['synthetic-secret'] = 'synthetic-private'
        cp['papers'][0]['publication']['synthetic-secret'] = 'synthetic-private'
        value = self.snapshot(cp)
        self.assertNotIn('synthetic-private', repr(value['result']))

    def test_custom_runner_and_corrupt_journal(self):
        self.assertIsNone(self.snapshot(runner='yes')['result'])
        self.assertIsNone(self.snapshot(runner=False)['result'])
        value = self.snapshot(calls=[{'state': 'bogus'}])
        self.assertFalse(value['recovery']['can_resume'])
        self.assertEqual(value['recovery']['reason'], 'journal_corrupt')
        self.assertEqual(len(value['result']['papers']), 2)
