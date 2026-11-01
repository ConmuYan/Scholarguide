"""Opt-in synthetic engineering comparison, not a research or multi-agent evaluation."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import Budget, BudgetExceeded, Config
from model_identity import research_model_identity
from specialist_pilot import _contains_key, run_pilot
from tools import CostMeter

FIXTURE = Path(__file__).with_name('synthetic_specialist_cases.json')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def source_hashes():
    files = [Path(__file__), FIXTURE, ROOT / 'specialist_pilot.py',
             ROOT / 'model_identity.py', ROOT / 'scripts' / 'verify_delivery.py',
             ROOT / 'scripts' / 'offline_test_site' / 'sitecustomize.py',
             ROOT / 'research_specialist.py',
             ROOT / 'common.py', ROOT / 'tools.py', ROOT / 'llm.py', ROOT / 'network.py',
             ROOT / 'model_policy.py', ROOT / 'tests/test_specialist_pilot.py',
             ROOT / 'tests/test_specialist_paired.py']
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def evidence_valid(case):
    papers = {p['id']: p['abstract'] for p in case['snapshot']['papers']}
    evidence = case['snapshot']['evidence']
    if len({e['id'] for e in evidence}) != len(evidence):
        return False
    by_id = {e['id']: e for e in evidence}
    for claim in case['claims']:
        if not claim['evidence_ids']:
            return False
        for identity in claim['evidence_ids']:
            row = by_id.get(identity)
            if not row:
                return False
            quote = row.get('quote')
            abstract = papers.get(row.get('paper_id'))
            if not isinstance(quote, str) or not quote.strip() or not isinstance(abstract, str) or quote not in abstract:
                return False
    return bool(case['claims'])


class _Response:
    status = 200
    headers = {}

    def __init__(self, output):
        self.body = json.dumps({'choices': [{'message': {'content': json.dumps(output)}}],
                                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.body


def run_arm(case, *, pilot=False, enabled=False, seconds=30, max_calls=1,
            authorized=True, cancelled=False, current=None, response=None, main_fail=False):
    """Run an isolated in-process arm. Test hooks are not exposed by the CLI."""
    started = time.perf_counter()
    case = deepcopy(case)
    input_hash = digest(case)
    decisions = {claim['claim_id']: 'retain' for claim in case['claims']}
    budget, meter = Budget(seconds), CostMeter()
    transport_calls = 0
    def guard():
        if budget.llm_calls >= max_calls:
            raise BudgetExceeded('request allowance exhausted')
    budget.request_guard = guard
    if cancelled:
        budget.cancel_token.request()
    def transport(req, timeout):
        nonlocal transport_calls
        transport_calls += 1
        output = response if response is not None else {'verdicts': [
            {'claim_id': c['claim_id'], 'evidence_ids': c['evidence_ids'], 'verdict': case['verdict']}
            for c in case['claims']]}
        return _Response(output)
    def deny(*args, **kwargs):
        raise OSError('network disabled')
    constraints = evidence = False
    main_executed = False
    applied = False
    applied_counts = {'retain': 0, 'qualify': 0, 'drop': 0}
    result = None
    failure = None
    try:
        budget.check_cancelled()
        budget.remaining()
        main_executed = True
        if main_fail:
            raise RuntimeError('synthetic main failure')
        constraints = (len(case['claims']) == case['task']['required_claims'] and bool(case['claims']))
        evidence = evidence_valid(case)
        if pilot:
            config = Config(model='offline-model', base_url='http://127.0.0.1:1', api_key='', cache=False)
            identity = research_model_identity(config)
            case['snapshot']['model_identity'] = identity
            initial = deepcopy(case['snapshot'])
            def get_current():
                state = deepcopy(current() if current is not None else case['snapshot'])
                state.setdefault('model_identity', identity)
                return state
            with patch.object(socket.socket, 'connect', deny), patch.object(socket.socket, 'connect_ex', deny), \
                 patch.object(socket, 'create_connection', deny), patch.object(socket, 'getaddrinfo', deny), \
                 patch('llm.urlopen', side_effect=transport):
                result = run_pilot(snapshot=case['snapshot'], claims=case['claims'], config=config,
                                   budget=budget, meter=meter,
                                   current=lambda: {'snapshot': get_current(), 'claims': case['claims']},
                                   enabled=enabled and authorized)
            budget.check_cancelled()
            budget.remaining()
            now = get_current()
            unchanged = (now == initial and research_model_identity(config) == identity and
                         not _contains_key([config.model, config.base_url], config.api_key))
            valid_findings = result.status == 'ok' and all(
                f['verdict'] in ('retain', 'qualify', 'drop') and
                any(f['claim_id'] == c['claim_id'] and f['evidence_ids'] and
                    set(f['evidence_ids']) <= set(c['evidence_ids']) for c in case['claims'])
                for f in result.findings) and len(result.findings) == len(case['claims'])
            applied = bool(authorized and constraints and evidence and unchanged and valid_findings)
            if applied:
                for finding in result.findings:
                    decisions[finding['claim_id']] = finding['verdict']
                    applied_counts[finding['verdict']] += 1
    except Exception:
        failure = 'unavailable'
    metrics = budget.metrics()
    status = ('completed' if main_executed and constraints and evidence and not failure and
              (not pilot or (result is not None and result.status == 'ok' and applied)) else 'unavailable')
    return {'status': status, 'input_sha256': input_hash, 'main_executed': main_executed,
            'output_counts': {name: sum(value == name for value in decisions.values())
                              for name in ('retain', 'qualify', 'drop')}, 'constraints_passed': constraints,
            'quote_substring_valid': evidence, 'authorized': authorized, 'memory_enabled': False,
            'critic_status': result.status if result else 'not_executed', 'verdict_applied': applied,
            'applied_counts': applied_counts,
            'transport_calls': transport_calls, 'usage': metrics,
            'critic_usage': dict(result.usage) if result else None, 'cost_units': meter.summary(),
            'monetary_cost': None, 'remote_billed': None, 'elapsed_seconds': time.perf_counter() - started,
            'exception_recovery': 'not_executed', 'security': 'unknown', 'failure': failure}


def run_case(case, *, enabled=False, **kwargs):
    single = run_arm(case, **kwargs)
    pilot = run_arm(case, pilot=True, enabled=enabled, **kwargs)
    return {'input_sha256': digest(case), 'arms': {'single': single, 'pilot': pilot},
            'comparison_available': (single['status'] == pilot['status'] == 'completed' and
                                     single['input_sha256'] == pilot['input_sha256'])}


def validate_output(path):
    if path.exists() or path.is_symlink():
        raise ValueError('output already exists')
    path = path.resolve()
    root = ROOT.resolve()
    allowed = [Path(tempfile.gettempdir()).resolve()]
    verification = (ROOT / 'runs' / 'verification').resolve()
    if verification.is_relative_to(root):
        allowed.append(verification)
    if not any(path != base and path.is_relative_to(base) for base in allowed):
        raise ValueError('output must be under runs/verification or the temporary directory')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if not args.pilot or args.output is None:
        parser.error('disabled by default; specify --pilot and a new --output')
    try:
        output = validate_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        if validate_output(output) != output:
            raise ValueError('output path changed')
        before = source_hashes()
        cases = json.loads(FIXTURE.read_text(encoding='utf-8'))['cases']
        rows = [run_case(case, enabled=True) for case in cases]
        after = source_hashes()
        report = {'suite': 'synthetic_specialist_paired_v1', 'synthetic': True,
                  'research_quality': 'not_measured', 'benefit': 'not_established',
                  'fixture_verdict': 'provided_directly', 'single_arm': 'no_model',
                  'comparison_available_meaning': 'workflow_executable_not_fairness_or_quality',
                  'semantic_support': 'not_measured',
                  'multiple_agents': False, 'budget_seconds_per_arm': 30, 'max_model_calls_per_arm': 1,
                  'source_sha256_before': before, 'source_sha256_after': after,
                  'source_unchanged': before == after, 'results': rows,
                  'comparison_available': before == after and bool(rows) and all(r['comparison_available'] for r in rows)}
        with output.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2)
            handle.write('\n')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit('comparison unavailable: ' + type(exc).__name__) from None
    print('Synthetic comparison recorded.' if report['comparison_available'] else 'Comparison unavailable.')
    return 0 if report['comparison_available'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
