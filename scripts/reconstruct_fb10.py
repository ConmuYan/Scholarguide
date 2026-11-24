"""Reconstruct the original FB-1.0 snapshots from Git history, offline.

Usage: python scripts/reconstruct_fb10.py --out /path/to/new-directory
The original manifest self-reference is documented but cannot be verified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TRACKS = {'agents': 'e81c2be0c15d2f40a13547c0244ddeec132fa6b3',
          'world_models': 'a2a81ce0'}


def git_blob(commit: str, path: str) -> bytes:
    return subprocess.check_output(['git', 'show', f'{commit}:{path}'], cwd=ROOT)


def reconstruct(out: Path) -> dict:
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = {}
    for track, commit in TRACKS.items():
        manifest_path = f'frontierlitbench/manifests/corpus_sha256_{track}.json'
        manifest_bytes = git_blob(commit, manifest_path)
        manifest = json.loads(manifest_bytes)
        directory = out / track
        directory.mkdir()
        (directory / 'historical-manifest.json').write_bytes(manifest_bytes)
        rows = []
        for name, expected in manifest['files'].items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts or '\\' in name:
                raise ValueError('unsafe historical manifest path')
            if name == f'manifests/corpus_sha256_{track}.json':
                rows.append({'path': name, 'status': 'self-reference excluded'})
                continue
            blob = git_blob(commit, 'frontierlitbench/' + name)
            lf = blob.replace(b'\r\n', b'\n')
            candidates = [('git-blob', blob), ('CRLF', lf.replace(b'\n', b'\r\n'))]
            selected = next(((form, content) for form, content in candidates
                             if hashlib.sha256(content).hexdigest() == expected), None)
            if selected is None:
                raise ValueError(f'no historical bytes match: {track}/{name}')
            target = directory.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(selected[1])
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise OSError(f'historical write failed: {track}/{name}')
            rows.append({'path': name, 'status': 'verified', 'form': selected[0]})
        resolved = subprocess.check_output(['git', 'rev-parse', commit], cwd=ROOT, text=True).strip()
        report[track] = {'source_commit': resolved, 'rows': rows}
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='New output directory')
    args = parser.parse_args()
    for track, record in reconstruct(args.out).items():
        count = sum(row['status'] == 'verified' for row in record['rows'])
        print(f'{track}: {count} files verified from {record["source_commit"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
