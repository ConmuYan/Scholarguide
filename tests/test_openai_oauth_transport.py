import io
import json
import sys
import time
import types
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from scholarguide.common import Budget, BudgetExceeded, Config, GuideError, RunCancelled
from scholarguide.llm import JsonLLM, NativeUnsupported, _Rejected
from scholarguide.network import BufferedResponse


SCHEMA = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
SECRET = 'oauth-secret-must-not-leak'


def event(kind, **kwargs):
    return ('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **kwargs}) + '\n\n').encode()


def completed():
    return event('response.completed', response={
        'status': 'completed', 'model': 'observed',
        'usage': {'input_tokens': 3, 'output_tokens': 2},
        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]})


class OAuthTransportTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(provider='openai', protocol='responses', profile_id='openai-oauth',
                             base_url='https://api.openai.com/v1', api_key='unused-api-key', model='gpt-test')
        self.budget = Budget(30)
        self.llm = JsonLLM(self.config, self.budget)
        self.access = Mock(return_value=SECRET)
        self.module = patch.dict(sys.modules, {'scholarguide.openai_oauth': types.SimpleNamespace(
            access_token=self.access, proxy_url=lambda *args, **kwargs: '')})
        self.module.start()
        self.addCleanup(self.module.stop)

    def call(self):
        return self.llm._call([{'role': 'system', 'content': 'instructions'},
                               {'role': 'user', 'content': 'hello'}], SCHEMA)

    def test_request_and_usage_and_fresh_token(self):
        self.access.side_effect = [SECRET, 'second-token']
        captured = []
        def open_fake(req, **kwargs):
            captured.append((req, kwargs))
            return BufferedResponse(event('response.created', response={}) + completed() + b'data: [DONE]\n\n')
        with patch('scholarguide.llm.urlopen', side_effect=open_fake):
            self.assertEqual(self.call(), '{"ok":true}')
            self.assertEqual(self.call(), '{"ok":true}')
        self.assertEqual(self.access.call_count, 2)
        self.assertEqual((self.budget.llm_calls, self.budget.usage_calls, self.budget.input_tokens), (2, 2, 6))
        self.assertEqual(self.llm.last_model, 'observed')
        for i, (req, kwargs) in enumerate(captured):
            self.assertEqual(req.full_url, 'https://api.openai.com/v1/responses')
            self.assertEqual(req.get_header('Authorization'), 'Bearer ' + [SECRET, 'second-token'][i])
            self.assertEqual(req.get_header('Accept'), 'text/event-stream')
            self.assertTrue(kwargs['reject_redirects'])
            payload = json.loads(req.data)
            self.assertIs(payload['stream'], True)
            self.assertIs(payload['store'], False)
            self.assertEqual(payload['input'][0]['role'], 'developer')
            self.assertNotIn('temperature', payload)
            self.assertEqual(payload['max_output_tokens'], self.config.max_output_tokens)

    def test_configured_output_cap_is_sent_verbatim(self):
        self.llm.config = replace(self.config, max_output_tokens=777)
        with patch('scholarguide.llm.urlopen', return_value=BufferedResponse(completed())) as opened:
            self.call()
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['max_output_tokens'], 777)

    def test_reasoning_policy_omits_output_cap_explicitly(self):
        policy = {'policy_version': 'fixture', 'reasoning_effort': 'low'}
        with patch('scholarguide.model_policy.inference_policy', return_value=policy), \
             patch('scholarguide.llm.urlopen', return_value=BufferedResponse(completed())) as opened:
            self.call()
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['reasoning'], {'effort': 'low'})
        self.assertNotIn('max_output_tokens', payload)

    def test_token_lookup_receives_absolute_deadline_and_cancel_probe(self):
        with patch('scholarguide.llm.urlopen', return_value=BufferedResponse(completed())):
            self.call()
        kwargs = self.access.call_args.kwargs
        self.assertIn('deadline', kwargs)
        self.assertGreater(kwargs['deadline'], time.monotonic())
        self.assertFalse(kwargs['cancelled']())

    def test_deadline_exhaustion_during_token_lookup_prevents_dispatch(self):
        self.budget = Budget(0.05)
        self.llm.budget = self.budget
        def slow_token(**kwargs):
            time.sleep(0.15)
            return SECRET
        self.access.side_effect = slow_token
        with patch('scholarguide.llm.urlopen') as opened, self.assertRaises(GuideError):
            self.call()
        opened.assert_not_called()
        self.assertEqual(self.budget.llm_calls, 0)

    def test_failed_and_cancelled_stream_usage_is_counted(self):
        for kind in ('response.failed', 'response.cancelled', 'response.incomplete'):
            with self.subTest(kind=kind):
                self.budget = Budget(30)
                self.llm.budget = self.budget
                body = event(kind, response={'status': kind.split('.')[1],
                                             'usage': {'input_tokens': 5, 'output_tokens': 2}})
                with patch('scholarguide.llm.urlopen', return_value=BufferedResponse(body)), \
                     self.assertRaises(GuideError):
                    self.call()
                self.assertTrue(self.budget.usage_observed)
                self.assertEqual((self.budget.usage_calls, self.budget.input_tokens,
                                  self.budget.output_tokens), (1, 5, 2))

    def test_unused_failed_stream_usage_stays_unaccounted(self):
        body = event('response.failed', response={'status': 'failed',
                                                  'usage': {'input_tokens': '5', 'output_tokens': 2}})
        with patch('scholarguide.llm.urlopen', return_value=BufferedResponse(body)), \
             self.assertRaises(GuideError):
            self.call()
        self.assertFalse(self.budget.usage_observed)
        self.assertEqual(self.budget.usage_calls, 0)

    def test_oauth_native_is_locally_unsupported_before_any_token(self):
        with patch('scholarguide.llm.urlopen') as opened, self.assertRaises(NativeUnsupported) as caught:
            self.llm._native_request({}, initial=True)
        self.assertIn('SG_NATIVE_FALLBACK=json_on_unsupported', str(caught.exception))
        self.assertIn('SG_ACTION_PROTOCOL=json', str(caught.exception))
        self.assertFalse(self.llm.last_native_request_sent)
        opened.assert_not_called()
        self.access.assert_not_called()

    def test_incompatible_schema_uses_json_object_without_retry(self):
        from scholarguide.prompts import PLAN_SCHEMA
        with patch('scholarguide.llm.urlopen', return_value=BufferedResponse(completed())) as opened:
            self.llm._call([{'role': 'user', 'content': 'hello'}], PLAN_SCHEMA)
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['text']['format'], {'type': 'json_object'})
        self.assertEqual(opened.call_count, 1)

    def test_only_explicit_oauth_proxy_is_passed_to_transport(self):
        module = types.SimpleNamespace(access_token=self.access, proxy_url=lambda *args, **kwargs: 'http://127.0.0.1:7890')
        with patch.dict(sys.modules, {'scholarguide.openai_oauth': module}), \
             patch('scholarguide.llm.urlopen', return_value=BufferedResponse(completed())) as opened:
            self.call()
        self.assertEqual(opened.call_args.kwargs['model_proxy'], 'http://127.0.0.1:7890')

    def test_invalid_configuration_never_reads_or_sends_token(self):
        for changes in ({'provider': 'ollama'}, {'protocol': 'chat_completions'},
                        {'base_url': 'https://evil.invalid/v1'},
                        {'base_url': 'https://api.openai.com/v1/'},
                        {'base_url': 'http://api.openai.com/v1'},
                        {'base_url': 'https://api.openai.com/v1/responses'}):
            with self.subTest(changes=changes), patch('scholarguide.llm.urlopen') as opened:
                self.llm.config = replace(self.config, **changes)
                with self.assertRaises(GuideError):
                    self.call()
                opened.assert_not_called()
                self.access.assert_not_called()

    def test_http_error_never_retries_or_exposes_body(self):
        def fail(req, **kwargs):
            raise HTTPError(req.full_url, 400, SECRET, {}, io.BytesIO(
                json.dumps({'error': {'message': 'temperature Bearer ' + SECRET}}).encode()))
        with patch('scholarguide.llm.urlopen', side_effect=fail) as opened:
            with self.assertRaises(GuideError) as caught:
                self.call()
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn('Bearer', str(caught.exception))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.access.call_count, 1)
        self.assertEqual(self.budget.llm_calls, 1)

    def test_known_error_codes_are_safe_and_actionable(self):
        code = 'subscription_sharing_usage_limit_exceeded'
        def fail(req, **kwargs):
            body = {'error': {'code': code, 'message': SECRET}}
            raise HTTPError(req.full_url, 429, SECRET, {}, io.BytesIO(json.dumps(body).encode()))
        with patch('scholarguide.llm.urlopen', side_effect=fail), self.assertRaises(GuideError) as caught:
            self.call()
        self.assertIn(code, str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        body = event('response.failed', response={'error': {'code': code, 'message': SECRET}})
        with patch('scholarguide.llm.urlopen', return_value=BufferedResponse(body)), self.assertRaises(GuideError) as caught:
            self.call()
        self.assertIn(code, str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_no_downgrade_even_if_rejection_has_field(self):
        with patch.object(self.llm, '_post', side_effect=_Rejected(400, '', 'temperature unsupported')) as post:
            with self.assertRaises(GuideError):
                self.call()
            self.assertEqual(post.call_count, 1)

    def test_token_errors_are_sanitized(self):
        for result in [RuntimeError('Bearer ' + SECRET), '', 'bad\nheader', None]:
            with self.subTest(result=type(result)):
                self.access.side_effect = result if isinstance(result, Exception) else None
                self.access.return_value = result
                with patch('scholarguide.llm.urlopen') as opened, self.assertRaises(GuideError) as caught:
                    self.call()
                self.assertNotIn(SECRET, str(caught.exception))
                opened.assert_not_called()

    def test_failed_invalid_and_truncated_streams(self):
        examples = [b'', b'data: [DONE]\n\n', b'data: not-json\n\n',
                    event('response.failed', response={'error': {'message': SECRET}}),
                    event('response.incomplete'), event('error', message=SECRET),
                    completed().rstrip(b'\n'),
                    event('response.completed', response={'status': 'incomplete'}),
                    completed() + event('response.failed'),
                    b'event: response.completed\ndata: {"type":"response.failed"}\n\n',
                    b'\xff\n\n', b'x' * 2_000_001]
        for body in examples:
            with self.subTest(body=body[:35]), patch('scholarguide.llm.urlopen', return_value=BufferedResponse(body)):
                with self.assertRaises(GuideError) as caught:
                    self.call()
                self.assertNotIn(SECRET, str(caught.exception))

    def test_cancel_budget_and_native_fail_closed(self):
        with patch('scholarguide.llm.urlopen') as opened:
            self.budget.cancel_token.request()
            with self.assertRaises(RunCancelled):
                self.call()
            opened.assert_not_called()
            self.access.assert_not_called()
        self.llm.budget = Budget(0)
        with patch('scholarguide.llm.urlopen') as opened, self.assertRaises(BudgetExceeded):
            self.call()
        opened.assert_not_called()
        self.llm.budget = Budget(30)
        with patch('scholarguide.llm.urlopen') as opened, self.assertRaises(GuideError):
            self.llm._native_request({}, initial=True)
        opened.assert_not_called()
        self.assertFalse(self.llm.last_native_request_sent)
        self.access.assert_not_called()

    def test_cancellation_during_token_lookup_prevents_dispatch(self):
        def token(*args, **kwargs):
            self.budget.cancel_token.request()
            return SECRET
        self.access.side_effect = token
        with patch('scholarguide.llm.urlopen') as opened, self.assertRaises(RunCancelled):
            self.call()
        opened.assert_not_called()
        self.assertEqual(self.budget.llm_calls, 0)

    def test_token_lookup_cancellation_error_is_not_wrapped(self):
        self.access.side_effect = RunCancelled()
        with patch('scholarguide.llm.urlopen') as opened, self.assertRaises(RunCancelled):
            self.call()
        opened.assert_not_called()
        self.assertEqual(self.budget.llm_calls, 0)

    def test_stream_multiline_data_and_comments(self):
        body = (b': keepalive\r\nevent: response.created\r\ndata: {"type":\r\n'
                b'data: "response.created"}\r\n\r\n' + completed())
        with patch('scholarguide.llm.urlopen', return_value=BufferedResponse(body)):
            self.assertEqual(self.call(), '{"ok":true}')

    def test_non_oauth_json_transport_unchanged(self):
        self.llm.config = replace(self.config, profile_id='regular', base_url='https://example.invalid/v1')
        data = {'status': 'completed', 'output_text': '{"ok":true}'}
        with patch('scholarguide.llm.urlopen', return_value=BufferedResponse(json.dumps(data).encode())) as opened:
            self.assertEqual(self.call(), '{"ok":true}')
        req = opened.call_args.args[0]
        self.assertEqual(req.get_header('Authorization'), 'Bearer unused-api-key')
        self.assertNotIn('reject_redirects', opened.call_args.kwargs)
        self.assertNotIn('stream', json.loads(req.data))
        self.access.assert_not_called()


if __name__ == '__main__':
    unittest.main()
