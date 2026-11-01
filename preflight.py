"""Cheap, metadata-only feasibility preflight before expensive assessment.

Assessment can cost thousands of output tokens.  If the current candidate pool
has explicit venue/year constraints and *known* metadata shows that no candidate
can satisfy them, assessing the pool cannot produce a valid paper.  This module
only blocks in the safe case: every candidate has known venue/year metadata and
none matches.  Unknown metadata stays assessable rather than being guessed.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from common import venue_matches, year_matches


def _value(paper: Any, name: str, default: Any = None) -> Any:
    return paper.get(name, default) if isinstance(paper, Mapping) else getattr(paper, name, default)


def constraint_papers(papers: Iterable[Any], request: Mapping[str, Any] | Any) -> dict:
    venue = str((request.get('venue') if isinstance(request, Mapping)
                 else getattr(request, 'venue', '')) or '')
    year_from = request.get('year_from') if isinstance(request, Mapping) else getattr(request, 'year_from', None)
    year_to = request.get('year_to') if isinstance(request, Mapping) else getattr(request, 'year_to', None)
    has_constraints = bool(venue or year_from or year_to)
    total = 0
    known = 0
    eligible = 0
    unknown = 0
    for paper in papers:
        total += 1
        from research_policy import publication_matches
        task = dict(request) if isinstance(request, Mapping) else vars(request)
        publication = _value(paper, 'publication', {}) or {}
        if publication_matches(paper, task) and (publication or not has_constraints):
            known += 1
            eligible += 1
            continue
        paper_venue = str(_value(paper, 'venue', '') or '')
        paper_year = _value(paper, 'year', None)
        # A candidate with no venue (or a year when a year filter is active)
        # cannot be safely excluded by this cheap gate.
        needs_year = year_from is not None or year_to is not None
        if (venue and not paper_venue) or (needs_year and paper_year is None):
            unknown += 1
            continue
        known += 1
        if venue_matches(paper_venue, venue) and year_matches(paper_year, year_from, year_to):
            eligible += 1
    blocked = bool(has_constraints and total > 0 and known == total and eligible == 0)
    reason = ''
    if blocked:
        bits = []
        if venue:
            bits.append(f'venue={venue}')
        if year_from or year_to:
            bits.append(f'years={year_from or "any"}-{year_to or "any"}')
        reason = 'no candidate metadata satisfies ' + ' · '.join(bits)
    return {'total': total, 'known': known, 'unknown': unknown,
            'eligible': eligible, 'blocked': blocked, 'reason': reason,
            'has_constraints': has_constraints}
