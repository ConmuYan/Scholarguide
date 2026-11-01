"""Run a small, offline engineering baseline with a fixed source fixture."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name('cases.json')
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_case(case: dict) -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromName(case['test'])
    buffer = io.StringIO()
    started = time.perf_counter()
    result = unittest.TextTestRunner(stream=buffer, verbosity=1).run(suite)
    return {
        'id': case['id'], 'split': case['split'], 'dimension': case['dimension'],
        'test': case['test'], 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped),
        'elapsed_seconds': round(time.perf_counter() - started, 4),
        'passed': result.wasSuccessful() and not result.skipped and result.testsRun > 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=('dev', 'holdout', 'all'), default='dev')
    parser.add_argument('--output', type=Path, required=True,
                        help='new output path; existing evidence is never overwritten')
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    source = manifest['source_snapshot']
    for name in ('fixture', 'active_fixture'):
        path = ROOT / source[name]
        if digest(path) != source['sha256']:
            raise SystemExit(f'fixture hash mismatch: {source[name]}')
    if args.output.exists():
        raise SystemExit('output already exists; choose a new evidence path')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    selected = [case for case in manifest['cases']
                if args.split == 'all' or case['split'] == args.split]
    rows = [run_case(case) for case in selected]
    report = {
        'suite': 'offline_engineering_baseline_v1', 'split': args.split,
        'manifest_sha256': digest(MANIFEST), 'fixture_sha256': source['sha256'],
        'python': sys.version.split()[0], 'source': 'fixed offline fixture or test double',
        'model': 'offline fixture or test double',
        'usage': None, 'estimated_cost': None,
        'cost_note': 'Not measured; this suite is not a live billing measurement.',
        'results': rows, 'passed': bool(rows) and all(row['passed'] for row in rows),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2)
        handle.write('\n')
    for row in rows:
        print(f"{'PASS' if row['passed'] else 'FAIL'} {row['id']} ({row['elapsed_seconds']}s)")
    print(f"Evidence: {args.output}")
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
