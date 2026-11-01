"""Non-MiniMax endpoints: strict-schema downgrade, error text, one bounded retry.

Root cause under test: `text.format.strict=true` is only legal for schemas whose
`required` lists every property and which avoid keywords OpenAI strict rejects.
MiniMax accepted our partial-`required` schemas; DeepSeek and OpenAI-compatible
gateways answered HTTP 400. These tests use a local mock only.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import Budget, BudgetExceeded, Config, GuideError
from llm import JsonLLM, strict_schema_compatible
from prompts import ACTION_SCHEMA, PLAN_SCHEMA, QUERY_SCHEMA

PARTIAL = {'type': 'object', 'properties': {'a': {'type': 'string'}, 'b': {'type': 'string'}},
           'required': ['a'], 'additionalProperties': False}
PROBE = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
         'required': ['ok'], 'additionalProperties': False}
CAPPED = {'type': 'object', 'properties': {'a': {'type': 'string', 'maxLength': 5}},
          'required': ['a'], 'additionalProperties': False}


def validate(value):
    if not isinstance(value, dict):
        raise GuideError('bad shape')


class Handler(BaseHTTPRequestHandler):
    requests = []
    replies = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length) or b'{}')
        type(self).requests.append((self.path, body))
        if type(self).replies:
            status, payload = type(self).replies.pop(0)
        else:  # a missing reply is a test bug, not a hang
            status, payload = 500, {'error': {'message': 'no queued reply'}}
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def ok_responses():
    return 200, {'status': 'completed', 'model': 'mock-model',
                 'output': [{'type': 'message', 'content': [
                     {'type': 'output_text', 'text': '{"ok": true}'}]}],
                 'usage': {'input_tokens': 11, 'output_tokens': 7}}


def ok_chat():
    return 200, {'choices': [{'message': {'content': '{"ok": true}'}, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}


class CompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        Handler.requests = []
        Handler.replies = []

    def llm(self, **changes):
        config = replace(Config(base_url=self.base, model='mock-model', api_key='secret-key',
                                protocol='responses', request_timeout=5), **changes)
        return JsonLLM(config, Budget(10)), config

    # --- schema classification -------------------------------------------
    def test_strict_is_kept_only_for_schemas_strict_mode_can_accept(self):
        self.assertTrue(strict_schema_compatible(PROBE))
        self.assertTrue(strict_schema_compatible(QUERY_SCHEMA))
        self.assertFalse(strict_schema_compatible(PARTIAL))
        self.assertFalse(strict_schema_compatible(CAPPED))
        self.assertFalse(strict_schema_compatible(PLAN_SCHEMA))
        self.assertFalse(strict_schema_compatible(ACTION_SCHEMA))
        self.assertFalse(strict_schema_compatible({'type': 'object'}))
        self.assertFalse(strict_schema_compatible(None))

    # --- request shape ----------------------------------------------------
    def test_partial_required_schema_is_sent_without_strict(self):
        Handler.replies = [ok_responses()]
        llm, _ = self.llm()
        self.assertEqual(llm.generate('assess', 'Return JSON.', {}, PARTIAL, validate), {'ok': True})
        self.assertEqual(len(Handler.requests), 1)
        body = Handler.requests[0][1]
        self.assertIn('text', body)
        self.assertNotIn('strict', body['text']['format'])

    def test_strict_stays_on_for_a_compatible_schema(self):
        Handler.replies = [ok_responses()]
        llm, _ = self.llm()
        llm.generate('assess', 'Return JSON.', {}, PROBE, validate)
        self.assertIs(Handler.requests[0][1]['text']['format'].get('strict'), True)

    def test_minimax_family_keeps_its_live_verified_strict_request(self):
        from unittest.mock import patch
        config = replace(Config(), base_url='https://api.minimaxi.com/v1',
                         model='MiniMax-M3.1-Flash', protocol='responses', request_timeout=5)
        llm = JsonLLM(config, Budget(10))
        captured = []

        def fake_post(url, payload, headers):
            captured.append(payload)
            return ok_responses()[1]

        with patch.object(llm, '_post', side_effect=fake_post):
            llm.generate('assess', 'Return JSON.', {}, PARTIAL, validate)
        self.assertIs(captured[0]['text']['format'].get('strict'), True)

    def test_unknown_backend_gets_the_non_strict_dialect(self):
        Handler.replies = [ok_responses()]
        llm, _ = self.llm(model='gpt-x', base_url=self.base)
        llm.generate('assess', 'Return JSON.', {}, PARTIAL, validate)
        self.assertNotIn('strict', Handler.requests[0][1]['text']['format'])

    # --- error text -------------------------------------------------------
    def test_provider_reason_is_surfaced_and_the_key_is_redacted(self):
        rejection = (400, {'error': {
            'message': "Invalid schema: 'required' must list every property. key secret-key",
            'param': 'text.format.schema', 'type': 'invalid_request_error'}})
        Handler.replies = [rejection, rejection]
        llm, _ = self.llm()
        with self.assertRaises(GuideError) as caught:
            llm.generate('assess', 'Return JSON.', {}, PROBE, validate)
        message = str(caught.exception)
        self.assertIn('HTTP 400', message)
        self.assertIn("'required' must list every property", message)
        self.assertIn('param: text.format.schema', message)
        self.assertNotIn('secret-key', message)

    def test_unrelated_400_is_not_retried(self):
        Handler.replies = [(400, {'error': {'message': 'account balance exhausted'}})]
        llm, _ = self.llm()
        with self.assertRaises(GuideError):
            llm.generate('assess', 'Return JSON.', {}, PROBE, validate)
        self.assertEqual(len(Handler.requests), 1)
        self.assertEqual(llm.budget.llm_calls, 1)

    # --- bounded retry ----------------------------------------------------
    def test_named_schema_rejection_is_retried_once_and_succeeds(self):
        Handler.replies = [(400, {'error': {'message': 'response_format is not supported',
                                            'param': 'response_format'}}), ok_chat()]
        llm, _ = self.llm(protocol='chat_completions')
        self.assertEqual(llm.generate('assess', 'Return JSON.', {}, PROBE, validate), {'ok': True})
        self.assertEqual(len(Handler.requests), 2)
        self.assertIn('response_format', Handler.requests[0][1])
        self.assertNotIn('response_format', Handler.requests[1][1])
        self.assertEqual(llm.budget.llm_calls, 2)

    def test_named_temperature_rejection_is_retried_without_temperature(self):
        Handler.replies = [(400, {'error': {'message': "Unsupported value: 'temperature'",
                                            'param': 'temperature'}}), ok_responses()]
        llm, _ = self.llm()
        llm.generate('assess', 'Return JSON.', {}, PROBE, validate)
        self.assertEqual(len(Handler.requests), 2)
        self.assertNotIn('temperature', Handler.requests[1][1])

    def test_retry_happens_at_most_once(self):
        Handler.replies = [(400, {'error': {'message': 'text.format is not supported'}}),
                           (400, {'error': {'message': 'text.format is not supported'}})]
        llm, _ = self.llm()
        with self.assertRaises(GuideError):
            llm.generate('assess', 'Return JSON.', {}, PROBE, validate)
        self.assertEqual(len(Handler.requests), 2)

    def test_retry_is_charged_against_the_phase_request_budget(self):
        Handler.replies = [(400, {'error': {'message': 'response_format is not supported'}})]
        budget = Budget(10)

        def guard():
            if budget.llm_calls >= 1:
                raise BudgetExceeded('exhausted')

        budget.request_guard = guard
        llm = JsonLLM(Config(base_url=self.base, model='mock-model',
                             protocol='chat_completions', request_timeout=5), budget)
        with self.assertRaises(BudgetExceeded):
            llm.generate('assess', 'Return JSON.', {}, PROBE, validate)
        self.assertEqual(len(Handler.requests), 1)


if __name__ == '__main__':
    unittest.main()
