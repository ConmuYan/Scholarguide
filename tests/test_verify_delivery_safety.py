"""The delivery runner must isolate live source credentials and local ledgers."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import load_env
from scripts.verify_delivery import isolated_environment, apply_browser_isolation, source_manifest, manifest_drift


class DeliveryEnvironmentTests(unittest.TestCase):
    def test_source_credentials_and_local_stores_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inherited = {'LUNE_API_KEY': 'synthetic-private', 'LUNE_TOKEN': 'synthetic-private',
                         'SG_API_KEY': 'synthetic-private', 'OPENALEX_API_KEY': 'synthetic-private',
                         'SG_LUNE_QUOTA_DB': '/real/quota', 'SG_LUNE_LEDGER': '/real/ledger',
                         'SG_MCP_TOKEN_FILE': '/real/tokens', 'SG_SOURCE_STORE': '/real/sources',
                         'SG_PROVIDER_STORE': '/real/providers', 'PI_CODING_AGENT_DIR': '/real/pi',
                         'SG_MEMORY_STORE': '/real/memory.sqlite',
                         'SG_MCP_CONFIG': '/real/config', 'SG_BASE_URL': 'https://secret.invalid',
                         'SE_OFFLINE': 'false', 'SE_CLEAR_CACHE': 'true',
                         'SE_CACHE_PATH': '/real/selenium', 'SE_CHROMEDRIVER': '/real/driver',
                         'EXTRA_API_KEY': 'synthetic-private', 'AWS_SECRET_ACCESS_KEY': 'synthetic-private',
                         'CUSTOM_TOKEN': 'synthetic-private', 'CUSTOM_PASSWORD': 'synthetic-private',
                         'HTTP_PROXY': 'http://private-proxy', 'PATH': 'fixture-path'}
            env = isolated_environment(root, inherited)
            for key in ('LUNE_API_KEY', 'LUNE_TOKEN', 'SG_API_KEY', 'OPENALEX_API_KEY',
                        'EXTRA_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'CUSTOM_TOKEN', 'CUSTOM_PASSWORD'):
                self.assertEqual(env[key], '')
            for key in ('SG_LUNE_QUOTA_DB', 'SG_LUNE_LEDGER', 'SG_MCP_TOKEN_FILE', 'SG_SOURCE_STORE',
                        'SG_PROVIDER_STORE', 'PI_CODING_AGENT_DIR', 'SG_MEMORY_STORE'):
                self.assertTrue(Path(env[key]).is_relative_to(root))
            self.assertNotEqual(env['SG_MCP_CONFIG'], '/real/config')
            self.assertEqual(env['SG_BASE_URL'], 'http://127.0.0.1:1')
            self.assertEqual(env['PATH'], 'fixture-path')
            self.assertEqual(inherited['SG_LUNE_QUOTA_DB'], '/real/quota')
            self.assertEqual(env['SG_SKIP_PROJECT_ENV'], '1')
            self.assertEqual(env['SE_OFFLINE'], 'true')
            self.assertEqual(env['SE_AVOID_STATS'], 'true')
            self.assertNotIn('SE_CLEAR_CACHE', env)
            self.assertTrue(Path(env['SE_CACHE_PATH']).is_relative_to(root))
            self.assertNotEqual(env.get('SE_CHROMEDRIVER'), '/real/driver')
            self.assertEqual(env['HTTP_PROXY'], '')
            self.assertEqual(env['SG_OFFLINE_NETWORK_GUARD'], '1')
            self.assertTrue(str(root) not in env['PYTHONPATH'])

    def test_source_manifest_covers_paired_evaluation_sources(self):
        manifest = source_manifest()
        for name in ('evaluation/engineering_agent/run_paired.py',
                     'evaluation/engineering_agent/paired_cases.json',
                     'evaluation/engineering_agent/PAIRED_PROTOCOL.md'):
            self.assertIn(name, manifest)

    def test_manifest_drift_detects_additions_deletions_and_changes(self):
        self.assertEqual(manifest_drift({'a': '1', 'b': '2'}, {'a': '3', 'c': '4'}), ['a', 'b', 'c'])
        self.assertEqual(manifest_drift({'a': '1'}, {'a': '1'}), [])

    def test_child_process_blocks_external_dns_and_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            env = isolated_environment(Path(directory))
            script = ('import socket\n'
                      'socket.getaddrinfo("localhost", 80)\n'
                      'try:\n socket.getaddrinfo("synthetic.invalid", 80)\n'
                      'except socket.gaierror: pass\n'
                      'else: raise AssertionError("external DNS permitted")\n'
                      's = socket.socket()\n'
                      'try:\n s.connect(("203.0.113.1", 9))\n'
                      'except OSError: pass\n'
                      'else: raise AssertionError("external socket permitted")\n'
                      'finally: s.close()\n')
            completed = subprocess.run([sys.executable, '-B', '-c', script], env=env,
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_browser_uses_unique_profile_and_unreachable_external_proxy(self):
        class Options:
            def __init__(self): self.arguments = []
            def add_argument(self, value): self.arguments.append(value)
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / 'profile'
            options = Options()
            apply_browser_isolation(options, profile)
            self.assertIn('--user-data-dir=' + str(profile), options.arguments)
            self.assertIn('--proxy-server=http://127.0.0.1:1', options.arguments)
            self.assertIn('--proxy-bypass-list=127.0.0.1;localhost', options.arguments)
            self.assertIn('--disable-quic', options.arguments)

    def test_isolation_skips_only_default_dotenv(self):
        with patch.dict('os.environ', {'SG_SKIP_PROJECT_ENV': '1'}), \
             patch('pathlib.Path.read_text') as reader:
            load_env()
            reader.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            explicit = Path(directory) / 'custom.env'
            explicit.write_text('SG_TEST_ISOLATION_VALUE=synthetic\n', encoding='utf-8')
            with patch.dict('os.environ', {'SG_SKIP_PROJECT_ENV': '1'}, clear=True):
                load_env(explicit)
                import os
                self.assertEqual(os.environ['SG_TEST_ISOLATION_VALUE'], 'synthetic')


if __name__ == '__main__':
    unittest.main()
