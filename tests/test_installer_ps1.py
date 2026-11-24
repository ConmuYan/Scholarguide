"""Windows installer: real PowerShell run against a loopback release.

Uses a stub archive with an empty requirements file, so it exercises the
download, checksum, traversal guard, tar invocation, venv creation and launcher
generation without third-party packages or admin rights.
"""
import hashlib
import http.server
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell') or shutil.which('pwsh')


def make_release(folder: Path, version: str, requirements: bytes = b'') -> None:
    top = f'scholarguide-{version}'
    archive = folder / f'{top}.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name, body in ((f'{top}/app.py', b'import sys\nprint("stub", *sys.argv[1:])\n'),
                           (f'{top}/requirements.txt', requirements),
                           (f'{top}/.env.example', b'SG_PROVIDER=openai\n')):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (folder / 'SHA256SUMS').write_text(f'{digest}  {top}.tar.gz\n', encoding='ascii')


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=str(directory), **kwargs)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'Windows PowerShell is required')
class WindowsInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release = self.root / 'release'
        self.release.mkdir()
        self.local = self.root / 'localappdata'
        self.home = self.root / 'home'
        self.local.mkdir()
        self.home.mkdir()
        self.server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), lambda *a, **k: _Handler(*a, directory=self.release, **k))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        make_release(self.release, 'v0.9.9')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def script(self) -> Path:
        text = (ROOT / 'install' / 'install.ps1.in').read_text(encoding='utf-8')
        text = text.replace('@REPOSITORY@', 'owner/repo').replace('@VERSION@', 'v0.9.9')
        text = text.replace('$ReleaseUrl = "https://github.com/$Repository/releases/download/$Version"',
                            f'$ReleaseUrl = "{self.base}"')
        self.assertIn(self.base, text)
        path = self.root / 'install.ps1'
        path.write_text(text, encoding='utf-8', newline='\r\n')
        return path

    def run_installer(self):
        env = dict(os.environ, LOCALAPPDATA=str(self.local), USERPROFILE=str(self.home))
        completed = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                    '-File', str(self.script())],
                                   env=env, capture_output=True, timeout=300)
        # PowerShell may emit localized text in the console code page, not UTF-8.
        completed.stdout = (completed.stdout or b'').decode('utf-8', 'replace')
        completed.stderr = (completed.stderr or b'').decode('utf-8', 'replace')
        return completed

    def test_install_succeeds_with_gnu_or_bsd_tar(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target = self.local / 'ScholarGuide' / 'releases' / 'v0.9.9'
        self.assertTrue((target / 'app.py').is_file())
        self.assertTrue((target / '.venv' / 'Scripts' / 'python.exe').is_file())
        pip = subprocess.run([str(target / '.venv' / 'Scripts' / 'pip.exe'), '--version'],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(pip.returncode, 0, pip.stderr)
        self.assertTrue((self.local / 'ScholarGuide' / 'current-version').is_file())
        launcher = self.local / 'ScholarGuide' / 'bin' / 'scholarguide.cmd'
        self.assertTrue(launcher.is_file())
        self.assertTrue((self.home / '.scholarguide' / '.env').is_file())
        # The installed launcher must forward explicit arguments, not replace
        # them with the default web command.
        invoked = subprocess.run([str(launcher), 'doctor'],
                                 env=dict(os.environ, LOCALAPPDATA=str(self.local),
                                          USERPROFILE=str(self.home)),
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(invoked.returncode, 0, invoked.stdout + invoked.stderr)
        self.assertIn('stub doctor', invoked.stdout)

    def test_existing_user_config_survives_install(self):
        data = self.home / '.scholarguide'
        data.mkdir(parents=True, exist_ok=True)
        env_file = data / '.env'
        env_file.write_text('LUNE_API_KEY=' + 'keep' + '-me' + chr(10), encoding='utf-8')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(env_file.read_text(encoding='utf-8'), 'LUNE_API_KEY=keep-me' + chr(10))

    def test_brackets_in_localappdata_and_home_are_literal(self):
        self.local = self.root / 'user[ab]' / 'localappdata'
        self.home = self.root / 'home[ab]'
        self.local.mkdir(parents=True)
        self.home.mkdir()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        base = self.local / 'ScholarGuide'
        self.assertTrue((base / 'releases' / 'v0.9.9' / 'app.py').is_file())
        self.assertTrue((self.home / '.scholarguide' / '.env').is_file())
        self.assertFalse(list(base.glob('stage-*')))
        self.assertFalse(list(base.glob('.install-*')))

    def test_failed_dependency_install_preserves_existing_version_and_cleans_target(self):
        self.local = self.root / 'user[ab]' / 'localappdata'
        self.local.mkdir(parents=True)
        base = self.local / 'ScholarGuide'
        old = base / 'releases' / 'v0.9.8'
        old.mkdir(parents=True)
        (old / 'app.py').write_text('old-version', encoding='utf-8')
        (base / 'current-version').write_text('v0.9.8', encoding='ascii')
        data = self.home / '.scholarguide'
        data.mkdir()
        (data / '.env').write_text('fixture-value', encoding='utf-8')
        # Invalid syntax makes pip fail locally without downloading packages.
        make_release(self.release, 'v0.9.9', requirements=b'???\n')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        base = self.local / 'ScholarGuide'
        self.assertFalse(list(base.glob('stage-*')))
        self.assertFalse(list(base.glob('.install-*')))
        self.assertFalse((base / 'releases' / 'v0.9.9').exists())
        self.assertEqual((base / 'current-version').read_text(encoding='ascii'), 'v0.9.8')
        self.assertEqual((old / 'app.py').read_text(encoding='utf-8'), 'old-version')
        self.assertEqual((data / '.env').read_text(encoding='utf-8'), 'fixture-value')

    def test_interrupted_install_fails_closed_without_deleting_existing_files(self):
        target = self.local / 'ScholarGuide' / 'releases' / 'v0.9.9'
        target.mkdir(parents=True)
        marker = target / '.install-incomplete'
        marker.write_text('another-install', encoding='ascii')
        (target / 'app.py').write_text('preserve', encoding='utf-8')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(marker.exists())
        self.assertEqual((target / 'app.py').read_text(encoding='utf-8'), 'preserve')
        self.assertFalse((self.local / 'ScholarGuide' / 'current-version').exists())

    def test_concurrent_installers_leave_one_complete_version(self):
        self.local = self.root / 'user[ab]' / 'localappdata'
        self.local.mkdir(parents=True)
        script = self.script()
        env = dict(os.environ, LOCALAPPDATA=str(self.local), USERPROFILE=str(self.home))
        first = subprocess.Popen([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        second = subprocess.Popen([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        outputs = [process.communicate(timeout=300)[0] for process in (first, second)]
        self.assertIn(0, [first.returncode, second.returncode], [value.decode('utf-8', 'replace') for value in outputs])
        base = self.local / 'ScholarGuide'
        target = base / 'releases' / 'v0.9.9'
        self.assertTrue((target / 'app.py').is_file())
        self.assertFalse((target / '.install-incomplete').exists())
        self.assertEqual((base / 'current-version').read_text(encoding='ascii'), 'v0.9.9')
        self.assertFalse(list(base.glob('stage-*')))
        self.assertFalse(list(base.glob('.install-*')))

    def test_checksum_mismatch_installs_nothing(self):
        (self.release / 'SHA256SUMS').write_text(('0' * 64) + '  scholarguide-v0.9.9.tar.gz\n',
                                                  encoding='ascii')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stdout + result.stderr)
        self.assertFalse((self.local / 'ScholarGuide' / 'releases' / 'v0.9.9').exists())


if __name__ == '__main__':
    unittest.main()
