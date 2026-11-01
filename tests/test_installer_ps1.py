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


def make_release(folder: Path, version: str) -> None:
    top = f'scholarguide-{version}'
    archive = folder / f'{top}.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name, body in ((f'{top}/app.py', b'print("stub")\n'),
                           (f'{top}/requirements.txt', b''),
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
        self.assertTrue((self.local / 'ScholarGuide' / 'current-version').is_file())
        self.assertTrue((self.local / 'ScholarGuide' / 'bin' / 'scholarguide.cmd').is_file())
        self.assertTrue((self.home / '.scholarguide' / '.env').is_file())

    def test_existing_user_config_survives_install(self):
        data = self.home / '.scholarguide'
        data.mkdir(parents=True, exist_ok=True)
        env_file = data / '.env'
        env_file.write_text('LUNE_API_KEY=' + 'keep' + '-me' + chr(10), encoding='utf-8')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(env_file.read_text(encoding='utf-8'), 'LUNE_API_KEY=keep-me' + chr(10))

    def test_checksum_mismatch_installs_nothing(self):
        (self.release / 'SHA256SUMS').write_text(('0' * 64) + '  scholarguide-v0.9.9.tar.gz\n',
                                                  encoding='ascii')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stdout + result.stderr)
        self.assertFalse((self.local / 'ScholarGuide' / 'releases' / 'v0.9.9').exists())


if __name__ == '__main__':
    unittest.main()
