"""`scholarguide update` keeps user data intact and verifies before installing."""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import update_cli


def release_payload(tag='v0.2.0'):
    return {'tag_name': tag}


class VersionTests(unittest.TestCase):
    def test_numeric_comparison_beats_string_comparison(self):
        self.assertTrue(update_cli.newer('v0.10.0', 'v0.9.0'))
        self.assertTrue(update_cli.newer('v1.0.0', 'v0.99.99'))
        self.assertFalse(update_cli.newer('v0.1.0', 'v0.1.0'))
        self.assertFalse(update_cli.newer('v0.1.0', 'v0.2.0'))

    def test_prerelease_sorts_before_release(self):
        self.assertFalse(update_cli.newer('v1.0.0-rc1', 'v1.0.0'))
        self.assertTrue(update_cli.newer('v1.0.0', 'v1.0.0-rc1'))

    def test_rejects_non_version_tags(self):
        for tag in ('latest', 'v1.0', '', 'v1.0.0;rm -rf /'):
            self.assertFalse(update_cli.newer(tag, 'v0.1.0'), tag)

    def test_posix_layout_is_found_when_the_platform_root_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            posix = Path(tmp) / '.local' / 'share' / 'scholarguide'
            (posix / 'releases').mkdir(parents=True)
            (posix / 'current-version').write_text('v0.3.0', encoding='ascii')
            saved = {k: os.environ.get(k) for k in ('SG_INSTALL_ROOT', 'XDG_DATA_HOME', 'LOCALAPPDATA')}
            os.environ.pop('SG_INSTALL_ROOT', None)
            os.environ['XDG_DATA_HOME'] = str(Path(tmp) / '.local' / 'share')
            os.environ['LOCALAPPDATA'] = str(Path(tmp) / 'appdata')
            try:
                self.assertEqual(update_cli.install_root(), posix)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_data_dir_prefers_an_existing_shared_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / '.scholarguide'
            shared.mkdir()
            saved = {k: os.environ.get(k) for k in ('SG_DATA_DIR', 'HOME', 'USERPROFILE')}
            os.environ.pop('SG_DATA_DIR', None)
            os.environ['HOME'] = tmp
            os.environ['USERPROFILE'] = tmp
            try:
                with unittest.mock.patch.object(update_cli.Path, 'home', return_value=Path(tmp)):
                    self.assertEqual(update_cli.data_dir(), shared)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_install_root_override_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get('SG_INSTALL_ROOT')
            os.environ['SG_INSTALL_ROOT'] = tmp
            try:
                self.assertEqual(update_cli.install_root(), Path(tmp).expanduser())
            finally:
                if old is None:
                    os.environ.pop('SG_INSTALL_ROOT', None)
                else:
                    os.environ['SG_INSTALL_ROOT'] = old

    def test_version_marker_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(update_cli.installed_version(root), '')
            (root / 'current-version').write_text('v0.1.1', encoding='ascii')
            self.assertEqual(update_cli.installed_version(root), 'v0.1.1')
            (root / 'current-version').write_text('garbage', encoding='ascii')
            self.assertEqual(update_cli.installed_version(root), '')


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'app'
        self.root.mkdir()
        (self.root / 'current-version').write_text('v0.1.1\n', encoding='ascii')
        self.data = Path(self.tmp.name) / 'data'
        self.data.mkdir()
        (self.data / '.env').write_text('LUNE_API_KEY=' + 'secret' + '-' + 'value' + chr(10), encoding='utf-8')
        (self.data / 'providers.json').write_text('{"profiles":[]}', encoding='utf-8')
        (self.data / 'lune-quota.sqlite3').write_bytes(b'sqlite-bytes')
        (self.data / 'lune-quota-schedule.json').write_text('{"enabled":true}', encoding='utf-8')
        self.snapshot = {p.name: p.read_bytes() for p in self.data.iterdir()}
        self.old_data = os.environ.get('SG_DATA_DIR')
        os.environ['SG_DATA_DIR'] = str(self.data)

    def tearDown(self):
        if self.old_data is None:
            os.environ.pop('SG_DATA_DIR', None)
        else:
            os.environ['SG_DATA_DIR'] = self.old_data
        self.tmp.cleanup()

    def assert_data_untouched(self):
        now = {p.name: p.read_bytes() for p in self.data.iterdir()}
        self.assertEqual(now, self.snapshot)

    def test_source_checkout_is_refused(self):
        empty = Path(self.tmp.name) / 'src'
        empty.mkdir()
        with self.assertRaises(update_cli.UpdateError) as caught:
            update_cli.update(root=empty, fetch_json=lambda url: release_payload())
        self.assertIn('source checkout', str(caught.exception))

    def test_already_latest_changes_nothing(self):
        called = []
        result = update_cli.update(root=self.root, fetch_json=lambda url: release_payload('v0.1.1'),
                                   runner=lambda release, work: called.append(release))
        self.assertEqual(result['action'], 'up-to-date')
        self.assertFalse(result['updated'])
        self.assertEqual(called, [])
        self.assert_data_untouched()

    def test_check_reports_without_installing(self):
        called = []
        result = update_cli.update(check_only=True, root=self.root,
                                   fetch_json=lambda url: release_payload('v0.2.0'),
                                   runner=lambda release, work: called.append(release))
        self.assertEqual(result['action'], 'available')
        self.assertEqual(result['latest'], 'v0.2.0')
        self.assertEqual(called, [])
        self.assert_data_untouched()

    def test_install_runs_the_verified_installer_and_keeps_data(self):
        seen = {}

        def runner(release, work):
            seen['tag'] = release.tag
            seen['installer_sh'] = release.installer_sh
            (self.root / 'current-version').write_text(release.tag + '\n', encoding='ascii')

        result = update_cli.update(assume_yes=True, root=self.root,
                                   fetch_json=lambda url: release_payload('v0.2.0'), runner=runner)
        self.assertEqual(seen['tag'], 'v0.2.0')
        self.assertTrue(seen['installer_sh'].endswith('/v0.2.0/install.sh'))
        self.assertTrue(result['updated'])
        self.assertEqual(update_cli.installed_version(self.root), 'v0.2.0')
        self.assert_data_untouched()

    def test_explicit_target_must_be_newer(self):
        with self.assertRaises(update_cli.UpdateError):
            update_cli.update(target='v0.1.0', assume_yes=True, root=self.root,
                              fetch_json=lambda url: release_payload('v0.2.0'))
        with self.assertRaises(update_cli.UpdateError):
            update_cli.update(target='not-a-tag', assume_yes=True, root=self.root,
                              fetch_json=lambda url: release_payload('v0.2.0'))

    def test_network_failure_is_reported_and_leaves_version(self):
        def boom(url):
            raise OSError('offline')
        with self.assertRaises(update_cli.UpdateError) as caught:
            update_cli.update(check_only=True, root=self.root, fetch_json=boom)
        self.assertIn('cannot reach GitHub', str(caught.exception))
        self.assertEqual(update_cli.installed_version(self.root), 'v0.1.1')
        self.assert_data_untouched()

    def test_bad_tag_from_api_is_rejected(self):
        with self.assertRaises(update_cli.UpdateError):
            update_cli.update(check_only=True, root=self.root,
                              fetch_json=lambda url: {'tag_name': 'nightly'})


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=str(directory), **kwargs)

    def log_message(self, *args):
        pass


class InstallerVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = Path(self.tmp.name) / 'release'
        self.files.mkdir()
        self.server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), lambda *a, **k: _Handler(*a, directory=self.files, **k))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        base = f'http://127.0.0.1:{self.server.server_port}'
        self.release = update_cli.Release(
            tag='v0.2.0', archive=base + '/scholarguide-v0.2.0.tar.gz',
            checksums=base + '/SHA256SUMS', installer_sh=base + '/install.sh',
            installer_ps1=base + '/install.ps1')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def write_release(self, marker: Path, *, checksum=None):
        """Publish both installer flavours and a matching checksum manifest."""
        import hashlib
        sh_body = '#!/bin/sh' + chr(10) + 'printf ran > "' + str(marker) + '"' + chr(10)
        ps_body = "Set-Content -Path '" + str(marker) + "' -Value 'ran'" + chr(10)
        manifest = []
        for name, body in (('install.sh', sh_body), ('install.ps1', ps_body)):
            (self.files / name).write_text(body, encoding='utf-8')
            digest = checksum or hashlib.sha256(body.encode()).hexdigest()
            manifest.append(digest + '  ' + name)
        (self.files / 'SHA256SUMS').write_text(chr(10).join(manifest) + chr(10), encoding='utf-8')

    def test_cli_check_on_source_checkout_explains_itself(self):
        code = update_cli.main(['--check'])
        self.assertEqual(code, 1)


if __name__ == '__main__':
    unittest.main()
