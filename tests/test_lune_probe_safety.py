"""Safety regressions for the historical Lune probe, using local mocks only."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('lune_probe_safety', Path(__file__).resolve().parents[1] / 'scripts' / 'lune_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class LuneProbeSafetyTests(unittest.TestCase):
    def test_existing_evidence_is_not_overwritten_or_probed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'existing.json'
            path.write_text('previous evidence', encoding='utf-8')
            with patch.object(probe.mcp_lune, 'access_token') as token, redirect_stdout(io.StringIO()):
                self.assertNotEqual(probe.main(['--output', str(path)]), 0)
            token.assert_not_called()
            self.assertEqual(path.read_text(encoding='utf-8'), 'previous evidence')

    def test_no_credentials_writes_only_new_bounded_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'new.json'
            with patch.object(probe.mcp_lune, 'access_token', return_value=''), \
                 patch.dict(os.environ, {'LUNE_API_KEY': ''}), redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(['--output', str(path)]), 2)
            report = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(report['ok'])
            self.assertEqual(report['credential'], 'none')

    def test_remote_shape_excludes_untrusted_keys_and_values(self):
        shape = probe._shape({'synthetic-secret-key': 'synthetic-secret-value',
                              'results': [{'title': 'not-for-evidence'}]})
        self.assertNotIn('synthetic-secret', repr(shape))
        self.assertNotIn('not-for-evidence', repr(shape))


if __name__ == '__main__':
    unittest.main()
