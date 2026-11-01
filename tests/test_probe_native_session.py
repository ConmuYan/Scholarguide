import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from common import Config
from network import BufferedResponse
from scripts.verify_delivery import isolated_environment


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, isolated_environment(self.root), clear=True)
        env.start()
        self.addCleanup(env.stop)
        guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        from scripts import probe_native_session as probe
        self.probe = probe
        self.config = Config(base_url='https://api.minimaxi.com/v1', model='MiniMax-test',
                             api_key='SYNTHETIC_SECRET', protocol='responses', input_price=1,
                             output_price=2, currency='CNY')
        self.requests = []

    def transport(self, request, timeout, **kwargs):
        self.assertTrue(kwargs.get('reject_redirects'))
        self.assertEqual(request.full_url, 'https://api.minimaxi.com/v1/responses')
        self.assertEqual(request.get_header('Authorization'), 'Bearer SYNTHETIC_SECRET')
        payload = json.loads(request.data)
        self.requests.append(payload)
        if len(self.requests) == 1:
            output = [{'type': 'function_call', 'call_id': 'SYNTHETIC_CALL_ID',
                       'name': 'sg_search', 'arguments': json.dumps({'query': 'synthetic query', 'reason': 'SYNTHETIC_REASON'})}]
        else:
            output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'SYNTHETIC_RESPONSE'}]}]
        value = {'status': 'completed', 'output': output, 'usage': {'input_tokens': 3, 'output_tokens': 2}}
        return BufferedResponse(json.dumps(value).encode())

    def run_probe(self, transport=None, **kwargs):
        return self.probe.run(self.config, transport=transport or self.transport, **kwargs)

    def test_success_and_no_secrets(self):
        validated = self.probe.validated(self.config)
        validated.validate()
        self.assertEqual(validated.native_fallback, 'disabled')
        report = self.run_probe()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual([p['tool_choice'] for p in self.requests], ['required', 'none'])
        self.assertEqual(report['requests'], 2)
        self.assertEqual(report['source_calls'], 1)
        self.assertEqual(report['candidates'], 1)
        self.assertTrue(report['call_id_present'])
        self.assertTrue(report['call_id_matching'])
        self.assertEqual(report['usage']['input_tokens'], 6)
        self.assertEqual([r['status'] for r in report['responses']], ['completed', 'completed'])
        self.assertEqual(report['responses'][1]['items'][0]['parts'],
                         [{'type': 'output_text', 'nonempty': True}])
        self.assertEqual(report['proposal_shape'], {'name_allowed': True, 'name_is_search': True,
                                                    'arguments_valid': True, 'call_id_valid': True,
                                                    'credential_guard_blocked': False})
        self.assertEqual(report['controller_result']['outcome'], 'native_authorized')
        for secret in ('SYNTHETIC_SECRET', 'SYNTHETIC_CALL_ID', 'SYNTHETIC_REASON', 'synthetic query', 'SYNTHETIC_RESPONSE'):
            self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(self.requests))

    def test_failure_no_retry(self):
        def fail(*args, **kwargs):
            raise RuntimeError('SYNTHETIC_SECRET')
        report = self.run_probe(fail)
        self.assertEqual(report['requests'], 1)
        self.assertEqual(report['source_calls'], 0)
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(report))

    def test_continuation_secret_is_rejected_before_session(self):
        def echo(*args, **kwargs):
            response = self.transport(*args, **kwargs)
            value = json.loads(response.read())
            if len(self.requests) == 2:
                value['output'][0]['content'][0]['text'] = 'SYNTHETIC_SECRET'
            return BufferedResponse(json.dumps(value).encode())
        report = self.run_probe(echo)
        self.assertEqual(report['requests'], 2)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['proposal_shape']['credential_guard_blocked'])
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(report))

    def test_unicode_escape_embedded_in_plain_text_is_detected(self):
        self.assertTrue(self.probe.contains_secret(r'prefix-\u0053YNTHETIC_SECRET', 'SYNTHETIC_SECRET'))

    def test_invalid_call_id_has_bounded_diagnostic(self):
        for call_id in ('  ', 'x' * 1025):
            with self.subTest(length=len(call_id)):
                def invalid(request, timeout, **kwargs):
                    value = {'status': 'completed', 'usage': {'input_tokens': 3, 'output_tokens': 2},
                             'output': [{'type': 'function_call', 'call_id': call_id,
                                         'name': 'sg_search', 'arguments': json.dumps(
                                             {'query': 'synthetic', 'reason': 'synthetic'})}]}
                    return BufferedResponse(json.dumps(value).encode())
                report = self.run_probe(invalid)
                self.assertEqual(report['requests'], 1)
                self.assertFalse(report['proposal_shape']['call_id_valid'])
                self.assertEqual(report['source_calls'], 0)
                self.assertNotIn(call_id, json.dumps(report))

    def test_controller_rejection_reports_fixed_code_without_query(self):
        def invalid(request, timeout, **kwargs):
            value = {'status': 'completed', 'usage': {'input_tokens': 3, 'output_tokens': 2},
                     'output': [{'type': 'function_call', 'call_id': 'SYNTHETIC_CALL_ID',
                                 'name': 'sg_search', 'arguments': json.dumps(
                                     {'query': '   ', 'reason': 'SYNTHETIC_REASON'})}]}
            return BufferedResponse(json.dumps(value).encode())
        report = self.run_probe(invalid, max_requests=1)
        self.assertTrue(report['proposal_shape']['arguments_valid'])
        self.assertEqual(report['controller_result']['rejection'], 'empty_query')
        self.assertEqual(report['source_calls'], 0)
        self.assertNotIn('SYNTHETIC_CALL_ID', json.dumps(report))
        self.assertNotIn('SYNTHETIC_REASON', json.dumps(report))

    def test_invalid_arguments_report_only_safe_diagnostics(self):
        def invalid(request, timeout, **kwargs):
            value = {'status': 'completed', 'usage': {'input_tokens': 3, 'output_tokens': 2},
                     'output': [{'type': 'function_call', 'call_id': 'SYNTHETIC_CALL_ID',
                                 'name': 'sg_search', 'arguments': '{"reason":"SYNTHETIC_REASON"}'}]}
            return BufferedResponse(json.dumps(value).encode())
        report = self.run_probe(invalid)
        self.assertEqual(report['requests'], 1)
        self.assertEqual(report['failed_phase'], 'proposal')
        self.assertEqual(report['failure_type'], 'guide')
        self.assertTrue(report['proposal_shape']['name_allowed'])
        self.assertFalse(report['proposal_shape']['arguments_valid'])
        self.assertNotIn('SYNTHETIC_REASON', json.dumps(report))
        self.assertNotIn('SYNTHETIC_CALL_ID', json.dumps(report))

    def test_incomplete_continuation_reports_only_safe_shape(self):
        def incomplete(*args, **kwargs):
            response = self.transport(*args, **kwargs)
            value = json.loads(response.read())
            if len(self.requests) == 2:
                value['status'] = 'incomplete'
                value['output'] = [{'type': 'reasoning', 'status': 'incomplete',
                                    'secret': 'SYNTHETIC_PRIVATE_RESPONSE'}]
            return BufferedResponse(json.dumps(value).encode())
        report = self.run_probe(incomplete)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['failed_phase'], 'apply_and_continuation')
        self.assertEqual(report['responses'][1]['status'], 'incomplete')
        self.assertEqual(report['responses'][1]['items'][0]['type'], 'reasoning')
        self.assertNotIn('SYNTHETIC_PRIVATE_RESPONSE', json.dumps(report))

    def test_missing_usage_unknown(self):
        def no_usage(*args, **kwargs):
            response = self.transport(*args, **kwargs)
            value = json.loads(response.read())
            value.pop('usage')
            return BufferedResponse(json.dumps(value).encode())
        report = self.run_probe(no_usage)
        self.assertIsNone(report['usage']['input_tokens'])
        self.assertIsNone(report['estimated_cost']['amount'])

    def test_cancel_before_apply(self):
        report = self.run_probe(before_apply=lambda chat: chat.request_stop())
        self.assertEqual(report['requests'], 1)
        self.assertEqual(report['source_calls'], 0)
        self.assertEqual(report['status'], 'cancelled')

    def test_limit_at_transport(self):
        report = self.run_probe(max_requests=1)
        self.assertEqual(report['requests'], 1)
        self.assertEqual(len(self.requests), 1)
        self.assertNotEqual(report['status'], 'completed')

    def test_second_failure_is_counted_and_not_retried(self):
        def fail_second(*args, **kwargs):
            if self.requests:
                raise RuntimeError('SYNTHETIC_SECRET')
            return self.transport(*args, **kwargs)
        report = self.run_probe(fail_second)
        self.assertEqual(report['requests'], 2)
        self.assertEqual(report['source_calls'], 1)
        self.assertEqual(report['candidates'], 1)
        self.assertIsNone(report['usage']['input_tokens'])
        self.assertEqual(report['status'], 'failed')

    def test_endpoint_and_model_rejected_before_dispatch(self):
        from dataclasses import replace
        for url in ('http://api.minimaxi.com/v1', 'https://user@api.minimaxi.com/v1',
                    'https://api.minimaxi.com/v1?x=1', 'https://api.minimaxi.com.evil/v1',
                    'https://api.minimaxi.com/v1#fragment'):
            with self.subTest(url=url):
                report = self.probe.run(replace(self.config, base_url=url), transport=self.transport)
                self.assertEqual(report['requests'], 0)
        report = self.probe.run(replace(self.config, model='bad model'), transport=self.transport)
        self.assertEqual(report['requests'], 0)
        self.assertEqual(self.requests, [])

    def test_secret_in_model_context_is_blocked(self):
        from dataclasses import replace
        config = replace(self.config, model='SYNTHETIC_SECRET')
        report = self.probe.run(config, transport=self.transport)
        self.assertEqual(report['requests'], 0)
        self.assertEqual(self.requests, [])

    def test_non_ascii_secret_in_call_id_is_not_sent_in_continuation(self):
        from dataclasses import replace
        config = replace(self.config, api_key='秘密synthetic')
        def first_with_secret(request, timeout, **kwargs):
            payload = json.loads(request.data)
            self.requests.append(payload)
            value = {'status': 'completed', 'usage': {'input_tokens': 3, 'output_tokens': 2},
                     'output': [{'type': 'function_call', 'call_id': 'id-秘密synthetic',
                                 'name': 'sg_search', 'arguments': json.dumps(
                                     {'query': 'synthetic query', 'reason': 'fixture'})}]}
            return BufferedResponse(json.dumps(value).encode())
        report = self.probe.run(config, transport=first_with_secret)
        self.assertEqual(report['requests'], 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(report['source_calls'], 0)
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('秘密synthetic', json.dumps(report, ensure_ascii=False))

    def test_escaped_secret_in_nested_json_is_not_sent_in_continuation(self):
        from dataclasses import replace
        config = replace(self.config, api_key='秘密synthetic')
        def first_with_secret(request, timeout, **kwargs):
            self.requests.append(json.loads(request.data))
            arguments = json.dumps({'query': '秘密synthetic', 'reason': 'fixture'}, ensure_ascii=True)
            value = {'status': 'completed', 'usage': {'input_tokens': 3, 'output_tokens': 2},
                     'output': [{'type': 'function_call', 'call_id': 'safe-id',
                                 'name': 'sg_search', 'arguments': arguments}]}
            return BufferedResponse(json.dumps(value).encode())
        report = self.probe.run(config, transport=first_with_secret)
        self.assertEqual(report['requests'], 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(report['source_calls'], 0)
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('秘密synthetic', json.dumps(report, ensure_ascii=False))

    def test_excessive_nested_json_fails_closed(self):
        text = '[' * 2000 + '"\\u79d8\\u5bc6synthetic"' + ']' * 2000
        try:
            self.assertTrue(self.probe.contains_secret(text, '秘密synthetic'))
        except ValueError:
            pass  # Resource bounds reject the response instead of decoding it.

    def test_existing_output_precedes_config(self):
        path = self.root / 'exists.json'
        path.write_text('untouched', encoding='utf-8')
        with patch.object(self.probe, 'load_config', side_effect=AssertionError('read config')):
            self.assertNotEqual(self.probe.main(['--output', str(path)]), 0)
        self.assertEqual(path.read_text(encoding='utf-8'), 'untouched')
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
