"""Reuse and invalidation planning for task-dependent judgements.

The rule (docs/ARCHITECTURE.md §5, docs/DESIGN_INVARIANTS.md I2):

    content_hash   = hash(title, abstract)
    semantic_key   = hash(topic, purpose, category, model, prompt_version)
    assessment_key = (paper_id, content_hash, semantic_key)

`task_revision` is audit metadata and never decides reuse. This module turns that
rule into two observable objects:

* ``ReusePlan``      - per-paper reused/stale status with a reason
* ``DeliveryCheck``  - whether the current list may be delivered at all

Nothing here writes state except :func:`record_reuse`, which appends one event.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from common import GuideError
from state import StateStore, semantic_key


class ReuseError(GuideError):
    """A delivery was attempted while its judgements were not valid."""


@dataclass(frozen=True)
class ReuseItem:
    paper_id: str
    content_hash: str
    status: str          # 'reused' | 'stale'
    reason: str          # 'unchanged' | 'missing' | 'content_changed' | ...


@dataclass(frozen=True)
class ReusePlan:
    items: tuple[ReuseItem, ...]
    valid: Mapping[str, Mapping[str, Any]]
    expected_semantic_key: str
    model: str
    prompt_version: str

    @property
    def reused_ids(self) -> list[str]:
        return [item.paper_id for item in self.items if item.status == 'reused']

    @property
    def stale_ids(self) -> list[str]:
        return [item.paper_id for item in self.items if item.status == 'stale']

    @property
    def reuse_rate(self) -> float:
        if not self.items:
            return 0.0
        return len(self.reused_ids) / len(self.items)


@dataclass(frozen=True)
class DeliveryCheck:
    ok: bool
    stale_ids: tuple[str, ...]
    unsupported_ids: tuple[str, ...]
    reason: str
    plan: ReusePlan

    @property
    def stale_reuse(self) -> int:
        """Delivered papers whose judgement is not valid under the current task."""
        return len(self.stale_ids)


def _stale_reason(store: StateStore, paper_id: str, content: str, *,
                  model: str, prompt_version: str) -> str:
    rows = store.assessments_for_paper(paper_id)
    if not rows:
        return 'missing'
    same_content = [row for row in rows if row['content_hash'] == content]
    if not same_content:
        return 'content_changed'
    latest = same_content[0]
    if (latest.get('model') or '') != model:
        return 'model_changed'
    if (latest.get('prompt_version') or '') != prompt_version:
        return 'prompt_changed'
    return 'semantic_key_changed'


def plan_reuse(store: StateStore, session_id: str, task: Mapping[str, Any], *,
               model: str, prompt_version: str,
               paper_ids: Sequence[str] | None = None) -> ReusePlan:
    """Decide, per paper, whether the stored judgement still applies."""
    refs = store.candidate_refs(session_id)
    if paper_ids is not None:
        wanted = set(paper_ids)
        refs = [ref for ref in refs if ref['paper_id'] in wanted]
    valid = store.valid_assessments(session_id, [ref['paper_id'] for ref in refs], task=task,
                                    model=model, prompt_version=prompt_version)
    items: list[ReuseItem] = []
    for ref in refs:
        paper_id = ref['paper_id']
        content = ref['content_hash']
        if paper_id in valid:
            items.append(ReuseItem(paper_id, content, 'reused', 'unchanged'))
        else:
            items.append(ReuseItem(paper_id, content, 'stale',
                                   _stale_reason(store, paper_id, content, model=model,
                                                 prompt_version=prompt_version)))
    expected = semantic_key(str(task.get('topic') or ''), str(task.get('purpose') or ''),
                            str(task.get('category') or 'none'), model, prompt_version,
                            str(task.get('venue') or ''), task.get('year_from'),
                            task.get('year_to'))
    return ReusePlan(tuple(items), valid, expected, model, prompt_version)


def verify_delivery(store: StateStore, session_id: str, paper_ids: Sequence[str],
                    task: Mapping[str, Any], *, model: str,
                    prompt_version: str, research: bool = False) -> DeliveryCheck:
    """A list may be delivered only if every judgement is current and evidence-backed."""
    plan = plan_reuse(store, session_id, task, model=model, prompt_version=prompt_version,
                      paper_ids=paper_ids)
    unsupported: list[str] = []
    publication_failed = False
    candidate_papers = {row['paper_id']: row for row in store.candidate_papers(session_id)} if research else {}
    for paper_id in paper_ids:
        row = plan.valid.get(paper_id)
        if row is None:
            continue  # already counted as stale
        if int(row.get('relevance', 0)) < 1 or not int(row.get('evidence_verified', 0)):
            unsupported.append(paper_id)
        if research:
            from research_policy import publication_matches
            if not publication_matches(candidate_papers.get(paper_id, {}), task):
                publication_failed = True
                if paper_id not in unsupported:
                    unsupported.append(paper_id)
    stale = tuple(plan.stale_ids)
    if stale:
        reason = 'stale_assessments'
    elif publication_failed:
        reason = 'publication_constraints_unmet'
    elif unsupported:
        reason = 'unsupported_evidence'
    else:
        reason = ''
    return DeliveryCheck(ok=not stale and not unsupported, stale_ids=stale,
                         unsupported_ids=tuple(unsupported), reason=reason, plan=plan)


def record_reuse(store: StateStore, session_id: str, plan: ReusePlan,
                 revision: int | None = None) -> int:
    """Append one event explaining what was reused and why the rest was recomputed."""
    payload = {
        'reused': plan.reused_ids,
        'stale': [{'paper_id': item.paper_id, 'reason': item.reason}
                  for item in plan.items if item.status == 'stale'],
        'reuse_rate': round(plan.reuse_rate, 4),
        'semantic_key': plan.expected_semantic_key,
        'model': plan.model,
        'prompt_version': plan.prompt_version,
    }
    return store.append_event(session_id, 'reuse', payload, revision=revision)


def assert_delivery_consistent(check: DeliveryCheck) -> None:
    """I4: refuse delivery when any judgement is stale or unsupported."""
    if not check.ok:
        raise ReuseError(f'delivery refused: {check.reason}; stale={list(check.stale_ids)}; '
                         f'unsupported={list(check.unsupported_ids)}')
