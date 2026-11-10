"""Explicit guided controls survive planning; all fixtures stay offline."""
from copy import deepcopy
from datetime import date
from http.client import HTTPConnection
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from common import Config, GuideError, Paper
from demo import demo_run
from providers import ProviderStore
import prompts
from webapp import ChatService, Jobs, create_server, validate_input
from workflow import run, validate_request_constraints


CONSTRAINTS = {'target_count': 3, 'category': 'evaluation', 'minimum_category_count': 2,
               'venue': 'CVPR', 'year_from': 2024, 'year_to': 2025}
QUOTE = 'We introduce a reproducible benchmark for visual language adaptation.'
PAPERS = [
    Paper('p1', 'First visual benchmark', QUOTE, 'https://example.test/p1', year=2024, venue='CVPR'),
    Paper('p2', 'Second visual benchmark', QUOTE, 'https://example.test/p2', year=2025, venue='CVPR'),
    Paper('p3', 'Older visual benchmark', QUOTE, 'https://example.test/p3', year=2023, venue='CVPR'),
    Paper('p4', 'Other venue benchmark', QUOTE, 'https://example.test/p4', year=2024, venue='ACL'),
    Paper('p5', 'A new visual method', QUOTE, 'https://example.test/p5', year=2025, venue='CVPR'),
]


class Source:
    mode = 'demo'

    def __init__(self):
        self.queries = []

    def search(self, query, limit=10):
        self.queries.append(query)
        return list(PAPERS)


class Planner:
    mode = 'demo'

    def __init__(self):
        self.calls = []
        self.raw = {'topic': 'visual language adaptation', 'query': 'visual adaptation benchmark',
                    'target_count': 4, 'category': 'survey', 'minimum_category_count': 2,
                    'venue': 'ACL', 'year_from': 2018, 'year_to': 2020,
                    'reading_goal': 'comparison', 'unsupported': []}

    def generate(self, stage, prompt, data, schema, validate):
        self.calls.append((stage, deepcopy(data)))
        if stage == 'plan':
            value = deepcopy(self.raw)
        elif stage == 'assess':
            value = {'assessments': [
                {'paper_id': p['paper_id'], 'relevance': 2,
                 'category_status': 'yes' if p['paper_id'] in ('p1', 'p2') else 'no',
                 'paper_type': 'benchmark' if p['paper_id'] in ('p1', 'p2') else 'research',
                 'evidence_quote': QUOTE, 'reason': 'The abstract describes a visual benchmark.'}
                for p in data['papers']]}
        else:
            raise AssertionError('unexpected extra planning/refinement call')
        validate(value)
        return value


