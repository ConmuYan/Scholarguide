import copy
import json
import unittest
from unittest.mock import patch

from common import Budget, BudgetExceeded, Config, GuideError, RunCancelled
from llm import JsonLLM
from native_tool_protocol import PROBE_TOOL


def first(arguments='{"value":"fixed"}', name='native_probe'):
    return {'status': 'completed', 'model': 'observed', 'usage': {'input_tokens': 3, 'output_tokens': 2},
            'output': [{'type': 'reasoning', 'id': 'r', 'summary': []},
                       {'type': 'function_call', 'call_id': 'c1', 'name': name, 'arguments': arguments}]}


def second():
    return {'status': 'completed', 'usage': {'input_tokens': 4, 'output_tokens': 1},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'done'}]}]}


class NativeActionTests(unittest.TestCase):
    def setUp(self):
        self.budget = Budget(60)
        self.guards = []
        self.budget.request_guard = lambda: self.guards.append(1)
        self.llm = JsonLLM(Config(protocol='responses', base_url='https://example.invalid', api_key='secret'), self.budget)
        self.requests = []

    def transport(self, replies):
        def post(url, payload, headers):
            self.requests.append(copy.deepcopy(payload))
            self.budget.llm_calls += 1
            self.llm.last_http_status = 200
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return patch.object(self.llm, '_post', side_effect=post)

    def propose(self):
        return self.llm.propose_action('private prompt', {'x': 1}, [PROBE_TOOL])

    def test_raw_transport_counts_and_status(self):
        from network import BufferedResponse
        replies = [first(), second()]
        def open_fake(request, timeout):
            return BufferedResponse(json.dumps(replies.pop(0)).encode(), status=200,
                                    headers=[('x-model', 'header-model')])
        with patch('llm.urlopen', side_effect=open_fake) as opened:
            proposal = self.propose()
            self.llm.continue_action(proposal, '{}')
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(self.budget.llm_calls, 2)
        self.assertEqual(self.budget.usage_calls, 2)
        self.assertEqual(len(self.guards), 2)
        self.assertEqual(self.llm.last_http_status, 200)
        self.assertEqual(self.llm.last_model, 'header-model')

    def test_usage_accounted_before_invalid_status(self):
        with self.transport([{**first(), 'status': 'incomplete'}]), self.assertRaises(GuideError):
            self.propose()
        self.assertEqual((self.budget.usage_calls, self.budget.input_tokens, self.budget.output_tokens), (1, 3, 2))
        self.assertEqual(self.llm.last_model, 'observed')

    def test_success_and_snapshot(self):
        with self.transport([first(), second()]):
            proposal = self.propose()
            self.assertNotIn('private prompt', repr(proposal))
            proposal.response['output'].clear()
            proposal.input_items.clear()
            proposal.arguments['value'] = 'changed'
            result = self.llm.continue_action(proposal, '{"ok":true}')
        self.assertEqual(result, {'status': 'completed', 'content': 'done'})
        self.assertEqual(self.requests[1]['tool_choice'], 'none')
        self.assertEqual(self.requests[1]['input'][-3]['type'], 'reasoning')
        self.assertEqual(self.requests[1]['input'][-1]['call_id'], 'c1')
        self.assertEqual((self.budget.llm_calls, self.budget.usage_calls, self.budget.input_tokens, self.budget.output_tokens), (2, 2, 7, 3))
        self.assertEqual(len(self.guards), 2)

    def test_invalid_calls(self):
        for response in [first(name='unknown'), first('{"value":NaN}'), first('{"value":"fixed","value":"fixed"}'),
                         first('{"value":"' + 'x' * 256001 + '"}'),
                         first('{"value":"fixed","force":true}'),
                         {**first(), 'output': first()['output'] * 2}, {**first(), 'status': 'incomplete'}]:
            with self.subTest(response_type=response.get('status')), self.transport([response]):
                with self.assertRaises(GuideError) as caught:
                    self.propose()
                self.assertNotIn('fixed', str(caught.exception))

    def test_rejection_and_no_retry(self):
        from llm import NativeUnsupported, _Rejected
        for error, expected in [(_Rejected(400, '', 'tools are not supported'), NativeUnsupported),
                                (_Rejected(400, '', 'invalid model'), GuideError),
                                (_Rejected(422, '', 'tools parameter: temperature is not supported'), GuideError),
                                (GuideError('timeout'), GuideError)]:
            before = len(self.requests)
            with self.transport([error]), self.assertRaises(expected) as caught:
                self.propose()
            if expected is GuideError:
                self.assertNotIsInstance(caught.exception, NativeUnsupported)
            self.assertEqual(len(self.requests), before + 1)

    def test_additional_boundaries(self):
        from llm import NativeUnsupported, _Rejected
        for response in [first('{"value":1e999}'), first('{"value":' + '[' * 40 + '0' + ']' * 40 + '}'),
                         {**first(), 'output': first()['output'] + [{'type': 'computer_call'}]},
                         {**first(), 'output': [{'type': 'function_call', 'name': 'native_probe', 'arguments': '{}'}]}]:
            with self.transport([response]), self.assertRaises(GuideError):
                self.propose()
        with self.transport([first(), _Rejected(400, '', 'tools are not supported')]):
            proposal = self.propose()
            with self.assertRaises(GuideError) as caught:
                self.llm.continue_action(proposal, '{}')
            self.assertNotIsInstance(caught.exception, NativeUnsupported)
        for schema in [{}, {'type': 'object'}, {'type': 'object', 'properties': {}, 'additionalProperties': False, 'required': 3}]:
            with self.assertRaises(GuideError):
                self.llm.propose_action('s', {}, [{'name': 't', 'parameters': schema}])
        self.budget.seconds = 0
        with self.transport([first()]), self.assertRaises(BudgetExceeded):
            self.propose()

    def test_followup_limits_and_cancel(self):
        with self.transport([first()]):
            proposal = self.propose()
            with self.assertRaises(GuideError):
                self.llm.continue_action(proposal, 'x' * 256001)
            self.budget.cancel_token.request()
            with self.assertRaises(RunCancelled):
                self.llm.continue_action(proposal, '{}')
            self.assertEqual(len(self.requests), 1)

    def test_followup_rejects_calls_and_accounts_usage(self):
        with self.transport([first(), first()]):
            proposal = self.propose()
            with self.assertRaises(GuideError):
                self.llm.continue_action(proposal, '{}')
        self.assertEqual(self.budget.usage_calls, 2)

    def test_cancel_and_budget_at_each_request(self):
        with self.transport([first(), second()]):
            self.budget.cancel_token.request()
            with self.assertRaises(RunCancelled):
                self.propose()
            self.assertFalse(self.requests)
        self.budget = Budget(60)
        self.llm.budget = self.budget
        with self.transport([first(), second()]):
            proposal = self.propose()
            self.budget.seconds = 0
            with self.assertRaises(BudgetExceeded):
                self.llm.continue_action(proposal, '{}')
            self.assertEqual(len(self.requests), 1)
