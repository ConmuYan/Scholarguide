#!/usr/bin/env python3
"""Build a clean, allow-listed runtime source release for GitHub Releases.

Never archives the working tree wholesale: local runs, .env files, PDFs, tests,
agent configuration and historical Cloudflare demos are excluded by design.
"""
from __future__ import annotations
import argparse
import hashlib
import gzip
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    'activity.py', 'agent_runtime.py', 'agent_tools.py', 'api_schema.py', 'api_v1.py',
    'app.py', 'app_mcp.py', 'common.py', 'controller.py', 'costing.py', 'demo.py',
    'diagnosis.py', 'domain_skill_runtime.py', 'execution_events.py', 'intent.py',
    'llm.py', 'lune_quota.py', 'lune_quota_schedule.py', 'mcp_client.py', 'mcp_lune.py', 'mcp_lune_policy.py',
    'mcp_plugin.py', 'mcp_skills.py', 'memory_context.py', 'memory_store.py', 'memory_view.py',
    'model_identity.py', 'model_policy.py', 'native_tool_protocol.py', 'network.py',
    'preflight.py', 'prompts.py', 'providers.py', 'quantities.py', 'recovery.py',
    'report.py', 'research_brief.py', 'research_landscape.py', 'research_policy.py',
    'research_runtime.py', 'research_specialist.py', 'retrieval.py', 'reuse.py',
    'session.py', 'source.py', 'source_parallel.py', 'source_scheduler.py',
    'source_store.py', 'state.py', 'tool_governance.py', 'tools.py', 'update_cli.py', 'webapp.py', 'workflow.py',
)
FILES = ('.gitignore', 'requirements.txt', '.env.example', 'mcp_servers.json', 'mcp_skills_lune.json',
         'state_schema.sql', 'LICENSE', 'NOTICE.md', 'docs/API.md', 'docs/INSTALL.md', 'examples/papers.json')
REPO_RE = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
VERSION_RE = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z')


def valid_repo(repository: str) -> bool:
    if not REPO_RE.fullmatch(repository):
        return False
    owner, name = repository.split('/', 1)
    return owner not in ('.', '..') and name not in ('.', '..')


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return not path.is_absolute() and '..' not in path.parts and '\\' not in value


def build(version: str, repository: str, output: Path) -> list[Path]:
    if not VERSION_RE.fullmatch(version):
        raise ValueError('version must be a stable vMAJOR.MINOR.PATCH tag')
    if not valid_repo(repository):
        raise ValueError('repository must be OWNER/REPO')
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise FileExistsError('release output directory must be empty')
    top = f'scholarguide-{version}'
    with tempfile.TemporaryDirectory(prefix='scholarguide-release-') as temporary:
        stage = Path(temporary) / top
        stage.mkdir()
        for name in MODULES + FILES:
            source = ROOT / name
            if not source.is_file() or source.is_symlink():
                raise FileNotFoundError(f'missing or unsafe release input: {name}')
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        shutil.copyfile(ROOT / 'PUBLIC_README.md', stage / 'README.md')
        for public_doc in (stage / 'README.md', stage / 'docs' / 'INSTALL.md'):
            text = public_doc.read_text(encoding='utf-8')
            text = text.replace('OWNER/REPOSITORY', repository).replace('vX.Y.Z', version)
            public_doc.write_text(text, encoding='utf-8', newline='\n')
        vendor = stage / 'vendor'
        vendor.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'vendor' / 'gptr_arxiv.py', vendor / 'gptr_arxiv.py')
        shutil.copyfile(ROOT / 'vendor' / 'LICENSE-gpt-researcher', vendor / 'LICENSE-gpt-researcher')
        shutil.copytree(ROOT / 'web', stage / 'web', symlinks=False,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.map'))
        # Guard against accidentally adding local material to the payload later.
        forbidden = ('.env', '.git', '.pi', '.codex', 'runs', 'tests', 'evaluation',
                     'archive', 'cloudflare-worker', 'cloudflare-pages')
        entries = [p.relative_to(stage).as_posix() for p in stage.rglob('*')]
        for entry in entries:
            if not safe_relative(entry) or Path(entry).parts[0] in forbidden:
                raise ValueError(f'unsafe release path: {entry}')
            if Path(entry).suffix.lower() == '.pdf' or Path(entry).name.lower() in ('auth.json', 'providers.json'):
                raise ValueError(f'private/research file in release payload: {entry}')
        (stage / 'install').mkdir(exist_ok=True)
        for template, target in (('install.sh.in', 'install.sh'), ('install.ps1.in', 'install.ps1')):
            text = (ROOT / 'install' / template).read_text(encoding='utf-8')
            text = text.replace('@REPOSITORY@', repository).replace('@VERSION@', version)
            if '@REPOSITORY@' in text or '@VERSION@' in text:
                raise ValueError(f'unexpanded install template: {template}')
            (output / target).write_text(text, encoding='utf-8', newline='\n')
        archive = output / f'{top}.tar.gz'
        # Deterministic archive: same source + version yields the same asset hash.
        epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w', format=tarfile.PAX_FORMAT) as tar:
            for item in sorted(stage.rglob('*')):
                relative = item.relative_to(stage.parent).as_posix()
                if not safe_relative(relative) or item.is_symlink():
                    raise ValueError(f'unsafe archive member: {relative}')
                info = tar.gettarinfo(str(item), arcname=relative)
                info.mtime = epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                if info.isfile():
                    with item.open('rb') as stream:
                        tar.addfile(info, stream)
                else:
                    tar.addfile(info)
        with archive.open('wb') as stream:
            with gzip.GzipFile(fileobj=stream, mode='wb', mtime=epoch, compresslevel=9) as zipped:
                zipped.write(raw.getvalue())
    assets = [archive, output / 'install.sh', output / 'install.ps1']
    lines = [f'{digest(path)}  {path.name}' for path in assets]
    (output / 'SHA256SUMS').write_text('\n'.join(lines) + '\n', encoding='ascii', newline='\n')
    return assets + [output / 'SHA256SUMS']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True, help='Release tag, e.g. v0.1.0')
    parser.add_argument('--repository', required=True, help='Public GitHub owner/repository')
    parser.add_argument('--out', type=Path, required=True, help='New or empty asset directory')
    args = parser.parse_args()
    for path in build(args.version, args.repository, args.out):
        print(f'{path}  sha256={digest(path)}')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
