"""Bounded specialist tickets for research sessions.

A specialist is a named, bounded unit of work. It receives an immutable
ticket that names exactly the snapshots it may judge, and it returns a result
that may only cite paper and evidence identities that already exist.

Two specialists are deterministic and make no model calls:

``evidence_critic``
    Validates the evidence of a research brief against the delivered papers
    and the abstract snapshot that was handed to the synthesizer. It may only
    remove a claim and disclose a gap; it never adds content.

``coverage_specialist``
    Plans at most one uncovered research facet plus its query and source
    scope, to be executed inside the remaining search allowance. It adds no
    search round of its own.

The legacy ``claim_critic`` entry point is disabled; model-assisted review must
use the separately governed ``specialist_pilot`` path.

The coordinator stays the only writer of session state and the only holder of
the total budget. A ticket never mutates global history, and a result that
does not match the ticket's revision, selection, model and abstract snapshot is
rejected even when the identifiers themselves look valid.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from common import GuideError

ROLES = ('evidence_critic', 'coverage_specialist', 'claim_critic')
DETERMINISTIC_ROLES = ('evidence_critic', 'coverage_specialist')
MODEL_ROLES = ('claim_critic',)

MAX_CLAIM_CRITIC_REQUESTS = 1
MAX_CLAIM_CRITIC_OUTPUT_TOKENS = 1200
MAX_CLAIM_CRITIC_SECONDS = 45.0

# Sections whose claims describe exactly one delivered paper.
PAPER_BOUND_SECTIONS = ('paper_notes',)
# Every section that cites evidence.
CLAIM_SECTIONS = ('paper_notes', 'themes', 'open_questions', 'research_hypotheses')
# Fields of one paper note that may carry a claim.
NOTE_CLAIM_FIELDS = ('contribution', 'method', 'author_limitation', 'model_inference')

MAX_FINDINGS = 32


class SpecialistError(GuideError):
    """A specialist ticket or result violated its contract."""


@dataclass(frozen=True)
class Ticket:
    """An immutable description of one bounded unit of specialist work."""

    ticket_id: str
    role: str
    task_revision: int
    task_key: str
    selection_key: str
    model_identity: str
    paper_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    abstract_sha256: Mapping[str, str]
    trigger: str
    deadline: float
    max_requests: int
    max_output_tokens: int


@dataclass(frozen=True)
class SpecialistResult:
    """The bounded answer of one specialist."""

    ticket_id: str
    status: str
    findings: tuple[Mapping[str, Any], ...] = ()
    gap_ids: tuple[str, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    failure_code: str = ''


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------

def digest(payload: Any) -> str:
    """A stable digest of a JSON-compatible payload."""
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), default=str).encode('utf-8')).hexdigest()


def abstract_hashes(papers: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Map every supplied paper to the digest of the abstract that was seen."""
    out: dict[str, str] = {}
    for paper in papers or ():
        identity = str(paper.get('id') or paper.get('paper_id') or '')
        if not identity:
            continue
        out[identity] = hashlib.sha256(str(paper.get('abstract') or '').encode('utf-8')).hexdigest()
    return out


def selection_key(papers: Sequence[Mapping[str, Any]]) -> str:
    """Identify an exact selection together with its abstract snapshots."""
    return digest([(str(paper.get('id') or paper.get('paper_id') or ''),
                    hashlib.sha256(str(paper.get('abstract') or '').encode('utf-8')).hexdigest())
                   for paper in papers or ()])


def make_ticket(role: str, *, ticket_id: str | None = None, task_revision: int = 0,
                task_key: str = '', selection_key: str = '', model_identity: str = '',
                paper_ids: Sequence[str] = (), evidence_ids: Sequence[str] = (),
                abstract_sha256: Mapping[str, str] | None = None, trigger: str = '',
                deadline: float = 0.0, max_requests: int = 0,
                max_output_tokens: int = 0) -> Ticket:
    """Build an immutable ticket. A ticket names snapshots, never URLs."""
    if role not in ROLES:
        raise SpecialistError(f'unknown specialist role: {role}')
    identities = tuple(dict.fromkeys(str(item) for item in paper_ids if str(item)))
    if role in DETERMINISTIC_ROLES and (max_requests or max_output_tokens):
        raise SpecialistError(f'{role} is deterministic and may not request model calls.')
    if role in MODEL_ROLES:
        if max_requests < 0 or max_requests > MAX_CLAIM_CRITIC_REQUESTS:
            raise SpecialistError('claim_critic may request at most one model call.')
        if max_output_tokens < 0 or max_output_tokens > MAX_CLAIM_CRITIC_OUTPUT_TOKENS:
            raise SpecialistError('claim_critic output is capped at 1200 tokens.')
        if deadline < 0 or deadline > MAX_CLAIM_CRITIC_SECONDS:
            raise SpecialistError('claim_critic deadline is capped at 45 seconds.')
    identity = digest([role, task_revision, task_key, selection_key, identities, trigger])
    ticket_id = ticket_id or f'{role}-{identity[:16]}'
    return Ticket(ticket_id=ticket_id, role=role, task_revision=int(task_revision),
                  task_key=str(task_key), selection_key=str(selection_key),
                  model_identity=str(model_identity), paper_ids=identities,
                  evidence_ids=tuple(dict.fromkeys(str(item) for item in evidence_ids if str(item))),
                  abstract_sha256=dict(abstract_sha256 or {}), trigger=str(trigger),
                  deadline=float(deadline), max_requests=int(max_requests),
                  max_output_tokens=int(max_output_tokens))


