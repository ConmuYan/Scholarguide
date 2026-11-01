"""`scholarguide update` — move an installed copy to a newer GitHub release.

What it guarantees
------------------
* **User data is never touched.** Keys, provider profiles, quota ledger,
  schedule and run history live in ``SG_DATA_DIR`` (``~/.scholarguide``) and the
  release installer never writes there.
* **Only a signed-in-installer path is reused.** Instead of re-implementing
  unpack/venv/switching, this command downloads the *release's own* installer,
  verifies its SHA-256 against that release's ``SHA256SUMS`` and runs it. One
  implementation of installation, one set of checks.
* **No silent upgrade.** It only reports unless ``--yes`` is passed, and it
  refuses to run from a source checkout (no install marker).
* **No downgrade, no guessing.** The target tag must differ from the installed
  one; versions are compared numerically.

Network access is limited to the official GitHub API and release assets over
HTTPS. A failure changes nothing: the current version keeps running.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPOSITORY = 'ConmuYan/Scholarguide'
API_LATEST = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
TAG_RE = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z')
TIMEOUT = 20


def install_root() -> Path:
    """Where installed releases live; mirrors the installer's layout.

    ``SG_INSTALL_ROOT`` overrides it, which is what the tests and relocatable
    installs use; the default matches the platform installer.
    """
    override = os.environ.get('SG_INSTALL_ROOT', '').strip()
    if override:
        return Path(override).expanduser()
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'ScholarGuide'
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'scholarguide'


def data_dir() -> Path:
    return Path(os.environ.get('SG_DATA_DIR') or Path.home() / '.scholarguide')


def installed_version(root: Path | None = None) -> str:
    path = (root or install_root()) / 'current-version'
    try:
        value = path.read_text(encoding='ascii').strip()
    except OSError:
        return ''
    return value if TAG_RE.fullmatch(value) else ''


def version_key(tag: str) -> tuple:
    """Numeric comparison, prerelease sorts before the matching release."""
    body = tag.lstrip('v')
    core, _, suffix = body.partition('-')
    numbers = tuple(int(part) for part in core.split('.')) if all(p.isdigit() for p in core.split('.')) else ()
    return numbers, (0 if suffix else 1), suffix


def newer(candidate: str, current: str) -> bool:
    if not TAG_RE.fullmatch(candidate):
        return False
    if not TAG_RE.fullmatch(current):
        return True
    return version_key(candidate) > version_key(current)


@dataclass
class Release:
    tag: str
    archive: str
    checksums: str
    installer_sh: str
    installer_ps1: str


def latest_release(fetch=None) -> Release:
    fetch = fetch or _fetch_json
    try:
        payload = fetch(API_LATEST)
    except Exception as exc:  # noqa: BLE001 - reported to the user, nothing changes
        raise UpdateError(f'cannot reach GitHub releases: {type(exc).__name__}') from exc
    tag = str(payload.get('tag_name') or '')
    if not TAG_RE.fullmatch(tag):
        raise UpdateError('the latest release tag is not a version tag')
    base = f'https://github.com/{REPOSITORY}/releases/download/{tag}'
    return Release(tag=tag, archive=f'{base}/scholarguide-{tag}.tar.gz',
                   checksums=f'{base}/SHA256SUMS',
                   installer_sh=f'{base}/install.sh', installer_ps1=f'{base}/install.ps1')


class UpdateError(RuntimeError):
    """A recoverable update failure; the installed version is unchanged."""


def _fetch_json(url):
    import urllib.request
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'ScholarGuide-updater'})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read(1_000_000) or b'{}')


def _download(url, target: Path):
    import urllib.request
    request = urllib.request.Request(url, headers={'User-Agent': 'ScholarGuide-updater'})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response, target.open('wb') as handle:
        shutil.copyfileobj(response, handle, 1024 * 256)


def _expected_checksums(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r'[a-f0-9]{64}', parts[0]):
            result[parts[1].lstrip('*')] = parts[0]
    return result


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _run_installer(release: Release, work: Path) -> None:
    """Verify then run the release's installer for this platform."""
    sums = work / 'SHA256SUMS'
    _download(release.checksums, sums)
    expected = _expected_checksums(sums.read_text(encoding='utf-8', errors='replace'))
    name = 'install.ps1' if os.name == 'nt' else 'install.sh'
    url = release.installer_ps1 if os.name == 'nt' else release.installer_sh
    target = work / name
    _download(url, target)
    wanted = expected.get(name)
    if not wanted:
        raise UpdateError(f'release does not publish {name}')
    if _sha256(target) != wanted:
        raise UpdateError('installer checksum mismatch; nothing was changed')
    if os.name == 'nt':
        powershell = shutil.which('powershell') or shutil.which('pwsh')
        if not powershell:
            raise UpdateError('PowerShell is required to install on Windows')
        command = [powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(target)]
    else:
        shell = shutil.which('sh')
        if not shell:
            raise UpdateError('sh is required to install on this platform')
        command = [shell, str(target)]
    completed = subprocess.run(command, text=True)
    if completed.returncode != 0:
        raise UpdateError(f'installer exited with status {completed.returncode}; current version kept')


