"""Pure tool adapters with explicit cost accounting.

Each function takes everything it needs as an argument (source, llm, prompts) and
records its own cost through a caller-supplied :class:`CostMeter`. Nothing here
reads config, environment or global state, so the same functions can be driven by
the single-pass workflow, the v2 controller and the tests.

Cost model (docs/DESIGN_INVARIANTS.md I5):

* one search call          = 1 unit
* one assessed paper       = 1 unit
* one plan/refine decision = 1 unit

A failed unit is refunded in full: ``reserved`` is charged up front, ``settled``
records what actually ran, and ``refunded = reserved - settled``.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from common import GuideError, REMOTE_OUTCOMES, RunCancelled
from costing import source_runs_cost

COST_UNITS = {'plan': 1.0, 'refine': 1.0, 'action': 1.0, 'update': 1.0, 'search': 1.0, 'assess': 1.0}


class CostError(GuideError):
    """A cost settlement violated the conservation rule."""


@dataclass
class CostEntry:
    action: str
    reserved: float
    settled: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def refunded(self) -> float:
        return self.reserved - self.settled


class CostMeter:
    """In-memory ledger with the same semantics as the SQLite budget_ledger."""

    def __init__(self) -> None:
        self.entries: list[CostEntry] = []

    def reserve(self, action: str, units: float, detail: Mapping[str, Any] | None = None) -> int:
        if units < 0:
            raise CostError('reserved units must be >= 0')
        self.entries.append(CostEntry(action=action, reserved=float(units),
                                      detail=dict(detail or {})))
        return len(self.entries) - 1

    def settle(self, handle: int, units: float) -> None:
        entry = self.entries[handle]
        if units < 0 or units > entry.reserved:
            raise CostError('settled units must satisfy 0 <= settled <= reserved')
        entry.settled = float(units)

    def refund(self, handle: int) -> None:
        self.entries[handle].settled = 0.0

    def summary(self) -> dict[str, Any]:
        by_action: dict[str, dict[str, float]] = {}
        for entry in self.entries:
            bucket = by_action.setdefault(entry.action, {'reserved': 0.0, 'settled': 0.0, 'refunded': 0.0})
            bucket['reserved'] += entry.reserved
            bucket['settled'] += entry.settled
            bucket['refunded'] += entry.refunded
        reserved = round(sum(e.reserved for e in self.entries), 6)
        settled = round(sum(e.settled for e in self.entries), 6)
        return {
            'reserved': reserved,
            'settled': settled,
            'refunded': round(reserved - settled, 6),
            'by_action': {k: {m: round(v, 6) for m, v in vals.items()} for k, vals in by_action.items()},
        }


@dataclass
class ToolOutcome:
    """The observable result of one tool call; ``cost`` is the settled unit count."""

    ok: bool
    items: list[Any]
    observation: dict[str, Any]
    cost: float
    refunded: float = 0.0
    cost_cny: float | None = 0.0


def search(source: Any, query: str, per_search: int, meter: CostMeter | None = None) -> ToolOutcome:
    """Run one search; network/API failures become observations, never exceptions."""
    meter = meter or CostMeter()
    handle = meter.reserve('search', COST_UNITS['search'], {'query': query})
    observation: dict[str, Any] = {'query': query}
    try:
        found = list(source.search(query, per_search))
    except RunCancelled as exc:
        from source import retained_partial_batch
        partial = retained_partial_batch(exc, query, getattr(source, 'providers', ()), per_search)
        meter.settle(handle, 1.0 if partial is not None else 0.0)
        raise
    except GuideError as exc:
        from source import retained_partial_batch
        partial = retained_partial_batch(exc, query, getattr(source, 'providers', ()), per_search)
        if partial is not None:
            meter.settle(handle, 1.0)
            raise
        meter.settle(handle, 0.0)
        providers = getattr(source, 'providers', ()) or ()
        possible_paid = (getattr(exc, 'remote_outcome', None) != 'not_started'
                         and any(name in ('lune', 'deepseek') for name in providers))
        cost_cny = None if possible_paid else 0.0
        remote = getattr(exc, 'remote_outcome', None)
        if isinstance(remote, str) and remote in REMOTE_OUTCOMES:
            observation['remote_outcome'] = remote
            observation['retry_safe'] = False
        if possible_paid:
            observation['paid_followups_blocked'] = True
            observation.setdefault('remote_outcome', 'unknown')
            observation['retry_safe'] = False
        observation.update({'status': 'error', 'error_type': type(exc).__name__,
                            'error': str(exc), 'retryable': False, 'cost_cny': cost_cny})
        return ToolOutcome(False, [], observation, cost=0.0, refunded=COST_UNITS['search'],
                           cost_cny=cost_cny)
    meter.settle(handle, COST_UNITS['search'])
    batch = getattr(source, 'last_batch', None)
    cost_cny = 0.0
    if batch is not None:
        cost_cny = source_runs_cost(getattr(batch, 'runs', []))
        observation['runs'] = [run.as_dict() for run in getattr(batch, 'runs', [])]
    observation.update({'status': 'success' if found else 'empty', 'returned': len(found),
                        'cost_cny': cost_cny})
    return ToolOutcome(True, found, observation, cost=COST_UNITS['search'], cost_cny=cost_cny)


def assess_batch(llm: Any, papers: Sequence[Any], *, topic: str, category: str,
                 category_definition: str, purpose: str = '', prompt: str, schema: Mapping[str, Any],
                 validate: Callable[[Any, Sequence[Any]], None],
                 meter: CostMeter | None = None, research_plan: dict | None = None) -> ToolOutcome:
    """Assess one batch. Exceptions propagate after the batch is refunded."""
    meter = meter or CostMeter()
    units = COST_UNITS['assess'] * len(papers)
    handle = meter.reserve('assess', units, {'batch': len(papers)})
    batch = list(papers)
    data = {
        'topic': topic,
        'purpose': purpose,
        'category': category,
        'category_definition': category_definition,
        'papers': [{'paper_id': p.id, 'title': p.title, 'abstract': p.abstract[:7000],
                    'venue': getattr(p, 'venue', ''),
                    'sources': list(getattr(p, 'sources', []) or []),
                    'abstract_source': getattr(p, 'abstract_source', ''),
                    'metadata_only': bool(getattr(p, 'metadata_only', False))} for p in batch],
    }
    batch_schema = deepcopy(dict(schema))
    batch_schema['properties']['assessments']['minItems'] = len(batch)
    batch_schema['properties']['assessments']['maxItems'] = len(batch)
    batch_schema['properties']['assessments']['items']['properties']['paper_id']['enum'] = [p.id for p in batch]
    planned = research_plan if research_plan and research_plan.get('scope') == 'landscape' else None
    if planned:
        from research_landscape import assessment_contract
        data['research_facets'] = planned['facets']
        batch_schema = assessment_contract(batch_schema, planned)
        prompt += ('\nIn this SAME assessment also return facet_ids for each paper. '
                   'Match only supplied research_facets and a substantive method described in that paper abstract. '
                   'Use the SAME short evidence_quote to support relevance and these facets; do not generate another quote. '
                   'Keep evidence_quote under 300 characters and reason under 100 characters. '
                   'The quote must name the specific mechanism for EACH claimed facet; a generic goal, '
                   'a broad survey introduction or merely using deep learning is insufficient. '
                   'Do not classify introductory mentions of related work. If no facet is supported by that quote, '
                   'return facet_ids=[]. Route labels are model judgements, not semantic proof.')
    try:
        response = llm.generate('assess', prompt, data, batch_schema,
                                lambda value: validate(value, batch))
    except GuideError:
        meter.settle(handle, 0.0)
        raise
    meter.settle(handle, units)
    rows = list(response['assessments'])
    if planned:
        from research_landscape import ground_facet_rows
        ground_facet_rows(rows, batch, planned)
    return ToolOutcome(True, rows, {'status': 'success', 'assessed': len(rows)}, cost=units)


def assess(llm: Any, papers: Sequence[Any], *, topic: str, category: str,
           category_definition: str, purpose: str = '', prompt: str, schema: Mapping[str, Any],
           validate: Callable[[Any, Sequence[Any]], None], batch_size: int,
           meter: CostMeter | None = None) -> ToolOutcome:
    """Assess every paper in fixed-size batches, preserving order."""
    meter = meter or CostMeter()
    rows: list[Any] = []
    batches = 0
    for start in range(0, len(papers), batch_size):
        batch = list(papers)[start:start + batch_size]
        outcome = assess_batch(llm, batch, topic=topic, category=category,
                               category_definition=category_definition, purpose=purpose,
                               prompt=prompt, schema=schema, validate=validate, meter=meter)
        rows.extend(outcome.items)
        batches += 1
    return ToolOutcome(True, rows, {'status': 'success', 'batches': batches,
                                    'assessed': len(rows)},
                       cost=COST_UNITS['assess'] * len(rows))


def decision(stage: str, llm: Any, payload: Mapping[str, Any], schema: Mapping[str, Any],
             validate: Callable[[Any], Any], prompt: str = '', meter: CostMeter | None = None) -> Any:
    """Run one plan/refine decision and charge one unit (refunded on failure)."""
    if stage not in ('plan', 'refine', 'action', 'update'):
        raise CostError(f'unknown decision stage: {stage}')
    meter = meter or CostMeter()
    handle = meter.reserve(stage, COST_UNITS[stage])
    try:
        value = llm.generate(stage, prompt, dict(payload), dict(schema), validate)
    except GuideError:
        meter.settle(handle, 0.0)
        raise
    meter.settle(handle, COST_UNITS[stage])
    return value