def session_ticket(role: str, chat: Any, papers: Sequence[Mapping[str, Any]] = (), *,
                   trigger: str = '', evidence_ids: Sequence[str] = (), deadline: float = 0.0,
                   max_requests: int = 0, max_output_tokens: int = 0) -> Ticket:
    """Build a ticket for the current session, task and selection."""
    from model_identity import research_model_identity
    from research_landscape import task_key
    task = chat.task()
    model = research_model_identity(chat.config)
    return make_ticket(role, task_revision=int(chat.revision),
                       task_key=task_key(task, model), selection_key=selection_key(papers),
                       model_identity=model,
                       paper_ids=[str(paper.get('id') or '') for paper in papers],
                       evidence_ids=evidence_ids, abstract_sha256=abstract_hashes(papers),
                       trigger=trigger, deadline=deadline, max_requests=max_requests,
                       max_output_tokens=max_output_tokens)


def assert_result_matches(ticket: Ticket, result: SpecialistResult, *,
                           revision: int | None = None, model_identity: str | None = None,
                           papers: Sequence[Mapping[str, Any]] | None = None) -> None:
    """Reject a result that does not belong to the ticket's snapshots.

    Valid-looking identifiers are not enough: a result computed against another
    revision, another selection, another model or another abstract snapshot is
    rejected.
    """
    if result.ticket_id != ticket.ticket_id:
        raise SpecialistError(f'result belongs to another ticket: {result.ticket_id}')
    if revision is not None and int(revision) != int(ticket.task_revision):
        raise SpecialistError('result was produced for another task revision.')
    if model_identity is not None and str(model_identity) != ticket.model_identity:
        raise SpecialistError('result was produced by another model identity.')
    if papers is not None:
        if selection_key(papers) != ticket.selection_key:
            raise SpecialistError('result was produced for another selection.')
        current = abstract_hashes(papers)
        for identity, expected in ticket.abstract_sha256.items():
            if current.get(identity) != expected:
                raise SpecialistError(f'abstract snapshot changed for {identity}.')


# ---------------------------------------------------------------------------
# Deterministic critics
# ---------------------------------------------------------------------------

def _normal(text: Any) -> str:
    """Case-folded, whitespace-collapsed text for quote comparison."""
    return re.sub(r'\s+', ' ', str(text or '')).strip().casefold()


def _claim_sections(brief: Mapping[str, Any]) -> list[tuple[str, int, str, Mapping[str, Any]]]:
    """Every evidence-bearing claim of a stored brief.

    Returns (section, section index, owner paper id, claim). A stored claim carries its
    materialized evidence, not the model's evidence ids.
    """
    out: list[tuple[str, int, str, Mapping[str, Any]]] = []
    for index, note in enumerate(brief.get('paper_notes') or ()):
        if not isinstance(note, dict):
            raise SpecialistError('Invalid paper note.')
        owner = str(note.get('paper_id') or '')
        for field in NOTE_CLAIM_FIELDS:
            claim = note.get(field)
            if isinstance(claim, dict):
                out.append((f'paper_notes.{field}', index, owner, claim))
            elif claim is not None:
                raise SpecialistError('Invalid paper claim.')
    for section in ('themes', 'open_questions', 'research_hypotheses'):
        for index, item in enumerate(brief.get(section) or ()):
            if isinstance(item, dict):
                out.append((section, index, '', item))
            else:
                raise SpecialistError('Invalid research claim.')
    return out


