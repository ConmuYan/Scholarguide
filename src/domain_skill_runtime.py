"""Host-controlled, bounded execution of the built-in domain plans."""
from collections import Counter
from urllib.parse import urlsplit

from agent_tools import ToolRegistry
from common import Budget, BudgetExceeded, GuideError, Paper, RunCancelled
from mcp_skills import domain_plan, domain_registry
from source import SearchBatch, SourceRun, retained_partial_batch


def execute_domain_skill(registry, skill, values, *, enabled=False, approved=False,
                         quotes=()):
    """Execute one search and deterministic extraction, never a recommendation.

    The host constructs the registry and grants each invocation. Quotes are
    (paper_id, exact_excerpt) pairs matched only against candidate abstracts.
    """
    if enabled is not True:
        return {'status': 'disabled', 'executed': False, 'candidates': [],
                'sources': [], 'reasons': ['domain_skills_disabled']}
    plan = domain_plan(domain_registry(enabled=True), skill, values)
    if approved is not True:
        return {'status': 'not_approved', 'executed': False, 'candidates': [],
                'sources': [], 'reasons': ['host_approval_required']}
    if type(registry) is not ToolRegistry or 'search' not in registry.specs:
        raise GuideError('trusted search registry required')
    source = registry.source
    providers = getattr(source, 'providers', None)
    budget = getattr(source, 'budget', None)
    if (providers != ['dblp'] or not isinstance(budget, Budget)
            or not callable(getattr(source, 'available', None))):
        raise GuideError('single DBLP source and source budget required')
    try:
        if source.available('dblp') is not True:
            return {'skill': skill, 'status': 'partial', 'executed': False,
                    'dispatch_status': 'not_started', 'search_status': 'source_unavailable',
                    'remote_outcome': 'not_started', 'sources': [{'source': 'dblp',
                    'status': 'unavailable', 'remote_outcome': 'not_started', 'audit_failed': False}],
                    'candidates': [], 'evidence': [],
                    'reasons': ['source_disabled_or_unavailable', 'no_attributed_candidates']}
    except (GuideError, OSError, TypeError, ValueError):
        return {'skill': skill, 'status': 'partial', 'executed': False,
                'dispatch_status': 'not_started', 'search_status': 'source_unavailable',
                'remote_outcome': 'not_started', 'sources': [], 'candidates': [], 'evidence': [],
                'reasons': ['source_unavailable', 'no_attributed_candidates']}

    if not isinstance(quotes, (tuple, list)) or len(quotes) > 10:
        raise GuideError('quotes must contain at most 10 pairs')
    seen_quotes = set()
    for item in quotes:
        if (not isinstance(item, (tuple, list)) or len(item) != 2
                or not isinstance(item[0], str) or not 1 <= len(item[0]) <= 300
                or not isinstance(item[1], str) or not 1 <= len(item[1]) <= 300
                or not item[1].strip()):
            raise GuideError('invalid quote pair')
        pair = (item[0], item[1])
        if pair in seen_quotes:
            raise GuideError('duplicate quote pair')
        seen_quotes.add(pair)
    query, limit = plan['inputs']['topic'], plan['inputs']['limit']
    batch = None
    status, remote = 'not_started', None
    dispatched = False
    dispatch_status = 'not_started'
    try:
        budget.check_cancelled()
        budget.remaining()
        observation = registry.search(query, limit)
        status, remote = observation.status, observation.remote_outcome
        dispatch_status = remote if remote in ('not_started', 'unknown', 'response_received', 'error', 'completed') else status
        dispatched = dispatch_status != 'not_started'
        batch = observation.data
    except (RunCancelled, BudgetExceeded) as exc:
        status = 'user_cancelled' if isinstance(exc, RunCancelled) else 'budget_exceeded'
        remote = getattr(exc, 'remote_outcome', None)
        batch = retained_partial_batch(exc, query, providers, limit)
    # Accept only bounded batches tied to this request and configured source.
    if not (isinstance(batch, SearchBatch) and batch.query == query
            and batch.providers == providers and isinstance(batch.runs, list)
            and len(batch.runs) == 1 and all(isinstance(r, SourceRun) and r.name == providers[0]
                                   for r in batch.runs)
            and isinstance(batch.papers, list) and len(batch.papers) <= limit
            and all(isinstance(p, Paper) for p in batch.papers)):
        batch = None
    sources, candidates, evidence = [], [], []
    seen_ids = set()
    matched_quotes = set()
    reasons = []
    if batch:
        for run in batch.runs:
            sources.append({'source': run.name, 'status': run.status,
                            'remote_outcome': run.remote_outcome,
                            'audit_failed': run.audit_status == 'failed'})
        if batch.stop_reason:
            reasons.append('search_' + batch.stop_reason if batch.stop_reason in
                           ('user_cancelled', 'budget_exceeded', 'source_error', 'tool_timeout')
                           else 'search_interrupted')
        if any(r.status not in ('ok', 'empty', 'cache') or r.audit_status == 'failed'
               for r in batch.runs):
            reasons.append('source_incomplete_or_audit_failed')
        for paper in batch.papers:
            attributed = set(paper.sources or []) | ({paper.source} if paper.source else set())
            if attributed != {'dblp'}:
                reasons.append('candidate_source_mismatch')
                continue
            if paper.id in seen_ids:
                reasons.append('duplicate_candidate_id')
                continue
            seen_ids.add(paper.id)
            if (not isinstance(paper.id, str) or not 1 <= len(paper.id) <= 300
                    or not isinstance(paper.title, str) or not 1 <= len(paper.title) <= 1000
                    or not isinstance(paper.url, str) or len(paper.url) > 2000
                    or not isinstance(paper.venue, str) or len(paper.venue) > 500):
                reasons.append('candidate_identity_missing')
                continue
            year = paper.year if type(paper.year) is int and 1900 <= paper.year <= 2100 else None
            if paper.url:
                parsed_url = urlsplit(paper.url)
                if parsed_url.scheme.lower() not in ('http', 'https') or parsed_url.username or parsed_url.password:
                    reasons.append('candidate_url_invalid')
                    continue
            candidates.append({'id': paper.id, 'title': paper.title, 'year': year,
                               'source': 'dblp', 'url': paper.url,
                               'venue': paper.venue})
            if skill == 'evidence_reading':
                for quote_index, (paper_id, excerpt) in enumerate(quotes):
                    if (quote_index not in matched_quotes and paper_id == paper.id
                            and isinstance(paper.abstract, str)
                            and len(paper.abstract) <= 100000 and excerpt in paper.abstract):
                        matched_quotes.add(quote_index)
                        evidence.append({'paper_id': paper.id, 'source': 'dblp',
                                         'field': 'abstract', 'excerpt': excerpt,
                                         'status': 'exact_text_match_not_verified'})
    if not sources:
        sources.append({'source': providers[0], 'status': status,
                        'remote_outcome': remote, 'audit_failed': False})
    if status not in ('ok', 'empty'):
        reasons.append('search_' + status if status in
                       ('user_cancelled', 'budget_exceeded', 'timeout', 'error',
                        'circuit_open', 'forbidden', 'invalid_input') else 'search_failed')
    if not candidates:
        reasons.append('no_attributed_candidates')
    if skill == 'evidence_reading':
        if len(evidence) < len(quotes):
            reasons.append('unmatched_or_unattributed_quotes')
        reasons.append('full_text_and_semantic_claim_support_not_assessed')
    else:
        if any(item['year'] is None for item in candidates):
            reasons.append('candidate_year_missing')
        reasons.append('sample_not_field_wide_trend')
    result = {'skill': skill, 'status': 'partial', 'executed': dispatched,
              'dispatch_status': dispatch_status, 'search_status': status, 'remote_outcome': remote, 'sources': sources,
              'candidates': candidates, 'evidence': evidence,
              'reasons': list(dict.fromkeys(reasons))}
    if skill == 'research_trends':
        result['sample_year_counts'] = dict(sorted(Counter(
            item['year'] for item in candidates if item['year'] is not None).items()))
    return result
