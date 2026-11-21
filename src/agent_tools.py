"""Tool registry and structured observations for the agent runtime.

The registry is a policy/metadata layer over the existing source and assessment
tools. It does not invent a second execution path: ChatSession still owns
business state, while every source call has a schema, timeout, retry/cooldown
policy, cost model and provenance contract attached to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from types import MappingProxyType
import json
import threading
import time
from typing import Any, Callable

from common import BudgetExceeded, GuideError, REMOTE_OUTCOMES, RunCancelled
from costing import source_runs_cost
from execution_events import operation
from network import execution_context, TransportDeadlineExceeded
from source import PROVIDERS, AcademicSource, SearchBatch, classify_source_error, provider_capabilities
from tool_governance import audit_arguments, validate_arguments, ToolGovernanceError

SOURCE_FILTER_FIELDS = frozenset({
    'venue', 'year_from', 'year_to', 'venue_scope', 'publication_type',
    'accepted_only', 'presentation_types', 'preferred_venues',
    'openreview_venue_ids', 'journal_issns',
})

SOURCE_FILTER_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'venue': {'type': ['string', 'null'], 'maxLength': 200},
        'year_from': {'type': ['integer', 'null'], 'minimum': 1900, 'maximum': 2100},
        'year_to': {'type': ['integer', 'null'], 'minimum': 1900, 'maximum': 2100},
        'venue_scope': {'type': ['string', 'null'], 'maxLength': 40},
        'publication_type': {'type': ['string', 'null'], 'maxLength': 40},
        'accepted_only': {'type': ['boolean', 'null']},
        **{key: {'type': ['array', 'null'], 'maxItems': 20,
                 'items': {'type': 'string', 'maxLength': 250}}
           for key in ('presentation_types', 'preferred_venues',
                       'openreview_venue_ids', 'journal_issns')},
    },
}

SOURCE_INPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 500},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10},
        'filters': SOURCE_FILTER_SCHEMA,
    },
    'required': ['query'],
    'additionalProperties': False,
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: str
    description: str
    input_schema: dict
    timeout_s: float
    max_retries: int
    cost_model: str
    provenance: tuple[str, ...]
    needs_key: bool = False
    circuit_breaker: str = 'source cooldown after 429/503/timeout'
    min_interval_s: float = 0.0
    failure_threshold: int = 3
    cooldown_s: float = 45.0

    def as_dict(self) -> dict:
        return {'name': self.name, 'kind': self.kind, 'description': self.description,
                'input_schema': deepcopy(self.input_schema), 'timeout_s': self.timeout_s,
                'max_retries': self.max_retries, 'cost_model': self.cost_model,
                'provenance': list(self.provenance), 'needs_key': self.needs_key,
                'circuit_breaker': self.circuit_breaker,
                'min_interval_s': self.min_interval_s,
                'failure_threshold': self.failure_threshold, 'cooldown_s': self.cooldown_s,
                'execution_boundary': 'native HTTP: isolated process with absolute deadline; Python extensions: cooperative',
                'remote_usage_on_abort': 'unknown; stopping the client cannot undo remote work'}


def _outcome_fields(value) -> dict:
    remote = getattr(value, 'remote_outcome', None)
    fields = {}
    if isinstance(remote, str) and remote in REMOTE_OUTCOMES:
        fields['remote_outcome'] = remote
    if getattr(value, 'retry_safe', None) is False:
        fields['retry_safe'] = False
    return fields


@dataclass
class ToolObservation:
    tool: str
    ok: bool
    status: str
    data: Any = None
    error: str = ''
    retryable: bool = False
    elapsed_ms: int = 0
    cost_units: float = 0.0
    cost_cny: float | None = 0.0
    provenance: dict = field(default_factory=dict)
    worker_waited: bool = False
    remote_outcome: str | None = None
    retry_safe: bool | None = None

    def as_dict(self) -> dict:
        return {'tool': self.tool, 'ok': self.ok, 'status': self.status,
                'error': self.error, 'retryable': self.retryable,
                'elapsed_ms': self.elapsed_ms, 'cost_units': self.cost_units,
                'cost_cny': self.cost_cny, 'provenance': self.provenance,
                'worker_waited': self.worker_waited, **_outcome_fields(self)}


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec], source: AcademicSource | None = None,
                 *, handlers: dict[str, Callable] | None = None):
        self.specs = {spec.name: spec for spec in specs}
        # Trusted host configuration only; copying prevents later map mutations.
        bindings = dict(handlers or {})
        if any(name == 'search' or name not in self.specs or not callable(fn)
               for name, fn in bindings.items()):
            raise ValueError('invalid host handler binding')
        self._handlers = MappingProxyType(bindings)
        self.source = source
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._last_call: dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._failure_version: dict[str, int] = {}

    @classmethod
    def from_source(cls, source: AcademicSource, request_timeout: float = 15.0) -> 'ToolRegistry':
        specs = []
        for name, provider in PROVIDERS.items():
            specs.append(ToolSpec(
                name=name, kind=provider.kind, description=provider.description,
                input_schema=SOURCE_INPUT_SCHEMA,
                timeout_s=request_timeout, max_retries=3 if name == 'semantic_scholar' else 1,
                cost_model=('5 CNY / 1000 successful requests' if name == 'deepseek'
                            else 'actual cost unknown' if name == 'lune' else 'free'),
                provenance=('sources', 'doi', 'arxiv_id', 'venue', 'open_access_url',
                            'abstract_source', 'metadata_only', 'retrieved_at'),
                needs_key=provider.needs_key,
                min_interval_s=provider.rate_seconds,
                failure_threshold=3 if name in ('semantic_scholar', 'openalex') else 2,
                cooldown_s=45.0,
            ))
        specs.insert(0, ToolSpec(
            name='search', kind='aggregate', description='Bounded multi-source search.',
            input_schema=SOURCE_INPUT_SCHEMA, timeout_s=request_timeout, max_retries=1,
            cost_model='1 cost unit per batch', provenance=('sources', 'runs', 'filters'),
            failure_threshold=2, cooldown_s=45.0,
        ))
        return cls(specs, source)

    def available(self, name: str) -> bool:
        spec = self.specs.get(name)
        with self._state_lock:
            open_circuit = self._open_until.get(name, 0) > time.monotonic()
        if spec is None or open_circuit:
            return False
        return bool(self.source and self.source.available(name)) if spec.needs_key else bool(self.source)

    def _record_failure(self, name: str, key: str | None = None) -> None:
        spec = self.specs.get(name)
        if spec is None:
            return
        key = key or name
        with self._state_lock:
            self._failure_version[key] = self._failure_version.get(key, 0) + 1
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= spec.failure_threshold:
                self._open_until[key] = time.monotonic() + spec.cooldown_s

    def _versions(self, name: str, key: str) -> dict[str, int]:
        # Caller holds _state_lock.
        return {k: self._failure_version.get(k, 0) for k in (name, key)}

    def _record_success(self, name: str, key: str | None = None,
                        versions: dict[str, int] | None = None) -> None:
        key = key or name
        with self._state_lock:
            for item in {name, key}:
                if versions is not None and self._failure_version.get(item, 0) != versions.get(item, 0):
                    continue
                self._failures[item] = 0
                self._open_until.pop(item, None)

    def _admit_handler(self, name: str, spec: ToolSpec, budget) -> dict[str, int] | None:
        # No callbacks, budget methods, or sleeps while holding the state lock.
        # The timestamp is admission immediately before handler dispatch.
        while True:
            if budget is not None:
                budget.check_cancelled()
                budget.remaining()
            with self._state_lock:
                now = time.monotonic()
                if self._open_until.get(name, 0) > now:
                    return None
                delay = max(0.0, self._last_call.get(name, float('-inf')) +
                            max(0.0, spec.min_interval_s) - now)
                if delay <= 0:
                    self._last_call[name] = now
                    return self._versions(name, name)
            # Bounded polling permits cancellation and budget exhaustion without
            # holding a lock or allocating abandoned reservations.
            if budget is not None:
                budget.wait(min(delay, 0.05))
            else:
                time.sleep(min(delay, 0.05))

    def validate_input(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.specs.get(name) if isinstance(name, str) else None
        if spec is None:
            raise ToolGovernanceError('Unknown tool.')
        return validate_arguments(arguments, spec.input_schema)

    def describe(self) -> list[dict]:
        return [spec.as_dict() for spec in self.specs.values()]

    def capabilities(self) -> dict[str, dict]:
        return {name: provider_capabilities(name) for name in self.specs if name != 'search'}

    def health(self) -> dict[str, dict]:
        now = time.monotonic()
        out = {}
        with self._state_lock:
            failures = dict(self._failures)
            open_until = dict(self._open_until)
        for name, spec in self.specs.items():
            if name == 'search':
                continue
            directed_key = 'search:' + name
            end = max(open_until.get(name, 0), open_until.get(directed_key, 0))
            out[name] = {'failures': max(int(failures.get(name, 0)), int(failures.get(directed_key, 0))),
                         'circuit_open': end > now,
                         'cooldown_remaining_s': max(0.0, round(end - now, 2)),
                         'timeout_s': spec.timeout_s}
        if self.source is not None and hasattr(self.source, 'health_snapshot'):
            for name, state in self.source.health_snapshot().items():
                item = out.setdefault(name, {'timeout_s': self.specs.get(name).timeout_s
                                             if name in self.specs else 0})
                item.update(state)
        return out

    def call(self, name: str, fn, *args, retry: bool = True,
             _circuit_key: str | None = None, _input: dict[str, Any] | None = None,
             **kwargs) -> ToolObservation:
        budget = getattr(self.source, 'budget', None)
        if budget is not None:
            budget.check_cancelled()
        spec = self.specs.get(name) if isinstance(name, str) else None
        if spec is None:
            return ToolObservation('unknown', False, 'unknown_tool', error='Unknown tool.')
        if _input is None:
            return ToolObservation(name, False, 'invalid_input',
                                   error='structured tool input is required')
        try:
            _input = self.validate_input(name, _input)
        except ToolGovernanceError as exc:
            return ToolObservation(name, False, 'invalid_input', error=str(exc))
        if name == 'search':
            if (fn != self._search_impl or len(args) != 4 or kwargs or _input != {
                    'query': args[0], 'limit': args[1], 'filters': args[3] or {}}):
                return ToolObservation(name, False, 'invalid_input',
                                       error='validated input does not match execution arguments')
            providers = args[2]
            configured = getattr(self.source, 'providers', None)
            if providers is not None and (not isinstance(providers, list) or not providers or
                    any(not isinstance(p, str) or p not in PROVIDERS for p in providers)):
                return ToolObservation(name, False, 'invalid_input', error='unsupported source')
            if providers is not None and configured is not None and any(p not in configured for p in providers):
                return ToolObservation(name, False, 'forbidden', error='source is not enabled for this operation')
            args = (_input['query'], _input['limit'], list(providers) if providers is not None else None,
                    deepcopy(_input['filters']))
        else:
            if self._handlers.get(name) is not fn or name not in self._handlers:
                return ToolObservation(name, False, 'forbidden', error='handler not authorized')
            if args or kwargs:
                return ToolObservation(name, False, 'invalid_input',
                                       error='execution arguments must come from structured input only')
        # Non-search callers cannot select a different key to bypass governance.
        key = (_circuit_key or name) if name == 'search' else name
        if name == 'search':
            with self._state_lock:
                open_circuit = self._open_until.get(key, 0) > time.monotonic()
                interval = max(0, spec.min_interval_s - (time.monotonic() - self._last_call.get(key, 0)))
            if open_circuit:
                return ToolObservation(name, False, 'circuit_open', retryable=True,
                                       error=f'{name} circuit open after repeated failures')
            if interval:
                if budget is not None:
                    budget.wait(min(interval, 2.0))
                else:
                    time.sleep(min(interval, 2.0))
        attempts = max(1, spec.max_retries) if retry else 1
        started = time.monotonic()
        last_error = ''
        for attempt in range(attempts):
            if budget is not None:
                budget.check_cancelled()
            with self._state_lock:
                versions = self._versions(name, key)
                if name == 'search':
                    self._last_call[key] = time.monotonic()
            try:
                # Native I/O has a killable process boundary. Arbitrary Python
                # extensions remain synchronous: no orphan worker is created and
                # no misleading thread timeout is presented as hard cancellation.
                with operation('tool.' + name, category='tool', tool_name=name,
                               arguments=audit_arguments(_input if _input is not None else kwargs)) as measured:
                    if name != 'search':
                        versions = self._admit_handler(name, spec, budget)
                        if versions is None:
                            measured.update(status='circuit_open')
                            return ToolObservation(name, False, 'circuit_open', retryable=True,
                                                   error=f'{name} circuit open after repeated failures')
                    limit = min(spec.timeout_s, budget.remaining()) if budget is not None else spec.timeout_s
                    call_started = time.monotonic()
                    with execution_context(timeout_s=limit,
                            cancelled=(lambda: budget.cancel_token.requested) if budget is not None else None):
                        if name == 'search':
                            data = fn(*deepcopy(args))
                        else:
                            data = fn(**deepcopy(_input))
                    if budget is not None:
                        try:
                            budget.check_cancelled()
                        except RunCancelled as exc:
                            # The host call returned, but a cancellation must not
                            # be converted to success or erase its local result.
                            exc.remote_outcome, exc.retry_safe = 'unknown', False
                            if name == 'search' and isinstance(data, SearchBatch):
                                retained = deepcopy(data)
                                retained.stop_reason = 'user_cancelled'
                                observed = False
                                uncertain = False
                                for run in retained.runs:
                                    if run.status in ('ok', 'empty'):
                                        run.remote_outcome, run.retry_safe = 'completed', False
                                        observed = True
                                    elif run.status not in ('cache', 'cached', 'skipped'):
                                        uncertain = True
                                if observed and not uncertain:
                                    exc.remote_outcome = 'completed'
                                exc.partial_batch = retained
                            else:
                                exc.partial_data = data
                            raise
                    if time.monotonic() - call_started > limit:
                        measured.update(status='timeout')
                        retained = data
                        remote = 'completed'
                        if name == 'search' and isinstance(data, SearchBatch):
                            retained = deepcopy(data)
                            retained.stop_reason = 'tool_timeout'
                            for run in retained.runs:
                                if run.status in ('ok', 'empty', 'cache'):
                                    run.remote_outcome, run.retry_safe = 'completed', False
                                elif run.status not in ('skipped',):
                                    remote = 'unknown'
                        self._record_failure(name, key)
                        return ToolObservation(name, False, 'timeout', data=retained,
                                               error=f'{name} returned after its deadline',
                                               retryable=False, worker_waited=True,
                                               remote_outcome=remote, retry_safe=False,
                                               elapsed_ms=int((time.monotonic() - started) * 1000))
                    measured.update(status='ok')
                if name == 'search' and hasattr(data, 'runs') and not any(
                        run.status in ('ok', 'empty', 'cache') for run in data.runs):
                    self._record_failure(name, key)
                else:
                    self._record_success(name, key, versions)
                return ToolObservation(name, True, 'ok', data=data,
                                       elapsed_ms=int((time.monotonic() - started) * 1000),
                                       provenance={'input': audit_arguments(
                                           _input if _input is not None else kwargs)})
            except (TimeoutError, TransportDeadlineExceeded) as exc:
                if budget is not None:
                    budget.check_cancelled()
                self._record_failure(name, key)
                native = isinstance(exc, TransportDeadlineExceeded)
                last_error = f'{name} timed out after {spec.timeout_s}s'
                if native:
                    last_error += '; local HTTP process reaped; remote outcome may be unknown'
                # Never implicitly repeat a timed-out call: its remote outcome
                # may be unknown, even after its local worker has been reaped.
                return ToolObservation(name, False, 'timeout', error=last_error,
                                       retryable=False, worker_waited=not native,
                                       remote_outcome='unknown', retry_safe=False,
                                       elapsed_ms=int((time.monotonic() - started) * 1000))
            except (RunCancelled, BudgetExceeded):
                raise
            except GuideError as exc:
                if budget is not None:
                    try:
                        budget.check_cancelled()
                    except RunCancelled as cancelled:
                        for field, value in _outcome_fields(exc).items():
                            setattr(cancelled, field, value)
                        partial = getattr(exc, 'partial_batch', None)
                        if isinstance(partial, SearchBatch):
                            cancelled.partial_batch = deepcopy(partial)
                        raise cancelled from exc
                retryable = getattr(exc, 'retry_safe', False) is True
                code = classify_source_error(exc)
                safe_reason = {'rate_limited': 'HTTP 429 rate limit',
                               'bot_challenge': 'bot challenge',
                               'timeout': 'request timed out',
                               'auth': 'authentication failed',
                               'quota_exhausted': 'quota exhausted'}.get(code, code)
                last_error = f'{name}: {safe_reason}'
                self._record_failure(name, key)
                if retryable and attempt + 1 < attempts:
                    if budget is not None:
                        budget.wait(min(2 ** attempt, 4))
                    else:
                        time.sleep(min(2 ** attempt, 4))
                    continue
                partial = getattr(exc, 'partial_batch', None)
                return ToolObservation(name, False, 'error', error=last_error,
                                       data=partial if isinstance(partial, SearchBatch) else None,
                                       retryable=retryable, **_outcome_fields(exc),
                                       elapsed_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:  # noqa: BLE001
                if budget is not None:
                    budget.check_cancelled()
                self._record_failure(name, key)
                return ToolObservation(name, False, 'error',
                                       error=f'{type(exc).__name__}: tool failed; details withheld',
                                       retryable=False,
                                       elapsed_ms=int((time.monotonic() - started) * 1000))
        return ToolObservation(name, False, 'error', error=last_error,
                               elapsed_ms=int((time.monotonic() - started) * 1000))

    def _search_impl(self, query, limit, providers, filters):
        if self.source is None:
            raise GuideError('No source configured.')
        return self.source.search_batch(query, limit, providers=providers, filters=filters)

    def search(self, query: str, limit: int, providers: list[str] | None = None,
               filters: dict | None = None) -> ToolObservation:
        if not isinstance(query, str) or not query.strip():
            return ToolObservation('search', False, 'invalid_input',
                                   error='query must be a non-empty string')
        if type(limit) is not int or not 1 <= limit <= 10:
            return ToolObservation('search', False, 'invalid_input',
                                   error='limit must be an integer between 1 and 10')
        if providers is not None and (not isinstance(providers, list) or not providers
                                      or any(not isinstance(name, str) or name not in PROVIDERS
                                             for name in providers)):
            return ToolObservation('search', False, 'invalid_input',
                                   error='providers contains an unsupported source')
        configured = getattr(self.source, 'providers', None)
        if providers is not None and configured is not None:
            if any(name not in configured for name in providers):
                return ToolObservation('search', False, 'forbidden',
                                       error='source is not enabled for this operation')
        if filters is not None and (not isinstance(filters, dict)
                                    or any(key not in SOURCE_FILTER_FIELDS for key in filters)):
            return ToolObservation('search', False, 'invalid_input',
                                   error='filters contain unsupported fields')
        input_args = {'query': query, 'limit': limit, 'filters': filters or {}}
        try:
            input_args = self.validate_input('search', input_args)
        except ToolGovernanceError as exc:
            return ToolObservation('search', False, 'invalid_input', error=str(exc))
        started = time.monotonic()
        key = 'search:' + providers[0] if providers and len(providers) == 1 else 'search'
        obs = self.call('search', self._search_impl, input_args['query'], input_args['limit'],
                        list(providers) if providers is not None else None, input_args['filters'],
                        retry=False, _circuit_key=key, _input=input_args)
        if not obs.ok:
            error = obs.error
            if obs.worker_waited:
                error = (error or 'search timed out') + ' (worker did not cancel)'
            possible_paid = (obs.status in ('error', 'timeout') and obs.remote_outcome != 'not_started'
                             and any(p in ('lune', 'deepseek') for p in (providers or configured or [])))
            return ToolObservation('search', False, obs.status, error=error, data=obs.data,
                                   **_outcome_fields(obs), cost_cny=None if possible_paid else obs.cost_cny,
                                   retryable=obs.retryable, worker_waited=obs.worker_waited,
                                   elapsed_ms=int((time.monotonic() - started) * 1000))
        batch = obs.data
        ok = any(run.status in ('ok', 'empty', 'cache') for run in batch.runs)
        # A single run has an unambiguous remote outcome; do not attribute a
        # mixed batch's outcome to the aggregate operation.
        outcome = _outcome_fields(batch.runs[0]) if len(batch.runs) == 1 else {}
        return ToolObservation(
            'search', ok, 'ok' if batch.papers else ('error' if not ok else 'empty'),
            **outcome,
            data=batch, elapsed_ms=int((time.monotonic() - started) * 1000),
            cost_units=1.0 if ok else 0.0,
            cost_cny=source_runs_cost(batch.runs),
            provenance={'runs': [run.as_dict() for run in batch.runs],
                        'filters': audit_arguments(filters or {})})
