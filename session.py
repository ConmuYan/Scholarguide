"""Stateful multi-turn runtime: task update -> policy -> tool -> verify -> deliver.

This is the v2 path. It keeps every fact in SQLite, asks the controller what is
legal next, and refuses to deliver a list whose judgements are stale. The
single-pass ``workflow.run`` remains untouched for the existing benchmark.
"""
from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
from datetime import date
from contextlib import contextmanager
from functools import wraps
import json
import re
import time
import uuid
from typing import Any, Mapping, Sequence

from common import (Budget, BudgetExceeded, CancellationToken, Config, GuideError,
                    Paper, Request, RunCancelled, utc_now)
from costing import apply_cost, paid_cost_may_be_unknown
from model_identity import assessment_prompt_version, research_model_identity
from controller import (
    ACTIONS,
    BLOCKING_FIELDS,
    BudgetView,
    ControlState,
    Decision,
    PolicyError,
    allowed_actions,
    action_payload,
    native_action_tools,
    native_decision,
    assert_budget_conservation,
    decide,
    partial_reason,
    record_decision,
    validate_issued_decision,
)
import prompts
from reuse import (DeliveryCheck, ReuseItem, ReusePlan, assert_delivery_consistent,
                   plan_reuse, record_reuse, verify_delivery)
from state import StateStore, semantic_key
from tools import CostMeter, ToolOutcome, assess_batch, decision as run_decision, search as run_search
from retrieval import RetrievalRouter
from diagnosis import diagnose
from preflight import constraint_papers
from recovery import recovery_patch
from intent import allocate, mix_text
from quantities import apply_constraints
from execution_events import bind_events
import workflow

PROMPT_VERSION = 'assess-v1'
MAX_ACTIONS_PER_PHASE = 6
MAX_LLM_REQUESTS = 24
MAX_SEARCHES = 4
MAX_CANDIDATES = 40
TURN_SECONDS = 120.0
_PRESENTATION_FIELDS = frozenset(('prefer_recent', 'year_from', 'year_to'))


def _presentation_user_fields(current, submitted=None, constraints=None, inferred=None, message=''):
    """Keep user/source fields distinct from normalized default values.

    Existing tasks without this host-owned metadata remain conservative; they
    are never rewritten merely to enable presentation preferences.
    """
    previous = current.get('_presentation_user_fields') if current else []
    if (not isinstance(previous, list) or any(type(name) is not str or name not in _PRESENTATION_FIELDS
                                               for name in previous)):
        fields = set(_PRESENTATION_FIELDS)
    else:
        fields = set(previous)
    for value in (submitted, constraints, inferred):
        if isinstance(value, Mapping):
            fields.update(_PRESENTATION_FIELDS.intersection(value))
    if message and re.search(r'\b(?:recent|newest|latest|older|oldest|chronological)\b|近期|近年|最新|较早|最早',
                             message, re.IGNORECASE):
        fields.add('prefer_recent')
    return sorted(fields)


def _normalized_query(text: Any) -> str:
    return ' '.join(str(text or '').split()).casefold()


def _query_seen(query: Any, state: ControlState, history: Sequence[Any] = ()) -> bool:
    """True when the query is empty or repeats one that was already used."""
    target = _normalized_query(query)
    if not target:
        return True
    if target == _normalized_query(state.last_query):
        return True
    return any(_normalized_query(row) == target for row in history)

class FeedbackBudgetExhausted(GuideError):
    """A new feedback turn cannot start; no state was mutated."""
    code = 'feedback_budget_exhausted'