class GuidedConstraintTests(unittest.TestCase):
    def test_explicit_purpose_reaches_request_and_assessment_while_old_plans_remain_valid(self):
        for purpose in ('用于设计可复现的实验并比较评测方案', None):
            with self.subTest(purpose=purpose):
                llm = Planner()
                if purpose is not None:
                    llm.raw['purpose'] = purpose
                result = run('研究视觉语言适配，' + (purpose or '推荐相关论文'), Config(), 'S',
                             source=Source(), llm=llm, request_constraints=CONSTRAINTS)
                self.assertEqual(result['request']['purpose'], purpose or '')
                assessment = next(data for stage, data in llm.calls if stage == 'assess')
                self.assertEqual(assessment['purpose'], purpose or '')
        schema = prompts.PLAN_SCHEMA
        self.assertEqual(schema['properties']['purpose'], {'type': 'string', 'maxLength': 500})
        self.assertNotIn('purpose', schema['required'])
        self.assertIn('topic, purpose, reading_goal', prompts.PLAN)

    def test_planner_receives_runtime_date_for_relative_years(self):
        class RelativePlanner(Planner):
            def generate(self, stage, prompt, data, schema, validate):
                if stage == 'plan':
                    current_year = date.fromisoformat(data['current_date']).year
                    self.raw.update(year_from=current_year - 1, year_to=current_year)
                return super().generate(stage, prompt, data, schema, validate)
        for current in (date(2026, 10, 20), date(2029, 1, 1)):
            with self.subTest(current=current):
                llm = RelativePlanner()
                with patch('workflow.date') as runtime_date:
                    runtime_date.today.return_value = current
                    result = run('Find visual adaptation papers from the last two years.',
                                 Config(), 'S', source=Source(), llm=llm)
                plan_input = llm.calls[0][1]
                self.assertEqual(plan_input['current_date'], current.isoformat())
                self.assertEqual(result['request']['year_from'], current.year - 1)
                self.assertEqual(result['request']['year_to'], current.year)
        self.assertIn('current_date and its preceding year', prompts.PLAN)
        self.assertNotIn('year_from=2024', prompts.PLAN)
        self.assertNotIn('2026-10', prompts.PLAN)

    def test_runtime_date_does_not_replace_explicit_years(self):
        llm = Planner()
        with patch('workflow.date') as runtime_date:
            runtime_date.today.return_value = date(2029, 1, 1)
            result = run('Find visual adaptation papers published in 2018–2020.', Config(), 'S',
                         source=Source(), llm=llm)
        self.assertEqual(llm.calls[0][1]['current_date'], '2029-01-01')
        self.assertEqual(result['request']['year_from'], 2018)
        self.assertEqual(result['request']['year_to'], 2020)

    def test_six_explicit_controls_override_drifting_plan_without_replacing_topic_or_query(self):
        llm, source = Planner(), Source()
        original = deepcopy(CONSTRAINTS)
        result = run('Find visual adaptation papers.', Config(batch_size=5), 'S',
                     source=source, llm=llm, request_constraints=CONSTRAINTS)
        self.assertEqual(result['status'], 'complete')
        for field, value in CONSTRAINTS.items():
            self.assertEqual(result['request'][field], value)
        self.assertEqual(result['request']['topic'], llm.raw['topic'])
        self.assertEqual(source.queries, [llm.raw['query']])
        self.assertEqual([p['id'] for p in result['papers']], ['p1', 'p2', 'p5'])
        self.assertEqual(CONSTRAINTS, original)
        plan = next(step for step in result['trace'] if step['action'] == 'plan')
        self.assertEqual(plan['llm_plan'], llm.raw)
        self.assertEqual(plan['request_constraints'], CONSTRAINTS)
        self.assertEqual(plan['constraint_source'], 'guided_form')
        self.assertNotEqual(plan['llm_plan']['venue'], plan['request']['venue'])
        self.assertEqual(llm.calls[0][1]['request_constraints'], CONSTRAINTS)
        self.assertEqual(next(data for stage, data in llm.calls if stage == 'assess')['category'], 'evaluation')

    def test_natural_request_without_dictionary_keeps_planner_fields(self):
        llm = Planner()
        result = run('An unrestricted full research request.', Config(), 'S', source=Source(), llm=llm)
        for field in CONSTRAINTS:
            self.assertEqual(result['request'][field], llm.raw[field])
        self.assertNotIn('request_constraints', llm.calls[0][1])
        plan = next(step for step in result['trace'] if step['action'] == 'plan')
        self.assertNotIn('request_constraints', plan)

    def test_auto_goal_records_actual_planner_origin_and_explicit_goal_records_override(self):
        for goal, expected_source, expected_goal in (
                ('auto', 'planner_output', 'comparison'),
                ('orientation', 'goal_hint', 'orientation')):
            with self.subTest(goal=goal):
                result = run('Visual adaptation.', Config(), 'S', source=Source(), llm=Planner(),
                             goal_hint=goal, request_constraints=CONSTRAINTS)
                intent = next(step for step in result['trace'] if step['action'] == 'intent')
                self.assertEqual(intent['source'], expected_source)
                self.assertEqual(intent['goal'], expected_goal)

    def test_constraint_validation_is_strict_and_does_not_spend_on_invalid_input(self):
        invalid = [None, [], {}, {**CONSTRAINTS, 'topic': 'injected topic'},
                   {**CONSTRAINTS, 'target_count': True}, {**CONSTRAINTS, 'target_count': '3'},
                   {**CONSTRAINTS, 'target_count': 0}, {**CONSTRAINTS, 'target_count': 11},
                   {**CONSTRAINTS, 'minimum_category_count': True},
                   {**CONSTRAINTS, 'minimum_category_count': 4},
                   {**CONSTRAINTS, 'category': 'none'}, {**CONSTRAINTS, 'category': []},
                   {**CONSTRAINTS, 'venue': 0}, {**CONSTRAINTS, 'venue': 'v' * 121},
                   {**CONSTRAINTS, 'year_from': '2024'}, {**CONSTRAINTS, 'year_to': True},
                   {**CONSTRAINTS, 'year_from': 1899}, {**CONSTRAINTS, 'year_to': 2101},
                   {**CONSTRAINTS, 'year_from': 2026}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(GuideError):
                validate_request_constraints(value)
        for value in invalid[1:]:  # None is the direct workflow's omitted optional argument
            llm, source = Planner(), Source()
            with self.subTest(workflow=value), self.assertRaises(GuideError):
                run('Invalid controls.', Config(), 'S', llm=llm, source=source,
                    request_constraints=value)
            self.assertEqual(llm.calls, [])
            self.assertEqual(source.queries, [])

    def test_unrestricted_controls_and_explicit_zero_quota_remain_explicit(self):
        constraints = {**CONSTRAINTS, 'category': 'none', 'minimum_category_count': 0,
                       'venue': '', 'year_from': None, 'year_to': None}
        result = run('Visual adaptation.', Config(), 'S', source=Source(), llm=Planner(),
                     request_constraints=constraints)
        for field, value in constraints.items():
            self.assertEqual(result['request'][field], value)

    def test_job_validation_and_execution_forward_only_the_validated_dictionary(self):
        data = validate_input({'mode': 'live', 'method': 'S', 'request': 'Visual adaptation.',
                               'source': 'arxiv', 'cache': False, 'reading_goal': 'auto',
                               'request_constraints': {**CONSTRAINTS, 'venue': '  CVPR  '}})
        self.assertEqual(data['request_constraints'], CONSTRAINTS)
        with patch('webapp.Config.from_env', return_value=Config()), patch('webapp.run', return_value={}) as runner:
            Jobs()._execute(data, lambda event: None)
        self.assertEqual(runner.call_args.kwargs['request_constraints'], CONSTRAINTS)
        self.assertEqual(runner.call_args.kwargs['goal_hint'], 'auto')
        with self.assertRaises(GuideError):
            validate_input({'mode': 'demo', 'request_constraints': CONSTRAINTS})
        with self.assertRaises(GuideError):
            validate_input({'mode': 'live', 'request': 'Visual adaptation.', 'request_constraints': None})


class GuidedConstraintHttpTests(unittest.TestCase):
    def test_invalid_json_controls_are_rejected_before_a_worker_and_valid_controls_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            received, entered = [], threading.Event()
            def offline(data, progress):
                received.append(data)
                entered.set()
                return demo_run('satisfied', 'S')
            jobs = Jobs(root / 'runs', runner=offline)
            chat = ChatService(root / 'chat')
            server = create_server(0, jobs=jobs, chat=chat,
                                   providers=ProviderStore(root / 'providers.json'))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            def post(body):
                connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('POST', '/api/jobs', json.dumps(body).encode(),
                                   {'Content-Type': 'application/json'})
                response = connection.getresponse()
                status, data = response.status, json.loads(response.read())
                connection.close()
                return status, data
            try:
                base = {'mode': 'live', 'method': 'S', 'request': 'Visual adaptation.',
                        'source': 'arxiv', 'request_constraints': CONSTRAINTS}
                for malformed in ({**CONSTRAINTS, 'target_count': '3'},
                                  {**CONSTRAINTS, 'minimum_category_count': 4},
                                  {**CONSTRAINTS, 'api_key': 'untrusted-field'}):
                    status, _ = post({**base, 'request_constraints': malformed})
                    self.assertEqual(status, 400)
                self.assertEqual(received, [])
                self.assertEqual(jobs.items, {})
                status, _ = post(base)
                self.assertEqual(status, 202)
                self.assertTrue(entered.wait(2))
                self.assertEqual(received[0]['request_constraints'], CONSTRAINTS)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(2)
                # The offline worker may still be saving/importing its fixture.
                deadline = threading.Event()
                for _ in range(100):
                    if jobs.active is None:
                        break
                    deadline.wait(.02)
                chat.close()


if __name__ == '__main__':
    unittest.main()

class SingleSourceFiltersTests(unittest.TestCase):
    def test_recovers_whitelist_from_actual_planner_failure_before_search(self):
        field = 'Eligible conferences: ICLR, ICML, NeurIPS, ACL, EMNLP, AAAI'
        llm = Planner()
        llm.raw.update(topic='agents', query='agents', target_count=1, category='none',
                       minimum_category_count=0, venue='', year_from=2024, year_to=2026,
                       reading_goal='general', unsupported=[field])
        class FilteredSource(Source):
            supports_search_filters = True
            def search(self, query, limit=10, *, filters=None):
                self.filters = filters
                return [Paper('p1', 'Paper', QUOTE, 'https://example.test', year=2025, venue='ACL')]
        source = FilteredSource()
        result = run('Recommend one paper.\n' + field, Config(), 'S', source=source, llm=llm)
        self.assertEqual(result['request']['unsupported'], [])
        self.assertEqual(result['request']['venue'], 'ICLR, ICML, NeurIPS, ACL, EMNLP, AAAI')
        self.assertEqual(source.filters['venue'], result['request']['venue'])
        self.assertNotEqual(result['status'], 'unsupported')

    def test_source_receives_filters_without_changing_legacy_adapter(self):
        class FilteredSource(Source):
            supports_search_filters = True
            def search(self, query, limit=10, *, filters=None):
                self.filters = filters
                return list(PAPERS)
        source = FilteredSource()
        result = run('Visual adaptation.', Config(), 'S', source=source, llm=Planner(), request_constraints=CONSTRAINTS)
        self.assertEqual(source.filters, {'venue':'CVPR', 'year_from':2024, 'year_to':2025})
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['metrics']['cost_units']['by_action']['search']['settled'], 1)

    def test_acl_verification_is_bounded_and_cancellation_propagates(self):
        from common import RunCancelled
        class ACLSource(Source):
            def __init__(self, cancel=False): self.calls = []; self.cancel = cancel
            def search(self, query, limit=10):
                return [Paper(str(i), 'Paper '+str(i), QUOTE, '', doi='10.18653/v1/2025.acl-long.'+str(i), year=2025, venue='ACL') for i in range(1,8)]
            def verify_acl_paper(self, paper):
                self.calls.append(paper.id)
                if self.cancel: raise RunCancelled()
                return paper
        source = ACLSource()
        run('x', Config(), 'S', source=source, llm=Planner(), request_data={'topic':'test','query':'test','accepted_only':True,'venue':'ACL','year_from':2024,'year_to':2026})
        self.assertEqual(len(source.calls),5)
        source = ACLSource(cancel=True)
        result = run('x', Config(), 'S', source=source, llm=Planner(), request_data={'topic':'test','query':'test','accepted_only':True,'venue':'ACL'})
        self.assertEqual(len(source.calls),1)
        self.assertIn('Stopped by the user', result['stop_reason'])


class ACLVerificationSelectionTest(unittest.TestCase):
    def test_officially_verified_candidate_can_be_selected(self):
        from dataclasses import replace
        from common import merge_publication_records
        class VerifiedSource(Source):
            def search(self, query, limit=10):
                return [Paper('p1', 'Paper One', QUOTE, 'https://example.test', doi='10.18653/v1/2025.acl-long.1', year=2025, venue='ACL')]
            def verify_acl_paper(self, paper):
                record = {'type':'conference','status':'published','venue_name':'ACL', 'venue_id':'2025.acl-long','date':'2025','verified':True,'identity_verified':True,'evidence':[{'source':'acl_anthology','url':'https://aclanthology.org/2025.acl-long.1/'}]}
                return replace(paper, publication=merge_publication_records([record]))
        result=run('x',Config(),'S',source=VerifiedSource(),llm=Planner(),request_data={'topic':'test','query':'test','target_count':1,'accepted_only':True,'venue':'ACL','year_from':2025,'year_to':2025})
        self.assertEqual([p['id'] for p in result['papers']],['p1'])
        self.assertEqual(result['papers'][0]['publication']['status'],'published')
