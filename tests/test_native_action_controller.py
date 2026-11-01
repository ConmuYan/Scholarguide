"""Offline tests for native action proposals and the shared policy gate."""
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import controller
from common import SOURCE_NAMES
from controller import BudgetView, ControlState, PolicyError, validate_issued_decision
from scripts.verify_delivery import isolated_environment
from tool_governance import validate_arguments


def state(**changes):
    values = dict(gaps=({'requirement': 'paper_count'},), unassessed=1,
                  candidate_ids=('p1',), unresolved=({'field': 'purpose', 'blocking': True},),
                  budget=BudgetView(60, 5, 1))
    values.update(changes)
    return ControlState(**values)


class NativeControllerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, isolated_environment(Path(tmp.name)), clear=True)
        env.start()
        self.addCleanup(env.stop)
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def test_tools_follow_allowed_actions(self):
        for s in (state(), state(gaps=()), state(awaiting_user=True),
                  state(budget=BudgetView(0, 0, 0))):
            tools = controller.native_action_tools(s)
            self.assertEqual([t['name'] for t in tools],
                             ['sg_' + a.lower() for a in controller.allowed_actions(s)])
            for tool in tools:
                self.assertEqual(set(tool), {'name', 'description', 'parameters'})
                self.assertFalse(tool['parameters']['additionalProperties'])

    def test_payload_exactly_matches_decide(self):
        s = state()
        obs = {'task': {'topic': 'example'}, 'gaps': [{'missing': 1}], 'other': [1]}
        captured = {}
        def fake(stage, llm, payload, schema, validate, prompt, meter):
            captured.update(payload)
            return {'action': 'SEARCH', 'query': 'new', 'reason': 'r'}
        with patch.object(controller, 'run_decision', side_effect=fake):
            controller.decide(None, s, obs)
        expected = {
            'allowed': ['ASSESS', 'SEARCH', 'ASK'],
            'allowed_reasons': {'ASSESS': 'candidates need a judgement under the current task',
                                'SEARCH': 'budget allows one more search',
                                'ASK': 'a blocking ambiguity would otherwise waste a search'},
            'task': {'topic': 'example'}, 'observation': {'other': [1]},
            'budget': {'seconds_left': 60, 'llm_left': 5, 'searches_left': 1},
            'gaps': [{'missing': 1}],
            'unresolved': [{'field': 'purpose', 'blocking': True}], 'candidate_ids': ['p1'],
        }
        self.assertEqual(captured, expected)
        self.assertEqual(controller.action_payload(s, obs), expected)
        self.assertEqual(obs['task'], {'topic': 'example'})

    def test_valid_actions_and_legal_gate(self):
        s = state()
        for name, args, expected in (
            ('sg_search', {'query': 'new', 'reason': 'r'}, 'SEARCH'),
            ('sg_assess', {'paper_ids': ['p1'], 'reason': 'r'}, 'ASSESS'),
            ('sg_ask', {'question': 'Which?', 'affected_field': 'purpose', 'reason': 'r'}, 'ASK'),
        ):
            with self.subTest(name=name):
                d = controller.native_decision(name, args, s)
                self.assertTrue(d.accepted)
                self.assertEqual(d.action, expected)
        d = controller.native_decision('sg_finish', {'reason': 'r'}, s)
        self.assertFalse(d.accepted)
        self.assertEqual((d.error, d.fallback), ('not_allowed', 'ASSESS'))
        self.assertTrue(controller.native_decision('sg_finish', {'reason': 'r'}, state(gaps=())).accepted)

    def test_invalid_parameters_are_not_reflected(self):
        for name, args in (
            ('unknown-secret', {'reason': 'secret-value'}),
            ('sg_search', {'query': 'x', 'reason': 'secret-value', 'action': 'FINISH'}),
            ('sg_assess', {'paper_ids': ['p1'], 'reason': 'r', 'force': True}),
            ('sg_finish', {'reason': 'r', 'extra': 'secret-value'}),
            ('sg_search', {'query': 'x', 'reason': 'r', 'providers': ['invalid']}),
            ('sg_search', {'query': 'x', 'reason': 'r', 'providers': list(SOURCE_NAMES[:3])}),
            ('sg_ask', {'question': 'x', 'affected_field': 'topic', 'reason': 'r'}),
            ('sg_search', {'query': 'x', 'reason': 'r', 'intent': 'bad'}),
        ):
            with self.subTest(name=name, args=args), self.assertRaises(PolicyError) as error:
                controller.native_decision(name, args, state(gaps=()))
            self.assertNotIn('secret-value', str(error.exception))
            self.assertNotIn('unknown-secret', str(error.exception))

    def test_unknown_candidate_and_repeated_query_use_policy(self):
        s = state(last_query='existing')
        self.assertEqual(controller.native_decision('sg_assess',
            {'paper_ids': ['p9'], 'reason': 'r'}, s).error, 'unknown_paper_ids')
        self.assertEqual(controller.native_decision('sg_search',
            {'query': 'existing', 'reason': 'r'}, s).error, 'repeated_query')

    def test_detached_arguments_and_independent_schemas(self):
        s = state()
        params = {'paper_ids': ['p1'], 'reason': 'r'}
        d = controller.native_decision('sg_assess', params, s)
        params['paper_ids'].append('p9')
        self.assertEqual(d.args['paper_ids'], ['p1'])
        tools = controller.native_action_tools(s)
        tools[0]['parameters']['properties']['paper_ids']['items']['type'] = 'integer'
        tools[0]['parameters']['properties']['reason']['maxLength'] = 1
        self.assertEqual(tools[1]['parameters']['properties']['reason']['maxLength'], 1000)
        tools[0]['name'] = 'sg_finish'
        fresh = controller.native_action_tools(s)
        self.assertEqual(fresh[0]['name'], 'sg_assess')
        validate_arguments({'paper_ids': ['p1'], 'reason': 'r'}, fresh[0]['parameters'])
        self.assertTrue(controller.native_decision('sg_assess',
            {'paper_ids': ['p1'], 'reason': 'r'}, s).accepted)

    def test_schema_boundaries_and_current_blocking_fields(self):
        s = state()
        schemas = {t['name']: t['parameters'] for t in controller.native_action_tools(s)}
        for schema in schemas.values():
            self.assertIn('reason', schema['required'])
            self.assertGreater(schema['properties']['reason']['maxLength'], 0)
        self.assertEqual(schemas['sg_ask']['properties']['affected_field']['enum'], ['purpose'])
        for args in ({'query': '', 'reason': 'r'}, {'query': 'x' * 251, 'reason': 'r'},
                     {'query': 'x', 'reason': 'r' * 10000}):
            with self.subTest(args=args), self.assertRaises(PolicyError):
                controller.native_decision('sg_search', args, s)
        for ids in ([], [''], ['x' * 10000], ['p1'] * 1000):
            with self.subTest(ids=ids[:1]), self.assertRaises(PolicyError):
                controller.native_decision('sg_assess', {'paper_ids': ids, 'reason': 'r'}, s)

    def test_issued_decision_revalidated_against_new_state(self):
        decision = controller.native_decision('sg_assess', {'paper_ids': ['p1'], 'reason': 'r'}, state())
        checked = validate_issued_decision(decision, state(unassessed=0, candidate_ids=()))
        self.assertFalse(checked.accepted)
        self.assertEqual(checked.error, 'not_allowed')
        with self.assertRaises(PolicyError):
            controller.native_decision('sg_ask',
                {'question': 'Which?', 'affected_field': 'purpose', 'reason': 'r'},
                state(unresolved=({'field': 'topic', 'blocking': True},)))


if __name__ == '__main__':
    unittest.main()
