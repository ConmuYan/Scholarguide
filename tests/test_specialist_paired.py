import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.engineering_agent import run_specialist_paired as paired


class PairedTests(unittest.TestCase):
    def test_source_hashes_include_identity_and_verification_scripts(self):
        hashes = paired.source_hashes()
        for name in ('model_identity.py', 'tests/test_specialist_pilot.py',
                     'scripts/verify_delivery.py', 'scripts/offline_test_site/sitecustomize.py'):
            with self.subTest(name=name):
                self.assertIn(name, hashes)

    def setUp(self):
        self.cases = json.loads(paired.FIXTURE.read_text(encoding='utf-8'))['cases']
        self.case = deepcopy(self.cases[0])

    def test_real_pilot_and_shared_source(self):
        self.assertGreaterEqual(len(self.cases), 3)
        for case in self.cases:
            with self.subTest(case=case['id']):
                with patch.object(paired, 'run_pilot', wraps=paired.run_pilot) as call:
                    row = paired.run_case(case, enabled=True)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(row['input_sha256'], paired.digest(case))
                self.assertTrue(row['comparison_available'])
                single, pilot = row['arms']['single'], row['arms']['pilot']
                self.assertEqual(single['input_sha256'], pilot['input_sha256'])
                self.assertEqual(single['output_counts']['retain'], 1)
                self.assertEqual(pilot['output_counts'][case['verdict']], 1)
                self.assertTrue(single['main_executed'] and pilot['main_executed'])
                self.assertTrue(single['quote_substring_valid'] and pilot['quote_substring_valid'])
                self.assertEqual(pilot['critic_usage']['input_tokens'], 10)
                self.assertEqual(single['usage']['llm_calls'], 0)
                self.assertEqual(pilot['usage']['llm_calls'], 1)
                self.assertEqual(pilot['transport_calls'], 1)
                self.assertEqual(pilot['cost_units']['settled'], 1)
                self.assertEqual(pilot['applied_counts'][case['verdict']], 1)
                self.assertEqual(pilot['security'], 'unknown')
                self.assertFalse(pilot['memory_enabled'])

    def test_fixture_identity_is_runtime_derived_and_application_rechecks_config(self):
        self.assertNotIn('model_identity', self.case['snapshot'])
        original = paired.run_pilot
        def mutate_after_result(**kwargs):
            result = original(**kwargs)
            self.assertEqual(kwargs['snapshot']['model_identity'],
                             paired.research_model_identity(kwargs['config']))
            kwargs['config'].base_url = 'http://127.0.0.1:2'
            return result
        with patch.object(paired, 'run_pilot', side_effect=mutate_after_result):
            arm = paired.run_arm(self.case, pilot=True, enabled=True)
        self.assertEqual(arm['critic_status'], 'ok')
        self.assertFalse(arm['verdict_applied'])
        self.assertEqual(arm['status'], 'unavailable')
        self.assertNotIn('model_identity', self.case['snapshot'])

    def test_application_rejects_key_rotated_into_backend(self):
        original = paired.run_pilot
        def mutate_after_result(**kwargs):
            result = original(**kwargs)
            kwargs['config'].api_key = 'offline-model'
            return result
        with patch.object(paired, 'run_pilot', side_effect=mutate_after_result):
            arm = paired.run_arm(self.case, pilot=True, enabled=True)
        self.assertEqual(arm['critic_status'], 'ok')
        self.assertFalse(arm['verdict_applied'])
        self.assertEqual(arm['status'], 'unavailable')

    def test_default_off(self):
        row = paired.run_case(self.case)
        self.assertFalse(row['comparison_available'])
        self.assertEqual(row['arms']['pilot']['transport_calls'], 0)
        self.assertEqual(row['arms']['pilot']['critic_status'], 'skipped')
        with self.assertRaises(SystemExit):
            paired.main([])

    def test_denials_and_drift(self):
        changed = deepcopy(self.case['snapshot'])
        changed['papers'][0]['abstract'] += ' drift'
        options = [dict(max_calls=0), dict(seconds=0), dict(cancelled=True),
                   dict(authorized=False), dict(current=lambda: changed)]
        for option in options:
            with self.subTest(option=option):
                row = paired.run_case(self.case, enabled=True, **option)
                self.assertFalse(row['comparison_available'])
                self.assertFalse(row['arms']['pilot']['verdict_applied'])
        self.assertEqual(paired.run_case(self.case, enabled=True, max_calls=0)['arms']['pilot']['transport_calls'], 0)

    def test_application_time_drift(self):
        changed = deepcopy(self.case['snapshot'])
        changed['revision'] += 1
        states = iter([self.case['snapshot'], changed])
        arm = paired.run_arm(self.case, pilot=True, enabled=True, current=lambda: next(states))
        self.assertEqual(arm['critic_status'], 'ok')
        self.assertFalse(arm['verdict_applied'])
        self.assertEqual(arm['status'], 'unavailable')

    def test_cancellation_after_request(self):
        original = paired.run_pilot
        def cancel_during(**kwargs):
            current = kwargs['current']
            def cancelling():
                kwargs['budget'].cancel_token.request()
                return current()
            kwargs['current'] = cancelling
            return original(**kwargs)
        with patch.object(paired, 'run_pilot', side_effect=cancel_during):
            arm = paired.run_arm(self.case, pilot=True, enabled=True)
        self.assertEqual(arm['transport_calls'], 1)
        self.assertEqual(arm['usage']['llm_calls'], 1)
        self.assertFalse(arm['verdict_applied'])
        self.assertEqual(arm['status'], 'unavailable')

    def test_invalid_evidence_and_model_output(self):
        for quote in ('', 'fake quoted evidence'):
            case = deepcopy(self.case)
            case['snapshot']['evidence'][0]['quote'] = quote
            row = paired.run_case(case, enabled=True)
            self.assertFalse(row['comparison_available'])
            self.assertFalse(row['arms']['pilot']['verdict_applied'])
            self.assertFalse(row['arms']['pilot']['quote_substring_valid'])
        response = {'verdicts': [{'claim_id': 'C1', 'verdict': 'retain', 'evidence_ids': ['fake']}]}
        arm = paired.run_arm(self.case, pilot=True, enabled=True, response=response)
        self.assertFalse(arm['verdict_applied'])
        self.assertEqual(arm['critic_status'], 'rejected')

    def test_main_failure_is_unavailable(self):
        row = paired.run_case(self.case, enabled=True, main_fail=True)
        self.assertFalse(row['comparison_available'])
        self.assertEqual(row['arms']['single']['status'], 'unavailable')
        self.assertEqual(row['arms']['pilot']['transport_calls'], 0)

    def test_report_privacy_and_exclusive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            self.assertEqual(paired.main(['--pilot', '--output', str(path)]), 0)
            report = json.loads(path.read_text(encoding='utf-8'))
            rendered = json.dumps(report)
            for case in self.cases:
                self.assertNotIn(case['claims'][0]['text'], rendered)
                self.assertNotIn(case['snapshot']['evidence'][0]['quote'], rendered)
                self.assertNotIn(case['snapshot']['papers'][0]['abstract'], rendered)
            self.assertTrue(report['comparison_available'])
            self.assertEqual(report['benefit'], 'not_established')
            with self.assertRaises(SystemExit):
                paired.main(['--pilot', '--output', str(path)])
            with self.assertRaises(ValueError):
                paired.validate_output(paired.ROOT / 'forbidden-output.json')


if __name__ == '__main__':
    unittest.main()
