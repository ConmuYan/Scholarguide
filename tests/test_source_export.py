"""Public source export: no secrets, no run history, and it can still build a release."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCE = _module('build_source_repo', ROOT / 'scripts' / 'build_source_repo.py')
RELEASE = _module('build_release', ROOT / 'scripts' / 'build_release.py')


class SourceExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / 'repo'

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_excludes_private_material_and_keeps_contributor_surface(self):
        report = SOURCE.export(self.out)
        self.assertEqual(report['secret_findings'], [])
        names = {p.relative_to(self.out).as_posix() for p in self.out.rglob('*') if p.is_file()}
        for required in ('app.py', 'common.py', 'LICENSE', 'NOTICE.md', 'README.md',
                         'requirements.txt', 'requirements-dev.txt', '.env.example',
                         'install/install.sh.in', 'install/install.ps1.in',
                         '.github/workflows/release.yml', 'scripts/build_release.py',
                         'scripts/set_repository.py', 'docs/API.md', 'docs/INSTALL.md',
                         'docs/PUBLISHING.md',
                         'tests/test_release_package.py', 'web/index.html', 'vendor/gptr_arxiv.py',
                         'vendor/LICENSE-gpt-researcher', 'SOURCE_MANIFEST.json'):
            self.assertIn(required, names)
        for banned in ('.env', 'NUL', 'examples/live_result.json', 'examples/live_report.md',
                       'core_lock.json'):
            self.assertNotIn(banned, names)
        for prefix in ('runs/', 'archive/', 'cloudflare-worker/', 'cloudflare-pages/',
                       '.pi/', '.codex/', 'evaluation/results/', 'docs/PROPOSAL_CORE.md'):
            self.assertFalse([n for n in names if n.startswith(prefix)], prefix)
        self.assertFalse([n for n in names if n.lower().endswith('.pdf')])

    def test_export_is_idempotent_and_refuses_nonempty_target(self):
        report = SOURCE.export(self.out)
        first = json.loads((self.out / 'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
        self.assertEqual(first['files'], report['files'])
        with self.assertRaises(FileExistsError):
            SOURCE.export(self.out)

    def test_exported_tree_builds_release_and_runs_offline_demo(self):
        report = SOURCE.export(self.out, include_evaluation=False)
        self.assertEqual(report['secret_findings'], [])
        assets = RELEASE.build('v0.2.0', 'owner/repo', Path(self.tmp.name) / 'assets')
        archive = next(p for p in assets if p.name.endswith('.tar.gz'))
        self.assertTrue(archive.is_file())
        demo = subprocess.run([sys.executable, str(self.out / 'app.py'), 'demo', '--scenario', 'satisfied',
                               '--out', str(Path(self.tmp.name) / 'run')],
                              cwd=self.out, capture_output=True, text=True, timeout=90)
        self.assertEqual(demo.returncode, 0, demo.stderr)

    def test_personal_paths_and_credentials_are_detected(self):
        self.out.mkdir(parents=True)
        # Built at runtime so this test file is not itself flagged on export.
        windows_home = 'C:' + chr(92) + 'Users' + chr(92) + 'alice' + chr(92) + 'project'
        (self.out / 'leak.md').write_text('cd ' + windows_home + chr(10), encoding='utf-8')
        (self.out / 'key.py').write_text('SG_API_KEY=' + 'abcdefghijklmnop' + '1234' + chr(10), encoding='utf-8')
        findings = SOURCE.secret_findings(self.out)
        self.assertTrue(any('home directory' in line for line in findings), findings)
        self.assertTrue(any('credential' in line for line in findings), findings)

    def test_repository_slug_helper_rewrites_and_checks(self):
        helper = _module('set_repository', ROOT / 'scripts' / 'set_repository.py')
        self.assertEqual(helper.main(['--repository', '../bad', '--check']), 2)
        self.assertEqual(helper.main(['--repository', 'owner/repo', '--check']), 0)


if __name__ == '__main__':
    unittest.main()
