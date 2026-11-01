"""Opt-in, offline engineering pilot; no multi-agent comparison is claimed."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import platform
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name('paired_cases.json')
MANIFEST_SHA256 = 'fa27de5af972b58d7f0fa2e63abb0544ad9b4e3574f9d0b92651288d1b5d5bd3'
sys.path.insert(0, str(ROOT))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()


def validate_manifest(manifest):
    if digest(manifest) != MANIFEST_CONTENT_SHA256:
        raise ValueError('paired manifest content mismatch')


def load_manifest():
    raw = MANIFEST.read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise ValueError('paired manifest file mismatch')
    manifest = json.loads(raw)
    validate_manifest(manifest)
    return manifest


MANIFEST_CONTENT_SHA256 = '6d342c951cc4b956e9d303a0482a75414c2ae130d916f003f3c93b97f166580c'


def check_evidence(selected, candidates, fixture):
    by_id = {p['id']: p['abstract'] for p in fixture}
    def consistent(rows):
        ids = [p.get('id') for p in rows]
        return (len(ids) == len(set(ids)) and
                all(p.get('id') in by_id and p.get('abstract') == by_id[p['id']] for p in rows))
    valid = 0
    for paper in selected:
        quote = ' '.join(str(paper.get('evidence_quote') or '').split())
        abstract = ' '.join(str(by_id.get(paper.get('id'), '')).split())
        if paper.get('evidence_verified') is True and len(quote) >= 12 and quote in abstract:
            valid += 1
    return {'checked': len(selected), 'valid': valid,
            'fixture_consistent': consistent(selected) and consistent(candidates),
            'passed': bool(selected) and valid == len(selected) and consistent(selected) and consistent(candidates)}


def execution_ok(status, reason, calls, limits):
    return (status == 'complete' and reason == 'requirements_met' and
            calls.count('search') <= limits['max_search_calls'] and
            sum(c != 'search' for c in calls) <= limits['max_model_calls'])


def source_hashes():
    files = list(ROOT.glob('*.py')) + [Path(__file__).resolve(), ROOT / 'scripts/verify_delivery.py',
                                      ROOT / 'scripts/offline_test_site/sitecustomize.py']
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(set(files))}


def references(manifest):
    return {key: {'reference': value, 'executed': False} for key, value in manifest['references'].items()}


def validate_output(path):
    if path.exists() or path.is_symlink():
        raise ValueError('output already exists')
    path = path.resolve()
    verification = ROOT / 'runs/verification'
    resolved_verification = verification.resolve()
    allowed = [Path(tempfile.gettempdir()).resolve()]
    if resolved_verification.is_relative_to(ROOT.resolve()):
        allowed.append(resolved_verification)
    if not any(path != base and path.is_relative_to(base) for base in allowed):
        raise ValueError('output must be under runs/verification or the OS temporary directory')
    return path


def worker(case, manifest, tmp):
    # The real pilot uses a dedicated subprocess. Restore hooks for callers
    # exercising this function directly in an existing interpreter.
    import socket
    from unittest.mock import patch
    def deny(*args, **kwargs):
        raise OSError('offline pilot blocks network')
    with patch.object(socket.socket, 'connect', deny), \
         patch.object(socket.socket, 'connect_ex', deny), \
         patch.object(socket, 'create_connection', deny), \
         patch.object(socket, 'getaddrinfo', deny):
        return _worker(case, manifest, tmp)


def _worker(case, manifest, tmp):
    from common import Budget, Config, Paper, venue_matches, year_matches
    from demo import DemoLLM, DemoSource
    from workflow import run

    before = source_hashes()
    raw = (ROOT / manifest['fixture']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['fixture_sha256']:
        raise ValueError('demo fixture mismatch')
    fixture = json.loads(raw)['papers']
    if len({p['id'] for p in fixture}) != len(fixture):
        raise ValueError('duplicate fixture IDs')
    limits = manifest['budget']
    calls = []
    attempts = {'search': 0, 'model': 0, 'denied': 0}
    class Source(DemoSource):
        def __init__(self):
            self.scenario = 'satisfied'
            self.calls = 0
            self.papers = [Paper(**p) for p in fixture]

        def search(self, query, limit=10):
            calls.append('search')
            attempts['search'] += 1
            if attempts['search'] > limits['max_search_calls']:
                attempts['denied'] += 1
                raise RuntimeError('search allowance exceeded')
            return super().search(query, limit)
    class Model(DemoLLM):
        def generate(self, stage, system, data, schema, validate):
            calls.append(stage)
            attempts['model'] += 1
            if stage == 'search' or stage not in manifest['tool_allowlist'] or attempts['model'] > limits['max_model_calls']:
                attempts['denied'] += 1
                raise RuntimeError('model stage denied or allowance exceeded')
            return super().generate(stage, system, data, schema, validate)
    source, llm = Source(), Model()
    config = Config(model='offline-demo', base_url='http://127.0.0.1:1', cache=False,
                    cache_dir=Path(tmp), task_timeout=limits['seconds'],
                    max_output_tokens=limits['max_output_tokens'])
    started = time.perf_counter()
    try:
        result = run('Synthetic offline fixture', config, 'S', request_data=case['request'],
                     source=source, llm=llm, budget=Budget(limits['seconds']))
    except Exception as exc:
        return {'status': 'error', 'error_type': type(exc).__name__,
                'checks': {'passed': False, 'tool_allowlist': all(c in manifest['tool_allowlist'] for c in calls)},
                'tool_calls': {'sequence': calls, 'count': len(calls), 'attempts': attempts},
                'elapsed_seconds': time.perf_counter() - started, 'usage': None, 'cost': None,
                'test_references': references(manifest), 'source_sha256_before': before,
                'source_sha256_after': source_hashes(), 'token_limit_status': 'unmeasured'}
    elapsed = time.perf_counter() - started
    selected = result['papers']
    request = case['request']
    count_ok = len(selected) == request['target_count']
    quota_ok = sum(p.get('category_status') == 'yes' for p in selected) >= request['minimum_category_count']
    filters_ok = all(venue_matches(p.get('venue', ''), request.get('venue', '')) and
                     year_matches(p.get('year'), request.get('year_from'), request.get('year_to')) for p in selected)
    evidence = check_evidence(selected, result['candidates'], fixture)
    permission = all(call in manifest['tool_allowlist'] for call in calls)
    execution = (execution_ok(result.get('status'), result.get('stop_reason'), calls, limits) and
                 attempts['search'] <= limits['max_search_calls'] and
                 attempts['model'] <= limits['max_model_calls'] and attempts['denied'] == 0)
    after = source_hashes()
    unchanged = before == after
    return {'status': 'completed', 'workflow_status': result.get('status'),
            'stop_reason': result.get('stop_reason'), 'source_sha256_before': before,
            'source_sha256_after': after, 'token_limit_status': 'unmeasured',
            'checks': {'count': count_ok, 'category_quota': quota_ok, 'filters': filters_ok,
                       'evidence': evidence, 'tool_allowlist': permission,
                       'execution': execution, 'source_unchanged': unchanged,
                       'passed': count_ok and quota_ok and filters_ok and evidence['passed'] and permission and execution and unchanged},
            'selected_count': len(selected), 'tool_calls': {'sequence': calls, 'count': len(calls), 'attempts': attempts},
            'elapsed_seconds': elapsed, 'usage': {'input_tokens': None, 'output_tokens': None},
            'cost': None, 'test_references': references(manifest)}


def run_case(case, manifest):
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='sg-paired-') as directory:
        from scripts.verify_delivery import isolated_environment
        from unittest.mock import patch
        inherited = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'PATHEXT', 'COMSPEC') if key in os.environ}
        with patch('scripts.verify_delivery._installed_chromedriver', return_value=None):
            env = isolated_environment(Path(directory), inherited)
        env.update({'HOME': directory, 'USERPROFILE': directory, 'TEMP': directory, 'TMP': directory,
                    'PYTHONDONTWRITEBYTECODE': '1', 'SG_PAIRED_WORKER': '1'})
        try:
            child = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker', case['id']],
                                   env=env, cwd=directory, capture_output=True, text=True,
                                   timeout=manifest['budget']['seconds'] + 15)
            if child.returncode:
                raise RuntimeError('isolated worker failed')
            result = json.loads(child.stdout)
        except (subprocess.TimeoutExpired, ValueError, OSError, RuntimeError) as exc:
            result = {'status': 'error', 'error_type': type(exc).__name__,
                      'elapsed_seconds': time.perf_counter() - started, 'usage': None, 'cost': None,
                      'checks': {'passed': False}, 'token_limit_status': 'unmeasured',
                      'test_references': references(manifest)}
    return {'id': case['id'], 'split': case['split'], 'input_sha256': digest({'case': case, 'budget': manifest['budget'],
            'tool_allowlist': manifest['tool_allowlist'], 'fixture_sha256': manifest['fixture_sha256']}),
            'arms': {'single': result, 'multi': {'status': 'unavailable', 'reason': 'no verified multi-agent implementation',
                                                   'checks': None, 'elapsed_seconds': None, 'usage': None, 'cost': None}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot', action='store_true')
    parser.add_argument('--split', choices=('dev', 'holdout', 'all'), default='dev')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    args = parser.parse_args()
    manifest = load_manifest()
    if args.worker:
        if os.environ.get('SG_PAIRED_WORKER') != '1' or os.environ.get('SG_OFFLINE_NETWORK_GUARD') != '1':
            raise SystemExit('worker requires isolated environment')
        case = next(c for c in manifest['cases'] if c['id'] == args.worker)
        print(json.dumps(worker(case, manifest, os.environ['TEMP'])))
        return 0
    if not args.pilot or args.output is None:
        raise SystemExit('pilot disabled; specify --pilot and a new --output path')
    try:
        output = validate_output(args.output)
    except ValueError as exc:
        raise SystemExit(str(exc))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Resolve again after directory creation; this is not a concurrent-writer sandbox.
    if validate_output(output) != output:
        raise SystemExit('output path changed')
    try:
        handle = output.open('x', encoding='utf-8')
    except FileExistsError:
        raise SystemExit('output already exists')
    with handle:
        before = source_hashes()
        cases = [c for c in manifest['cases'] if args.split == 'all' or c['split'] == args.split]
        rows = [run_case(c, manifest) for c in cases]
        after = source_hashes()
        if before != after:
            for row in rows:
                row['arms']['single']['checks']['passed'] = False
                row['arms']['single']['checks']['parent_source_unchanged'] = False
        packages = {}
        for dist in importlib.metadata.distributions():
            name = dist.metadata.get('Name')
            if name:
                packages[name] = dist.version
        report = {'suite': 'paired_offline_engineering_v1', 'split': args.split,
                  'synthetic': False, 'synthetic_tasks': True, 'paper_fixture_kind': 'bundled_real_metadata',
                  'python_version': sys.version, 'platform': platform.platform(),
                  'dependency_versions': packages, 'source_sha256_before': before,
                  'source_sha256_after': after, 'source_unchanged': before == after,
                  'manifest_sha256': MANIFEST_SHA256, 'fixture_sha256': manifest['fixture_sha256'],
                  'budget': manifest['budget'], 'tool_allowlist': manifest['tool_allowlist'],
                  'denominator': {'tasks': len(cases), 'planned_arms': len(cases) * 2,
                                  'single_completed': sum(r['arms']['single']['status'] == 'completed' for r in rows),
                                  'single_passed': sum(r['arms']['single']['checks']['passed'] for r in rows),
                                  'multi_unavailable': len(rows)},
                  'results': rows, 'comparison_passed': False}
        json.dump(report, handle, indent=2)
        handle.write('\n')
    print('Pilot evidence written; comparison unavailable.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
