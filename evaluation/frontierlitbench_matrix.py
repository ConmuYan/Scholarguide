"""Run the fixed ScholarGuide task matrix, preserving each attempt."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

from frontierlitbench_cli import FORMAT, ROOT, prompt_for, read_json, save_new
from frontierlitbench_score import TASKS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--only', nargs='+', help='Start only the listed planned directories, e.g. FB-01-1.')
    args = parser.parse_args()
    root = args.out.resolve()
    tasks = [(task, repeat) for repeat in (1, 2, 3) for task in sorted(TASKS)]
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    plan = {'system': 'ScholarGuide', 'model': 'deepseek-flash', 'method': 'A',
            'revision': revision,
            'source': 'multi', 'cache': False, 'timeout_seconds': 300,
            'max_output_tokens': 8000, 'runs': [
                {'task': t, 'repeat': n, 'directory': f'{t}-{n}',
                 'prompt': prompt_for(t) + FORMAT} for t, n in tasks]}
    path = root / 'matrix.json'
    if path.exists():
        if read_json(path) != plan:
            parser.error('Existing matrix differs from the current plan; use a new output directory.')
    else:
        save_new(path, plan)
    if args.prepare_only:
        print(f'Prepared {len(tasks)} runs in {root}', flush=True)
        return 0
    if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=ROOT).returncode:
        parser.error('Commit tracked changes before starting a comparison run.')
    env = {**os.environ, 'SG_MAX_OUTPUT_TOKENS': '8000', 'SG_MODEL': plan['model']}

    def run(row):
        folder = root / row['directory']
        # Starting a second process against the same matrix never repeats
        # a paid attempt or replaces its evidence.
        try:
            folder.mkdir()
        except FileExistsError:
            print(f'{row["directory"]}: existing attempt', flush=True)
            return
        command = [sys.executable, 'app.py', 'run', '--request', row['prompt'],
                   '--method', 'A', '--source', 'multi', '--no-cache',
                   '--timeout', '300', '--out', str(folder / 'run')]
        print(f'{row["directory"]}: starting', flush=True)
        with (folder / 'process.log').open('wb') as log:
            try:
                process = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                         stderr=subprocess.STDOUT, timeout=360)
                outcome = {'exit_code': process.returncode, 'process_status': 'exited'}
            except subprocess.TimeoutExpired:
                outcome = {'exit_code': None, 'process_status': 'timeout'}
        result = folder / 'run' / 'result.json'
        if result.is_file():
            data = read_json(result)
            outcome.update(run_status=data.get('status'), stop_reason=data.get('stop_reason'),
                           papers=len(data.get('papers', [])), run_id=data.get('run_id'))
        save_new(folder / 'attempt.json', outcome)
        print(f'{row["directory"]}: {outcome.get("run_status", outcome["process_status"])}', flush=True)

    selected = set(args.only or (r['directory'] for r in plan['runs']))
    if not selected <= {r['directory'] for r in plan['runs']}:
        parser.error('Unknown planned directory in --only.')
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run, (r for r in plan['runs'] if r['directory'] in selected)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
