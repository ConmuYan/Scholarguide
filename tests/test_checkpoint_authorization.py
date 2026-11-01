from copy import deepcopy
from unittest.mock import patch

from common import Config, Request
from demo import DemoLLM, DemoSource
from tests.test_checkpoint_preview import CheckpointPreviewTests
from webapp import Jobs
from api_v1 import public_result
from workflow import grounded_assessments, select_papers, run


class AuthorizationTests(CheckpointPreviewTests):
    def test_assessment_cannot_replace_paper_metadata(self):
        cp = deepcopy(self.checkpoint)
        paper = next(p for p in cp['candidates'] if p['id'] == cp['papers'][0]['id'])
        from common import Paper
        original = Paper(**paper)
        row = deepcopy(cp['assessments'][original.id])
        row.update(id='forged', source='lune', title='forged', publication={'status': 'accepted'}, fit_score=87)
        checked = grounded_assessments([row], [original], cp['request']['category'])
        selected = select_papers([original], checked, Request.from_dict(cp['request']))[0]
        for field in ('id', 'source', 'title', 'publication'):
            self.assertEqual(selected[field], paper[field])
        self.assertEqual(selected['fit_score'], 87)
        cp['assessments'][original.id] = row
        cp['papers'][0].update({k: row[k] for k in ('source', 'title', 'publication')})
        value = self.snapshot(cp)
        self.assertFalse((value.get('result') or {}).get('papers'))

    def test_malicious_model_cannot_change_workflow_or_api_metadata(self):
        class Malicious(DemoLLM):
            def generate(self, stage, *args):
                value = super().generate(stage, *args)
                if stage == 'assess':
                    for row in value['assessments']:
                        row.update(id='forged', source='lune', title='forged',
                                   publication={'status': 'accepted'})
                return value
        result = run('fixture', Config(research_mode=False), 'S',
                     source=DemoSource('satisfied'), llm=Malicious())
        candidates = {p['id']: p for p in result['candidates']}
        self.assertTrue(result['papers'])
        for paper in result['papers']:
            self.assertIn(paper['id'], candidates)
            for key in ('title', 'source', 'publication'):
                self.assertEqual(paper[key], candidates[paper['id']][key])
        public = public_result(result)
        self.assertTrue(public['papers'])
        self.assertNotIn('forged', str(public['papers']))

    def test_explicit_goal_and_structured_request_authorization(self):
        cp = deepcopy(self.checkpoint)
        goal = cp['request']['reading_goal']
        other = 'frontier' if goal != 'frontier' else 'comparison'
        self.assertIsNone(self.snapshot(cp, data={'mode': 'demo', 'reading_goal': other})['result'])
        data = {'mode': 'demo', 'request_data': {'topic': cp['request']['topic'], 'target_count': 1}}
        self.assertIsNone(self.snapshot(cp, data=data)['result'])
        data = {'mode': 'demo', 'request_data': {'topic': cp['request']['topic'],
                'reading_goal': other}, 'reading_goal': goal or 'general'}
        cp['request']['reading_goal'] = goal or 'general'
        self.assertIsNotNone(self.snapshot(cp, data=data)['result'])

    def test_unselected_source_and_input_secret(self):
        cp = deepcopy(self.checkpoint)
        cp['config']['source'] = 'arxiv'
        selected = {p['id'] for p in cp['papers']}
        candidate = next(p for p in cp['candidates'] if p['id'] not in selected)
        candidate.update(source='lune', sources=['lune'])
        data = {'mode': 'live', 'source': 'arxiv', 'research_mode': False}
        self.assertIsNone(self.snapshot(cp, data=data)['result'])
        data = {'mode': 'demo', 'request_data': {'topic': cp['request']['topic'],
                                                 'synthetic_secret': 'DO_NOT_EXPOSE'}}
        result = self.snapshot(data=data)['result']
        self.assertIsNotNone(result)
        self.assertNotIn('DO_NOT_EXPOSE', str(result))

    def test_nonchat_structured_request_is_forwarded(self):
        data = {'mode': 'live', 'method': 'S', 'request': 'fixture', 'research_mode': False,
                'request_data': {'topic': 'explicit topic', 'target_count': 3,
                                 'reading_goal': 'orientation'},
                'reading_goal': 'comparison'}
        with patch.object(Jobs, '_config', return_value=Config(research_mode=False)), \
             patch('webapp.AcademicSource', return_value=DemoSource('satisfied')), \
             patch('webapp.JsonLLM', return_value=DemoLLM()):
            result = Jobs()._execute(data, lambda event: None)
        self.assertEqual(result['request']['topic'], 'explicit topic')
        self.assertEqual(result['request']['target_count'], 3)
        self.assertEqual(result['request']['reading_goal'], 'comparison')