def evidence_critic(ticket: Ticket, papers: Sequence[Mapping[str, Any]],
                     brief: Mapping[str, Any]) -> SpecialistResult:
    """Check every cited claim against the delivered papers (0 model calls).

    A claim is removed when it cites an unknown evidence id, evidence from
    outside the delivered selection, evidence that belongs to another paper in
    a paper-bound section, or a quote that is no longer inside the abstract.
    A brief whose abstract snapshot no longer matches is rejected outright.
    Removed claims are disclosed as gaps; valid claims are retained.
    """
    from research_brief import ABSTRACT_LIMIT
    delivered = {}
    for paper in papers or ():
        identity = str(paper.get('id') or paper.get('paper_id') or '')
        if not identity or identity in delivered:
            return SpecialistResult(ticket_id=ticket.ticket_id, status='rejected',
                                    failure_code='invalid_paper_selection', usage={'model_calls': 0})
        delivered[identity] = '' if paper.get('metadata_only') else str(paper.get('abstract') or '')
    current = abstract_hashes(papers)
    for identity, expected in ticket.abstract_sha256.items():
        if current.get(identity) != expected:
            return SpecialistResult(ticket_id=ticket.ticket_id, status='rejected',
                                    failure_code='abstract_snapshot_changed',
                                    findings=({'paper_id': identity,
                                               'reason': 'abstract_snapshot_changed'},))
    evidence = {}
    duplicates = set()
    for row in brief.get('evidence') or ():
        if isinstance(row, dict) and row.get('evidence_id'):
            identity = str(row['evidence_id'])
            if identity in evidence:
                duplicates.add(identity)
            evidence[identity] = row
    findings: list[Mapping[str, Any]] = []
    gap_ids: list[str] = []
    for section, index, owner, claim in _claim_sections(brief):
        cited = claim.get('evidence')
        removed_reason = '' if isinstance(cited, list) and cited else 'missing_evidence_reference'
        kinds = {'paper_notes.contribution': 'abstract_reported',
                 'paper_notes.method': 'abstract_reported',
                 'paper_notes.author_limitation': 'author_reported_limitation',
                 'paper_notes.model_inference': 'model_inference',
                 'themes': 'model_inference', 'open_questions': 'model_inference',
                 'research_hypotheses': 'hypothesis_not_tested'}
        if claim.get('kind') != kinds[section]:
            removed_reason = 'invalid_claim_kind'
        if section == 'research_hypotheses' and claim.get('experiment_status') != 'proposed_not_run':
            removed_reason = 'invalid_experiment_status'
        seen_refs = set()
        for reference in cited if isinstance(cited, list) else ():
            if not isinstance(reference, dict):
                removed_reason = removed_reason or 'malformed_evidence_reference'
                break
            identity = str(reference.get('evidence_id') or '')
            if identity in seen_refs:
                removed_reason = 'duplicate_claim_reference'
                break
            seen_refs.add(identity)
            if identity in duplicates:
                removed_reason = 'duplicate_evidence_id'
                break
            row = evidence.get(identity)
            if row is None:
                removed_reason = 'unknown_evidence_id'
                break
            paper_id = str(row.get('paper_id') or '')
            if reference.get('paper_id') != row.get('paper_id') or reference.get('quote') != row.get('quote'):
                removed_reason = 'evidence_reference_mismatch'
                break
            if paper_id not in delivered:
                removed_reason = removed_reason or 'evidence_outside_selection'
                break
            if section.startswith('paper_notes') and paper_id != owner:
                removed_reason = removed_reason or 'cross_paper_evidence'
                break
            quote = row.get('quote')
            if not isinstance(quote, str) or not quote.strip() or ' '.join(quote.split()) not in ' '.join(delivered.get(paper_id, '')[:ABSTRACT_LIMIT].split()):
                removed_reason = removed_reason or 'quote_not_in_abstract'
                break
            snapshot = row.get('abstract_sha256')
            expected_snapshot = hashlib.sha256(delivered[paper_id][:ABSTRACT_LIMIT].encode('utf-8')).hexdigest()
            if not isinstance(snapshot, str) or snapshot != expected_snapshot:
                removed_reason = removed_reason or 'abstract_snapshot_changed'
                break
        if removed_reason:
            gap = f'{section}:{removed_reason}'
            if gap not in gap_ids:
                gap_ids.append(gap)
            findings.append({'section': section, 'reason': removed_reason,
                             'paper_id': owner, 'index': index, 'claim_sha256': digest(claim),
                             'evidence_ids': [str(item.get('evidence_id') or '')
                                               for item in (cited if isinstance(cited, list) else ())
                                               if isinstance(item, dict)]})
            if len(findings) > MAX_FINDINGS:
                return SpecialistResult(ticket_id=ticket.ticket_id, status='rejected',
                                        failure_code='findings_overflow', usage={'model_calls': 0})
    return SpecialistResult(ticket_id=ticket.ticket_id, status='ok',
                            findings=tuple(findings), gap_ids=tuple(gap_ids),
                            usage={'model_calls': 0})


