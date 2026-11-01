import importlib.util
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'evaluation/engineering_agent/run_paired.py'


class PairedEvaluationTests(unittest.TestCase):
    def test_independent_fixture_and_duplicates(self):
        check = self.module().check_evidence
        fixture = [{'id': 'a', 'abstract': 'A sufficiently long quote here.'}]
        good = {'id': 'a', 'abstract': fixture[0]['abstract'], 'evidence_verified': True,
                'evidence_quote': 'A sufficiently long quote'}
        self.assertFalse(check([good, good], fixture, fixture)['passed'])
        forged = {**good, 'abstract': 'A sufficiently long quote fabricated.'}
        self.assertFalse(check([forged], [forged], fixture)['passed'])
        self.assertFalse(check([good], [forged], fixture)['passed'])

    def test_completion_and_attempt_budgets(self):
        module = self.module()
        limits = {'max_search_calls': 1, 'max_model_calls': 2}
        self.assertTrue(module.execution_ok('complete', 'requirements_met', ['search', 'assess'], limits))
        for status, reason in [('partial', 'requirements_met'), ('complete', 'user_cancelled'),
                               ('failed', 'error')]:
            self.assertFalse(module.execution_ok(status, reason, ['search', 'assess'], limits))
        self.assertFalse(module.execution_ok('complete', 'requirements_met',
                                            ['search', 'search', 'assess'], limits))

    def test_output_rejects_outside(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(module.tempfile, 'gettempdir', return_value=str(root / 'allowed')):
                (root / 'allowed').mkdir()
                with self.assertRaises(ValueError):
                    module.validate_output(root / 'outside.json')

    def test_output_rejects_symlink_escape(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(module.tempfile, 'gettempdir', return_value=str(root / 'allowed')):
                (root / 'allowed').mkdir()
                with self.assertRaises(ValueError):
                    module.validate_output(root / 'outside.json')
                link = root / 'allowed' / 'link'
                try:
                    link.symlink_to(root, target_is_directory=True)
                except OSError:
                    if os.name != 'nt':
                        raise
                    result = subprocess.run(['wsl', '--', 'env', '-i', 'PATH=/usr/bin:/bin',
                        'HOME=/tmp', 'LANG=C.UTF-8', 'python3', '-B', '-m', 'unittest',
                        'tests.test_paired_evaluation.PairedEvaluationTests.test_output_rejects_symlink_escape'],
                        cwd=ROOT, capture_output=True, timeout=40)
                    self.assertEqual(result.returncode, 0, repr(result.stderr[-2000:]))
                    return
                with self.assertRaises(ValueError):
                    module.validate_output(link / 'outside.json')

    def module(self):
        spec = importlib.util.spec_from_file_location('paired_runner', RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_attribution_requires_verified_nonempty_matching_own_abstract(self):
        check = self.module().check_evidence
        papers = [{'id': 'a', 'abstract': 'A sufficiently long quote here.'},
                  {'id': 'b', 'abstract': 'Another sufficiently long quote.'}]
        candidate = {'id': 'a', 'abstract': papers[0]['abstract']}
        self.assertTrue(check([{**candidate, 'evidence_verified': True,
                                'evidence_quote': 'A sufficiently long quote'}], papers, papers)['passed'])
        for quote in ('', 'Another sufficiently long quote'):
            self.assertFalse(check([{**candidate, 'evidence_verified': True,
                                     'evidence_quote': quote}], papers, papers)['passed'])
        self.assertFalse(check([], papers, papers)['passed'])

    def test_swallowed_overrun_and_partial_never_pass(self):
        import workflow
        module = self.module()
        manifest = module.load_manifest()
        original = workflow.run
        def altered(*args, **kwargs):
            result = original(*args, **kwargs)
            try:
                kwargs['source'].search('overrun')
            except RuntimeError:
                pass
            return result
        with tempfile.TemporaryDirectory() as tmp, patch.object(workflow, 'run', altered):
            row = module.worker(manifest['cases'][0], manifest, tmp)
        self.assertEqual(row['tool_calls']['sequence'].count('search'), 2)
        self.assertFalse(row['checks']['passed'])
        self.assertEqual(row['token_limit_status'], 'unmeasured')
        def partial(*args, **kwargs):
            result = original(*args, **kwargs)
            result.update(status='partial', stop_reason='user_cancelled')
            return result
        with tempfile.TemporaryDirectory() as tmp, patch.object(workflow, 'run', partial):
            row = module.worker(manifest['cases'][0], manifest, tmp)
        self.assertFalse(row['checks']['passed'])

    def test_fixture_is_read_once_and_snapshot_is_used(self):
        module = self.module()
        manifest = module.load_manifest()
        fixture_path = module.ROOT / manifest['fixture']
        original_read = Path.read_bytes
        reads = []
        def read(path):
            if path == fixture_path:
                reads.append(path)
                if len(reads) > 1:
                    raise AssertionError('fixture reread')
            return original_read(path)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'read_bytes', read):
            row = module.worker(manifest['cases'][0], manifest, tmp)
        self.assertEqual(len(reads), 1)
        self.assertTrue(row['checks']['passed'])

    def test_worker_restores_socket_hooks_after_execution(self):
        module = self.module()
        manifest = module.load_manifest()
        names = ('connect', 'connect_ex', 'create_connection', 'getaddrinfo')
        original = (socket.socket.connect, socket.socket.connect_ex,
                    socket.create_connection, socket.getaddrinfo)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                module.worker(manifest['cases'][0], manifest, tmp)
            actual = (socket.socket.connect, socket.socket.connect_ex,
                      socket.create_connection, socket.getaddrinfo)
            for name, before, after in zip(names, original, actual):
                self.assertIs(after, before, name)
        finally:
            socket.socket.connect, socket.socket.connect_ex = original[:2]
            socket.create_connection, socket.getaddrinfo = original[2:]

    def test_source_drift_fails(self):
        module = self.module()
        manifest = module.load_manifest()
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, 'source_hashes', side_effect=[{'x': 'a'}, {'x': 'b'}]):
            row = module.worker(manifest['cases'][0], manifest, tmp)
        self.assertFalse(row['checks']['passed'])
        self.assertFalse(row['checks']['source_unchanged'])

    def test_pilot_runs_all_rows_and_never_passes_unavailable_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result.json'
            result = subprocess.run([sys.executable, '-B', str(RUNNER), '--pilot',
                                     '--split', 'all', '--output', str(output)],
                                    capture_output=True, text=True, timeout=90)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertFalse(report['comparison_passed'])
            self.assertEqual(len(report['results']), report['denominator']['tasks'])
            self.assertTrue(all(row['arms']['multi']['status'] == 'unavailable'
                                for row in report['results']))
            self.assertTrue(all(row['arms']['single']['status'] == 'completed'
                                for row in report['results']), report)
            self.assertTrue(all(row['arms']['single']['checks']['passed']
                                for row in report['results']), report)
            self.assertEqual(report['results'][0]['arms']['single']['token_limit_status'], 'unmeasured')
            self.assertTrue(all(not ref['executed'] for ref in report['results'][0]['arms']['single']['test_references'].values()))
            self.assertTrue(report['source_unchanged'])
            self.assertFalse(report['synthetic'])
            self.assertTrue(report['synthetic_tasks'])
            self.assertEqual(report['paper_fixture_kind'], 'bundled_real_metadata')
            self.assertIsNone(report['results'][0]['arms']['single']['usage']['input_tokens'])
            self.assertNotIn('abstract', output.read_text(encoding='utf-8'))
            again = subprocess.run([sys.executable, '-B', str(RUNNER), '--pilot',
                                    '--output', str(output)], capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)

    def test_failed_worker_is_retained(self):
        module = self.module()
        manifest = module.load_manifest()
        with patch.object(module.subprocess, 'run', side_effect=OSError('synthetic')):
            row = module.run_case(manifest['cases'][0], manifest)
        self.assertEqual(row['id'], manifest['cases'][0]['id'])
        self.assertEqual(row['arms']['single']['status'], 'error')
        self.assertFalse(row['arms']['single']['checks']['passed'])
        self.assertEqual(row['arms']['multi']['status'], 'unavailable')
        self.assertIsNone(row['arms']['single']['usage'])

    def test_default_is_disabled_and_manifest_integrity_is_enforced(self):
        module = self.module()
        manifest = module.load_manifest()
        manifest['cases'][0]['request']['target_count'] += 1
        with self.assertRaises(ValueError):
            module.validate_manifest(manifest)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result.json'
            result = subprocess.run([sys.executable, '-B', str(RUNNER), '--output', str(output)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
