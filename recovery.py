"""Deterministic, user-confirmed recovery actions.

These actions are deliberately small and explicit.  They are only applied after
the user clicks a diagnosis action; the LLM is not asked to reinterpret a vague
natural-language relaxation.
"""
from __future__ import annotations

from typing import Any, Mapping
from copy import deepcopy
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from common import GuideError, Paper, ROOT, utc_now


class RecoveryUnsafe(GuideError):
    """A recorded external boundary cannot be safely repeated."""


def atomic_json(path: Path, value: dict) -> None:
    """Durable replacement; a crash leaves either the previous or new JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                # Windows readers briefly hold non-delete-sharing handles.
                time.sleep(.01 * (attempt + 1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class JobLease:
    """OS-held lease, released even after process termination (no stale PID test)."""
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = (directory / '.execution.lock').open('a+b')
        try:
            self.stream.seek(0, 2)
            if self.stream.tell() == 0:
                self.stream.write(b'0')
                self.stream.flush()
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RecoveryUnsafe('此任务仍由另一个服务进程执行，请先等待原任务结束。') from None

    def close(self):
        if not self.stream.closed:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def execution_identity(config) -> dict:
    """Pin model, transport and implementation without storing endpoint secrets."""
    implementation = hashlib.sha256()
    for name in ('common.py', 'workflow.py', 'retrieval.py', 'research_policy.py',
                 'research_landscape.py', 'research_brief.py', 'prompts.py', 'llm.py',
                 'model_policy.py','model_identity.py',
                 'source.py', 'tools.py', 'recovery.py', 'agent_tools.py', 'network.py'):
        path = ROOT / name
        if path.exists():
            implementation.update(name.encode())
            implementation.update(path.read_bytes())
    public = config.public()
    from common import source_names
    authorized = source_names(config.source)
    if config.source not in ('multi', 'all') and 'deepseek' not in authorized:
        public.pop('deepseek_search_enabled', None)
    return {'config': public,
            'endpoint_hash': hashlib.sha256(config.base_url.encode()).hexdigest(),
            'implementation_hash': implementation.hexdigest()}


class SearchJournal:
    """Replay completed logical calls; an interrupted call is never reissued.

    This is intentionally conservative: even a response received immediately
    before a crash is ambiguous until its journal entry has been committed.
    There is no exactly-once guarantee from an external model or source API.
    """
    COUNTERS = ('search_http_calls', 'llm_calls', 'cache_hits', 'input_tokens',
                'output_tokens', 'usage_calls')

    def __init__(self, path: Path, config, *, resume: bool = False):
        self.path = path
        self.lock = threading.RLock()
        self.cursor = 0
        self.replayed_calls = 0
        identity = execution_identity(config)
        if resume:
            self.document = self.read(path)
            rows = self.document.get('calls')
            if (self.document.get('version') != 1 or not isinstance(rows, list)
                    or any(not isinstance(row, dict) or row.get('state') not in ('done', 'error', 'pending')
                           for row in rows)):
                raise RecoveryUnsafe('恢复记录缺失或损坏；已保留结果，不能安全重放。')
            if self.document.get('identity') != identity:
                raise RecoveryUnsafe('模型、后端、配置或程序版本已改变；保留现有结果，请重新创建任务。')
            if any(row.get('state') == 'pending' for row in self.document.get('calls', [])):
                raise RecoveryUnsafe('中断时有外部请求尚未记录完成，可能已产生费用，不能安全自动重放。')
        else:
            self.document = {'version': 1, 'identity': identity, 'created_at': utc_now(), 'calls': []}
            atomic_json(path, self.document)
        self.initial_calls = len(self.document.get('calls', []))

    @staticmethod
    def read(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _signature(kind, payload):
        raw = json.dumps([kind, payload], ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def call(self, kind, payload, invoke, budget, encode=lambda value: value,
             decode=lambda value: value, owner=None):
        signature = self._signature(kind, payload)
        with self.lock:
            rows = self.document['calls']
            index = self.cursor
            self.cursor += 1
            if index < len(rows):
                row = rows[index]
                if row.get('signature') != signature or row.get('kind') != kind:
                    raise RecoveryUnsafe('恢复输入或执行顺序与已保存记录不一致，已停止以避免重复外部请求。')
                if row.get('state') not in ('done', 'error'):
                    raise RecoveryUnsafe('外部请求完成状态未知，不能安全自动重放。')
                self._restore(row, budget, owner)
                self.replayed_calls += 1
                if row['state'] == 'error':
                    raise GuideError(row.get('error') or 'Recorded external call failed.')
                return decode(deepcopy(row['output']))
            row = {'kind': kind, 'signature': signature, 'state': 'pending',
                   'started_at': utc_now()}
            rows.append(row)
            atomic_json(self.path, self.document)
        try:
            value = invoke()
        except GuideError as exc:
            # Persist known termination, including usage already reported by a
            # response that failed validation. Replaying re-raises, never retries.
            with self.lock:
                row.update(state='error', error=str(exc), finished_at=utc_now())
                self._capture(row, budget, owner)
                atomic_json(self.path, self.document)
            raise
        with self.lock:
            row.update(state='done', output=encode(value), finished_at=utc_now())
            self._capture(row, budget, owner)
            atomic_json(self.path, self.document)
        return value

    def _capture(self, row, budget, owner):
        row['budget'] = {name: getattr(budget, name) for name in self.COUNTERS}
        row['budget']['usage_observed'] = budget.usage_observed
        row['elapsed_active_seconds'] = max(0, time.monotonic() - budget.started)
        if owner is not None:
            if hasattr(owner, 'events'):
                row['events'] = deepcopy(owner.events)
            if hasattr(owner, 'health_snapshot'):
                row['source_health'] = owner.health_snapshot()
            for name in ('deepseek_calls',):
                if hasattr(owner, name):
                    row[name] = getattr(owner, name)
            if hasattr(owner, 'cooldown'):
                row['cooldowns'] = {name: max(0, end - time.monotonic())
                                    for name, end in owner.cooldown.items()}

    def _restore(self, row, budget, owner):
        # Snapshots are cumulative, so replay does not charge twice. Reported
        # usage still includes previously paid calls from this same task.
        for name, value in row.get('budget', {}).items():
            setattr(budget, name, value)
        budget.started = time.monotonic() - float(row.get('elapsed_active_seconds') or 0)
        if owner is not None:
            if 'events' in row:
                owner.events[:] = deepcopy(row['events'])
            if 'source_health' in row and hasattr(owner, 'health'):
                owner.health = deepcopy(row['source_health'])
            if 'deepseek_calls' in row:
                owner.deepseek_calls = row['deepseek_calls']
            if 'cooldowns' in row and hasattr(owner, 'cooldown'):
                owner.cooldown = {name: time.monotonic() + seconds
                                  for name, seconds in row['cooldowns'].items()}

    def recovery(self):
        rows = self.document.get('calls', [])
        uncertain = sum(row.get('state') == 'pending' for row in rows)
        return {'can_resume': not uncertain, 'completed_calls': len(rows) - uncertain,
                'uncertain_calls': uncertain,
                'reason': 'unknown_external_outcome' if uncertain else 'process_interrupted',
                'message': ('中断时有外部请求尚未确认完成，费用可能已产生；已保存结果可用，不能安全自动重放。'
                            if uncertain else '已保存完成的步骤。继续后会复用这些结果，仅执行尚未开始的步骤。')}


def _paper(value):
    names = {item.name for item in fields(Paper)}
    return Paper(**{key: val for key, val in value.items() if key in names})


def _encode_batch(batch):
    return {'query': batch.query, 'providers': batch.providers,
            'papers': [paper.to_dict() for paper in batch.papers],
            'runs': [run.as_dict() for run in batch.runs]}


def _decode_batch(value):
    from source import SearchBatch, SourceRun
    return SearchBatch(value['query'], value['providers'],
                       [_paper(paper) for paper in value['papers']],
                       [SourceRun(**run) for run in value['runs']])


class JournalSource:
    def __init__(self, source, journal, budget):
        self._source, self._journal, self.budget = source, journal, budget

    def __getattr__(self, name):
        return getattr(self._source, name)

    def search_batch(self, query, limit=10, providers=None, filters=None):
        value = self._journal.call('source.search_batch', [query, limit, providers, filters],
            lambda: self._source.search_batch(query, limit, providers=providers, filters=filters),
            self.budget, _encode_batch, _decode_batch, self._source)
        self._source.last_batch = value
        return value

    def search(self, query, limit=10):
        def encode(papers):
            batch = getattr(self._source, 'last_batch', None)
            return {'papers': [paper.to_dict() for paper in papers],
                    'batch': _encode_batch(batch) if batch is not None else None}
        def decode(value):
            if value.get('batch'):
                self._source.last_batch = _decode_batch(value['batch'])
            return [_paper(paper) for paper in value['papers']]
        return self._journal.call('source.search', [query, limit],
            lambda: self._source.search(query, limit), self.budget, encode, decode, self._source)


class JournalLLM:
    def __init__(self, llm, journal, budget):
        self._llm, self._journal, self.budget = llm, journal, budget

    def __getattr__(self, name):
        return getattr(self._llm, name)

    def generate(self, stage, system, data, schema, validate):
        value = self._journal.call('llm.' + stage, [system, data, schema],
            lambda: self._llm.generate(stage, system, data, schema, validate),
            self.budget, owner=self._llm)
        # Never trust a stale schema merely because an earlier version accepted it.
        validate(value)
        return value


def _get(request: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    return request.get(key, default) if isinstance(request, Mapping) else getattr(request, key, default)


def recovery_patch(code: str, request: Mapping[str, Any] | Any, *, found: int = 0) -> dict:
    """Return the minimal task patch for a recovery action.

    No action can silently change a hard constraint: the caller must use the
    returned patch only after explicit user confirmation.
    """
    if code == 'relax_year':
        return {'year_from': None, 'year_to': None}
    if code == 'relax_venue':
        return {'venue': ''}
    if code == 'lower_target':
        target = int(_get(request, 'target_count', 5) or 5)
        lower, upper = _get(request, 'minimum_count'), _get(request, 'maximum_count')
        interval_floor = int(lower or 0) if lower != upper else 0
        floor = max(1, int(_get(request, 'minimum_category_count', 0) or 0), interval_floor)
        lowered = max(floor, min(target - 1, max(1, int(found or 0))))
        if lowered >= target:
            return {}
        return {'target_count': lowered, 'minimum_count': None, 'maximum_count': None}
    return {}