def _cancellable_turn(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        try:
            # Keep actual source/LLM operation spans bound for the whole turn.
            with bind_events(lambda action, payload: self._event(action, payload)):
                return fn(self, *args, **kwargs)
        except GuideError:
            if self._turn_open and self.cancel_requested:
                return self._partial('user_cancelled')
            raise
        finally:
            self._flush_progress()
    return wrapped


DEFAULT_LIMITS: dict[str, Any] = {
    'llm_left': MAX_LLM_REQUESTS,
    'searches_left': MAX_SEARCHES,
    'candidates_left': MAX_CANDIDATES,
    'phase': 1,
    'actions_left': MAX_ACTIONS_PER_PHASE,
    'searches_left_phase': 2,
    'phase_deadline': 0.0,
    'phase_seconds_left': 0.0,
    'paused_seconds_left': -1.0,
    'asks_left_phase': 1,
    'feedback_phases_left': 2,
}
COUNTER_KEYS = ('llm_left', 'searches_left', 'candidates_left', 'phase', 'actions_left',
                'searches_left_phase', 'asks_left_phase', 'feedback_phases_left')
FLOAT_KEYS = ('phase_deadline', 'phase_seconds_left', 'paused_seconds_left')


class LedgerMeter:
    """Adapter so tool functions can charge the persisted SQLite ledger."""

    def __init__(self, store: StateStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.action_seq = len(store.events(session_id))

    def reserve(self, action: str, units: float, detail: Mapping[str, Any] | None = None) -> int:
        self.action_seq += 1
        return self.store.reserve(self.session_id, self.action_seq, action, units, detail)

    def settle(self, handle: int, units: float) -> None:
        self.store.settle(handle, units)

    def summary(self) -> dict[str, Any]:
        return self.store.session_cost(self.session_id)


def _query_history(session: Any) -> list[str]:
    """The queries a session already used (empty when unavailable)."""
    store = getattr(session, 'store', None)
    if store is None:
        return []
    return [row['query'] for row in store.query_history(session.session_id)]


def _authorized_ids(session: Any, refs: Any = None) -> list[str]:
    """The forced re-assessment papers that are still in the candidate pool."""
    authorizations = list(getattr(session, '_forced_authorizations', ()) or ())
    if not authorizations:
        return []
    if getattr(session, '_forced_authorization_revision', None) != session.revision:
        return []
    if refs is None:
        store = getattr(session, 'store', None)
        if store is None:
            return []
        refs = store.candidate_refs(session.session_id)
    pool = {row['paper_id'] for row in refs}
    return [paper_id for paper_id in authorizations if paper_id in pool]


def _assert_action_budget(session: Any, decision: Decision) -> None:
    """Budget exhaustion keeps its own error semantics (``BudgetExceeded``)."""
    limits = getattr(session, 'limits', None)
    if not callable(limits):
        raise PolicyError('Action budget is unavailable.')
    current = limits()
    if not isinstance(current, Mapping):
        raise PolicyError('Action budget is unavailable.')
    required = ('searches_left', 'searches_left_phase', 'candidates_left', 'llm_left')
    try:
        values = {key: current[key] for key in required}
    except (KeyError, TypeError):
        raise PolicyError('Action budget is incomplete.') from None
    seconds = getattr(session, '_seconds_left', None)
    if not callable(seconds):
        raise PolicyError('Action time budget is unavailable.')
    try:
        time_left = seconds()
    except GuideError:
        raise
    except Exception as exc:
        raise PolicyError(f'Action time budget is unavailable: {type(exc).__name__}.') from None
    if decision.action == 'SEARCH':
        if (values['searches_left'] <= 0 or values['searches_left_phase'] <= 0
                or values['candidates_left'] <= 0 or time_left <= 0):
            raise BudgetExceeded('Search budget exhausted.')
    elif decision.action == 'ASSESS':
        if values['llm_left'] <= 0:
            raise BudgetExceeded('Assessment budget exhausted.')


def _assert_forced_authorized(session: Any, ids: Sequence[str]) -> None:
    """A forced re-assessment is only legal for the papers the user asked for."""
    authorized = set(_authorized_ids(session))
    if not authorized:
        raise PolicyError('No forced re-assessment is authorized in this state.')
    unknown = [paper_id for paper_id in ids if paper_id not in authorized]
    if unknown:
        raise PolicyError(f'Forced re-assessment is not authorized for: {unknown}')


def _release_forced_authorization(session: Any, ids: Sequence[str]) -> None:
    done = {str(paper_id) for paper_id in ids}
    session._forced_authorizations = [
        paper_id for paper_id in getattr(session, '_forced_authorizations', ()) or ()
        if paper_id not in done]
    if not session._forced_authorizations:
        session._forced_authorization_revision = None


class ChatSession:
    """One session = one task across multiple user turns."""

    def __init__(self, config: Config, store: StateStore, session_id: str | None = None, *,
                 source: Any = None, llm: Any = None, clock=time.monotonic,
                 reuse: bool = True, progress: Any = None) -> None:
        self.config = config
        self.prompt_version = assessment_prompt_version(config)
        self.store = store
        self.budget = Budget(config.task_timeout)
        if source is None:
            from source import AcademicSource
            source = AcademicSource(config, self.budget)
        if llm is None:
            from llm import JsonLLM
            llm = JsonLLM(config, self.budget)
        self.source = source
        self.router = (RetrievalRouter(config, source)
                       if hasattr(source, 'search_batch') and (config.research_mode or len(getattr(source, 'providers', [])) > 1)
                       else None)
        self.llm = llm
        self._native_pending = None
        self._program_pending = None
        self.clock = clock
        self.reuse = reuse
        self.progress = progress
        self._pending_progress: list[dict[str, Any]] = []
        self._goal_hint: str | None = None
        self.backend_identity = research_model_identity(config)
        self.mode = ('demo' if getattr(source, 'mode', '') == 'demo'
                     or getattr(llm, 'mode', '') == 'demo' else 'live')
        if session_id is None:
            self.session_id = store.create_session(DEFAULT_LIMITS)
        elif store.get_session(session_id) is None:
            # Caller-supplied ids (web/API) may address a fresh database.
            self.session_id = store.create_session(DEFAULT_LIMITS, session_id=session_id)
        else:
            self.session_id = session_id
        self.meter = LedgerMeter(store, self.session_id)
        self._llm_request_start: int | None = None
        self._llm_request_allowance = 0
        self.budget.request_guard = self._request_guard
        self._restore_phase_clock()
        self._forced_stale: set[str] = set()
        self._forced_reassess: list[str] = []
        self._forced_reassess_revision: int | None = None
        # Forced reassessment is authorized for a specific task revision.
        self._forced_authorizations: list[str] = []
        self._forced_authorization_revision: int | None = None
        self.model = config.model
        self._message = ''
        self._trace: list[dict[str, Any]] = []
        self._warnings: list[str] = []
        self._turn_open = False
        self._execution_prepared = False
        self._turn_started = time.monotonic()
        self._turn_usage_start = (0, 0, 0, 0, 0, 0)
        self._turn_trace_start = 0
        self._turn_query_start = 0
        self._turn_event_start = 0
        self._turn_cost_start = self.meter.summary()
        self.tools = None
        try:
            from agent_tools import ToolRegistry
            self.tools = ToolRegistry.from_source(source, config.source_timeout)
        except Exception:
            self.tools = None
        if self.router is not None:
            self.router.tools = self.tools
        self.agent_runtime = None
        if getattr(config, 'agent_runtime', 'legacy') == 'langgraph':
            try:
                from agent_runtime import LangGraphRuntime
                self.agent_runtime = LangGraphRuntime(self)
            except Exception as exc:  # optional dependency; keep legacy path usable
                self._warnings.append(f'LangGraph unavailable; using legacy runtime: {exc}')

    def rebind_provider(self, config: Config, llm: Any = None) -> None:
        """Atomically stage the provider/source/router/tool bundle."""
        identity = research_model_identity(config)
        if not identity:
            raise GuideError('active provider has no stable backend identity')
        if llm is None:
            from llm import JsonLLM
            llm = JsonLLM(config, self.budget)
        source = self.source
        router = (RetrievalRouter(config, source)
                  if hasattr(source, 'search_batch') and
                  (config.research_mode or len(getattr(source, 'providers', [])) > 1)
                  else None)
        tools = None
        try:
            from agent_tools import ToolRegistry
            tools = ToolRegistry.from_source(source, config.source_timeout)
        except Exception:
            tools = None
        if router is not None:
            router.tools = tools
        old_source_config = getattr(source, 'config', None)
        try:
            if hasattr(source, 'config'):
                source.config = config
            if hasattr(source, 'providers'):
                from common import source_names
                source.providers = source_names(config.source)
                source.store = getattr(config, 'source_store', None)
                if source.store is not None and config.source in ('multi', 'all'):
                    source.providers = source.store.enabled_sources(source.providers)
                if router is not None:
                    router.configured = list(source.providers)
        except Exception:
            if old_source_config is not None:
                source.config = old_source_config
            raise
        self._native_pending = None
        self._program_pending = None
        self.config = config
        self.prompt_version = assessment_prompt_version(config)
        self.model = config.model
        self.source = source
        self.llm = llm
        self.router = router
        self.tools = tools
        self.backend_identity = identity

    def prepare_execution(self) -> CancellationToken:
        """Admit work before launching its thread; a subsequent Stop is retained."""
        self.budget.cancel_token = CancellationToken()
        self._execution_prepared = True
        return self.budget.cancel_token

    def request_stop(self) -> bool:
        """Thread-safe signal only. The worker owns state, evidence and delivery."""
        return self.budget.cancel_token.request()

    @property
    def cancel_requested(self) -> bool:
        return self.budget.cancel_token.requested

    # -- task state --------------------------------------------------------
    def task(self) -> dict[str, Any]:
        current = self.store.current_task(self.session_id)
        task = dict(current['task']) if current else {}
        # Normalise legacy/LLM-emitted venue labels such as "CVPR 2026" to
        # venue="CVPR", year_from/year_to=2026 before routing or searching.
        if task:
            try:
                task.update(asdict(self._request(task)))
            except GuideError:
                pass
        return task

    @property
    def revision(self) -> int:
        return self.store.current_revision(self.session_id) or 0

    def _semantic(self, task: Mapping[str, Any]) -> str:
        return semantic_key(str(task.get('topic') or ''), str(task.get('purpose') or ''),
                            str(task.get('category') or 'none'), self.model, self.prompt_version,
                            str(task.get('venue') or ''), task.get('year_from'),
                            task.get('year_to'))

    def _request(self, task: Mapping[str, Any]) -> Request:
        return Request.from_dict(task)

    def _task_problem(self, task: Mapping[str, Any]) -> str:
        """Return a blocking reason when the task cannot be executed yet."""
        if not task or not str(task.get('topic') or '').strip():
            return 'missing_topic'
        if task.get('unsupported'):
            return 'unsupported'
        if task.get('task_error'):
            return 'unsatisfiable'
        minimum = int(task.get('minimum_category_count') or 0)
        target = int(task.get('target_count') or 5)
        if minimum > target:
            return 'unsatisfiable'
        return ''

    def _validate_update(self, raw: Any) -> None:
        if not isinstance(raw, dict) or not isinstance(raw.get('patch'), dict):
            raise GuideError('task update must contain a patch object')
        if not isinstance(raw.get('unresolved', []), list):
            raise GuideError('unresolved must be a list')

    def _llm_used(self, before: int) -> int:
        # Actual HTTP calls made by one stage (including JSON repair), minimum one.
        return max(1, self.budget.llm_calls - before)

    def _sync_http_deadline(self) -> None:
        """Give transport calls the current active phase's remaining time."""
        remaining = min(self.config.task_timeout, self._seconds_left())
        self.budget.started = time.monotonic() - (self.budget.seconds - remaining)

    def _request_guard(self) -> None:
        """Called before every HTTP request, including structured-output repair."""
        self.budget.check_cancelled()
        if self._seconds_left() <= 0:
            raise BudgetExceeded('Active phase time budget exhausted.')
        if self._llm_request_start is None:
            remaining = self.limits()['llm_left']
        else:
            remaining = self._llm_request_allowance - (
                self.budget.llm_calls - self._llm_request_start)
        if remaining <= 0:
            raise BudgetExceeded('Phase LLM request budget exhausted.')
        self._sync_http_deadline()

    @contextmanager
    def _llm_stage(self):
        """Bound a model stage and charge attempted requests even when it fails."""
        self._llm_request_start = self.budget.llm_calls
        self._llm_request_allowance = self.limits()['llm_left']
        before = self.budget.llm_calls
        started = False
        try:
            self._request_guard()
            started = True
            yield
        finally:
            if started:
                self._consume_llm(self._llm_used(before))
            self._llm_request_start = None
            self._llm_request_allowance = 0

    def _recent_messages(self, limit: int = 3) -> list[str]:
        out = []
        for event in self.store.events(self.session_id):
            if event['action'] in ('task_update', 'task_initial'):
                message = event['payload'].get('message')
                if message:
                    out.append(str(message))
        return out[-limit:]

    def _last_delivery(self) -> list[dict[str, Any]]:
        for event in reversed(self.store.events(self.session_id)):
            if event['action'] != 'delivered':
                continue
            normalized = []
            for position, item in enumerate(event['payload'].get('papers') or [], start=1):
                if not isinstance(item, Mapping):
                    continue
                paper_id = (item.get('paper_id') or item.get('id') or item.get('doi') or
                            item.get('url') or '')
                normalized.append({'index': item.get('index') or item.get('rank') or position,
                                   'paper_id': str(paper_id),
                                   'title': str(item.get('title') or '')})
            return normalized
        return []

    def update_task(self, message: str, *, constraints: Mapping[str, Any] | None = None,
                    goal_hint: str | None = None) -> dict[str, Any]:
        """Merge the latest user message into the task and persist a new revision."""
        current = self.task()
        from quantities import quantity_constraints
        count_patch = quantity_constraints(message, default=not current)
        with self._llm_stage():
            raw = run_decision('update', self.llm,
                               {'task': current, 'message': message,
                                'last_recommendations': self._last_delivery(),
                                'recent_messages': self._recent_messages(),
                                'current_date': date.today().isoformat()},
                               prompts.TASK_UPDATE_SCHEMA, self._validate_update,
                               prompts.TASK_UPDATE, self.meter)
        patch = {k: v for k, v in raw['patch'].items()
                 if v is not None or k in ('year_from', 'year_to')}
        from quantities import calibrate_user_requirements
        patch = calibrate_user_requirements(patch, message, current)
        unsupported = [str(x) for x in (raw.get('unsupported') or []) if str(x).strip()]
        for excerpt in unsupported:
            if excerpt.casefold() not in message.casefold():
                raise GuideError('unsupported must contain exact excerpts from the message.')
        merged = dict(current)
        merged.pop('task_error', None)
        if current and not count_patch:
            for key in ('target_count', 'minimum_count', 'maximum_count'):
                patch.pop(key, None)
        merged.update(patch)
        merged.update(count_patch)
        merged['unresolved'] = [u for u in raw.get('unresolved', []) if isinstance(u, dict)]
        merged['unsupported'] = unsupported
        merged['assumptions'] = [a for a in raw.get('assumptions', [])
                                 if isinstance(a, dict) and a.get('text')]
        # API constraints override parsed values, then the complete task is
        # validated before the next revision can be persisted.
        merged = apply_constraints(merged, constraints, message=message,
                                   goal_hint=goal_hint)
        origins = dict(current.get('requirement_origins') or {})
        message_id = f'msg-{self.revision + 1}'
        for field, value in patch.items():
            verified = isinstance(value, str) and value.strip().casefold() in message.casefold()
            origins[field] = {'message_id': message_id, 'message': message,
                              'excerpt': value if verified else message[:200],
                              'verified': bool(verified)}
        merged['requirement_origins'] = origins
        merged['_presentation_user_fields'] = _presentation_user_fields(
            current, constraints=constraints, inferred=patch, message=message)
        problem = self._task_problem(merged)
        if not problem:
            try:
                request = self._request(merged)
                merged.update(asdict(request))
            except GuideError as exc:
                merged['task_error'] = str(exc)
                problem = 'unsatisfiable'
        with self.store.tx():
            self.store.save_task(self.session_id, self.revision + 1, merged,
                                 source_message=message,
                                 change_summary=str(raw.get('summary') or ''))
            self._event('task_update', {'message': message, 'patch': patch,
                                        'summary': raw.get('summary', ''),
                                        'revision': self.revision, 'problem': problem,
                                        'unsupported': unsupported,
                                        'assumptions': merged['assumptions']})
        self._message = message
        return merged

    @_cancellable_turn
    def start(self, request_data: Mapping[str, Any] | None = None,
              message: str | None = None, constraints: Mapping[str, Any] | None = None,
              goal_hint: str | None = None) -> dict[str, Any]:
        self._begin_turn_metrics()
        self._message = message or str((request_data or {}).get('topic') or '')
        self._goal_hint = goal_hint
        self._new_phase(first=True)
        if request_data is not None:
            # Parse the structured request first; explicit constraints are then
            # merged and validated again as one atomic candidate task.
            task = dict(request_data)
            request = self._request(task)
            task.update(asdict(request))
            task = apply_constraints(task, constraints, message='',
                                     initial=False, goal_hint=goal_hint)
            request = self._request(task)
            task.update(asdict(request))
            task.setdefault('unresolved', [])
            task.setdefault('requirement_origins', {})
            task['_presentation_user_fields'] = _presentation_user_fields(
                {}, submitted=request_data, constraints=constraints)
            task.setdefault('assumptions', [])
            task.setdefault('unsupported', [])
            self.store.save_task(self.session_id, self.revision + 1, task,
                                 source_message=json.dumps(dict(request_data), ensure_ascii=False),
                                 change_summary='initial structured request')
            self._event('task_initial', {'request': asdict(request)})
            self._message = str(request_data.get('topic') or '')
        elif message:
            self.update_task(message, constraints=constraints, goal_hint=goal_hint)
        else:
            raise GuideError('start requires request_data or message')
        return self.turn()

    @_cancellable_turn
    def send(self, message: str, *, constraints: Mapping[str, Any] | None = None,
             goal_hint: str | None = None) -> dict[str, Any]:
        """A user message: answer an ASK or give feedback, then run one turn."""
        session = self.store.get_session(self.session_id)
        if not message.strip():
            raise GuideError('empty message')
        if session.get('status') == 'running':
            raise GuideError('session is already running')
        self._goal_hint = goal_hint
        self._begin_turn_metrics()
        self._message = message
        if session['status'] == 'awaiting_user':
            self.store.set_status(self.session_id, 'active')
            self._resume_clock()
            self._event('resume', {'message': message})
        else:
            if session['status'] in ('finished', 'failed'):
                self.store.set_status(self.session_id, 'active')
                self._event('reopen', {'message': message})
            self._new_phase(first=False)
        self.update_task(message, constraints=constraints, goal_hint=goal_hint)
        return self.turn()

    @_cancellable_turn
    def apply_recovery(self, code: str) -> dict[str, Any]:
        """Apply one explicit recovery action without asking the updater LLM.

        The diagnosis UI is the consent boundary.  The patch is still validated
        by Request before it reaches the store, and a new turn is recorded
        normally so Activity can audit the relaxation.
        """
        if not self.can_send():
            raise FeedbackBudgetExhausted('本会话的反馈预算已用尽；请新建会话。')
        session = self.store.get_session(self.session_id) or {}
        if session.get('status') == 'awaiting_user':
            raise GuideError('请先回答当前澄清问题，再应用 recovery。')
        found = len(self.select())
        current = self.task()
        patch = recovery_patch(code, current, found=found)
        if not patch:
            raise GuideError(f'recovery action has no effect: {code}')
        if all(current.get(key) == value for key, value in patch.items()):
            raise GuideError(f'recovery action has no effect: {code}')
        merged = dict(self.task())
        merged.update(patch)
        request = self._request(merged)
        merged.update(asdict(request))
        self._begin_turn_metrics()
        self.store.set_status(self.session_id, 'active')
        self._new_phase(first=False)
        with self.store.tx():
            self.store.save_task(self.session_id, self.revision + 1, merged,
                                 source_message=f'recovery:{code}',
                                 change_summary=f'user-confirmed {code}')
            self._event('recovery', {'code': code, 'patch': patch,
                                     'revision': self.revision})
        self._message = f'恢复操作：{code}'
        return self.turn()

    # -- per-turn accounting ------------------------------------------------
    def _begin_turn_metrics(self) -> None:
        if not self._execution_prepared:
            self.budget.cancel_token = CancellationToken()
        self._execution_prepared = False
        self._turn_open = True
        self._turn_started = time.monotonic()
        # Use raw cumulative counters here. Budget.metrics() intentionally hides
        # token totals when a previous turn had partial usage; using those None
        # values would make the next full turn absorb the previous partial turn.
        self._turn_usage_start = (
            self.budget.input_tokens,
            self.budget.output_tokens,
            self.budget.llm_calls,
            self.budget.search_http_calls,
            len(self.store.query_history(self.session_id)),
            self.budget.usage_calls,
        )
        self._turn_trace_start = len(self._trace)
        self._turn_query_start = len(self.store.query_history(self.session_id))
        self._turn_event_start = len(self.store.events(self.session_id))
        self._turn_cost_start = self.meter.summary()

    @staticmethod
    def _cost_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
        def diff(key: str) -> float:
            return round(float(after.get(key, 0) or 0) - float(before.get(key, 0) or 0), 6)
        keys = set((before.get('by_action') or {})) | set((after.get('by_action') or {}))
        by_action = {}
        for key in keys:
            old = (before.get('by_action') or {}).get(key) or {}
            new = (after.get('by_action') or {}).get(key) or {}
            by_action[key] = {field: round(float(new.get(field, 0) or 0)
                                         - float(old.get(field, 0) or 0), 6)
                              for field in ('reserved', 'settled', 'refunded')}
        return {'reserved': diff('reserved'), 'settled': diff('settled'),
                'refunded': diff('refunded'), 'by_action': by_action}

    def _turn_metrics(self) -> dict[str, Any]:
        usage = self.budget.metrics()
        start_input, start_output, start_llm, start_http, start_queries, start_usage_calls = self._turn_usage_start
        usage_calls = self.budget.usage_calls - start_usage_calls
        llm_calls = self.budget.llm_calls - start_llm
        complete = llm_calls > 0 and llm_calls == usage_calls
        current = self.meter.summary()
        return {
            'elapsed_seconds': round(time.monotonic() - self._turn_started, 3),
            'search_http_calls': max(0, self.budget.search_http_calls - start_http),
            'llm_calls': max(0, llm_calls),
            'cache_hits': 0,
            'input_tokens': (self.budget.input_tokens - start_input) if complete else None,
            'output_tokens': (self.budget.output_tokens - start_output) if complete else None,
            'input_tokens_total': self.budget.input_tokens if complete else None,
            'output_tokens_total': self.budget.output_tokens if complete else None,
            'usage_complete': complete,
            'usage_calls': max(0, usage_calls),
            'cost_units': self._cost_delta(self._turn_cost_start, current),
            'turn_queries': max(0, len(self.store.query_history(self.session_id)) - start_queries),
        }

    # -- budgets -----------------------------------------------------------
    def can_send(self) -> bool:
        """Whether this session can accept another user message.

        Answering an outstanding clarification is allowed even when feedback
        phases are exhausted; starting a new feedback phase is not.
        """
        session = self.store.get_session(self.session_id) or {}
        if session.get('status') == 'awaiting_user':
            return True
        return session.get('status') not in ('running', 'archived')

    def capabilities(self) -> dict[str, Any]:
        limits = self.limits()
        session = self.store.get_session(self.session_id) or {}
        awaiting = session.get('status') == 'awaiting_user'
        can_send = awaiting or session.get('status') not in ('running', 'archived')
        return {'can_send': can_send,
                'can_retry': can_send,
                'feedback_phases_left': None,
                'feedback_phases_cap': None,
                'awaiting_user': awaiting,
                'session_status': session.get('status', '')}

    def limits(self) -> dict[str, Any]:
        session = self.store.get_session(self.session_id)
        raw = dict(DEFAULT_LIMITS)
        raw.update(session.get('budget') or {})
        limits: dict[str, Any] = {}
        for key in COUNTER_KEYS:
            limits[key] = max(0, int(raw.get(key, DEFAULT_LIMITS[key])))
        for key in FLOAT_KEYS:
            limits[key] = float(raw.get(key, DEFAULT_LIMITS[key]))
        return limits

    def _save_limits(self, limits: Mapping[str, Any]) -> None:
        updated = dict(limits)
        paused = float(updated.get('paused_seconds_left', -1.0))
        updated['phase_seconds_left'] = (paused if paused >= 0 else max(
            0.0, float(updated.get('phase_deadline', 0)) - self.clock()))
        self.store.update_budget(self.session_id, updated)

    def _restore_phase_clock(self) -> None:
        """Persist remaining active time, never a prior process's clock origin."""
        session = self.store.get_session(self.session_id) or {}
        raw = session.get('budget') or {}
        limits = self.limits()
        remaining = raw.get('phase_seconds_left')
        if remaining is None:  # databases written before active-time snapshots
            remaining = max(0.0, limits['phase_deadline'] - self.clock())
        cap = min(240.0 if self.config.research_mode else TURN_SECONDS, self.config.task_timeout)
        remaining = min(cap, max(0.0, float(remaining)))
        if limits['paused_seconds_left'] >= 0:
            remaining = min(cap, limits['paused_seconds_left'])
            limits['paused_seconds_left'] = remaining
        limits['phase_deadline'] = self.clock() + remaining
        self._save_limits(limits)
        self._sync_http_deadline()

    def _new_phase(self, *, first: bool) -> None:
        limits = self.limits()
        limits['phase'] = 1 if first else limits['phase'] + 1
        limits['actions_left'] = MAX_ACTIONS_PER_PHASE
        if not first:
            limits['llm_left'] = MAX_LLM_REQUESTS
        limits['searches_left'] = MAX_SEARCHES
        limits['candidates_left'] = MAX_CANDIDATES
        limits['searches_left_phase'] = MAX_SEARCHES
        limits['phase_deadline'] = self.clock() + min(240.0 if self.config.research_mode else TURN_SECONDS, self.config.task_timeout)
        limits['paused_seconds_left'] = -1.0
        limits['asks_left_phase'] = 1
        # feedback_phases_left is legacy metadata, not a lifetime chat cap.
        self._save_limits(limits)
        self._sync_http_deadline()

    def _resume_clock(self) -> None:
        limits = self.limits()
        paused = limits.get('paused_seconds_left', -1.0)
        if paused >= 0:
            limits['phase_deadline'] = self.clock() + paused
            limits['paused_seconds_left'] = -1.0
            self._save_limits(limits)
            self._sync_http_deadline()

    def _pause_clock(self) -> None:
        limits = self.limits()
        limits['paused_seconds_left'] = max(0.0, limits['phase_deadline'] - self.clock())
        self._save_limits(limits)

    def _consume(self, **changes: int) -> None:
        limits = self.limits()
        for key, delta in changes.items():
            limits[key] = max(0, limits[key] - int(delta))
        self._save_limits(limits)

    def _consume_llm(self, count: int = 1) -> None:
        self._consume(llm_left=count)

    def _seconds_left(self) -> float:
        return max(0.0, self.limits()['phase_deadline'] - self.clock())

    # -- observations ------------------------------------------------------
    def _deliver_progress(self, pending: list[dict[str, Any]]) -> None:
        callback = self.progress
        if callback is None:
            return
        persisted = {event['seq']: event for event in self.store.events(self.session_id)}
        for event in pending:
            seq = event.pop('_seq', None)
            if seq is not None and seq not in persisted:
                continue
            try:
                callback(event)
            except Exception:
                # Persistence is authoritative; a UI callback cannot lock chat.
                pass

    def _flush_progress(self) -> None:
        pending, self._pending_progress = self._pending_progress, []
        self._deliver_progress(pending)

    def _event(self, action: str, payload: Mapping[str, Any] | None = None) -> None:
        data = dict(payload or {})
        revision = self.revision or 0
        seq = self.store.append_event(self.session_id, action, data, revision=revision or None)
        self.store.commit()
        self._trace.append({'step': seq, 'action': action, **data})
        event = {'action': action, 'at': utc_now(), 'revision': revision,
                 'payload': data, '_seq': seq}
        # Events emitted inside a StateStore transaction are deferred until the
        # public-turn wrapper observes the commit.  Outside a transaction, the
        # explicit commit above makes delivery immediate.
        if getattr(self.store, '_tx_depth', 0):
            self._pending_progress.append(event)
        else:
            self._deliver_progress([event])

    def _papers(self, ids: Sequence[str]) -> list[Paper]:
        rows = {row['paper_id']: row for row in self.store.candidate_papers(self.session_id)}
        out = []
        for paper_id in ids:
            row = rows.get(paper_id)
            if row is None:
                continue
            try:
                authors = json.loads(row.get('authors_json') or '[]')
            except ValueError:
                authors = []
            try:
                sources = json.loads(row.get('sources_json') or '[]')
            except ValueError:
                sources = []
            if not sources:
                sources = [x for x in str(row.get('source') or 'arxiv').split(',') if x]
            out.append(Paper(id=row['paper_id'], title=row['title'], abstract=row['abstract'],
                             url=row['url'] or '', year=row['year'], authors=authors,
                             venue=row.get('venue') or '', doi=row.get('doi') or '',
                             arxiv_id=row.get('arxiv_id') or '',
                             open_access_url=row.get('open_access_url') or '',
                             sources=sources, source=(sources[0] if sources else ''),
                             abstract_source=row.get('abstract_source') or '',
                             metadata_only=bool(row.get('metadata_only')),
                             authority=float(row.get('authority') or 0),
                             snippet=row.get('snippet') or '',
                             provenance_conflict=bool(row.get('provenance_conflict')),
                             publication=json.loads(row.get('publication_json') or '{}'),
                             fetched_at=row.get('fetched_at') or utc_now()))
        return out

    def _assessments(self, task: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        ids = [ref['paper_id'] for ref in self.store.candidate_refs(self.session_id)]
        rows = self.store.valid_assessments(self.session_id, ids, task=task,
                                            model=self.model, prompt_version=self.prompt_version)
        return {paper_id: {'relevance': row['relevance'],
                           'category_status': row['category_status'],
                           'paper_type': row.get('paper_type') or 'unknown',
                           'fit_score': row.get('fit_score', 50),
                           'evidence_verified': bool(row['evidence_verified']),
                           'evidence_quote': row['evidence_quote'],
                           'reason': row.get('understanding_sentence') or ''}
                for paper_id, row in rows.items()}

    def select(self) -> list[dict[str, Any]]:
        task = self.task()
        if not task or self._task_problem(task):
            return []
        papers = self._papers([ref['paper_id'] for ref in self.store.candidate_refs(self.session_id)])
        from research_runtime import current_landscape
        landscape = current_landscape(self) if self.config.research_mode else None
        return workflow.select_papers(papers, self._assessments(task), self._request(task),
                                      research=self.config.research_mode, landscape=landscape)

    def gaps(self) -> list[dict[str, Any]]:
        task = self.task()
        if not task or self._task_problem(task):
            return []
        from research_runtime import current_landscape
        landscape = current_landscape(self) if self.config.research_mode else None
        return workflow.coverage(self.select(), self._request(task), research=self.config.research_mode,
                                  landscape=landscape)

    def _plan(self):
        """Reuse plan.

        With reuse disabled, candidates that existed at the start of the turn are
        forced stale once; after they are reassessed they become valid again, so
        the controller does not loop on the same papers.
        """
        plan = plan_reuse(self.store, self.session_id, self.task(), model=self.model,
                          prompt_version=self.prompt_version)
        if self.reuse or not self._forced_stale:
            return plan
        base = {item.paper_id: item for item in plan.items}
        items = []
        for ref in self.store.candidate_refs(self.session_id):
            paper_id = ref['paper_id']
            if paper_id in self._forced_stale:
                items.append(ReuseItem(paper_id, ref['content_hash'], 'stale', 'reuse_disabled'))
            elif paper_id in base:
                items.append(base[paper_id])
            else:
                items.append(ReuseItem(paper_id, ref['content_hash'], 'stale', 'missing'))
        valid = {key: value for key, value in plan.valid.items()
                 if key not in self._forced_stale}
        return ReusePlan(tuple(items), valid, plan.expected_semantic_key, plan.model,
                         plan.prompt_version)

    def control_state(self) -> ControlState:
        task = self.task()
        plan = self._plan()
        limits = self.limits()
        refs = self.store.candidate_refs(self.session_id)
        papers = self._papers([ref['paper_id'] for ref in refs])
        preflight = constraint_papers(papers, task)
        unresolved = list(task.get('unresolved') or ())
        for assumption in task.get('assumptions') or ():
            if isinstance(assumption, dict) and assumption.get('field') in BLOCKING_FIELDS:
                unresolved.append({'field': assumption['field'], 'blocking': True,
                                   'assumption': assumption.get('text', '')})
        session = self.store.get_session(self.session_id)
        if session is None:
            raise GuideError('Session no longer exists.')
        return ControlState(
            candidates=len(refs),
            awaiting_user=session['status'] == 'awaiting_user',
            unassessed=(len(self._forced_reassess)
                        if self._forced_reassess_revision == self.revision else 0)
                       + len(_authorized_ids(self, refs)),
            stale=len(plan.stale_ids),
            gaps=tuple(self.gaps()),
            unresolved=tuple(unresolved),
            budget=BudgetView(seconds_left=self._seconds_left(),
                              llm_left=limits['llm_left'],
                              searches_left=(min(limits['searches_left'],
                                                 limits['searches_left_phase'])
                                             if limits['candidates_left'] > 0 else 0)),
            last_query=self._last_query(),
            unsatisfiable=self._task_problem(task) == 'unsatisfiable',
            candidate_ids=tuple(ref['paper_id'] for ref in refs),
            asks_left=limits['asks_left_phase'],
            metadata_eligible=preflight['eligible'],
            preflight_blocked=preflight['blocked'],
        )

    def _last_query(self) -> str:
        history = self.store.query_history(self.session_id)
        return history[-1]['query'] if history else ''

    # -- turn loop ---------------------------------------------------------
    def prepare_turn(self) -> dict[str, Any] | None:
        """Preconditions, reuse accounting and per-turn resets.

        Returns a terminal result for missing/unsupported/unsatisfiable tasks; otherwise None.
        The graph runtime and the legacy loop both call this exactly once per turn.
        """
        session = self.store.get_session(self.session_id)
        if session is None:
            raise GuideError('Session no longer exists.')
        if session['status'] == 'awaiting_user':
            return self._partial('awaiting_user')
        if hasattr(self.source, 'begin_turn'):
            self.source.begin_turn()
        self._event('provider', {
            'profile_id': self.config.profile_id,
            'profile_name': self.config.profile_name,
            'provider': self.config.provider,
            'model': self.config.model,
            'protocol': self.config.protocol,
            'input_price': self.config.input_price,
            'output_price': self.config.output_price,
            'currency': self.config.currency,
        })
        task = self.task()
        problem = self._task_problem(task)
        if problem == 'missing_topic':
            question = 'What research topic should this reading list cover?'
            self.store.set_status(self.session_id, 'awaiting_user')
            self._pause_clock()
            self._event('ask', {'question': question, 'field': 'topic', 'program': True})
            return self._result('awaiting_user', 'missing_topic', [], question=question)
        if problem == 'unsupported':
            self.store.set_status(self.session_id, 'finished')
            self._event('unsupported', {'requirements': task.get('unsupported') or []})
            return self._result('unsupported', 'unsupported_requirements', [])
        if problem == 'unsatisfiable':
            self._event('unsatisfiable', {'task': task})
            return self._result('no_results', 'unsatisfiable', self.select())

        if task.get('reading_goal'):
            mix = allocate(int(task.get('target_count') or 5), str(task.get('reading_goal')))
            self._event('intent', {
                'goal': task.get('reading_goal'),
                'policy': 'deterministic_intent_v1',
                'mix': mix_text(mix),
                'allocations': dict(mix),
                'target_count': int(task.get('target_count') or 5),
                'category': task.get('category') or 'none',
                'minimum_category_count': int(task.get('minimum_category_count') or 0),
                'source': 'task.reading_goal',
            })
        if self.config.research_mode and self.mode != 'demo':
            from research_runtime import prepare_landscape, refresh_landscape
            prepare_landscape(self)
            refresh_landscape(self)
        self._forced_stale = ({ref['paper_id']
                               for ref in self.store.candidate_refs(self.session_id)}
                              if not self.reuse else set())
        initial_plan = self._plan()
        record_reuse(self.store, self.session_id, initial_plan, revision=self.revision)
        self.store.commit()
        self._trace.append({'step': len(self.store.events(self.session_id)),
                            'action': 'reuse',
                            'reused': len(initial_plan.reused_ids),
                            'stale': len(initial_plan.stale_ids),
                            'reuse_rate': round(initial_plan.reuse_rate, 4)})
        return None

    def _native_operation_open(self) -> bool:
        # A mode switch must not erase a previously started native operation.
        for event in reversed(self.store.events(self.session_id)):
            if event['action'] == 'native_operation' and event.get('revision') == self.revision:
                return event['payload'].get('status') == 'started'
        return False

    def _native_operation_start(self, expected_revision: int | None = None) -> None:
        # Claim and commit the marker before any external dispatch. SQLite's
        # write lock serializes competing instances that share this database.
        # Never dispatch while an enclosing transaction could roll it back.
        if expected_revision is None:
            expected_revision = self.revision
        con = self.store.con
        if getattr(self.store, '_tx_depth', 0) or con.in_transaction:
            raise PolicyError('Native operation requires its own durable transaction.')
        try:
            con.execute('BEGIN IMMEDIATE')
            if self.revision != expected_revision:
                raise PolicyError('Native authorization revision changed.')
            if self._native_operation_open():
                con.rollback()
                if getattr(self, '_native_owned_revision', None) != self.revision:
                    raise PolicyError('Unresolved native operation belongs to another session instance.')
                return
            self._event('native_operation', {'status': 'started'})
            self._native_owned_revision = self.revision
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise

    def _native_operation_close(self) -> None:
        if self._native_operation_open():
            if getattr(self, '_native_owned_revision', None) != self.revision:
                raise PolicyError('Unresolved native operation belongs to another session instance.')
            self._event('native_operation', {'status': 'closed'})
        self._native_owned_revision = None

    def next_decision(self) -> tuple[Decision | None, str]:
        """Choose one bounded action. Returns (None, stop_reason) at a budget boundary."""
        self._native_pending = None
        self._program_pending = None
        if self.cancel_requested:
            return None, 'user_cancelled'
        if self._native_operation_open():
            return None, 'pending_operation_not_replayed'
        task = self.task()
        limits = self.limits()
        if limits['actions_left'] <= 0 or self._seconds_left() <= 0:
            return None, ('time_budget_exhausted' if self._seconds_left() <= 0
                          else 'action_budget_exhausted')
        state = self.control_state()
        if state.awaiting_user:
            return None, 'awaiting_user'
        program_decision = None
        legal = allowed_actions(state)
        forced = self._take_forced_reassessment()
        if forced:
            if 'ASSESS' not in legal:
                self._warnings.append('forced reassessment is not currently executable')
                return None, 'no_legal_action'
            program_decision = Decision('ASSESS', {'paper_ids': forced, 'force': True},
                                        True, 'explicit_reassessment_requested')
        elif self.config.research_mode and self.mode != 'demo':
            if not state.gaps and 'FINISH' in legal:
                selected = self.select()
                checked = verify_delivery(self.store, self.session_id,
                    [p['id'] for p in selected], task, model=self.model,
                    prompt_version=self.prompt_version, research=True)
                excluded = {str(item) for item in (task.get('excluded_paper_ids') or ())}
                blocked = [paper['id'] for paper in selected if paper['id'] in excluded]
                if checked.ok and not blocked and selected:
                    program_decision = Decision('FINISH', {}, True, 'requirements_verified_by_program')
                elif blocked:
                    self._warnings.append(
                        f'delivery blocked: excluded paper ids {blocked}')
            elif 'ASSESS' in legal and self._plan().stale_ids:
                ids = self._plan().stale_ids[:10]
                if ids:
                    program_decision = Decision(
                        'ASSESS', {'paper_ids': ids}, True,
                        'assess_available_evidence_before_search')
            elif legal == ('SEARCH',):
                program_decision = self._coverage_search(state)
            elif legal == ('FINISH',):
                program_decision = Decision('FINISH', {}, True, 'no_productive_action_remains')
        observation = {
            'task': task,
            'gaps': list(state.gaps),
            'pending_ids': self._plan().stale_ids,
            'allowed_reasons': None,
            'configured_sources': list(getattr(self.source, 'providers', [])),
        }
        if self.config.research_mode and program_decision is None:
            from research_runtime import research_context
            observation['research_context'] = research_context(self)
        if limits['llm_left'] <= 0 and program_decision is None:
            return None, 'llm_budget_exhausted'
        proposal = None
        revision, provider = self.revision, self.llm
        session_id, identity = self.session_id, research_model_identity(self.config)
        try:
            if program_decision is not None:
                decision = program_decision
            elif getattr(self.config, 'action_protocol', 'json') == 'responses_native':
                from llm import NativeUnsupported
                self._native_operation_start(expected_revision=revision)
                try:
                    try:
                        with self._llm_stage():
                            handle = self.meter.reserve('action', 1.0)
                            try:
                                proposal = self.llm.propose_action(
                                    prompts.ACTION_NATIVE, action_payload(state, observation), native_action_tools(state))
                            except BaseException:
                                self.meter.settle(handle, 0.0)
                                raise
                            self.meter.settle(handle, 1.0)
                    except NativeUnsupported:
                        # The provider returned an explicit unsupported response;
                        # this is known, not an unknown operation. Close the native
                        # marker before the separately governed JSON fallback.
                        self._native_operation_close()
                        if getattr(self.config, 'native_fallback', 'disabled') != 'json_on_unsupported':
                            raise
                        self._event('native_fallback', {'reason': 'unsupported', 'protocol': 'json'})
                        with self._llm_stage():
                            decision = decide(self.llm, self.control_state(), observation, self.meter)
                        decision = Decision(decision.action, decision.args, decision.accepted,
                                            '', decision.attempted, decision.error, decision.fallback)
                    else:
                        self.budget.check_cancelled()
                        if (self.session_id != session_id or self.revision != revision or self.llm is not provider
                                or research_model_identity(self.config) != identity
                                or getattr(self.config, 'action_protocol', 'json') != 'responses_native'):
                            raise PolicyError('Native proposal state changed.')
                        decision = native_decision(proposal.name, proposal.arguments, self.control_state())
                        decision = Decision(decision.action, decision.args, decision.accepted,
                                            '', decision.attempted, decision.error, decision.fallback)
                except BaseException:
                    if not getattr(self.llm, 'last_native_request_sent', True):
                        self._native_operation_close()
                    raise
            else:
                with self._llm_stage():
                    decision = decide(self.llm, state, observation, self.meter)
        except PolicyError as exc:
            self._warnings.append(str(exc))
            return None, 'no_legal_action'
        if decision.accepted and decision.action == 'SEARCH':
            normalized = ' '.join(str(decision.args.get('query') or '').split()).casefold()
            previous = {' '.join(row['query'].split()).casefold() for row in self.store.query_history(self.session_id)}
            if normalized in previous:
                from controller import fallback_action
                decision = Decision('FINISH', {}, False, decision.reason, 'SEARCH', 'repeated_query', fallback_action(state))
        with self.store.tx():
            self._consume(actions_left=1)
            record_decision(
                self.store, self.session_id, decision, revision=self.revision,
                context={
                    'legal_actions': list(allowed_actions(state)),
                    'candidates': state.candidates,
                    'stale': state.stale,
                    'gaps': len(state.gaps),
                    'decision_source': 'program' if program_decision else 'model',
                    'budget': {
                        'seconds_left': round(state.budget.seconds_left, 1),
                        'llm_left': state.budget.llm_left,
                        'searches_left': state.budget.searches_left,
                    },
                })
        if not decision.accepted:
            proposal = None
            self._warnings.append(
                f'{decision.attempted}: {decision.error}; fallback={decision.fallback}')
            decision = self._forced(decision.fallback, state)
            if decision is None:
                self._warnings.append('no legal fallback action remains')
                return None, 'no_legal_action'
            with self.store.tx():
                record_decision(self.store, self.session_id, decision, revision=self.revision,
                                context={'decision_source': 'program_fallback',
                                         'legal_actions': list(allowed_actions(self.control_state()))})
        if proposal is not None:
            self._native_pending = (proposal, session_id, revision, provider,
                                    identity, decision.action, deepcopy(decision.args))
        elif getattr(self.config, 'action_protocol', 'json') == 'responses_native':
            self._authorize_program_decision(decision)
        return decision, ''

    def _authorize_program_decision(self, decision: Decision) -> None:
        self._program_pending = (self.session_id, self.revision, self.llm,
                                 self.backend_identity, decision.action, deepcopy(decision.args))

    def apply_decision(self, decision: Decision) -> dict[str, Any] | None:
        """Execute one accepted decision; return a terminal result for ASK/FINISH.

        The decision is re-checked against the state that is current *now*: the
        budget may have moved, the ambiguity may have been resolved, or the
        session may have been reloaded since the decision was chosen. An
        unaccepted or unknown action must never reach a tool, and a budget that
        ran out keeps its own `BudgetExceeded` semantics.
        """
        pending = getattr(self, '_native_pending', None)
        if pending is not None:
            self._native_pending = None
        program = getattr(self, '_program_pending', None)
        if self._native_operation_open() and (
                getattr(self, '_native_owned_revision', None) != self.revision
                or getattr(self.config, 'action_protocol', 'json') != 'responses_native'
                or (pending is None and program is None)):
            raise PolicyError('Unresolved native operation cannot be replayed.')
        if getattr(getattr(self, 'config', None), 'action_protocol', 'json') == 'responses_native':
            self._program_pending = None
            if pending is None:
                if (program is None or not isinstance(decision, Decision)
                        or program != (self.session_id, self.revision, self.llm,
                                       self.backend_identity, decision.action, decision.args)):
                    raise PolicyError('Decision has no current native authorization.')
        try:
            if (getattr(getattr(self, 'config', None), 'action_protocol', 'json') == 'responses_native'
                    and isinstance(decision, Decision) and decision.action in ('SEARCH', 'ASSESS')):
                expected_revision = pending[2] if pending is not None else program[1]
                self._native_operation_start(expected_revision=expected_revision)
                if self.revision != expected_revision:
                    raise PolicyError('Native authorization revision changed.')
            result = self._apply_decision_native_guarded(decision, pending)
            if (getattr(getattr(self, 'config', None), 'action_protocol', 'json') == 'responses_native'
                    and isinstance(decision, Decision) and decision.action in ('ASK', 'FINISH')):
                self._native_operation_close()
            return result
        except BaseException:
            if pending is not None and isinstance(decision, Decision) and decision.action not in ('ASK', 'FINISH'):
                self._native_continuation_event('not_continued')
            raise
        finally:
            if pending is not None:
                self._native_pending = None
                if isinstance(decision, Decision) and decision.action in ('ASK', 'FINISH'):
                    self._native_continuation_event('not_continued')

    def _native_continuation_event(self, status: str) -> None:
        try:
            self._event('native_continuation', {'status': status})
        except Exception:
            self._warnings.append('Native continuation audit unavailable.')

    def _apply_decision_native_guarded(self, decision: Decision, pending: Any) -> dict[str, Any] | None:
        self.budget.check_cancelled()
        if not isinstance(decision, Decision) or not decision.accepted:
            raise PolicyError('Only an accepted decision may be executed.')
        if pending is not None:
            _, session_id, revision, provider, identity, action, args = pending
            if (self.session_id != session_id or self.revision != revision or self.llm is not provider
                    or self.backend_identity != identity or research_model_identity(self.config) != identity
                    or getattr(self.config, 'action_protocol', 'json') != 'responses_native'
                    or decision.action != action or decision.args != args):
                raise PolicyError('Native decision binding changed.')
        if decision.action not in ACTIONS:
            raise PolicyError(f'Unknown action: {decision.action}')
        _assert_action_budget(self, decision)
        state = self.control_state()
        checked = validate_issued_decision(decision, state)
        if not checked.accepted:
            raise PolicyError(f'Action rejected by the policy: {checked.error}')
        if decision.action == 'ASK':
            self._ask(Decision('ASK', checked.args, True, decision.reason))
            return self._result('awaiting_user', 'needs_clarification',
                                self.select(), question=checked.args.get('question'))
        if decision.action == 'SEARCH':
            event_start = len(self.store.events(self.session_id))
            self._do_search(checked.args['query'], providers=checked.args.get('providers'),
                            intent=checked.args.get('intent', ''))
            if pending is not None and self._continue_native(pending, 'SEARCH', event_start):
                self._native_operation_close()
            elif pending is None and getattr(self.config, 'action_protocol', 'json') == 'responses_native':
                events = self.store.events(self.session_id)[event_start:]
                latest = next((e for e in reversed(events) if e['action'] == 'search'), None)
                if latest and latest['payload'].get('status') in ('success', 'ok', 'empty'):
                    self._native_operation_close()
            return None
        if decision.action == 'ASSESS':
            ids = checked.args['paper_ids']
            force = decision.args.get('force', False)
            if not isinstance(force, bool):
                raise PolicyError('Forced reassessment must be a boolean.')
            if force:
                _assert_forced_authorized(self, ids)
            event_start = len(self.store.events(self.session_id))
            self._do_assess(ids, force=force)
            if force:
                _release_forced_authorization(self, ids)
            if pending is not None and self._continue_native(pending, 'ASSESS', event_start):
                self._native_operation_close()
            elif pending is None and getattr(self.config, 'action_protocol', 'json') == 'responses_native':
                events = self.store.events(self.session_id)[event_start:]
                if any(e['action'] == 'assess' and e['payload'].get('assessed', 0) > 0 for e in events):
                    self._native_operation_close()
            return None
        if decision.action == 'FINISH':
            return self._finish()
        return None

    def _continue_native(self, pending: Any, action: str, event_start: int) -> bool:
        proposal, session_id, revision, provider, identity, _, _ = pending
        status = 'not_continued'
        try:
            if self.cancel_requested:
                raise RunCancelled()
            if (self.session_id != session_id or self.revision != revision
                    or self.llm is not provider or self.backend_identity != identity
                    or research_model_identity(self.config) != identity
                    or getattr(self.config, 'action_protocol', 'json') != 'responses_native'):
                raise PolicyError('Native continuation state changed.')
            if self.limits()['llm_left'] <= 0 or self._seconds_left() <= 0:
                raise BudgetExceeded('Native continuation budget unavailable.')
            session = self.store.get_session(self.session_id) or {}
            if session.get('status') in ('finished', 'failed', 'awaiting_user', 'archived'):
                return
            events = self.store.events(self.session_id)[event_start:]
            if action == 'SEARCH':
                latest = next((e for e in reversed(events) if e['action'] == 'search'), None)
                if not latest or latest['payload'].get('status') not in ('success', 'ok', 'empty'):
                    return
            elif not any(e['action'] == 'assess' and e['payload'].get('assessed', 0) > 0
                         for e in events):
                return
            state = self.control_state()
            summary = json.dumps({'action': action, 'status': 'completed',
                                  'candidates': state.candidates, 'pending': state.pending,
                                  'gaps': len(state.gaps)}, separators=(',', ':'))
            with self._llm_stage():
                handle = self.meter.reserve('action', 1.0)
                try:
                    result = self.llm.continue_action(proposal, summary)
                except BaseException:
                    self.meter.settle(handle, 0.0)
                    raise
                self.meter.settle(handle, 1.0)
            status = 'completed' if result.get('status') == 'completed' else 'invalid'
            return status == 'completed'
        except RunCancelled:
            status = 'cancelled'
            raise
        except BudgetExceeded:
            status = 'budget_exceeded'
            raise
        except GuideError:
            status = 'failed'
            raise GuideError('Native continuation failed; prior results were retained.') from None
        finally:
            self._native_continuation_event(status)

    def turn(self, *, deadline_seconds: float | None = None) -> dict[str, Any]:
        session = self.store.get_session(self.session_id)
        if session is None:
            raise GuideError('Session no longer exists.')
        if session['status'] == 'awaiting_user':
            return self._partial('awaiting_user')
        if not self._turn_open:
            self._begin_turn_metrics()
        if self.cancel_requested:
            return self._partial('user_cancelled')
        if self._native_operation_open():
            return self._partial('pending_operation_not_replayed')
        if self.agent_runtime is not None:
            return self.agent_runtime.run_turn()
        return self._legacy_turn(deadline_seconds=deadline_seconds)

    def _legacy_turn(self, *, deadline_seconds: float | None = None) -> dict[str, Any]:
        terminal = self.prepare_turn()
        if terminal is not None:
            return terminal
        stop_reason = 'action_budget_exhausted'
        try:
            while True:
                decision, stop_reason = self.next_decision()
                if decision is None:
                    break
                delivered = self.apply_decision(decision)
                if delivered is not None:
                    return delivered
                if self._native_operation_open():
                    stop_reason = 'pending_operation_not_replayed'
                    break
        except RunCancelled:
            stop_reason = 'user_cancelled'
        except GuideError as exc:
            self._warnings.append(str(exc))
            self._event('error', {'error': str(exc), 'type': type(exc).__name__})
            stop_reason = self._error_stop_reason(exc)
        return self._partial(stop_reason)

    def _error_stop_reason(self, exc: GuideError) -> str:
        if self.cancel_requested or isinstance(exc, RunCancelled):
            return 'user_cancelled'
        if isinstance(exc, PolicyError):
            return 'policy_rejected'
        if isinstance(exc, BudgetExceeded):
            if self.limits()['llm_left'] <= 0:
                return 'llm_budget_exhausted'
            if self._seconds_left() <= 0:
                return 'time_budget_exhausted'
            return 'budget_exhausted'
        return 'model_or_tool_error'

    def _partial(self, stop_reason: str) -> dict[str, Any]:
        if stop_reason == 'awaiting_user':
            last_ask = next((event for event in reversed(self.store.events(self.session_id))
                             if event['action'] == 'ask'), None)
            question = (last_ask or {}).get('payload', {}).get('question')
            return self._result('awaiting_user', 'needs_clarification', self.select(), question=question)
        task = self.task()
        selected = self.select()
        check = verify_delivery(self.store, self.session_id, [p['id'] for p in selected], task,
                                model=self.model, prompt_version=self.prompt_version, research=self.config.research_mode)
        delivered = [p for p in selected if p['id'] in check.plan.valid] if check.ok else []
        state = self.control_state()
        keep = ('model_or_tool_error', 'llm_budget_exhausted', 'feedback_budget_exhausted',
                'user_cancelled', 'no_legal_action', 'policy_rejected')
        if stop_reason == 'pending_operation_not_replayed':
            keep += ('pending_operation_not_replayed',)
        reason = stop_reason if stop_reason in keep else (partial_reason(state) or stop_reason)
        return self._deliver('partial' if delivered else 'no_results', reason, delivered)

    def _forced(self, fallback: str | None, state: ControlState) -> Decision | None:
        """Build one legal fallback decision, or None when none is legal.

        The fallback is not a licence to finish: every candidate goes through
        the same action and argument gate as a model decision. An empty or
        repeated query, a paper outside the candidate pool, or a FINISH that the
        policy does not allow are refused. When no legal fallback exists the
        caller stops with `no_legal_action` instead of pretending the task is
        complete.
        """
        legal = allowed_actions(state)
        order: list[str] = []
        if fallback in ACTIONS and fallback in legal:
            order.append(fallback)
        for action in ('ASSESS', 'SEARCH', 'FINISH'):
            if action in legal and action not in order:
                order.append(action)
        for action in order:
            current = state
            decision: Decision | None = None
            if action == 'ASSESS':
                pool = set(state.candidate_ids)
                ids = [paper_id for paper_id in list(self._plan().stale_ids[:10])
                       if paper_id in pool]
                if ids:
                    decision = Decision('ASSESS', {'paper_ids': ids}, True, 'program fallback')
            elif action == 'SEARCH':
                query = self._refine_query()
                if query:
                    # Refinement costs time and budget, so the gate is checked
                    # again against the state that is current now.
                    current = self.control_state()
                    if ('SEARCH' in allowed_actions(current)
                            and not _query_seen(query, current, _query_history(self))):
                        decision = Decision('SEARCH', {'query': query}, True, 'program fallback')
            elif action == 'FINISH':
                decision = Decision('FINISH', {}, True, 'program fallback')
            if decision is None:
                continue
            checked = validate_issued_decision(decision, current)
            if checked.accepted:
                return decision
        return None

    def _refine_query(self) -> str:
        task = self.task()
        try:
            with self._llm_stage():
                raw = run_decision('refine', self.llm,
                                   {'topic': task.get('topic', ''),
                                    'previous_queries': [q['query'] for q in self.store.query_history(self.session_id)],
                                    'gaps': self.gaps()},
                                   prompts.QUERY_SCHEMA, lambda value: None,
                                   prompts.REFINE, self.meter)
        except GuideError:
            return ''
        return ' '.join(str(raw.get('query') or '').split())[:250]

    def _do_search(self, query: str, *, providers: list[str] | None = None, intent: str = '') -> None:
        self.budget.check_cancelled()
        limits = self.limits()
        if (limits['searches_left'] <= 0 or limits['searches_left_phase'] <= 0
                or limits['candidates_left'] <= 0 or self._seconds_left() <= 0):
            raise BudgetExceeded('Search budget exhausted.')
        normalized = ' '.join(query.split()).casefold()
        if normalized in {' '.join(row['query'].split()).casefold()
                          for row in self.store.query_history(self.session_id)}:
            raise GuideError('Repeated query rejected; choose a new search query.')
        self._sync_http_deadline()
        task = self.task()
        router_observation = None
        pending_error = None
        authorized = list(getattr(self.source, 'providers', []))
        if providers is not None and authorized:
            usable = [name for name in providers if name in authorized]
            if not usable:
                self._event('source_hint_rejected', {'requested': providers, 'authorized': authorized,
                            'reason': 'Model hint cannot override the user source allow-list.'})
            providers = usable or None
        if self.router is not None:
            from research_landscape import routing_task
            from research_runtime import landscape_plan
            discovery_task = routing_task(task, landscape_plan(self)) if self.config.research_mode else task
            result = self.router.retrieve(discovery_task, query, limit=10, meter=self.meter,
                **({'providers': providers, 'intent': intent} if self.config.research_mode else {}))
            papers = result.papers
            router_observation = result.observation()
            errors = result.errors
            from retrieval import unresolved_paid_error
            pending_error = unresolved_paid_error(result)
            status = ('partial' if pending_error is not None else
                      'ok' if papers else 'error' if errors else 'empty')
        else:
            try:
                outcome = run_search(self.source, query, per_search=10, meter=self.meter)
            except GuideError as exc:
                from source import retained_partial_batch
                partial = retained_partial_batch(exc, query, getattr(self.source, 'providers', ()), 10)
                if partial is None:
                    if paid_cost_may_be_unknown(self.source, exc):
                        self._event('source_cost_unknown', {'cost_cny': None})
                    raise
                pending_error = exc
                outcome = ToolOutcome(True, partial.papers,
                                      {'status': 'partial', 'stop_reason': partial.stop_reason},
                                      cost=1.0, cost_cny=None)
            papers = outcome.items
            errors = [str(outcome.observation.get('error'))] if not outcome.ok else []
            status = outcome.observation.get('status')
            if pending_error is None:
                from retrieval import unresolved_paid_error
                pending_error = unresolved_paid_error(outcome.observation)
        with self.store.tx():
            new = 0
            limits = self.limits()
            existing = {ref['paper_id'] for ref in self.store.candidate_refs(self.session_id)}
            for paper in papers:
                if paper.id not in existing:
                    if new >= limits['candidates_left']:
                        continue
                    existing.add(paper.id)
                    new += 1
                self.store.add_candidate(self.session_id, paper.to_dict(), query)
            self._consume(searches_left=1, searches_left_phase=1, candidates_left=new)
            self.store.record_query(self.session_id, self.revision, len(self._trace) + 1, query,
                                    len(papers), new)
            event = {'query': query, 'status': status, 'returned': len(papers), 'new': new,
                     'error': '; '.join(x for x in errors if x)[:500]}
            if self.router is None:
                amount = getattr(outcome, 'cost_cny', None)
                event['cost_cny'] = None if amount is None else float(amount)
                if pending_error is not None:
                    event['stop_reason'] = outcome.observation.get('stop_reason') or 'paid_outcome_unresolved'
            if router_observation is not None:
                event['router'] = router_observation
                if pending_error is not None:
                    event['stop_reason'] = 'paid_outcome_unresolved'
            self._event('search', event)
        if pending_error is not None:
            raise pending_error
        if not papers and errors:
            self._warnings.extend(str(x) for x in errors if x)

    def _take_forced_reassessment(self) -> list[str]:
        """Consume an explicit re-assessment request, validated against the pool.

        The request is *moved* into the program authorization list. Taking it
        must not silently change what is legal: the papers stay pending work
        until they are actually assessed, and the authorization is scoped to
        exactly those papers.
        """
        revision = self.revision
        refs = {row['paper_id'] for row in self.store.candidate_refs(self.session_id)}
        pending = ([paper_id for paper_id in self._forced_reassess if paper_id in refs]
                   if self._forced_reassess_revision == revision else [])
        authorized = ([paper_id for paper_id in self._forced_authorizations if paper_id in refs]
                      if self._forced_authorization_revision == revision else [])
        combined = list(dict.fromkeys([*authorized, *pending]))[:self.config.batch_size]
        self._forced_reassess = []
        self._forced_reassess_revision = None
        if not combined:
            return []
        self._forced_authorizations = combined
        self._forced_authorization_revision = revision
        return list(combined)

    def request_reassessment(self, paper_ids: Sequence[str]) -> list[str]:
        """Ask for an explicit re-assessment of specific candidates.

        This is the only channel that may re-assess a paper whose judgement is
        already valid for the current task. The model cannot request it, and
        the request is still bounded by the normal action allowance, the batch
        size and the task budget.
        """
        refs = {row['paper_id'] for row in self.store.candidate_refs(self.session_id)}
        wanted = list(dict.fromkeys(str(item) for item in paper_ids if str(item) in refs))
        if not wanted:
            return []
        refs = {row['paper_id'] for row in self.store.candidate_refs(self.session_id)}
        wanted = list(dict.fromkeys(str(item) for item in paper_ids if str(item) in refs))
        if not wanted:
            return []
        revision = self.revision
        pending = (list(self._forced_reassess)
                   if self._forced_reassess_revision == revision else [])
        authorized = (list(self._forced_authorizations)
                      if self._forced_authorization_revision == revision else [])
        combined = list(dict.fromkeys([*authorized, *pending, *wanted]))[:self.config.batch_size]
        if authorized:
            self._forced_authorizations = combined
            self._forced_authorization_revision = revision
            self._forced_reassess = []
            self._forced_reassess_revision = None
        else:
            self._forced_reassess = combined
            self._forced_reassess_revision = revision
        self._event('assessment_requested', {'paper_ids': list(combined),
                                             'reason': 'explicit_reassessment_requested'})
        return list(combined)

    def _coverage_search(self, state) -> Decision | None:
        """A program search scoped by the coverage specialist (0 model calls).

        The specialist plans at most one uncovered facet; the search runs
        inside the remaining search allowance, so no extra round is added.
        """
        from research_runtime import current_landscape
        from research_specialist import coverage_specialist, session_ticket
        try:
            landscape = current_landscape(self)
        except GuideError as exc:
            self._warnings.append(f'coverage specialist unavailable: {exc}')
            return None
        if not landscape or landscape.get('scope') != 'landscape':
            return None
        selected = self.select()
        ticket = session_ticket('coverage_specialist', self, selected,
                                trigger='planned_facet_missing')
        previous = [row['query'] for row in self.store.query_history(self.session_id)]
        result = coverage_specialist(ticket, landscape, selected, previous,
                                     searches_left=state.budget.searches_left)
        self._event('specialist_ticket', {
            'ticket_id': ticket.ticket_id, 'role': ticket.role, 'trigger': ticket.trigger,
            'status': result.status, 'gap_ids': list(result.gap_ids),
            'failure_code': result.failure_code, 'model_calls': 0,
            'findings': [dict(item) for item in result.findings]})
        if result.status != 'ok' or not result.findings:
            return None
        query = str(result.findings[0].get('query') or '')
        if not query:
            return None
        return Decision('SEARCH', {'query': query, 'intent': 'fill_gap'}, True,
                        'coverage_specialist')

    def _do_assess(self, ids: Sequence[str], *, force: bool = False) -> None:
        task = self.task()
        request = self._request(task)
        semantic = self._semantic(task)
        refs = {row['paper_id']: row for row in self.store.candidate_refs(self.session_id)}
        pending = list(dict.fromkeys(paper_id for paper_id in ids if paper_id in refs))
        rejected = [paper_id for paper_id in ids if paper_id not in refs]
        if rejected:
            raise GuideError(f'ASSESS received ids outside the candidate pool: {rejected}')
        if not force:
            stale = set(self._plan().stale_ids)
            skipped = [paper_id for paper_id in pending if paper_id not in stale]
            pending = [paper_id for paper_id in pending if paper_id in stale]
            if skipped:
                self._event('assessment_reuse', {'paper_ids': skipped,
                    'reason': 'same_task_and_abstract_already_verified'})
                if not pending:
                    return
        elif force:
            self._event('assessment_forced', {'paper_ids': list(pending),
                'reason': 'explicit_reassessment_requested'})
        if not pending:
            pending = self._plan().stale_ids[:10]
        papers = self._papers(pending)
        from research_runtime import landscape_plan
        planned = landscape_plan(self) if self.config.research_mode else None
        assessment_size = min(self.config.batch_size, 3) if planned and planned.get('scope') == 'landscape' else self.config.batch_size
        for start in range(0, len(papers), assessment_size):
            batch = papers[start:start + assessment_size]
            with self._llm_stage():
                outcome = assess_batch(self.llm, batch, topic=request.topic, category=request.category,
                                       category_definition=prompts.CATEGORY_DEFINITIONS[request.category],
                                       purpose=getattr(request, 'purpose', ''),
                                       prompt=prompts.ASSESS_RESEARCH if self.config.research_mode else prompts.ASSESS,
                                       schema=prompts.ASSESS_RESEARCH_SCHEMA if self.config.research_mode else prompts.ASSESS_SCHEMA,
                                       validate=workflow.validate_research_assessments if self.config.research_mode else workflow.validate_assessments,
                                       meter=self.meter, research_plan=planned)
            grounded = workflow.grounded_assessments(outcome.items, batch, request.category)
            with self.store.tx():
                for paper in batch:
                    row = grounded.get(paper.id)
                    if row is None:
                        continue
                    self.store.put_assessment(
                        self.session_id, paper_id=paper.id, content=refs[paper.id]['content_hash'],
                        semantic=semantic,
                        computed={'content_hash': refs[paper.id]['content_hash'],
                                  'semantic_key': semantic, 'model': self.model,
                                  'prompt_version': self.prompt_version},
                        relevance=int(row['relevance']), category_status=row['category_status'],
                        paper_type=row.get('paper_type', 'unknown'),
                        fit_score=row.get('fit_score', 50),
                        evidence_quote=row.get('evidence_quote', ''),
                        evidence_verified=bool(row.get('evidence_verified')),
                        understanding_sentence=row.get('reason'),
                        unknown_reason=row.get('warning'),
                    )
                for paper in batch:
                    self._forced_stale.discard(paper.id)
                from research_landscape import facet_observations
                self._event('assess', {'assessed': len(batch), 'verified': sum(
                    1 for paper in batch if grounded.get(paper.id, {}).get('evidence_verified')),
                    **({'facet_task_key':planned['task_key'],
                        'facet_observations':facet_observations(outcome.items,batch,planned)} if planned else {})})

            if self.config.research_mode and self.mode != 'demo' and not self.cancel_requested:
                from research_runtime import refresh_landscape
                refresh_landscape(self)
                if not self.gaps():
                    remaining = len(papers) - start - len(batch)
                    if remaining:
                        self._event('assessment_complete', {'skipped_remaining': remaining,
                            'reason': 'requirements_and_planned_coverage_met'})
                    break

        if self.config.research_mode and self.mode != 'demo':
            from research_runtime import refresh_landscape
            refresh_landscape(self)

    def _ask(self, decision: Decision) -> None:
        with self.store.tx():
            self.store.set_status(self.session_id, 'awaiting_user')
            self._pause_clock()
            self._consume(asks_left_phase=1)
            self._event('ask', {'question': decision.args.get('question', ''),
                                'field': decision.args.get('affected_field', '')})

    def _finish(self) -> dict[str, Any] | None:
        task = self.task()
        selected = self.select()
        check: DeliveryCheck = verify_delivery(
            self.store, self.session_id, [p['id'] for p in selected], task,
            model=self.model, prompt_version=self.prompt_version, research=self.config.research_mode)
        self._event('delivery_check', {'ok': check.ok, 'reason': check.reason,
                                       'stale': len(check.stale_ids),
                                       'reuse_rate': round(check.plan.reuse_rate, 4)})
        if not check.ok:
            self._warnings.append(f'delivery refused: {check.reason}')
            self._event('delivery_refused', {'reason': check.reason,
                                             'stale': list(check.stale_ids),
                                             'unsupported': list(check.unsupported_ids)})
            return None
        assert_delivery_consistent(check)
        gaps = self.gaps()
        if gaps:
            reason = partial_reason(self.control_state()) or 'requirements_unmet'
            return self._deliver('partial' if selected else 'no_results', reason, selected)
        return self._deliver('complete', 'requirements_met', selected)

    # -- result ------------------------------------------------------------
    def _deliver(self, status: str, stop_reason: str,
                 papers: list[dict[str, Any]]) -> dict[str, Any]:
        if self.cancel_requested or stop_reason == 'user_cancelled':
            status = 'partial' if papers else 'no_results'
            stop_reason = 'user_cancelled'
        payload = {'status': status,
                   'papers': [{'index': i + 1, 'paper_id': p['id'], 'title': p['title']}
                              for i, p in enumerate(papers)]}
        with self.store.tx():
            self.store.set_status(self.session_id, 'finished')
            if stop_reason == 'user_cancelled':
                self._event('cancelled', {'reason': stop_reason,
                                          'verified_papers': len(papers),
                                          'message': self._message})
            self._event('delivered', payload)
        return self._result(status, stop_reason, papers)

    def _result(self, status: str, stop_reason: str, papers: list[dict[str, Any]], *,
                question: str | None = None) -> dict[str, Any]:
        task = self.task()
        brief = None
        if self.config.research_mode and status in ('complete', 'partial', 'no_results'):
            from research_runtime import attach_brief
            brief = attach_brief(self, papers)
        # Synthesis can receive a Stop after selection and delivery admission.
        # Reconcile the terminal status and audit event after that stage too.
        if self.cancel_requested and status != 'awaiting_user':
            status = 'partial' if papers else 'no_results'
            stop_reason = 'user_cancelled'
            turn_events = self.store.events(self.session_id)[self._turn_event_start:]
            if not any(event['action'] == 'cancelled' for event in turn_events):
                self._event('cancelled', {'reason': stop_reason, 'verified_papers': len(papers),
                                          'message': self._message})
            delivered = next((event for event in reversed(turn_events) if event['action'] == 'delivered'), None)
            if delivered and delivered['payload'].get('status') != status:
                self._event('delivery_status', {'status': status, 'reason': stop_reason})
        metrics = self._turn_metrics()
        events = self.store.events(self.session_id)
        turn_events = events[self._turn_event_start:]
        last_reuse = next((e for e in reversed(turn_events) if e['action'] == 'reuse'), None)
        ledger = self.store.con.execute(
            'SELECT reserved, settled, refunded FROM budget_ledger WHERE session_id = ?',
            (self.session_id,)).fetchall()
        assert_budget_conservation([dict(row) for row in ledger])
        limits = self.limits()
        metrics.update({'search_rounds': metrics['turn_queries'],
                        'actions': len(self._trace) - self._turn_trace_start,
                        'phase': limits['phase'],
                        'actions_left': limits['actions_left'],
                        'searches_left': limits['searches_left'],
                        'reuse': last_reuse['payload'] if last_reuse else None,
                        'asks': sum(1 for e in turn_events if e['action'] == 'ask')})
        result = {
            'schema_version': 2,
            'run_id': uuid.uuid4().hex,
            'started_at': utc_now(),
            'method': 'V2',
            'mode': self.mode,
            'config': self.config.public(),
            'user_input': self._message,
            'request': task,
            'assumptions': list(task.get('assumptions') or []),
            'unsupported': list(task.get('unsupported') or []),
            'status': status,
            'stop_reason': stop_reason,
            'queries': self.store.query_history(self.session_id),
            'papers': papers,
            'candidates': self.store.candidate_papers(self.session_id),
            'assessments': self._assessments(task),
            'gaps': self.gaps(),
            'trace': self._trace,
            'warnings': self._warnings,
            'metrics': metrics,
            'session_id': self.session_id,
            'revision': self.revision,
        }
        if self.config.research_mode:
            from research_runtime import current_landscape
            from research_landscape import summary as landscape_summary
            result['research_coverage'] = landscape_summary(current_landscape(self), papers)
        if question:
            result['question'] = question
        if brief is not None:
            result['research_brief'] = brief
        apply_cost(result, self.config, self._trace[self._turn_trace_start:])
        health = (self.source.health_snapshot()
                  if hasattr(self.source, 'health_snapshot') else {})
        result['diagnosis'] = diagnose(
            request=task, gaps=result.get('gaps'), trace=self._trace,
            candidates=result.get('candidates'), selected=result.get('papers'),
            source_health=health, stop_reason=stop_reason, limits=self.limits())
        self._turn_open = False
        return result
