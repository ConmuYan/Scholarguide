#!/usr/bin/env python3
"""Export the current project as a clean public source repository tree.

The working directory also holds local secrets, run history, research PDFs and
personal agent configuration. This exporter copies only publishable source,
tests and documentation, then scans the result for credential-shaped strings.
It does not run git, contact GitHub or touch the working tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]

TOP_LEVEL_FILES = ('app.py', 'app_mcp.py', 'activity.py', 'agent_runtime.py', 'agent_tools.py',
                   'api_schema.py', 'api_v1.py', 'common.py', 'controller.py', 'costing.py',
                   'deletion_lifecycle.py', 'demo.py', 'diagnosis.py', 'domain_skill_runtime.py',
                   'execution_events.py', 'intent.py', 'llm.py', 'lune_quota.py', 'lune_quota_schedule.py', 'mcp_client.py',
                   'mcp_lune.py', 'mcp_lune_policy.py', 'mcp_plugin.py', 'mcp_skills.py',
                   'memory_context.py', 'memory_store.py', 'memory_view.py', 'model_identity.py',
                   'model_policy.py', 'native_tool_protocol.py', 'network.py', 'preflight.py',
                   'prompts.py', 'providers.py', 'quantities.py', 'recovery.py', 'report.py',
                   'research_brief.py', 'research_landscape.py', 'research_policy.py',
                   'research_runtime.py', 'research_specialist.py', 'retrieval.py', 'reuse.py',
                   'session.py', 'source.py', 'source_parallel.py', 'source_scheduler.py',
                   'source_store.py', 'specialist_pilot.py', 'state.py', 'tool_governance.py',
                   'tools.py', 'webapp.py', 'workflow.py',
                   'README.md', 'README.zh-CN.md', 'PUBLIC_README.md', 'UPSTREAM.md', 'VERIFICATION.md',
                   'LICENSE', 'NOTICE.md', '.gitattributes', 'requirements.txt', 'requirements-dev.txt',
                   '.gitignore', '.env.example', 'state_schema.sql', 'mcp_servers.json',
                   'mcp_skills_lune.json')
SOURCE_DIRS = ('web', 'tests', 'docs', 'scripts', 'examples', 'vendor', 'install', '.github', 'evaluation')
ALWAYS_EXCLUDED_DIRS = {
    '__pycache__', '.venv', '.venv-upstream', '.cache', 'runs', 'archive', 'node_modules',
    'cloudflare-worker', 'cloudflare-pages', '.pi', '.codex', '.git', '.idea', '.vscode',
    'dist', 'build', 'outputs',
}
EXCLUDED_PATH_PARTS = {('evaluation', 'results'), ('scripts', 'offline_test_site')}
EXCLUDED_SUFFIXES = ('.pdf', '.pyc', '.pyo', '.log', '.sqlite', '.sqlite3', '.sqlite3-wal',
                     '.sqlite3-shm', '.zip', '.tar', '.gz', '.png', '.jpg', '.jpeg', '.gif', '.webp')
EXCLUDED_NAMES = {'.env', 'NUL', 'auth.json', 'providers.json', 'sources.sqlite3',
                  '.DS_Store', 'Thumbs.db', 'core_lock.json'}
# Recorded live provider output is illustrative, not a fixture; it is not published.
EXCLUDED_EXAMPLE_FILES = {'live_result.json', 'live_report.md', 'verification_manifest.json'}
PUBLIC_DOCS = {
    'ADR-001-agent-runtime.md', 'AGENT_TRACE.md', 'API.md', 'ARCHITECTURE.md',
    'DESIGN_INVARIANTS.md', 'DEVELOPMENT.md', 'FAILURE_POLICY.md',
    'FRONTEND_DESIGN_NOTES_20261031.md', 'INSTALL.md', 'MCP_AND_SKILLS.md',
    'PRODUCT_CONTRACT_20261028.md', 'PROVIDER_PROFILES.md', 'PUBLISHING.md', 'RELEASE_PACKAGING.md',
    'SOURCES.md', 'UI.md',
}
PERSONAL_PATH = re.compile(r'(?:C:\\Users\\[A-Za-z0-9._-]+|/Users/[A-Za-z0-9._-]+/)')
SECRET_PATTERNS = (
    (re.compile(r'\bsk-[A-Za-z0-9]{20,}\b'), 'OpenAI-style secret key'),
    (re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}\b'), 'Anthropic-style secret key'),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{30,}\b'), 'Google API key'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), 'private key block'),
    (re.compile(r'(?i)\b(?:SG_API_KEY|DEEPSEEK_SEARCH_KEY|OPENALEX_API_KEY|S2_API_KEY|LUNE_API_KEY)'
                r'\s*=\s*(?!@pi:|your|<|\$|\*|example|$)[A-Za-z0-9_\-]{12,}'), 'literal credential assignment'),
    (PERSONAL_PATH, 'developer-specific home directory path'),
)


def posix(relative: Path) -> str:
    return relative.as_posix()


def included(relative: Path, all_docs: bool = False) -> bool:
    parts = relative.parts
    if any(part in ALWAYS_EXCLUDED_DIRS for part in parts):
        return False
    if any(tuple(parts[:len(prefix)]) == prefix for prefix in EXCLUDED_PATH_PARTS):
        return False
    if parts[-1] in EXCLUDED_NAMES:
        return False
    if len(parts) == 2 and parts[0] == 'examples' and parts[1] in EXCLUDED_EXAMPLE_FILES:
        return False
    if parts[0] == 'docs' and len(parts) > 1 and parts[1] == 'assets':
        # README screenshots and motion clips are part of the published docs.
        return parts[-1].lower().endswith(('.png', '.gif', '.svg', '.jpg', '.webp'))
    if parts[0] == 'docs' and not all_docs and relative.name not in PUBLIC_DOCS:
        return False
    if parts[-1].startswith('.env') and parts[-1] != '.env.example':
        return False
    if parts[-1].lower().endswith(EXCLUDED_SUFFIXES):
        return False
    return True


def candidates(all_docs: bool = False) -> list[Path]:
    found: list[Path] = []
    for name in TOP_LEVEL_FILES:
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            found.append(path)
    for directory in SOURCE_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob('*')):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(ROOT)
            # Third-party logo binaries are handled explicitly below.
            if included(relative, all_docs=all_docs):
                found.append(path)
    logo_dir = ROOT / 'web' / 'assets' / 'logos'
    if logo_dir.is_dir():
        for path in sorted(logo_dir.rglob('*')):
            if path.is_file() and path.suffix.lower() in ('.svg', '.png', '.md'):
                found.append(path)
    # The logo directory is also reachable through web/; keep one entry per file.
    unique: dict[str, Path] = {}
    for path in found:
        unique.setdefault(path.relative_to(ROOT).as_posix(), path)
    return [unique[key] for key in sorted(unique)]


def secret_findings(tree: Path) -> list[str]:
    findings = []
    for path in sorted(tree.rglob('*')):
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        if path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.woff', '.woff2'):
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='strict')
        except (UnicodeDecodeError, OSError):
            continue
        for pattern, label in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count('\n', 0, match.start()) + 1
                findings.append(f'{path.relative_to(tree)}:{line}: {label}')
    return findings


def export(output: Path, include_evaluation: bool = True, all_docs: bool = False) -> dict:
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise FileExistsError('export directory must be empty')
    copied = []
    for path in candidates(all_docs=all_docs):
        relative = path.relative_to(ROOT)
        if not include_evaluation and relative.parts[0] == 'evaluation':
            continue
        if 'install' in relative.parts and path.suffix == '.in':
            pass  # Installer templates are published source, deliberately included.
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied.append(posix(relative))
    manifest = {}
    for name in copied:
        data = (output / name).read_bytes()
        manifest[name] = hashlib.sha256(data).hexdigest()
    findings = secret_findings(output)
    report = {'files': len(copied), 'sha256': manifest, 'secret_findings': findings}
    (output / 'SOURCE_MANIFEST.json').write_text(
        json.dumps({'files': len(copied), 'sha256': manifest}, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8', newline='\n')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='New or empty export directory')
    parser.add_argument('--without-evaluation', action='store_true')
    parser.add_argument('--allow-findings', action='store_true',
                        help='Write the tree anyway; findings must be reviewed manually')
    parser.add_argument('--all-docs', action='store_true',
                        help='Publish every docs/*.md, including historical internal notes')
    args = parser.parse_args()
    report = export(args.out, include_evaluation=not args.without_evaluation,
                    all_docs=args.all_docs)
    print(f'exported {report["files"]} files to {args.out}')
    if report['secret_findings']:
        print('credential-shaped findings:')
        for line in report['secret_findings']:
            print('  ' + line)
        if not args.allow_findings:
            shutil.rmtree(args.out, ignore_errors=True)
            print('export removed; review the findings and rerun with --allow-findings only after auditing.')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
