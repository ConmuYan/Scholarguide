"""Installer mechanics against a local HTTP release, with no third-party downloads.

The real installer pulls pinned dependencies from PyPI. This test replaces the
release URL with a loopback server and an empty requirements file so it can
verify checksum enforcement, archive traversal rejection, unpacking, launcher
generation and the data-directory contract without network access outside
127.0.0.1 or a real package install.
"""
import http.server
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'install' / 'install.sh.in'


def _posix(path: Path) -> str:
    """Git Bash needs /c/... rather than C:\\... so tar does not treat it as a remote host."""
    value = str(path)
    if os.name == 'nt' and len(value) > 1 and value[1] == ':':
        return '/' + value[0].lower() + value[2:].replace('\\', '/')
    return value.replace('\\', '/')


def make_release(folder: Path, version='v0.9.9', extra_members=()) -> tuple[Path, str]:
    top = f'scholarguide-{version}'
    archive = folder / f'{top}.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name, body in (
            (f'{top}/app.py', b'import sys\nif __name__ == "__main__":\n    print("stub", *sys.argv[1:])\n'),
            (f'{top}/requirements.txt', b''),
            (f'{top}/.env.example', b'SG_PROVIDER=openai\n'),
        ) + tuple(extra_members):
            data = body
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (folder / 'SHA256SUMS').write_text(f'{digest}  {top}.tar.gz\n', encoding='ascii')
    return archive, digest


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=str(directory), **kwargs)

    def log_message(self, *args):
        pass


class InstallerMechanicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release = self.root / 'release'
        self.release.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), lambda *a, **k: _Handler(*a, directory=self.release, **k))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.cleanup_paths = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def installer(self, version='v0.9.9') -> Path:
        text = INSTALLER.read_text(encoding='utf-8')
        text = text.replace('@REPOSITORY@', 'owner/repo').replace('@VERSION@', version)
        text = text.replace('RELEASE_URL="https://github.com/${REPOSITORY}/releases/download/${VERSION}"',
                            f'RELEASE_URL="{self.base}"')
        # Production pins HTTPS; the loopback fixture serves plain HTTP only.
        text = text.replace("--proto '=https'", "--proto '=http'")
        self.assertIn(f'RELEASE_URL="{self.base}"', text)
        path = self.root / 'install.sh'
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def run_installer(self, version='v0.9.9'):
        env = dict(os.environ, HOME=_posix(self.home), XDG_DATA_HOME=_posix(self.home / '.local' / 'share'))
        env.pop('SG_DATA_DIR', None)
        bash = shutil.which('bash') or shutil.which('sh')
        return subprocess.run([bash, str(self.installer(version))], env=env, capture_output=True,
                              text=True, timeout=120)

    def test_verified_release_installs_launcher_and_keeps_data_separate(self):
        make_release(self.release)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        app = self.home / '.local' / 'share' / 'scholarguide' / 'releases' / 'v0.9.9'
        self.assertTrue((app / 'app.py').is_file())
        self.assertTrue((app / '.venv' / 'bin' / 'python').exists() or (app / '.venv' / 'Scripts' / 'python.exe').exists())
        launcher = self.home / '.local' / 'bin' / 'scholarguide'
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertTrue((self.home / '.scholarguide' / '.env').is_file())
        self.assertTrue((self.home / '.scholarguide' / 'runs').is_dir())
        text = launcher.read_text(encoding='utf-8')
        for contract in ('SG_DATA_DIR', 'SG_RUNS_DIR', 'SG_CACHE_DIR', 'SG_ENV_FILE'):
            self.assertIn(contract, text)
        rerun = self.run_installer()
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertIn('already installed', rerun.stdout)
        env = dict(os.environ, HOME=_posix(self.home), XDG_DATA_HOME=_posix(self.home / '.local' / 'share'))
        bash = shutil.which('bash') or shutil.which('sh')
        run = subprocess.run([bash, _posix(launcher), 'doctor'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('stub doctor', run.stdout)
        self.assertIn('SG_DATA_DIR', env and launcher.read_text(encoding='utf-8'))

    def test_checksum_mismatch_installs_nothing(self):
        _archive, digest = make_release(self.release)
        (self.release / 'SHA256SUMS').write_text(('0' * 64) + '  scholarguide-v0.9.9.tar.gz\n', encoding='ascii')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stderr)
        self.assertFalse((self.home / '.local' / 'share' / 'scholarguide' / 'releases' / 'v0.9.9').exists())

    def test_parent_traversal_member_is_rejected(self):
        make_release(self.release, extra_members=(('scholarguide-v0.9.9/../../escape.txt', b'x'),))
        # Sign the tampered archive so only the traversal guard can stop it.
        archive = self.release / 'scholarguide-v0.9.9.tar.gz'
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.release / 'SHA256SUMS').write_text(f'{digest}  scholarguide-v0.9.9.tar.gz\n', encoding='ascii')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsafe path', result.stderr)
        self.assertFalse((self.home / 'escape.txt').exists())
        self.assertFalse((self.home / '.local' / 'share' / 'scholarguide' / 'releases' / 'v0.9.9').exists())

    def test_missing_python_3_11_fails_closed(self):
        make_release(self.release)
        text = self.installer().read_text(encoding='utf-8')
        text = text.replace('sys.version_info < (3, 11)', 'sys.version_info < (99, 0)')
        path = self.root / 'install-no-python.sh'
        path.write_text(text, encoding='utf-8', newline='\n')
        env = dict(os.environ, HOME=_posix(self.home))
        bash = shutil.which('bash') or shutil.which('sh')
        result = subprocess.run([bash, str(path)], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Python 3.11', result.stderr)
        self.assertFalse((self.home / '.local' / 'share' / 'scholarguide' / 'releases' / 'v0.9.9').exists())


if __name__ == '__main__':
    unittest.main()