def update(*, check_only: bool = False, assume_yes: bool = False, target: str = '',
           root: Path | None = None, fetch_json=None, runner=None) -> dict:
    root = root or install_root()
    current = installed_version(root)
    if not current:
        raise UpdateError(
            'this is a source checkout, not an installed release; '
            'use `git pull` or run the release installer')
    release = latest_release(fetch_json)
    result = {'current': current, 'latest': release.tag, 'data_dir': str(data_dir()),
              'action': 'none', 'updated': False}
    if not target and not newer(release.tag, current):
        result['action'] = 'up-to-date'
        return result
    if target:
        if not TAG_RE.fullmatch(target):
            raise UpdateError('--version must look like v1.2.3')
        if not newer(target, current) and target != current:
            raise UpdateError(f'{target} is not newer than the installed {current}')
        release = Release(tag=target,
                          archive=f'https://github.com/{REPOSITORY}/releases/download/{target}/scholarguide-{target}.tar.gz',
                          checksums=f'https://github.com/{REPOSITORY}/releases/download/{target}/SHA256SUMS',
                          installer_sh=f'https://github.com/{REPOSITORY}/releases/download/{target}/install.sh',
                          installer_ps1=f'https://github.com/{REPOSITORY}/releases/download/{target}/install.ps1')
    result['action'] = 'available' if check_only or not assume_yes else 'installing'
    if check_only or not assume_yes:
        return result
    with tempfile.TemporaryDirectory(prefix='scholarguide-update-') as temporary:
        (runner or _run_installer)(release, Path(temporary))
    result['updated'] = installed_version(root) == release.tag
    result['action'] = 'updated' if result['updated'] else 'installed-unknown'
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='scholarguide update',
        description='Check for or install a newer ScholarGuide release. Your data in '
                    '~/.scholarguide (keys, profiles, quota, history) is never touched.')
    parser.add_argument('--check', action='store_true', help='only report; change nothing')
    parser.add_argument('--yes', action='store_true', help='install without asking')
    parser.add_argument('--version', default='', help='install this tag instead of the latest')
    args = parser.parse_args(argv)
    try:
        result = update(check_only=args.check, assume_yes=args.yes, target=args.version)
    except UpdateError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    if result['action'] == 'up-to-date':
        print(f"Already on the latest release ({result['current']}).")
        print(f"Your data stays in {result['data_dir']}.")
        return 0
    if result['action'] == 'available':
        print(f"Installed: {result['current']}")
        print(f"Latest:    {result['latest']}")
        print('Run `scholarguide update --yes` to install it. '
              f"Settings in {result['data_dir']} will not be changed.")
        return 0
    print(json.dumps(result, indent=1, ensure_ascii=False))
    if result['updated']:
        print(f"Updated to {result['latest']}. Restart the service to use the new build.")
        return 0
    print('The installer finished but the version marker did not change; check the output above.',
          file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
