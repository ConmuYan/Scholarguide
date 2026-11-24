"""Keep documented source-tree entry points usable after reorganizing modules."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LegacyEntrypointTests(unittest.TestCase):
    def test_imported_entrypoints_are_the_implementation_modules(self):
        import app
        import app_mcp
        import lune_quota_schedule
        import mcp_skills
        import source
        import update_cli
        from scholarguide import app as real_app, app_mcp as real_mcp
        from scholarguide import lune_quota_schedule as real_schedule, mcp_skills as real_skills
        from scholarguide import source as real_source, update_cli as real_update
        for legacy, real in ((app, real_app), (app_mcp, real_mcp),
                             (lune_quota_schedule, real_schedule), (mcp_skills, real_skills),
                             (source, real_source), (update_cli, real_update)):
            self.assertIs(legacy, real)
        self.assertIs(app.build_parser, real_app.build_parser)
        self.assertIs(app._background_web, real_app._background_web)

    def test_legacy_compatibility_path_preserves_module_identity(self):
        aliases = sorted(p.stem for p in (ROOT / 'compat' / 'legacy').glob('*.py'))
        self.assertEqual(len(aliases), 53)
        script = '''import importlib, sys
canonical_first = sys.argv[1] == 'canonical-first'
for name in sys.argv[2:]:
    package = 'scholarguide.experimental' if name in ('deletion_lifecycle', 'specialist_pilot') else 'scholarguide'
    full = package + '.' + name
    first, second = (full, name) if canonical_first else (name, full)
    assert importlib.import_module(first) is importlib.import_module(second), name
'''
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / 'compat' / 'legacy'), str(ROOT))))
        with tempfile.TemporaryDirectory() as cwd:
            for order in ('legacy-first', 'canonical-first'):
                result = subprocess.run([sys.executable, '-c', script, order, *aliases], cwd=cwd, env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_mcp_skills_module_commands(self):
        for command in ('list', 'check'):
            result = subprocess.run([sys.executable, '-m', 'mcp_skills', command], cwd=ROOT,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('literature_review', result.stdout)