def apply_evidence_findings(brief: Mapping[str, Any],
                             result: SpecialistResult) -> tuple[dict, list[dict]]:
    """Remove the claims a deterministic critic rejected and disclose the gaps.

    Only removal happens here: the critic may never add text to the brief.
    """
    brief = deepcopy(brief)
    if result.status != 'ok' or not result.findings:
        return brief, []
    targets = []
    seen = set()
    for finding in result.findings:
        section = finding.get('section')
        index = finding.get('index')
        if not isinstance(section, str) or type(index) is not int or index < 0:
            raise SpecialistError('Invalid finding identity.')
        if section.startswith('paper_notes.'):
            field = section[len('paper_notes.'):]
            items = brief.get('paper_notes')
            if field not in NOTE_CLAIM_FIELDS or not isinstance(items, list) or index >= len(items):
                raise SpecialistError('Unknown finding identity.')
            note = items[index]
            if not isinstance(note, dict) or str(note.get('paper_id') or '') != finding.get('paper_id'):
                raise SpecialistError('Finding owner changed.')
            claim = note.get(field)
        elif section in ('themes', 'open_questions', 'research_hypotheses'):
            field = None
            items = brief.get(section)
            if not isinstance(items, list) or index >= len(items):
                raise SpecialistError('Unknown finding identity.')
            claim = items[index]
        else:
            raise SpecialistError('Unknown finding section.')
        identity = (section, index)
        if identity in seen or not isinstance(claim, dict) or finding.get('claim_sha256') != digest(claim):
            raise SpecialistError('Finding identity changed or conflicts.')
        seen.add(identity)
        targets.append((section, index, field, finding))
    gaps: list[dict] = []
    for section, index, field, finding in sorted(targets, key=lambda row: row[1], reverse=True):
        if field is not None:
            note = brief['paper_notes'][index]
            note[field] = None
            paper_id = str(note.get('paper_id') or '')
            gap_section = field
        else:
            item = brief[section].pop(index)
            paper_id = str(item.get('paper_id') or '')
            gap_section = section
        gaps.append({'section': gap_section, 'paper_id': paper_id,
                     'reason': 'claim_removed_by_evidence_critic',
                     'detail': str(finding.get('reason') or '')})
    return brief, gaps


def coverage_specialist(ticket: Ticket, landscape: Mapping[str, Any] | None,
                        selected: Sequence[Mapping[str, Any]], previous_queries: Sequence[str],
                        *, searches_left: int) -> SpecialistResult:
    """Plan at most one uncovered facet and its query scope (0 model calls).

    The plan is executed inside the remaining search allowance; the specialist
    itself starts no search round.
    """
    if searches_left <= 0:
        return SpecialistResult(ticket_id=ticket.ticket_id, status='skipped',
                                failure_code='no_affordable_search_round')
    if not landscape or landscape.get('scope') != 'landscape':
        return SpecialistResult(ticket_id=ticket.ticket_id, status='skipped',
                                failure_code='no_landscape_plan')
    from research_landscape import missing_query
    query = missing_query(landscape, selected, list(previous_queries or ()))
    if not query:
        return SpecialistResult(ticket_id=ticket.ticket_id, status='skipped',
                                failure_code='no_uncovered_facet')
    facet_id = ''
    for facet in landscape.get('facets') or ():
        if isinstance(facet, dict) and str(facet.get('query') or '') == query:
            facet_id = str(facet.get('id') or '')
            break
    return SpecialistResult(ticket_id=ticket.ticket_id, status='ok',
                            findings=({'facet_id': facet_id, 'query': query,
                                       'intent': 'fill_gap',
                                       'searches_left': int(searches_left)},),
                            gap_ids=(facet_id,) if facet_id else (),
                            usage={'model_calls': 0})


# ---------------------------------------------------------------------------
# Optional model specialist
# ---------------------------------------------------------------------------

def claim_critic(ticket: Ticket, claims: Sequence[Mapping[str, Any]], *, llm: Any,
                 budget: Any = None, meter: Any = None, deep: bool = False) -> SpecialistResult:
    """Disable the legacy entry point; its signature cannot enforce request,
    output, evidence and validation boundaries. Use specialist_pilot instead.
    """
    return SpecialistResult(ticket_id=ticket.ticket_id, status='skipped',
                            failure_code='legacy_critic_disabled' if deep else 'deep_mode_disabled',
                            usage={'model_calls': 0})
