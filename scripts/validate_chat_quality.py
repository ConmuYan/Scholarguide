"""Isolated live Chat acceptance: natural language, SQLite rehydration, exclusion.

Run serially with validate_research_quality.py against the SAME monetary ledger.
At most two user turns are sent. No structured request_data, private history,
profile mutation, deployment, expert gold or semantic-quality certification.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Config, GuideError
from costing import cost_breakdown
from model_identity import research_model_identity
from network import execution_context
from providers import ProviderStore
from recovery import JobLease
from report import save_run
from research_brief import validated_cached_brief
from research_policy import publication_matches, venue_identity
from session import ChatSession
from source import AcademicSource
from state import StateStore
from scripts.validate_research_quality import MonetaryGuard, GuardedLLM, atomic, checks, fingerprint

INITIAL_MESSAGE = '请推荐3篇ICLR 2025正式录用为Poster、关于视觉表征学习的论文，比较方法。'
EXPECTED = {'target_count': 3, 'venue': 'ICLR', 'year_from': 2025, 'year_to': 2025,
            'publication_type': 'conference', 'accepted_only': True,
            'presentation_types': ['poster']}
COUNTER_KEYS = ('llm_left', 'searches_left', 'candidates_left', 'phase', 'actions_left',
                'searches_left_phase', 'asks_left_phase', 'feedback_phases_left')


def source_fingerprint():
    result = fingerprint()
    result[Path(__file__).resolve().relative_to(ROOT).as_posix()] = hashlib.sha256(
        Path(__file__).read_bytes()).hexdigest()
    return result


def followup_message(paper):
    identity = str(paper['id'])[:160]
    return (f'请明确排除首轮第一篇论文，paper ID为“{identity}”，寻找替代论文并继续推荐3篇。'
            '保留原始要求：ICLR 2025、正式录用为Poster、视觉表征学习、比较方法；不要放宽这些约束。')


def task_checks(task):
    return {'target_count_retained': task.get('target_count') == 3,
            'venue_retained': venue_identity(task.get('venue')) == 'ICLR',
            'year_retained': task.get('year_from') == task.get('year_to') == 2025,
            'publication_type_retained': task.get('publication_type') == 'conference',
            'acceptance_required': task.get('accepted_only') is True,
            'poster_required': task.get('presentation_types') == ['poster'],
            'topic_present': bool(str(task.get('topic') or '').strip())}


def chat_checks(result, *, excluded_id=None):
    result_checks = checks(result)
    task, papers = result.get('request') or {}, result.get('papers') or []
    brief = result.get('research_brief') or {}
    result_checks.update(task_checks(task))
    result_checks.update({
        'requested_count_delivered': len(papers) == 3,
        'original_publication_constraints': bool(papers) and all(
            publication_matches(paper, EXPECTED) for paper in papers),
        'methods_with_evidence': sum(bool((note.get('method') or {}).get('evidence'))
                                     for note in brief.get('paper_notes') or []),
        'cross_paper_comparison_themes': len(brief.get('themes') or []),
        'awaiting_user': result.get('status') == 'awaiting_user',
        'question': result.get('question') or '',
    })
    if excluded_id is not None:
        result_checks.update(exclusion_recorded=excluded_id in (task.get('excluded_paper_ids') or []),
                             excluded_paper_absent=all(paper.get('id') != excluded_id for paper in papers))
    return result_checks


class ObservedStore(StateStore):
    """Observe committed protocol events without changing the production runner."""
    def __init__(self, path):
        self.observer = None
        super().__init__(path)

    def append_event(self, session_id, action, payload=None, revision=None):
        seq = super().append_event(session_id, action, payload, revision)
        if self.observer is not None:
            self.observer({'seq': seq, 'action': action, 'revision': revision,
                           'payload': deepcopy(dict(payload or {}))})
        return seq


def bind_chat(config, store, guard, case, *, session_id=None,
              source_factory=AcademicSource, llm_factory=GuardedLLM):
    # ChatSession owns the runtime Budget. Both injected clients are rebound to
    # that exact object after construction, so admission/cancellation counters
    # and the persistent phase request guard cannot diverge.
    from common import Budget
    initial_budget = Budget(config.task_timeout)
    source = source_factory(config, initial_budget)
    llm = llm_factory(config, initial_budget, guard, case)
    chat = ChatSession(config, store, session_id=session_id, source=source, llm=llm)
    chat._qa_client_constructor_calls = initial_budget.llm_calls + initial_budget.search_http_calls
    source.budget = llm.budget = chat.budget
    return chat


def _run_turn(chat, guard, directory, message, index, *, first=False, excluded_id=None):
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    event_start = len(chat.store.events(chat.session_id))
    observed = []
    chat.store.observer = lambda event: observed.append({
        'elapsed_seconds': round(time.monotonic() - started, 3), **event})
    result = None
    failure = None
    try:
        with execution_context(cancelled=lambda: chat.budget.cancel_token.requested):
            # This is the natural-language updater/action loop, deliberately
            # without the structured request_data shortcut used in Search QA.
            result = chat.start(message=message) if first else chat.send(message)
        result['turn'] = index
        save_run(result, directory)
    except Exception as exc:
        failure = {'type': type(exc).__name__,
                   'message': str(exc) if isinstance(exc, GuideError) else 'Unexpected validator/runtime exception.'}
        atomic(directory / 'failure.json', failure)
    finally:
        chat.store.observer = None
        atomic(directory / 'events.json', chat.store.events(chat.session_id)[event_start:])
        atomic(directory / 'timed-events.json', observed)
        atomic(directory / 'model-events.json', list(getattr(chat.llm, 'events', [])))
    row = {'turn': index, 'message': message, 'structured_request_data_supplied': False,
           'elapsed_seconds': round(time.monotonic() - started, 3),
           'status': result.get('status') if result else 'exception',
           'stop_reason': result.get('stop_reason') if result else None,
           'failure': failure, 'checks': chat_checks(result, excluded_id=excluded_id) if result else None,
           'source_health': chat.source.health_snapshot() if hasattr(chat.source, 'health_snapshot') else {},
           'limits': chat.limits(), 'model_stages': [event.get('stage') for event in getattr(chat.llm, 'events', [])],
           'protected_budget_consumed_cny': guard.used()}
    if result is not None:
        row.update(metrics=result.get('metrics'),
                   cost=cost_breakdown(chat.config, result.get('metrics'), result.get('trace')))
    atomic(directory / 'checks.json', row)
    return result, row


def inspect_restored(chat, guard, saved_path, previous, previous_limits, *,
                     entries_before_rehydration=None, cost_before_rehydration=None):
    """Passive read only: never call turn(), send(), start() or attach_brief()."""
    before_entries = len(guard.data['entries']) if entries_before_rehydration is None else entries_before_rehydration
    before_usage = guard.used() if cost_before_rehydration is None else cost_before_rehydration
    file_before = saved_path.read_bytes()
    saved = json.loads(file_before)
    selected = chat.select()
    task = chat.task()
    cached = None
    for event in reversed(chat.store.events(chat.session_id)):
        if event['action'] == 'research_brief':
            cached = validated_cached_brief(event['payload'].get('brief'), task, selected,
                                             research_model_identity(chat.config))
            if cached is not None:
                break
    return {
        'scope': 'Closed SQLite connection and reconstructed ChatSession in this process; not a hard-kill test.',
        'session_id': chat.session_id,
        'saved_result_sha256': hashlib.sha256(file_before).hexdigest(),
        'saved_result_unchanged': saved_path.read_bytes() == file_before,
        'saved_result_equals_previous': saved == previous,
        'task_restored': task == previous.get('request'),
        'selected_ids_restored': [paper['id'] for paper in selected] == [paper['id'] for paper in previous.get('papers') or []],
        'limits_counters_restored': all(chat.limits().get(key) == previous_limits.get(key) for key in COUNTER_KEYS),
        'saved_brief_status': (saved.get('research_brief') or {}).get('status'),
        'brief_cache_valid': cached is not None,
        'brief_cache_read_triggered_model_call': False,
        'external_calls_during_read': (chat.budget.llm_calls + chat.budget.search_http_calls
                                        + chat._qa_client_constructor_calls),
        'ledger_entries_unchanged': len(guard.data['entries']) == before_entries,
        'protected_cost_unchanged': guard.used() == before_usage,
        'request': task,
    }


def run_validation(config, output, guard, *, source_factory=AcademicSource,
                   llm_factory=GuardedLLM):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise GuideError('Use an empty output directory; validation evidence cannot be overwritten.')
    state_path = output / 'state.sqlite3'
    manifest = {'started_at': datetime.now(timezone.utc).isoformat(), 'config': config.public(),
                'source_sha256': source_fingerprint(), 'initial_message': INITIAL_MESSAGE,
                'expected_constraints': EXPECTED, 'maximum_user_turns': 2,
                'structured_request_data_supplied': False, 'turns': [],
                'mode': 'Live natural-language Chat product QA; no expert gold or semantic-quality certification.',
                'admission_limit_cny': guard.limit, 'budget_ledger': str(guard.path.resolve()),
                'pricing_admission_basis': guard.data.get('official_price_verification') or guard.data.get('pricing_basis'),
                'state_path': str(state_path.resolve())}
    atomic(output / 'manifest.json', manifest)
    store = ObservedStore(state_path)
    try:
        chat = bind_chat(config, store, guard, 'chat-turn-1', source_factory=source_factory,
                         llm_factory=llm_factory)
        manifest['session_id'] = chat.session_id
        first, row = _run_turn(chat, guard, output / 'turn-1', INITIAL_MESSAGE, 1, first=True)
        manifest['turns'].append(row)
        atomic(output / 'manifest.json', manifest)
        first_limits = chat.limits()
        session_id = chat.session_id
        entries_before_rehydration, cost_before_rehydration = len(guard.data['entries']), guard.used()
        store.close()
        store = ObservedStore(state_path)
        chat = bind_chat(config, store, guard, 'chat-turn-2', session_id=session_id,
                         source_factory=source_factory, llm_factory=llm_factory)
        if first is not None:
            restoration = inspect_restored(chat, guard, output / 'turn-1' / 'result.json', first, first_limits,
                entries_before_rehydration=entries_before_rehydration,
                cost_before_rehydration=cost_before_rehydration)
            atomic(output / 'recovery.json', restoration)
            manifest['recovery'] = restoration
        else:
            manifest['recovery'] = {'scope': 'Reconstructed SQLite session after first-turn exception.',
                                    'session_id': session_id, 'saved_result_available': False,
                                    'external_calls_during_read': chat.budget.llm_calls + chat.budget.search_http_calls}
            atomic(output / 'recovery.json', manifest['recovery'])
        atomic(output / 'manifest.json', manifest)
        if first is None or not first.get('papers'):
            manifest['followup_skipped_reason'] = 'No first selected paper exists to exclude; no fabricated follow-up target.'
        else:
            excluded_id = first['papers'][0]['id']
            manifest['excluded_id'] = excluded_id
            message = followup_message(first['papers'][0])
            second, row = _run_turn(chat, guard, output / 'turn-2', message, 2, excluded_id=excluded_id)
            row['same_session'] = second is not None and second.get('session_id') == first.get('session_id')
            row['revision_advanced'] = second is not None and second.get('revision', 0) > first.get('revision', 0)
            manifest['turns'].append(row)
            atomic(output / 'turn-2' / 'checks.json', row)
    finally:
        store.close()
    manifest['source_changed_during_run'] = [name for name, digest in manifest['source_sha256'].items()
        if not (ROOT / name).exists() or hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
    manifest['protected_budget_consumed_cny'] = guard.used()
    manifest['scope_completed'] = {'first_turn_recorded': bool(manifest['turns']),
        'sqlite_rehydration_recorded': 'recovery' in manifest,
        'exclusion_turn_recorded': len(manifest['turns']) == 2,
        'human_quality_evaluation_performed': False}
    atomic(output / 'manifest.json', manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--budget-cny', type=float)
    parser.add_argument('--authorize-cny', type=float)
    parser.add_argument('--authorization-id')
    parser.add_argument('--authorization-note', default='')
    args = parser.parse_args(argv)
    if args.authorize_cny is not None:
        if not args.authorization_id:
            parser.error('--authorize-cny requires --authorization-id.')
        if not math.isfinite(args.authorize_cny) or not 0 < args.authorize_cny <= 10:
            parser.error('Each authorization is limited to CNY 10.')
    if args.budget_cny is not None and not (math.isfinite(args.budget_cny) and args.budget_cny > 0):
        parser.error('--budget-cny is the effective authorized total and must be positive.')
    if args.budget_cny is None and args.authorize_cny is None:
        parser.error('Provide --budget-cny (effective authorized total) and/or --authorize-cny.')
    if args.out.exists() and any(args.out.iterdir()):
        parser.error('Use a new output directory; evidence is never overwritten.')
    expected = MonetaryGuard.expected_total(args.ledger, args.authorize_cny, args.authorization_id)
    if expected is None:
        if args.budget_cny is None or not 0 < args.budget_cny <= 10:
            parser.error('A new ledger can only start with one authorization of at most CNY 10.')
    elif args.budget_cny is not None and abs(args.budget_cny - expected) > 1e-9:
        parser.error('--budget-cny does not match the recorded authorizations.')
    args.out.mkdir(parents=True, exist_ok=True)
    lease = JobLease(args.ledger.parent / (args.ledger.name + '.lease'))
    try:
        base = ProviderStore().config(Config.from_env())
        config = replace(base, source='multi', research_mode=True, cache=True,
                         cache_dir=args.out / 'cache', source_timeout=base.source_timeout,
                         task_timeout=base.task_timeout, deepseek_search_key='')
        config.validate()
        guard = MonetaryGuard(args.ledger,
                              None if args.authorize_cny is not None else args.budget_cny, config)
        if args.authorize_cny is not None:
            guard.grant(args.authorize_cny, args.authorization_id, args.authorization_note)
            if args.budget_cny is not None:
                guard.require_limit(args.budget_cny)
        manifest = run_validation(config, args.out, guard)
        print(json.dumps({'turns': len(manifest['turns']), 'session_id': manifest.get('session_id'),
                          'scope_completed': manifest['scope_completed'],
                          'protected_budget_consumed_cny': guard.used(),
                          'output': str(args.out.resolve())}, ensure_ascii=True), flush=True)
    finally:
        lease.close()


if __name__ == '__main__':
    main()
