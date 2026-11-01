"""Deterministic action gating for the stateful agent.

The model proposes; this module decides what is legal. Two rules carry most of
the weight:

* FINISH is not offered while a requirement gap remains and a productive action
  is still affordable. A model can therefore never converge early by itself.
* ASK is offered only for a blocking ambiguity that affects a field able to
  change the next action, and only while a costly search is still affordable.

Everything here is pure: give it a ControlState, get a decision back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from common import GuideError, SOURCE_NAMES
import prompts
from tools import CostMeter, decision as run_decision
from tool_governance import ToolGovernanceError, validate_arguments

ACTIONS = ('SEARCH', 'ASSESS', 'ASK', 'FINISH')
BLOCKING_FIELDS = ('topic', 'purpose', 'category', 'minimum_category_count')


class PolicyError(GuideError):
    """The requested transition is impossible in the current state."""


@dataclass(frozen=True)
class BudgetView:
    seconds_left: float = 0.0
    llm_left: int = 0
    searches_left: int = 0

    @property
    def alive(self) -> bool:
        return self.seconds_left > 0 and self.llm_left > 0


@dataclass(frozen=True)
class FactCounts:
    """The only inputs allowed to drive a confidence threshold (I3)."""
    verified: int = 0
    quota_covered: int = 0
    candidates: int = 0


@dataclass(frozen=True)
class ControlState:
    candidates: int = 0
    unassessed: int = 0
    stale: int = 0
    gaps: tuple[Mapping[str, Any], ...] = ()
    unresolved: tuple[Mapping[str, Any], ...] = ()
    budget: BudgetView = field(default_factory=BudgetView)
    awaiting_user: bool = False
    last_query: str = ''
    last_action: str = ''
    invalid_actions: int = 0
    unsatisfiable: bool = False
    candidate_ids: tuple[str, ...] = ()
    asks_left: int = 1
    metadata_eligible: int = 0
    preflight_blocked: bool = False

    @property
    def pending(self) -> int:
        return self.unassessed + self.stale


@dataclass(frozen=True)
class Decision:
    action: str
    args: dict[str, Any]
    accepted: bool
    reason: str = ''
    attempted: str = ''
    error: str = ''
    fallback: str | None = None


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------

def blocking_unresolved(state: ControlState) -> list[Mapping[str, Any]]:
    return [item for item in state.unresolved
            if item.get('blocking') is True and item.get('field') in BLOCKING_FIELDS]


def ask_permitted(state: ControlState) -> bool:
    """Asking must be able to prevent a wasted action, not just satisfy curiosity."""
    if state.awaiting_user or not state.budget.alive or state.asks_left <= 0:
        return False
    if not blocking_unresolved(state):
        return False
    # A clarification only pays for itself if a search could run otherwise.
    return state.budget.searches_left > 0


def _can_assess(state: ControlState) -> bool:
    return state.budget.llm_left > 0 and state.pending > 0 and not state.preflight_blocked


def _can_search(state: ControlState) -> bool:
    return state.budget.searches_left > 0 and state.budget.seconds_left > 0


def allowed_actions(state: ControlState) -> tuple[str, ...]:
    """Return the legal action set in preference order."""
    if state.awaiting_user:
        return ()
    out: list[str] = []
    if _can_assess(state):
        out.append('ASSESS')
    if _can_search(state):
        out.append('SEARCH')
    if ask_permitted(state):
        out.append('ASK')
    productive = _can_assess(state) or _can_search(state)
    if not state.gaps or not productive:
        out.append('FINISH')
    return tuple(out)


def action_rationale(state: ControlState) -> dict[str, str]:
    allowed = allowed_actions(state)
    reasons: dict[str, str] = {}
    if 'ASSESS' in allowed:
        reasons['ASSESS'] = 'candidates need a judgement under the current task'
    elif state.preflight_blocked:
        reasons['ASSESS'] = 'blocked by metadata preflight: no candidate can satisfy the hard constraints'
    if 'SEARCH' in allowed:
        reasons['SEARCH'] = 'budget allows one more search'
    if 'ASK' in allowed:
        reasons['ASK'] = 'a blocking ambiguity would otherwise waste a search'
    if 'FINISH' in allowed:
        reasons['FINISH'] = 'no gap remains' if not state.gaps else 'no productive action remains'
    return reasons


def fallback_action(state: ControlState, allowed: Sequence[str] | None = None) -> str | None:
    legal = tuple(allowed if allowed is not None else allowed_actions(state))
    for action in ('FINISH', 'ASSESS', 'SEARCH', 'ASK'):
        if action in legal:
            return action
    return None


def budget_exhausted(state: ControlState) -> bool:
    if not state.budget.alive:
        return True
    return state.budget.searches_left <= 0 and state.pending == 0


def partial_reason(state: ControlState) -> str:
    """Reason code for a partial delivery; empty when requirements are met."""
    if not state.gaps:
        return ''
    if state.unsatisfiable:
        return 'unsatisfiable'
    if budget_exhausted(state):
        return 'budget_exhausted'
    if state.candidates and any(g.get('requirement') == 'paper_count' for g in state.gaps):
        return 'source_lacks_topic'
    return 'quota_unmet'


# --------------------------------------------------------------------------
# Decision validation
# --------------------------------------------------------------------------

def _clean(value: Any) -> str:
    return ' '.join(str(value or '').split())


def blocking_fields(state: ControlState) -> tuple[str, ...]:
    """The fields that are blocking *right now*.

    A question may only be attached to a field that is still ambiguous and
    still able to change the next action. Several fields may block at once;
    each one of them is a legal target.
    """
    names: list[str] = []
    for item in blocking_unresolved(state):
        name = _clean(item.get('field'))
        if name and name not in names:
            names.append(name)
    return tuple(names)


def validate_decision(raw: Mapping[str, Any], state: ControlState,
                      allowed: Sequence[str] | None = None) -> Decision:
    legal = tuple(allowed if allowed is not None else allowed_actions(state))
    attempted = _clean(raw.get('action')).upper()
    reason = _clean(raw.get('reason'))

    def reject(error: str) -> Decision:
        return Decision(action='FINISH', args={}, accepted=False, reason=reason,
                        attempted=attempted, error=error,
                        fallback=fallback_action(state, legal))

    if attempted not in ACTIONS:
        return reject('unknown_action')
    if attempted not in legal:
        return reject('not_allowed')

    if attempted == 'SEARCH':
        query = _clean(raw.get('query'))
        if not query:
            return reject('empty_query')
        if len(query) > 250:
            return reject('query_too_long')
        if query.casefold() == _clean(state.last_query).casefold():
            return reject('repeated_query')
        args = {'query': query}
        if 'providers' in raw:
            providers = raw['providers']
            if (not isinstance(providers, list) or len(providers) > 2
                    or any(not isinstance(name, str) or name not in SOURCE_NAMES for name in providers)):
                return reject('invalid_search_providers')
            if providers:
                args['providers'] = list(dict.fromkeys(providers))
        if 'intent' in raw:
            intent = raw['intent']
            if intent not in ('', 'discovery', 'accepted', 'journal', 'survey', 'fill_gap'):
                return reject('invalid_search_intent')
            if intent:
                args['intent'] = intent
        return Decision('SEARCH', args, True, reason)

    if attempted == 'ASSESS':
        ids = [str(x).strip() for x in (raw.get('paper_ids') or []) if str(x).strip()]
        if not ids:
            return reject('no_paper_ids')
        if not state.candidate_ids:
            return reject('no_candidates')
        unknown = [paper_id for paper_id in ids if paper_id not in state.candidate_ids]
        if unknown:
            return reject('unknown_paper_ids')
        return Decision('ASSESS', {'paper_ids': ids}, True, reason)

    if attempted == 'ASK':
        question = _clean(raw.get('question'))
        field_name = _clean(raw.get('affected_field'))
        if not question:
            return reject('empty_question')
        if field_name not in BLOCKING_FIELDS:
            return reject('unknown_field')
        # A question is only worth asking when it targets an ambiguity that is
        # blocking *now*. A field that is already resolved, or that never
        # blocked, must not reopen a clarification round.
        if field_name not in blocking_fields(state):
            return reject('field_not_blocking')
        return Decision('ASK', {'question': question, 'affected_field': field_name}, True, reason)

    # FINISH is only ever legal when allowed_actions put it there.
    return Decision('FINISH', {}, True, reason)


def validate_issued_decision(decision: Any, state: ControlState,
                             allowed: Sequence[str] | None = None) -> Decision:
    """Re-run the policy on a decision that was already issued.

    A decision may only be executed when the same action, with the same
    arguments, passes the same gate in the state that is current at execution
    time. The state may have moved on since the decision was chosen (the
    budget was spent, the ambiguity was resolved, the session was reloaded),
    and an unaccepted or unknown action must never reach a tool.
    """
    action = _clean(getattr(decision, 'action', '')).upper()
    if getattr(decision, 'accepted', False) is not True:
        return Decision(action='FINISH', args={}, accepted=False, attempted=action,
                        error='unaccepted_decision', fallback=fallback_action(state, allowed))
    args = getattr(decision, 'args', None)
    raw: dict[str, Any] = dict(args) if isinstance(args, dict) else {}
    raw['action'] = action
    return validate_decision(raw, state, allowed)


def validate_shape(raw: Any) -> None:
    if not isinstance(raw, dict) or not isinstance(raw.get('action'), str):
        raise PolicyError('action response must be an object with a string action')


def action_payload(state: ControlState, observation: Mapping[str, Any] | None = None) -> dict:
    """Build the unchanged action context shared by both proposal protocols."""
    legal = allowed_actions(state)
    observation = dict(observation or {})
    # The task and the requirement gaps are sent once. They used to appear both
    # at the top level and inside the observation, which doubled the repeated
    # controller context without adding a single fact.
    task = observation.pop('task', {})
    gaps = observation.pop('gaps', [])
    payload = {
        'allowed': list(legal),
        'allowed_reasons': action_rationale(state),
        'task': task,
        'observation': observation,
        'budget': {'seconds_left': state.budget.seconds_left,
                   'llm_left': state.budget.llm_left,
                   'searches_left': state.budget.searches_left},
        'gaps': gaps,
        'unresolved': list(state.unresolved),
        'candidate_ids': list(state.candidate_ids),
    }
    return payload


def native_action_tools(state: ControlState) -> list[dict]:
    """Describe only actions currently authorized by the controller."""
    reason = {'type': 'string', 'maxLength': 1000}
    definitions = {
        'SEARCH': ('sg_search', 'Search for candidate papers.', {
            'query': {'type': 'string', 'minLength': 1, 'maxLength': 250},
            'providers': {'type': 'array', 'maxItems': 2,
                          'items': {'type': 'string', 'enum': list(SOURCE_NAMES)}},
            'intent': {'type': 'string', 'enum': ['', 'discovery', 'accepted', 'journal', 'survey', 'fill_gap']},
            'reason': dict(reason),
        }, ['query', 'reason']),
        'ASSESS': ('sg_assess', 'Assess candidate papers.', {
            'paper_ids': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                          'items': {'type': 'string', 'minLength': 1, 'maxLength': 256}},
            'reason': dict(reason),
        }, ['paper_ids', 'reason']),
        'ASK': ('sg_ask', 'Ask about a blocking ambiguity.', {
            'question': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
            'affected_field': {'type': 'string', 'enum': list(blocking_fields(state))},
            'reason': dict(reason),
        }, ['question', 'affected_field', 'reason']),
        'FINISH': ('sg_finish', 'Finish the current task.', {
            'reason': dict(reason),
        }, ['reason']),
    }
    tools = []
    for action in allowed_actions(state):
        name, description, properties, required = definitions[action]
        tools.append({'name': name, 'description': description,
                      'parameters': {'type': 'object', 'properties': properties,
                                     'required': required, 'additionalProperties': False}})
    return tools


def native_decision(name: str, args: Any, state: ControlState) -> Decision:
    """Validate a native proposal without executing it or trusting its action."""
    actions = {'sg_search': 'SEARCH', 'sg_assess': 'ASSESS',
               'sg_ask': 'ASK', 'sg_finish': 'FINISH'}
    if not isinstance(name, str) or name not in actions:
        raise PolicyError('unknown native action tool')
    action = actions[name]
    legal = allowed_actions(state)
    if action not in legal:
        return validate_decision({'action': action}, state, legal)
    schema = next(tool['parameters'] for tool in native_action_tools(state)
                  if tool['name'] == name)
    try:
        raw = validate_arguments(args, schema)
    except ToolGovernanceError:
        raise PolicyError('invalid native action arguments') from None
    raw['action'] = action
    return validate_decision(raw, state, legal)


def decide(llm: Any, state: ControlState, observation: Mapping[str, Any] | None = None,
           meter: CostMeter | None = None) -> Decision:
    """Ask the model for one action, then enforce the policy."""
    if state.awaiting_user:
        raise PolicyError('session is awaiting user input; no action may be chosen')
    legal = allowed_actions(state)
    if not legal:
        raise PolicyError('no legal action in the current state')
    payload = action_payload(state, observation)
    raw = run_decision('action', llm, payload, prompts.ACTION_SCHEMA, validate_shape,
                       prompts.ACTION, meter)
    return validate_decision(raw, state, legal)


def record_decision(store: Any, session_id: str, decision: Decision,
                    revision: int | None = None,
                    context: Mapping[str, Any] | None = None) -> int:
    """Persist a decision outcome; rejections become explicit `invalid_action` events."""
    action = 'invalid_action' if not decision.accepted else 'decision'
    payload = {
        'decision': decision.action,
        'attempted': decision.attempted or decision.action,
        'error': decision.error,
        'fallback': decision.fallback,
        'reason': decision.reason,
    }
    if context:
        payload['context'] = dict(context)
    return store.append_event(session_id, action, payload, revision=revision)


def assert_threshold_source(facts: Any) -> None:
    """I3: thresholds may only be computed from countable facts."""
    if not isinstance(facts, FactCounts):
        raise PolicyError('I3: confidence thresholds must be computed from FactCounts, not model scores.')


def assert_action_allowed(action: str, state: ControlState) -> None:
    """I6: an action is legal only if the program put it in the allowed set."""
    if action not in allowed_actions(state):
        raise PolicyError(f'I6: {action} is not allowed in this state.')


def assert_budget_conservation(entries: Sequence[Mapping[str, Any]]) -> None:
    """I5: every ledger row must satisfy settled + refunded == reserved."""
    for entry in entries:
        reserved = float(entry.get('reserved', 0))
        settled = float(entry.get('settled', 0))
        refunded = float(entry.get('refunded', 0))
        if settled < 0 or settled > reserved or abs(settled + refunded - reserved) > 1e-9:
            raise PolicyError(f'I5: ledger row violates conservation: {entry}')
