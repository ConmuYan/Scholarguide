"""The complete agent workflow: plan -> search -> assess/select -> maybe refine.

This is a bounded application workflow, not a reproduction of FlowSearcher/CRAG.
The LLM interprets the task and observations; Python controls budgets and selection.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
import re
import uuid

from costing import apply_cost, paid_cost_may_be_unknown, source_runs_cost
from model_identity import assessment_prompt_version, research_model_identity
from diagnosis import diagnose
from common import (Budget, BudgetExceeded, Config, GuideError, Paper, Request, RunCancelled, utc_now,
                    venue_matches, year_matches)
from intent import IntentPolicy, TYPE_LABELS, allocate, mix_text, type_from_assessment
from llm import JsonLLM
import prompts
from source import AcademicSource, merge_papers, retained_partial_batch
from tools import CostMeter, ToolOutcome, assess_batch, decision, search as run_search
from retrieval import RetrievalRouter, unresolved_paid_error


REQUEST_CONSTRAINT_FIELDS = ('target_count', 'category', 'minimum_category_count',
                             'venue', 'year_from', 'year_to')


def validate_request_constraints(value: object) -> dict:
    """Validate explicit guided controls before spending any model/tool budget."""
    if not isinstance(value, dict) or set(value) != set(REQUEST_CONSTRAINT_FIELDS):
        raise GuideError('request_constraints must contain exactly target_count, category, '
                         'minimum_category_count, venue, year_from and year_to.')
    if not isinstance(value['category'], str) or not isinstance(value['venue'], str):
        raise GuideError('request_constraints category and venue must be strings.')
    request = Request.from_dict({'topic': 'guided form constraints', **value})
    if request.minimum_category_count > request.target_count:
        raise GuideError('minimum_category_count must not exceed target_count.')
    validated = asdict(request)
    return {field: validated[field] for field in REQUEST_CONSTRAINT_FIELDS}


def clean_query(data: dict) -> str:
    query = data.get('query')
    if not isinstance(query, str) or not query.strip() or len(query) > 250:
        raise GuideError('query must be a nonempty keyword string of at most 250 characters.')
    return ' '.join(query.split())


ASSESSMENT_FIELDS = frozenset(('paper_id', 'relevance', 'fit_score', 'category_status',
                               'paper_type', 'evidence_quote', 'reason',
                               'evidence_verified', 'warning', 'provenance_conflict',
                               'facet_ids', 'facet_quote', 'facet_status'))


def validate_assessments(data: dict, papers: list[Paper]) -> None:
    rows = data.get('assessments')
    expected = {p.id for p in papers}
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise GuideError('Return exactly one assessment per supplied paper_id.')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('paper_id') not in expected or row['paper_id'] in seen:
            raise GuideError('Unknown or duplicate paper_id in assessments.')
        seen.add(row['paper_id'])
        if type(row.get('relevance')) is not int or row['relevance'] not in (0, 1, 2):
            raise GuideError('relevance must be integer 0, 1 or 2.')
        if 'fit_score' in row and (type(row['fit_score']) is not int or not 0 <= row['fit_score'] <= 100):
            raise GuideError('fit_score must be an integer from 0 to 100.')
        if row.get('category_status') not in ('yes', 'no', 'unknown'):
            raise GuideError('category_status must be yes, no or unknown.')
        if not all(isinstance(row.get(k), str) for k in ('evidence_quote', 'reason')):
            raise GuideError('evidence_quote and reason must be strings.')
        if row.get('paper_type') not in (None, '', 'survey', 'benchmark', 'research', 'unknown'):
            raise GuideError('paper_type must be survey, benchmark, research or unknown.')
        if len(row['evidence_quote']) > 1000 or len(row['reason']) > 1000:
            raise GuideError('Keep quotes and reasons short (at most 1000 characters each).')


def validate_research_assessments(data: dict, papers: list[Paper]) -> None:
    validate_assessments(data, papers)
    if any('fit_score' not in row for row in data['assessments']):
        raise GuideError('Research assessments require fit_score for every candidate.')


def _calibrate_paired_significance(plan, user_input, unsupported):
    paired = re.search(r'\bmost\s+influential\s+and\s+research-significant\b', user_input, re.I)
    if not paired:
        return list(unsupported)
    plan['ranking'] = 'citations'
    return [x for x in unsupported if str(x).strip(' .').casefold() != 'research-significant']


def _restore_explicit_conferences(plan, user_input):
    """Recover a literal conference whitelist only when it is unambiguous."""
    from common import requested_venues
    from research_policy import curated_venue_catalog, venue_identity
    matches = re.findall(r'^\s*Eligible conferences:\s*([^\r\n]+?)\s*$', user_input, re.I | re.M)
    if len(matches) != 1:
        return
    value = matches[0].strip()
    names = requested_venues(value)
    known = {v['name'] for v in curated_venue_catalog()['venues'] if v['type'] == 'conference'}
    known.update({'ACM MM', 'SIGGRAPH'})
    if not names or len(value) > 120 or any(venue_identity(name) not in known for name in names):
        return
    existing = str(plan.get('venue') or '').strip()
    if existing and {venue_identity(x) for x in requested_venues(existing)} != {venue_identity(x) for x in names}:
        return
    plan['venue'] = value
    exact_field = re.compile(r'^eligible conferences:\s*' + re.escape(value) + r'\s*$', re.I)
    plan['unsupported'] = [x for x in (plan.get('unsupported') or [])
                           if not (exact_field.fullmatch(str(x).strip())
                                   or str(x).strip().casefold() == value.casefold())]


def grounded_assessments(rows: list[dict], papers: list[Paper], category: str) -> dict:
    by_id = {p.id: p for p in papers}
    checked = {}
    for raw in rows:
        row = {key: value for key, value in raw.items() if key in ASSESSMENT_FIELDS}
        quote = ' '.join(row['evidence_quote'].split())
        abstract = ' '.join(by_id[row['paper_id']].abstract.split())
        valid = len(quote) >= 12 and quote in abstract and bool(row['reason'].strip())
        row['evidence_verified'] = valid  # substring check, NOT semantic support ground truth
        if not valid:
            row['relevance'] = 0
            row['category_status'] = 'unknown'
            row['warning'] = 'No usable verbatim abstract evidence; excluded from recommendation.'
        if category == 'none':
            row['category_status'] = 'unknown'
        row['paper_type'] = type_from_assessment(row, category) or 'unknown'
        if getattr(by_id[row['paper_id']], 'provenance_conflict', False):
            row['provenance_conflict'] = True
            row['warning'] = (row.get('warning') or '') + ' Cross-source abstracts disagree; quote was checked against the selected abstract.'
        if getattr(by_id[row['paper_id']], 'metadata_only', False) and not abstract:
            row['warning'] = 'No abstract available from the selected source; metadata-only record.'
        checked[row['paper_id']] = row
    return checked


def select_papers(papers: list[Paper], assessments: dict, request: Request, *, research: bool = False,
                  landscape: dict | None = None) -> list[dict]:
    excluded = set(getattr(request, 'excluded_paper_ids', []) or [])
    venue = str(getattr(request, 'venue', '') or '')
    year_from = getattr(request, 'year_from', None)
    year_to = getattr(request, 'year_to', None)
    publication_constraints = (request.publication_type != 'any' or request.accepted_only
                               or request.presentation_types or request.venue_scope == 'preferred_only')
    if research or publication_constraints:
        from research_policy import publication_matches, research_selection_key, matched_publication
    def packet(paper):
        row = {**paper.to_dict(), **{k: v for k, v in assessments[paper.id].items()
                                  if k in ASSESSMENT_FIELDS and k != 'paper_id'
                                  and k not in Paper.__dataclass_fields__}}
        if research or publication_constraints:
            record = matched_publication(paper, asdict(request))
            if record:
                row['publication'] = {**deepcopy(paper.publication), **record,
                                      'records': deepcopy(paper.publication.get('records') or [record])}
                row['venue'] = record.get('venue_name') or paper.venue
                date = str(record.get('date') or '')
                row['year'] = int(date[:4]) if re.match(r'^(19|20)\d{2}', date) else None
        return row
    eligible = [p for p in papers if p.id not in excluded
                and assessments.get(p.id, {}).get('relevance', 0) >= 1
                and assessments.get(p.id, {}).get('evidence_verified')
                and (publication_matches(p, asdict(request)) if research or publication_constraints
                     else venue_matches(p.venue, venue) and year_matches(p.year, year_from, year_to))]

    if request.ranking == 'citations':
        # Rank only grounded, eligible candidates. Missing is unknown, not zero.
        ranked = sorted(eligible, key=lambda p: (p.citation_count is None,
                        -(p.citation_count if p.citation_count is not None else 0), p.id))
        required = [p for p in ranked if assessments[p.id]['category_status'] == 'yes'][:request.minimum_category_count]
        selected = required + [p for p in ranked if p.id not in {x.id for x in required}]
        selected = selected[:request.target_count]
        selected.sort(key=lambda p: (p.citation_count is None,
                       -(p.citation_count if p.citation_count is not None else 0), p.id))
        return [packet(p) for p in selected]

    if research:
        preferred_type = {'orientation': 'survey', 'comparison': 'benchmark', 'frontier': 'research'}.get(request.reading_goal, '')
        ranked = sorted(eligible, key=lambda p: research_selection_key(p, assessments[p.id], asdict(request), preferred_type))
        required = [p for p in ranked if assessments[p.id]['category_status'] == 'yes'][:request.minimum_category_count]
        selected = required + [p for p in ranked if p.id not in {item.id for item in required}]
        if landscape:
            from research_landscape import diversify
            selected = diversify(selected, assessments, request, landscape)
        return [packet(p) for p in selected[:request.target_count]]

    def order(paper: Paper, preferred_type: str = ''):
        assessment = assessments[paper.id]
        kind = preferred_type or ''
        type_match = 1 if (preferred_type and assessment.get('paper_type') == preferred_type) else 0
        representative = float(getattr(paper, 'authority', 0) or 0)
        recency = -(paper.year or 0) if getattr(request, 'prefer_recent', False) else 0
        if kind == 'research':
            # Representative research first: venue/authority proxy, then relevance.
            return (-type_match, -representative, -assessment.get('relevance', 0), recency, paper.id)
        return (-type_match, -assessment.get('relevance', 0), -representative, recency, paper.id)

    goal = str(getattr(request, 'reading_goal', '') or '')
    if not goal:
        # Exact legacy ordering: relevance, then optional recency, then id.
        def legacy_order(p: Paper):
            recency = -(p.year or 0) if getattr(request, 'prefer_recent', False) else 0
            return (-assessments[p.id].get('relevance', 0), recency, p.id)
        ranked = sorted(eligible, key=legacy_order)
        required = [p for p in ranked if assessments[p.id]['category_status'] == 'yes'][:request.minimum_category_count]
        selected = required + [p for p in ranked if p.id not in {x.id for x in required}]
        return [packet(p) for p in selected[:request.target_count]]

    mix = allocate(request.target_count, goal)
    selected: list[Paper] = []
    # Explicit category quota is part of the total, not an extra block: count it
    # against the corresponding profile slot, then fill the remaining slots.
    explicit = [p for p in eligible if assessments[p.id]['category_status'] == 'yes']
    explicit = sorted(explicit, key=lambda p: order(
        p, 'research' if request.category == 'research' else ''))[:request.minimum_category_count]
    selected.extend(explicit)
    for paper_type, count in mix.items():
        have = sum(1 for p in selected if assessments[p.id].get('paper_type') == paper_type)
        need = max(0, count - have)
        candidates = [p for p in eligible if assessments[p.id].get('paper_type') == paper_type
                      and p.id not in {x.id for x in selected}]
        selected.extend(sorted(candidates, key=lambda p: order(p, paper_type))[:need])
    # Fill remaining slots with the best remaining papers (fallback), never pad with unrelated.
    remaining = [p for p in eligible if p.id not in {x.id for x in selected}]
    selected.extend(sorted(remaining, key=order)[:max(0, request.target_count - len(selected))])
    return [packet(p) for p in selected[:request.target_count]]


def coverage(selected: list[dict], request: Request, *, research: bool = False,
             landscape: dict | None = None) -> list[dict]:
    gaps = []
    required_count = request.minimum_count or request.target_count
    if len(selected) < required_count:
        gaps.append({'requirement': 'paper_count', 'required': required_count,
                     'found': len(selected), 'missing': required_count - len(selected)})
    count = sum(p['category_status'] == 'yes' for p in selected)
    if count < request.minimum_category_count:
        gaps.append({'requirement': request.category, 'required': request.minimum_category_count,
                     'found': count, 'missing': request.minimum_category_count - count})
    if request.ranking == 'citations' and selected:
        known = sum(type(p.get('citation_count')) is int and p['citation_count'] >= 0 for p in selected)
        if known < len(selected):
            gaps.append({'requirement': 'citation_counts', 'required': len(selected),
                         'found': known, 'missing': len(selected) - known})
    constraint_bits = []
    if getattr(request, 'venue', ''):
        constraint_bits.append(f'venue={request.venue}')
    if getattr(request, 'year_from', None) or getattr(request, 'year_to', None):
        constraint_bits.append(f'years={request.year_from or "any"}-{request.year_to or "any"}')
    if constraint_bits and len(selected) < request.target_count:
        gaps.append({'requirement': 'constraints', 'value': ' · '.join(constraint_bits),
                     'required': request.target_count, 'found': len(selected),
                     'missing': max(0, request.target_count - len(selected))})
    if getattr(request, 'reading_goal', '') and not research and request.ranking != 'citations':
        mix = allocate(request.target_count, request.reading_goal)
        counts = {kind: 0 for kind in mix}
        for paper in selected:
            kind = paper.get('paper_type', '')
            if kind in counts:
                counts[kind] += 1
        missing = {kind: mix[kind] - counts[kind] for kind in mix if counts[kind] < mix[kind]}
        if missing:
            gaps.append({'requirement': 'mix',
                         'value': ' · '.join(f'{TYPE_LABELS[k]} {counts[k]}/{mix[k]}' for k in mix),
                         'required': request.target_count, 'found': len(selected),
                         'missing': sum(missing.values()), 'mix': missing})
    if research and landscape:
        from research_landscape import gaps as landscape_gaps
        gaps.extend(landscape_gaps(landscape, selected))
    return gaps


def run(user_input: str, config: Config, method: str = 'A', *, request_data: dict | None = None,
        source=None, llm=None, budget: Budget | None = None, progress=None,
        goal_hint: str | None = None, request_constraints: dict | None = None,
        research_preferences: dict | None = None) -> dict:
    config.validate()
    if method not in ('S', 'A'):
        raise GuideError('Core methods are S (single pass) and A (adaptive).')
    constraints = (validate_request_constraints(request_constraints)
                   if request_constraints is not None else None)
    preferences = {}
    if research_preferences is not None:
        allowed = ('venue_scope', 'publication_type', 'accepted_only', 'presentation_types')
        if not isinstance(research_preferences, dict) or set(research_preferences) - set(allowed):
            raise GuideError('Invalid research preferences.')
        parsed_preferences = asdict(Request.from_dict({'topic': 'research preferences', **research_preferences}))
        preferences = {name: parsed_preferences[name] for name in research_preferences}
    budget = budget or Budget(config.task_timeout)
    source = source or AcademicSource(config, budget)
    llm = llm or JsonLLM(config, budget)
    router = (RetrievalRouter(config, source)
              if hasattr(source, 'search_batch') and (config.research_mode or len(getattr(source, 'providers', [])) > 1)
              else None)
    result = {'schema_version': 1, 'run_id': uuid.uuid4().hex, 'started_at': utc_now(),
              'method': method, 'mode': 'live', 'config': config.public(), 'user_input': user_input,
              'request': None, 'status': 'failed', 'stop_reason': '', 'queries': [],
              'papers': [], 'candidates': [], 'assessments': {}, 'gaps': [], 'trace': [], 'warnings': [],
              'assessment_provenance': {'model': config.model,
                  'prompt_version': assessment_prompt_version(config)}}
    if getattr(source, 'mode', '') == 'demo' or getattr(llm, 'mode', '') == 'demo':
        result['mode'] = 'demo'
    candidates: list[Paper] = []
    assessments = {}
    assessed_abstracts = {}
    meter = CostMeter()
    landscape = None
    acl_checked: set[str] = set()
    openreview_checked: set[str] = set()
    citation_checked: set[str] = set()

    def event(action: str, **details) -> None:
        entry = {'step': len(result['trace']) + 1, 'action': action, 'at': utc_now(), **details}
        result['trace'].append(entry)
        if progress:
            if getattr(progress, 'accepts_checkpoint', False):
                progress(entry, checkpoint=deepcopy(result))
            else:
                progress(entry)

    try:
        budget.check_cancelled()
        from quantities import quantity_constraints, apply_constraints
        parsed_count = quantity_constraints(user_input) if request_data is None else {}
        raw_plan = None
        if request_data is None:
            def validate_plan(value):
                parsed = Request.from_dict({**value, **parsed_count, **(constraints or {}), **preferences})
                clean_query(value)
                if any(len(x.strip()) < 3 or x.casefold() not in user_input.casefold() for x in parsed.unsupported):
                    raise GuideError('unsupported may contain only exact excerpts actually present in the user input; otherwise return [].')
            payload = {'user_input': user_input, 'current_date': date.today().isoformat()}
            if constraints is not None:
                payload['request_constraints'] = deepcopy(constraints)
            raw_plan = decision('plan', llm, payload,
                                prompts.PLAN_SCHEMA, validate_plan, prompts.PLAN, meter)
            from quantities import calibrate_user_requirements
            plan = calibrate_user_requirements(dict(raw_plan), user_input)
            # Correct an overly conservative planner only for this exact,
            # now-supported ranking requirement, never for unrelated clauses.
            influence = re.compile(r'^(?:rank|order)(?:\s+the\s+papers)?\s+by\s+(?:research\s+)?(?:influence|citation(?:\s+count)?s?)$', re.I)
            unsupported = plan.get('unsupported') or []
            # The benchmark's paired wording expresses the same broad ranking
            # request; citations are disclosed below as an imperfect proxy.
            # Do not suppress a standalone scientific-significance requirement.
            unsupported = _calibrate_paired_significance(plan, user_input, unsupported)
            plan['unsupported'] = unsupported
            _restore_explicit_conferences(plan, user_input)
            unsupported = plan.get('unsupported') or []
            if any(influence.fullmatch(str(x).strip(' .')) for x in unsupported):
                plan['ranking'] = 'citations'
                plan['unsupported'] = [x for x in unsupported if not influence.fullmatch(str(x).strip(' .'))]
        else:
            plan = dict(request_data)
            plan.setdefault('query', plan.get('topic', ''))
        if request_data is None:
            plan.update(parsed_count)
        if constraints is not None:
            plan.update(constraints)
        plan.update(preferences)
        request = Request.from_dict(plan)
        if request.ranking == 'citations':
            result['warnings'].append('Influence ranking uses reported citation counts as a limited proxy among retrieved, relevant candidates; missing counts rank last. Sources and publication years can differ; this is not a measure of scientific quality.')
        explicit_goal = goal_hint in ('orientation', 'comparison', 'frontier', 'general')
        if explicit_goal:
            request = replace(request, reading_goal=goal_hint)
        result['request'] = asdict(request)
        event('provider', profile_id=config.profile_id, profile_name=config.profile_name,
              provider=config.provider, model=config.model, protocol=config.protocol,
              input_price=config.input_price, output_price=config.output_price,
              currency=config.currency)
        if request.reading_goal and request.ranking != 'citations':
            mix = allocate(request.target_count, request.reading_goal)
            event('intent', goal=request.reading_goal,
                  policy='deterministic_intent_v1', mix=mix_text(mix),
                  allocations=dict(mix), target_count=request.target_count,
                  category=request.category,
                  minimum_category_count=request.minimum_category_count,
                  source=('goal_hint' if explicit_goal else 'planner_output'))
        result['gaps'] = coverage([], request, research=config.research_mode)
        query = clean_query(plan)
        event('plan', request=asdict(request), query=query,
              plan_source=('structured_request' if request_data is not None else 'llm'),
              llm_plan=deepcopy(raw_plan),
              **({'request_constraints': deepcopy(constraints),
                  'constraint_source': 'guided_form'} if constraints is not None else {}))
        if request.unsupported:
            result['status'] = 'unsupported'
            result['stop_reason'] = 'unsupported_requirements'
            result['warnings'].append('Unsupported conditions were not silently dropped: ' + '; '.join(request.unsupported))
            return result

        if config.research_mode and result['mode'] != 'demo':
            from research_landscape import plan as plan_landscape
            landscape = plan_landscape(llm, request, meter, budget,
                                       model=research_model_identity(config))
            result['research_coverage'] = deepcopy(landscape)
            event('research_plan', scope=landscape['scope'], facets=landscape['facets'],
                  preferred_venues=landscape.get('preferred_venues',[]),
                  status=landscape['status'], reason=landscape.get('reason', ''))

        max_rounds = 1 if method == 'S' else 2
        for round_index in range(max_rounds):
            budget.check_cancelled()
            budget.remaining()
            result['queries'].append(query)
            event('search', round=round_index + 1, query=query, tool='search')
            if router is not None:
                from research_landscape import routing_task
                retrieval = router.retrieve(routing_task(request, landscape), query, config.per_search, meter,
                                            allow_refine=config.research_mode)
                errors = retrieval.errors
                status = 'ok' if retrieval.papers else ('error' if errors else 'empty')
                router_observation = retrieval.observation()
                cost_cny = (None if retrieval.tool_cost_unknown else
                            source_runs_cost(run for batch in retrieval.batches for run in batch.runs))
                outcome = ToolOutcome(bool(retrieval.papers) or not errors, retrieval.papers,
                                      {'status': status, 'error': '; '.join(errors),
                                       'router': router_observation},
                                      cost=retrieval.cost, cost_cny=cost_cny)
            else:
                filters = {'venue': request.venue, 'year_from': request.year_from, 'year_to': request.year_to}
                outcome = run_search(source, query, config.per_search, meter,
                                     filters=filters if any(filters.values()) else None)
            if outcome.ok:
                found = outcome.items
                before = len(candidates)
                candidates = merge_papers(candidates + found)[:20]
                event('search_result', returned=len(found), unique_candidates=len(candidates),
                      new_candidates=len(candidates) - before,
                      cost_cny=outcome.cost_cny,
                      router=(outcome.observation.get('router') if router is not None else None))
            else:
                result['warnings'].append(outcome.observation['error'])
                event('search_failed', error=outcome.observation['error'],
                      cost_cny=outcome.cost_cny,
                      router=(outcome.observation.get('router') if router is not None else None))
                found = []
            result['candidates'] = [p.to_dict() for p in candidates]
            blocked_error = unresolved_paid_error(retrieval if router is not None else outcome.observation)
            if blocked_error is not None:
                raise blocked_error
            enrich = getattr(source, 'enrich_openalex_citations', None)
            if (request.ranking == 'citations' and callable(enrich)
                    and 'openalex' in (getattr(source, 'providers', ()) or ())
                    and len(citation_checked) < 10):
                from source import normalize_doi
                remaining = 10 - len(citation_checked)
                admitted_dois = []
                for paper in candidates:
                    doi = normalize_doi(paper.doi)
                    if (paper.citation_count is None and doi and doi not in citation_checked
                            and doi not in admitted_dois and len(admitted_dois) < remaining):
                        admitted_dois.append(doi)
                if admitted_dois:
                    to_enrich = [p for p in candidates if p.citation_count is None
                                 and normalize_doi(p.doi) in admitted_dois]
                    budget.check_cancelled()
                    citation_checked.update(admitted_dois)
                    try:
                        enriched, stats = enrich(to_enrich, max_lookups=len(admitted_dois))
                        by_id = {p.id: p for p in enriched}
                        candidates = [by_id.get(p.id, p) for p in candidates]
                        result['candidates'] = [p.to_dict() for p in candidates]
                        event('citation_enrichment', **stats)
                    except (RunCancelled, BudgetExceeded):
                        raise
                    except GuideError as exc:
                        event('citation_enrichment_failed', error=str(exc))
            # Verify a small number of exact ACL Anthology identities before
            # applying accepted-only publication filters. Unknown remains unknown.
            verifier = getattr(source, 'verify_acl_paper', None)
            if request.accepted_only and callable(verifier):
                for index, paper in enumerate(candidates):
                    if len(acl_checked) >= 5:
                        break
                    if paper.id in acl_checked or not re.fullmatch(r'10\.18653/(?:v1/)?(?:19|20)\d{2}\.(?:acl|emnlp)-(?:long|short|main)\.\d+', paper.doi or '', re.I):
                        continue
                    from research_policy import matched_publication
                    if matched_publication(paper, asdict(request)):
                        continue
                    budget.check_cancelled()
                    if budget.remaining() < 5:
                        break
                    acl_checked.add(paper.id)
                    try:
                        candidates[index] = verifier(paper)
                    except RunCancelled:
                        raise
                    except GuideError:
                        pass
                result['candidates'] = [p.to_dict() for p in candidates]
            # A Lune conference entry is useful discovery evidence but does
            # not itself prove acceptance. Check a bounded number of exact
            # candidates against the official OpenReview edition before the
            # accepted-only filter excludes them.
            verifier = getattr(source, 'verify_openreview_paper', None)
            if (request.accepted_only and callable(verifier) and
                    'openreview' in getattr(source, 'providers', ())):
                from research_policy import matched_publication, venue_identity
                for index, paper in enumerate(candidates):
                    if len(openreview_checked) >= 5 or budget.remaining() < 15:
                        break
                    if (paper.id in openreview_checked or not paper.year or
                            venue_identity(paper.venue or '') not in ('ICLR', 'ICML', 'NeurIPS') or
                            matched_publication(paper, asdict(request))):
                        continue
                    openreview_checked.add(paper.id)
                    try:
                        candidates[index] = verifier(paper)
                    except RunCancelled:
                        raise
                    except GuideError:
                        pass
                result['candidates'] = [p.to_dict() for p in candidates]
            # Reuse assessments when metadata is unchanged; process only new records.
            pending = [p for p in candidates if p.id not in assessments or assessed_abstracts.get(p.id) != p.abstract]
            # A stop before reassessment must not expose a prior judgement against
            # a newly merged abstract. Keep only compatible verified work.
            compatible_ids = {p.id for p in candidates if assessed_abstracts.get(p.id) == p.abstract}
            assessments = {paper_id: row for paper_id, row in assessments.items()
                           if paper_id in compatible_ids}
            result['assessments'] = assessments
            result['papers'] = select_papers(candidates, assessments, request, research=config.research_mode,
                                             landscape=landscape)
            result['gaps'] = coverage(result['papers'], request, research=config.research_mode,
                                       landscape=landscape)
            assessment_size = min(config.batch_size, 3) if landscape and landscape.get('scope') == 'landscape' else config.batch_size
            for start in range(0, len(pending), assessment_size):
                budget.check_cancelled()
                batch = pending[start:start + assessment_size]
                outcome = assess_batch(llm, batch, topic=request.topic, category=request.category,
                                       category_definition=prompts.CATEGORY_DEFINITIONS[request.category],
                                       purpose=getattr(request, 'purpose', ''),
                                       prompt=prompts.ASSESS_RESEARCH if config.research_mode else prompts.ASSESS,
                                       schema=prompts.ASSESS_RESEARCH_SCHEMA if config.research_mode else prompts.ASSESS_SCHEMA,
                                       validate=validate_research_assessments if config.research_mode else validate_assessments,
                                       meter=meter, research_plan=landscape)
                assessments.update(grounded_assessments(outcome.items, batch, request.category))
                assessed_abstracts.update({p.id: p.abstract for p in batch})
                result['assessments'] = assessments
                if landscape and landscape.get('scope') == 'landscape':
                    from research_landscape import assessment_coverage, MAX_CANDIDATES
                    reviewed = select_papers(candidates, assessments,
                        replace(request, target_count=MAX_CANDIDATES), research=True)
                    shared = assessment_coverage(request, reviewed, assessments, landscape,
                                                   model=research_model_identity(config))
                    if shared:
                        landscape = shared
                result['papers'] = select_papers(candidates, assessments, request, research=config.research_mode,
                                                 landscape=landscape)
                result['gaps'] = coverage(result['papers'], request, research=config.research_mode,
                                           landscape=landscape)
                event('assess', assessed=len(batch), eligible=sum(a['relevance'] >= 1 for a in assessments.values()))
                if landscape and landscape.get('scope') == 'landscape' and not result['gaps']:
                    event('assessment_sufficient', remaining_candidates=len(pending)-start-len(batch),
                          reason='Verified list covers the planned routes and explicit requirements.')
                    break
            budget.check_cancelled()
            # Re-check quotes if merging metadata changed the retained abstract.
            assessments = grounded_assessments(list(assessments.values()),
                [p for p in candidates if p.id in assessments], request.category) if assessments else {}
            result['assessments'] = assessments
            if landscape:
                from research_landscape import review as review_landscape, assessment_coverage, MAX_CANDIDATES
                review_papers = select_papers(candidates, assessments,
                    replace(request, target_count=MAX_CANDIDATES), research=True)
                shared_coverage = assessment_coverage(request, review_papers, assessments, landscape,
                                                       model=research_model_identity(config))
                landscape = shared_coverage or review_landscape(llm, request, review_papers, landscape,
                    meter, budget, model=research_model_identity(config))
                event('research_coverage', status=landscape['status'],
                      assignments=deepcopy(landscape.get('assignments') or []),
                      reason=landscape.get('reason', ''))
            result['papers'] = select_papers(candidates, assessments, request, research=config.research_mode,
                                             landscape=landscape)
            result['gaps'] = coverage(result['papers'], request, research=config.research_mode,
                                       landscape=landscape)
            event('check', selected=len(result['papers']), gaps=result['gaps'])
            if not result['gaps']:
                result['status'], result['stop_reason'] = 'complete', 'requirements_met'
                break
            if round_index + 1 == max_rounds:
                result['status'] = 'partial' if result['papers'] else 'no_results'
                result['stop_reason'] = 'search_budget_exhausted'
                break
            budget.check_cancelled()
            from research_landscape import missing_query
            facet_query = missing_query(landscape, result['papers'], result['queries']) if landscape else ''
            if facet_query:
                query = facet_query
                event('refine', query=query, reason='Fill an uncovered research facet from the task plan.')
                continue
            refined = decision('refine', llm,
                {'topic': request.topic, 'purpose': request.purpose,
                 'previous_queries': result['queries'], 'gaps': result['gaps'],
                 **({'research_context': {'selected': result['papers'],
                         'assessments': list(assessments.values())[:20],
                         'source_health': source.health_snapshot() if hasattr(source, 'health_snapshot') else {},
                         'last_retrieval': router_observation if router is not None else None}}
                    if config.research_mode else {})},
                prompts.QUERY_SCHEMA, clean_query, prompts.REFINE, meter)
            query = clean_query(refined)
            if query.casefold() in {q.casefold() for q in result['queries']}:
                result['status'] = 'partial' if result['papers'] else 'no_results'
                result['stop_reason'] = 'repeated_query'
                break
            event('refine', query=query, reason='Unmet requirements in the selected list.')
    except GuideError as exc:
        partial = (retained_partial_batch(exc, result['queries'][-1],
                                          getattr(source, 'providers', ()), config.per_search)
                   if result['queries'] else None)
        if partial is not None:
            candidates = merge_papers(candidates + partial.papers)[:20]
            result['candidates'] = [paper.to_dict() for paper in candidates]
            event('search_partial', returned=len(partial.papers), stop_reason=partial.stop_reason,
                  cost_cny=None)
        elif result['queries'] and paid_cost_may_be_unknown(source, exc):
            event('source_cost_unknown', cost_cny=None)
        if budget.cancel_token.requested:
            result['status'] = 'partial' if result['papers'] else 'no_results'
            result['stop_reason'] = 'user_cancelled'
        else:
            result['status'] = 'partial' if result['papers'] else 'failed'
            result['stop_reason'] = str(exc)
            event('error', error=str(exc))
    finally:
        if budget.cancel_token.requested:
            result['status'] = 'partial' if result['papers'] else 'no_results'
            result['stop_reason'] = 'user_cancelled'
            event('cancelled', reason='user_cancelled', verified_papers=len(result['papers']))
        if landscape:
            from research_landscape import summary as landscape_summary
            result['research_coverage'] = landscape_summary(landscape, result['papers'])
        if config.research_mode:
            from research_brief import synthesize
            event('synthesis', status='preparing', source_level='abstract_only')
            result['research_brief'] = synthesize(
                llm, result.get('request') or {}, [] if result['mode'] == 'demo' else result['papers'], meter=meter,
                budget=budget, model=research_model_identity(config))
            if result['mode'] == 'demo':
                result['research_brief']['reason'] = 'offline_demo'
            event('synthesis', status=result['research_brief']['status'],
                  reason=result['research_brief'].get('reason', ''))
        if budget.cancel_token.requested and result['stop_reason'] != 'user_cancelled':
            result['status'] = 'partial' if result['papers'] else 'no_results'
            result['stop_reason'] = 'user_cancelled'
            event('cancelled', reason='user_cancelled', verified_papers=len(result['papers']))
        result['metrics'] = budget.metrics()
        result['metrics']['search_rounds'] = len(result['queries'])
        result['metrics']['cost_units'] = meter.summary()
        if result['mode'] == 'live' and budget.cache_hits:
            result['mode'] = 'live_model_cached_search'
        result['model_events'] = getattr(llm, 'events', [])
        event('stop', status=result['status'], reason=result['stop_reason'])
        apply_cost(result, config)
        health = source.health_snapshot() if hasattr(source, 'health_snapshot') else {}
        result['diagnosis'] = diagnose(
            request=result.get('request') or {}, gaps=result.get('gaps'),
            trace=result.get('trace'), candidates=result.get('candidates'),
            selected=result.get('papers'), source_health=health,
            stop_reason=result.get('stop_reason') or '')
    return result
