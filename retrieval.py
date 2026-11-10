"""Intent-aware retrieval routing, query variants and source fusion.

The router is deliberately deterministic: an LLM may propose a query, but the
cheap routing decisions (which sources, whether to refine, whether to spend a
DeepSeek call) remain inspectable and testable. The legacy arXiv-only workflow
keeps its original path, so the frozen main experiment stays unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import re
from typing import Any

from common import BudgetExceeded, Config, GuideError, Paper, REMOTE_OUTCOMES, RunCancelled, source_names, venue_matches, year_matches
from source import (SearchBatch, SourceRun, merge_papers, normalize_doi, title_key,
                    curated_preferences, preferred_venue_matches, retained_partial_batch)
from tools import CostMeter
from vendor.gptr_arxiv import arxiv_id
from research_policy import (CATALOG_VERSION, curated_venue_catalog, journal_issns_for_venue,
                             publication_matches, publication_preference, publication_records, venue_identity)

VENUE_WORDS = (
    'acl', 'emnlp', 'naacl', 'eacl', 'tacl', 'iclr', 'neurips', 'nips', 'icml',
    'cvpr', 'iccv', 'eccv', 'aaai', 'ijcai', 'kdd', 'sigir', 'www', 'usenix',
    'osdi', 'sosp', 'mlr', 'pmlr', 'jmlr', 'conference', 'proceedings', 'venue',
)
TOP_VENUE_WORDS = tuple(w for w in VENUE_WORDS if w not in ('conference', 'proceedings', 'venue'))
STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'in', 'is',
    'of', 'on', 'or', 'that', 'the', 'to', 'with', 'we', 'this', 'these', 'it',
    'paper', 'papers', 'study', 'using', 'based', 'can', 'do', 'does', 'how',
    'what', 'which', 'their', 'our', 'into', 'via', 'over', 'under', 'about',
}


_PAID_PROVIDERS = frozenset(('lune', 'deepseek'))


def _observation_error(observation):
    error = GuideError(observation.error or f'{observation.tool} failed')
    remote = getattr(observation, 'remote_outcome', None)
    if isinstance(remote, str) and remote in REMOTE_OUTCOMES:
        error.remote_outcome = remote
    if getattr(observation, 'retry_safe', None) is False:
        error.retry_safe = False
    if isinstance(getattr(observation, 'data', None), SearchBatch):
        error.partial_batch = observation.data
    return error


def _paid_failure_outcome(value, providers):
    if not any(name in _PAID_PROVIDERS for name in providers):
        return None
    # A later free source can hit the deadline after a paid source completed.
    # Only clear the paid ambiguity when the retained batch explicitly proves
    # that every paid provider in this call completed successfully.
    batch = getattr(value, 'partial_batch', None)
    if isinstance(batch, SearchBatch):
        paid = [name for name in providers if name in _PAID_PROVIDERS]
        if all(any(run.name == name and run.status in ('ok', 'empty', 'cache')
                       and run.remote_outcome == 'completed'
                       for run in batch.runs) for name in paid):
            return None
    remote = getattr(value, 'remote_outcome', None)
    return remote if isinstance(remote, str) and remote in REMOTE_OUTCOMES else 'unknown'


def unresolved_paid_error(result):
    """Stop the enclosing workflow when an earlier paid outcome is uncertain."""
    if isinstance(result, dict):
        blocked = result.get('paid_followups_blocked') is True
        outcomes = [result]
    else:
        blocked = getattr(result, 'paid_followups_blocked', False)
        outcomes = getattr(result, 'remote_outcomes', ()) or ()
    if not blocked:
        return None
    error = GuideError('A paid source outcome is unresolved; automatic follow-up stopped.')
    remote = next((row.get('remote_outcome') for row in outcomes if isinstance(row, dict)
                   and row.get('remote_outcome') in REMOTE_OUTCOMES
                   and row.get('remote_outcome') != 'not_started'), 'unknown')
    error.remote_outcome, error.retry_safe = remote, False
    return error


def _terms(value: str) -> set[str]:
    return {tok for tok in re.findall(r'[a-z0-9]+', str(value or '').lower())
            if len(tok) > 1 and tok not in STOPWORDS}


def _clean_query(value: str) -> str:
    return ' '.join(str(value or '').split())[:250]


@dataclass
class RetrievalPlan:
    providers: list[str]
    queries: list[str]
    reasons: list[str]
    deepseek_allowed: bool = False
    single_source: bool = False
    research: bool = False
    steps: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fallback_steps: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        value = {'providers': list(self.providers), 'queries': list(self.queries),
                'reasons': list(self.reasons), 'deepseek_allowed': self.deepseek_allowed,
                'single_source': self.single_source}
        if self.research:
            value.update(research=True, policy_version=CATALOG_VERSION,
                         steps=list(self.steps), notes=list(self.notes),
                         fallback_steps=list(self.fallback_steps),
                         max_calls=len(self.steps) + len(self.fallback_steps))
        return value


@dataclass
class RetrievalResult:
    papers: list[Paper]
    plan: RetrievalPlan
    batches: list[SearchBatch] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    cost: float = 0.0
    errors: list[str] = field(default_factory=list)
    executions: list[dict] = field(default_factory=list)
    retrieval_gap: str = ''
    tool_cost_unknown: bool = False
    paid_followups_blocked: bool = False
    remote_outcomes: list[dict] = field(default_factory=list)

    def observation(self) -> dict:
        batches = [b.summary() for b in self.batches]
        value = {
            'plan': self.plan.as_dict(),
            'batches': batches,
            'cost': self.cost,
            'returned': len(self.papers),
            'selected': [
                {'id': p.id, 'title': p.title[:120], 'score': round(self.scores.get(p.id, 0.0), 4),
                 'sources': list(p.sources), 'venue': p.venue, 'metadata_only': p.metadata_only}
                for p in self.papers[:10]
            ],
            'errors': list(self.errors),
        }
        if self.tool_cost_unknown:
            value['tool_cost_unknown'] = True
        if self.paid_followups_blocked:
            value['paid_followups_blocked'] = True
        if self.remote_outcomes:
            value['remote_outcomes'] = list(self.remote_outcomes)
            unsafe = next((row for row in self.remote_outcomes
                           if row['remote_outcome'] != 'not_started'), None)
            if unsafe:
                value['remote_outcome'] = unsafe['remote_outcome']
                value['retry_safe'] = False
        if self.plan.research:
            value.update(semantic_satisfaction=None,
                         selection_stage='lexical_candidate_selection; semantic assessment required',
                         raw_candidates=sum(len(batch.papers) for batch in self.batches),
                         retrieval_execution=list(self.executions), retrieval_gap=self.retrieval_gap)
        return value


class RetrievalRouter:
    def __init__(self, config: Config, source: Any, today: int | None = None,
                 tools: Any = None):
        self.config = config
        self.source = source
        self.tools = tools
        self.today = today or datetime.now(timezone.utc).year
        configured = getattr(source, 'providers', None) or source_names(config.source)
        self.configured = list(dict.fromkeys(configured))
        self.preset = (config.source or '').strip().lower() in ('multi', 'all')
        self.last_plan: RetrievalPlan | None = None

    # -- routing ---------------------------------------------------------
    def _venue_query(self, task: dict, query: str) -> bool:
        text = ' '.join(str(task.get(k) or '') for k in ('topic', 'purpose', 'venue')) + ' ' + query
        tokens = set(re.findall(r'[a-z0-9]+', text.lower()))
        return bool(tokens & set(VENUE_WORDS))

    def _variant(self, task: dict, query: str) -> str:
        category = str(task.get('category') or 'none')
        low = query.lower()
        suffix = []
        if category == 'evaluation':
            suffix += [term for term in ('evaluation', 'benchmark') if term not in low]
        elif category == 'survey' and 'survey' not in low:
            suffix.append('survey')
        venue = str(task.get('venue') or '').strip()
        if venue and venue.casefold() not in low:
            suffix.append(venue)
        if task.get('year_from') or task.get('year_to'):
            years = f"{task.get('year_from') or ''} {task.get('year_to') or ''}".strip()
            if years and years not in query:
                suffix.append(years)
        if not suffix and task.get('prefer_recent'):
            suffix.append(f'{self.today - 2} {self.today - 1}')
        return _clean_query(query + (' ' + ' '.join(suffix) if suffix else ''))

    def plan(self, task: dict, query: str, *, providers: list[str] | None = None,
             intent: str = '') -> RetrievalPlan:
        query = _clean_query(query)
        if not query:
            raise GuideError('Retrieval query is empty.')
        if getattr(self.config, 'research_mode', False):
            return self._research_plan(task, query, providers, intent)
        venue = self._venue_query(task, query)
        explicit_providers = providers is not None
        requested = list(providers) if explicit_providers else None
        category = str(task.get('category') or 'none')
        reasons: list[str] = []
        if len(self.configured) <= 1:
            name = self.configured[0] if self.configured else 'arxiv'
            reasons.append('single-source main experiment: ' + name)
            plan = RetrievalPlan([name], [query], reasons, False, True)
            self.last_plan = plan
            return plan

        if explicit_providers:
            providers = [name for name in requested if name in self.configured]
            reasons.append('explicit source allow-list')
        elif self.preset:
            providers = [p for p in ('lune', 'arxiv', 'openalex', 'semantic_scholar') if p in self.configured
                         and (p != 'lune' or getattr(self.source, 'available', lambda _: True)(p))]
            reasons.append('scholarly core: configured source priority')
            if venue:
                extra = [p for p in ('dblp', 'openreview', 'acl_anthology') if p in self.configured]
                providers.extend(extra)
                reasons.append('venue intent: DBLP + OpenReview + ACL Anthology')
            elif category in ('evaluation', 'survey'):
                extra = [p for p in ('openreview', 'acl_anthology') if p in self.configured]
                providers.extend(extra)
                reasons.append(f'{category} intent: venue OA coverage')
            else:
                reasons.append('topic-only: no venue crawl')
            store = getattr(self.source, 'store', None)
            if store is not None:
                priority = {name: index for index, name in enumerate(store.enabled_sources())}
                providers.sort(key=lambda name: priority.get(name, 100))
        else:
            providers = list(self.configured)
            reasons.append('explicit source list')
        providers = list(dict.fromkeys(providers)) or (['arxiv'] if 'arxiv' in self.configured else self.configured[:1])
        deepseek_allowed = ('deepseek' in self.configured or self.preset) and bool(
            getattr(self.source, 'available', lambda name: False)('deepseek'))
        if deepseek_allowed:
            reasons.append('DeepSeek reserved for one coverage-gap call')
        variant = self._variant(task, query)
        queries = [query]
        if variant and variant != query:
            queries.append(variant)
            reasons.append('one deterministic query variant for ' +
                           ('category quota' if category != 'none' else 'recency'))
        plan = RetrievalPlan(providers, queries, reasons, deepseek_allowed, False)
        self.last_plan = plan
        return plan

    def _research_plan(self, task: dict, query: str, providers: list[str] | None,
                       intent: str) -> RetrievalPlan:
        """Choose a small set of directed requests, never an all-source spray."""
        notes, allowed = [], []
        hinted = providers is not None
        requested = list(dict.fromkeys(providers if hinted else self.configured))
        for name in requested:
            if name not in self.configured:
                notes.append(f'Source {name} is not configured; request was not executed.')
                continue
            if not getattr(self.source, 'available', lambda _: True)(name):
                notes.append(f'Source {name} is unavailable; request was not executed.')
                continue
            allowed.append(name)
        identity = venue_identity(task.get('venue'))
        kind = task.get('publication_type') or 'any'
        accepted = bool(task.get('accepted_only') or task.get('presentation_types'))
        catalog_kinds = {entry['name']: entry['type'] for entry in curated_venue_catalog()['venues']}
        preferred = curated_preferences(task.get('preferred_venues'), kind) if not task.get('venue') else []
        if task.get('preferred_venues') and preferred != task.get('preferred_venues'):
            notes.append('Soft venue hints were canonicalized against the bounded curated catalogue; '
                         'explicit venue and publication constraints take precedence.')
        if preferred:
            official_pool = [name for name in preferred if name in ('ICLR', 'NeurIPS', 'ICML')]
            if len(official_pool) == len(preferred):
                preference = ['openreview', 'openalex', 'semantic_scholar', 'arxiv']
            elif official_pool:
                preference = ['openalex', 'openreview', 'semantic_scholar', 'arxiv']
            else:
                preference = ['openalex', 'semantic_scholar', 'acl_anthology', 'dblp', 'arxiv']
            reason = 'Soft curated venue discovery: ' + ', '.join(preferred) + '; user hard conditions are unchanged.'
        elif identity in ('ACL', 'EMNLP', 'NAACL', 'EACL', 'TACL'):
            preference = ['acl_anthology', 'openalex', 'semantic_scholar', 'dblp', 'arxiv']
            reason = 'NLP venue evidence: prefer the official Anthology and scholarly metadata.'
        elif identity in ('ICLR', 'NeurIPS', 'ICML'):
            preference = ['openreview', 'openalex', 'semantic_scholar', 'dblp', 'arxiv']
            reason = 'Decision/presentation evidence requires an official publication record.'
        elif kind == 'journal' or catalog_kinds.get(identity) == 'journal':
            preference = ['openalex', 'semantic_scholar', 'dblp', 'arxiv']
            reason = (f'Journal discovery for {identity} uses precise venue metadata/ISSN.'
                      if task.get('venue') else
                      'Journal discovery uses journal metadata/ISSN rather than preprint prestige.')
        elif task.get('venue'):
            # A conference label is not evidence that OpenReview/Anthology
            # indexes it. CVPR/ICCV/ECCV and other indexed proceedings need
            # scholarly metadata, then DBLP for bibliographic corroboration.
            preference = ['openalex', 'semantic_scholar', 'dblp', 'arxiv']
            reason = f'{identity or task["venue"]} coverage uses scholarly venue metadata and DBLP.'
            if task.get('presentation_types'):
                notes.append('Requested presentation evidence is not covered by an official '
                             'adapter for this venue; hard conditions remain in force.')
        elif kind == 'preprint':
            preference = ['arxiv', 'semantic_scholar', 'openalex']
            reason = 'Preprint intent prioritizes arXiv discovery.'
        elif kind == 'conference' or accepted or task.get('presentation_types'):
            preference = ['openreview', 'openalex', 'semantic_scholar', 'acl_anthology', 'dblp', 'arxiv']
            reason = 'Conference/acceptance discovery starts with official publication evidence.'
        elif task.get('venue_scope') in ('preferred', 'preferred_only'):
            preference = ['openalex', 'semantic_scholar', 'openreview', 'acl_anthology', 'dblp', 'arxiv']
            reason = 'Publication constraints prioritize scholarly venue evidence.'
        else:
            preference = ['arxiv', 'openalex', 'semantic_scholar', 'openreview', 'acl_anthology', 'dblp']
            reason = 'Topic discovery combines preprint abstracts with scholarly publication metadata.'
        # An explicit model hint is an allow-list, not permission to add sources.
        ordered = ([name for name in requested if name in allowed] if hinted or len(requested) == 1 else
                   [name for name in preference if name in allowed])
        publication_intent = bool(task.get('venue') or accepted or kind not in ('any', '')
                                  or task.get('venue_scope') == 'preferred_only')
        if not hinted and len(requested) > 1 and 'lune' in allowed and not publication_intent:
            # Prefer the configured conference corpus for topic discovery.
            ordered = ['lune'] + [name for name in ordered if name != 'lune']
        ordered.extend(name for name in allowed if name not in ordered)
        if not publication_intent and self.config.source in ('multi', 'all') and getattr(self.source, 'store', None) is not None:
            priority = {name: index for index, name in enumerate(self.source.store.enabled_sources())}
            ordered.sort(key=lambda name: priority.get(name, 100))
        if preferred and not any(name in ('openalex', 'semantic_scholar', 'openreview', 'acl_anthology', 'dblp')
                                 for name in ordered):
            notes.append('The authorized source has no venue discovery capability; soft venue hints are not enforced.')
            preferred = []
        # Explicit lists remain allow-lists. Automatic venue routes must not
        # append unrelated official pools merely because they are configured.
        steps = []
        filters = {key: task.get(key) for key in ('venue', 'year_from', 'year_to', 'venue_scope',
                                                'publication_type', 'accepted_only', 'presentation_types')}
        filters['venue'] = str(filters.get('venue') or '')
        if preferred:
            filters['preferred_venues'] = list(preferred)
            official = [name for name in preferred if name in ('ICLR', 'NeurIPS', 'ICML')]
            if official:
                start = int(task.get('year_from') or self.today - 2)
                end = int(task.get('year_to') or self.today)
                editions = [f'{name}.cc/{year}/Conference' for year in range(end, max(start, end - 2) - 1, -1)
                            for name in official]
                filters['openreview_venue_ids'] = editions[:3]
                if len(editions) > 3:
                    notes.append('OpenReview soft-pool edition coverage is bounded to three requests; '
                                 'scholarly metadata supplies complementary venue/year coverage.')
        journal_issns = journal_issns_for_venue(filters['venue'])
        if journal_issns:
            filters['journal_issns'] = journal_issns
        if identity in ('ICLR', 'NeurIPS', 'ICML') and task.get('year_from') and task.get('year_to'):
            start, end = int(task['year_from']), int(task['year_to'])
            if end - start < 3:
                host = {'ICLR': 'ICLR.cc', 'NeurIPS': 'NeurIPS.cc', 'ICML': 'ICML.cc'}[identity]
                filters['openreview_venue_ids'] = [f'{host}/{year}/Conference' for year in range(start, end + 1)]
            else:
                notes.append('OpenReview edition IDs omitted for a broad year range; coverage must be assessed explicitly.')
        if ordered:
            first = ordered[0]
            steps.append({'provider': first, 'query': query, 'filters': dict(filters),
                          'intent': str(intent or 'topic_discovery'),
                          'purpose': ('Explicit web discovery; snippets do not satisfy abstract evidence.'
                                      if first == 'deepseek' else reason)})
            alternate = self._research_alternate(task, query, intent)
            if len(ordered) > 1:
                steps.append({'provider': ordered[1], 'query': alternate or query, 'filters': dict(filters),
                              'intent': 'complementary_source',
                              'purpose': 'Independent source coverage supplements the primary request; '
                                         'a query alias is optional.'})
        fallback_steps = []
        if preferred and ordered:
            used = [step['provider'] for step in steps]
            hard_publication = bool(task.get('accepted_only') or task.get('presentation_types')
                                    or kind not in ('any', '') or task.get('venue_scope') == 'preferred_only')
            candidates = ([name for name in ('openalex', 'semantic_scholar', 'openreview', 'acl_anthology')
                           if name in allowed] if hard_publication else
                          [name for name in ('arxiv', 'openalex', 'semantic_scholar', 'openreview', 'acl_anthology')
                           if name in allowed])
            fallback_provider = next((name for name in candidates if name not in used),
                                     candidates[0] if candidates else '')
            if fallback_provider:
                hard_filters = {key:value for key,value in filters.items()
                                if key not in ('preferred_venues', 'openreview_venue_ids')}
                fallback_steps.append({'provider':fallback_provider, 'query':query, 'filters':hard_filters,
                    'intent':'soft_venue_preference_fallback', 'soft_preference_fallback':True,
                    'from_preferred_venues':list(preferred),
                    'purpose':'One broad fallback if the soft venue pool yields no usable evidence; '
                              'only the soft source preference is removed, never the user hard conditions.'})
        elif len(ordered) > 2:
            fallback_steps.append({'provider': ordered[2], 'query': query, 'filters': dict(filters),
                'intent': 'evidence_gap_fallback',
                'purpose': 'One bounded fallback for empty discovery, missing abstracts, source failure, '
                           'or unmet publication evidence. Hard conditions are unchanged.'})
            # DBLP is bibliographic metadata, never a source of abstracts. An
            # alternative branch uses the same single fallback allowance to
            # enrich one already verified work, if arXiv was authorized.
            if (ordered[2] == 'dblp' and 'arxiv' in allowed
                    and 'arxiv' not in {step['provider'] for step in steps}):
                fallback_steps[0]['abstract_enrichment'] = {
                    'provider': 'arxiv', 'when': 'missing_abstracts',
                    'query_strategy': 'verified_work_title',
                    'identity_gate': 'shared DOI/arXiv ID or exact title and complete author set; no ID conflicts',
                    'publication_gate': 'original hard conditions applied to the existing work; no borrowed publication records'}
        # A paid discovery tool only enters ordered via an explicit source hint.
        if not steps:
            notes.append('No configured and available source can execute this search plan.')
        plan = RetrievalPlan(list(dict.fromkeys(step['provider'] for step in steps)),
                             [step['query'] for step in steps] or [query], [reason],
                             any(step['provider'] == 'deepseek' for step in steps + fallback_steps),
                             len(self.configured) == 1, True, steps, notes, fallback_steps)
        self.last_plan = plan
        return plan

    def _research_alternate(self, task: dict, query: str, intent: str) -> str:
        low = query.casefold()
        category = str(task.get('category') or '')
        goal = str(task.get('reading_goal') or '')
        if category == 'evaluation' or goal == 'comparison' or 'benchmark' in intent.casefold():
            if 'benchmark' not in low:
                return _clean_query(query + ' benchmark evaluation')
        if category == 'survey' or goal == 'orientation' or 'survey' in intent.casefold():
            if 'survey' not in low:
                return _clean_query(query + ' survey review')
        for phrase, alias in (('retrieval augmented generation', 'RAG'),
                              ('large language models', 'LLM'),
                              ('large language model', 'LLM'),
                              ('3d reconstruction', 'scene reconstruction')):
            if phrase in low:
                return _clean_query(re.sub(re.escape(phrase), alias, query, flags=re.I))
        return ''

    # -- scoring ---------------------------------------------------------
    def _authority(self, paper: Paper) -> float:
        value = float(paper.authority or 0.0)
        if value > 1.5:
            value /= 4.0
        return max(0.0, min(1.0, value))

    def _recency(self, paper: Paper, prefer_recent: bool) -> float:
        if not paper.year:
            return 0.3
        age = max(0, self.today - int(paper.year))
        if prefer_recent:
            return 1.0 if age <= 2 else 0.7 if age <= 5 else 0.3
        return 0.6 if age <= 5 else 0.5

    def _venue_score(self, paper: Paper, task: dict, query: str) -> float:
        venue = (paper.venue or '').lower()
        if not venue:
            return 0.2
        hit = any(word in venue for word in TOP_VENUE_WORDS)
        asked = any(word in (query + ' ' + str(task.get('topic') or '')).lower()
                    for word in VENUE_WORDS)
        if hit and asked:
            return 1.0
        return 0.8 if hit else 0.4

    def score(self, paper: Paper, task: dict, query: str) -> float:
        q = _terms(query + ' ' + str(task.get('topic') or '') + ' ' + str(task.get('purpose') or ''))
        title = _terms(paper.title)
        body = _terms(paper.title + ' ' + paper.abstract + ' ' + paper.snippet)
        if not q:
            lexical = 0.0
        else:
            lexical = 0.55 * (len(q & body) / len(q)) + 0.45 * (len(q & title) / len(q))
        score = (0.52 * lexical + 0.18 * self._authority(paper)
                 + 0.12 * self._venue_score(paper, task, query)
                 + 0.10 * self._recency(paper, bool(task.get('prefer_recent')))
                 + 0.05 * (1.0 if paper.open_access_url else 0.0)
                 + 0.03 * (0.0 if paper.metadata_only else 1.0))
        return round(min(1.0, max(0.0, score)), 6)

    def rank(self, papers: list[Paper], task: dict, query: str) -> tuple[list[Paper], dict[str, float]]:
        if getattr(self.config, 'research_mode', False):
            return self._research_rank(papers, task, query)
        scores = {p.id: self.score(p, task, query) for p in papers}
        ordered = sorted(papers, key=lambda p: (-scores[p.id], -(p.year or 0), p.title))
        return ordered, scores

    def _research_rank(self, papers: list[Paper], task: dict, query: str) -> tuple[list[Paper], dict[str, float]]:
        # This is candidate recall triage, not an asserted semantic judgement.
        # Protect topic overlap from static source prestige and preserve room for
        # more than one source/facet before the semantic assessor sees the pool.
        aliases = {'rag': 'retrieval augmented generation', 'llm': 'large language model',
                   'llms': 'large language models'}
        def terms(value):
            text = str(value or '').casefold().replace('-', ' ')
            for alias, expansion in aliases.items():
                text = re.sub(rf'\b{alias}\b', expansion, text)
            return _terms(text)
        wanted = terms(query + ' ' + str(task.get('topic') or ''))
        scores = {}
        for paper in papers:
            title = terms(paper.title)
            body = terms(paper.abstract)
            overlap = ((0.65 * len(wanted & title) + 0.35 * len(wanted & body)) / len(wanted)
                       if wanted else 0.0)
            scores[paper.id] = round(overlap, 6)
        remaining = sorted(papers, key=lambda paper: (-scores[paper.id],
                           -int(bool(paper.abstract)), -publication_preference(paper, task),
                           -(paper.year or 0) if task.get('prefer_recent') else 0, paper.id))
        ordered, seen_titles, source_counts = [], set(), {}
        while remaining:
            best_score = max(scores[paper.id] for paper in remaining)
            near = [paper for paper in remaining if scores[paper.id] >= best_score - 0.10]
            pick = min(near, key=lambda paper: (
                min((source_counts.get(source, 0) for source in paper.sources), default=0),
                -scores[paper.id], -int(bool(paper.abstract)), -publication_preference(paper, task),
                -(paper.year or 0) if task.get('prefer_recent') else 0, paper.id))
            remaining.remove(pick)
            title = ' '.join(re.findall(r'\w+', pick.title.casefold()))
            if title in seen_titles:
                continue
            seen_titles.add(title)
            ordered.append(pick)
            for source in pick.sources:
                source_counts[source] = source_counts.get(source, 0) + 1
        return ordered, scores

    def _constraints(self, papers: list[Paper], task: dict) -> list[Paper]:
        venue = str(task.get('venue') or '')
        year_from, year_to = task.get('year_from'), task.get('year_to')
        return [p for p in papers
                if venue_matches(p.venue, venue) and year_matches(p.year, year_from, year_to)]

    def _coverage(self, papers: list[Paper], task: dict) -> bool:
        target = int(task.get('target_count') or 5)
        usable = self._constraints(papers, task)
        usable = [p for p in usable if p.title and (p.abstract or p.snippet)]
        return len(usable) >= target

    # -- execution -------------------------------------------------------
    def retrieve(self, task: dict, query: str, limit: int = 10,
                 meter: CostMeter | None = None, allow_refine: bool = True, *,
                 providers: list[str] | None = None, intent: str = '') -> RetrievalResult:
        meter = meter or CostMeter()
        plan = self.plan(task, query, providers=providers, intent=intent)
        if plan.research:
            return self._research_retrieve(task, query, limit, meter, plan,
                                           allow_complement=allow_refine)
        batches: list[SearchBatch] = []
        papers: list[Paper] = []
        errors: list[str] = []
        cost = 0.0
        tool_cost_unknown = False
        paid_followups_blocked = False
        remote_outcomes = []

        def note_failure(value, providers):
            nonlocal tool_cost_unknown, paid_followups_blocked
            outcome = _paid_failure_outcome(value, providers)
            if outcome is not None:
                remote_outcomes.append({'providers': [p for p in providers if p in _PAID_PROVIDERS],
                                        'remote_outcome': outcome, 'retry_safe': False})
                if outcome != 'not_started':
                    tool_cost_unknown = True
                    paid_followups_blocked = True

        filters = {'venue': str(task.get('venue') or ''),
                   'year_from': task.get('year_from'), 'year_to': task.get('year_to')}
        if not any(filters.values()):
            filters = None

        def run(q: str, providers: list[str]) -> SearchBatch | None:
            nonlocal cost, tool_cost_unknown
            if paid_followups_blocked:
                providers = [p for p in providers if p not in _PAID_PROVIDERS]
                if not providers:
                    return None
            budget = getattr(self.source, 'budget', None)
            if budget is not None:
                if budget.cancel_token.requested:
                    return None
                try:
                    budget.remaining()
                except GuideError:
                    return None
            handle = meter.reserve('search', 1.0, {'query': q, 'providers': list(providers),
                                                    'router': True, 'filters': filters or {}})
            try:
                if self.tools is not None:
                    observation = self.tools.search(q, limit, providers=providers, filters=filters)
                    if not observation.ok:
                        raise _observation_error(observation)
                    batch = observation.data
                else:
                    batch = self.source.search_batch(q, limit, providers=providers, filters=filters)
            except RunCancelled as exc:
                note_failure(exc, providers)
                partial = retained_partial_batch(exc, q, providers, limit)
                if partial is not None:
                    meter.settle(handle, 1.0)
                    cost += 1.0
                    batches.append(partial)
                    return partial
                meter.settle(handle, 0.0)
                return None
            except GuideError as exc:
                note_failure(exc, providers)
                partial = retained_partial_batch(exc, q, providers, limit)
                if partial is not None:
                    meter.settle(handle, 1.0)
                    cost += 1.0
                    batches.append(partial)
                    errors.append(str(exc))
                    return partial
                meter.settle(handle, 0.0)
                if budget is not None and budget.cancel_token.requested:
                    return None
                errors.append(str(exc))
                return None
            except Exception as exc:
                note_failure(exc, providers)
                meter.settle(handle, 1.0)
                cost += 1.0
                errors.append(f'unknown execution failure ({type(exc).__name__}): {exc}')
                return None
            except BaseException:
                meter.settle(handle, 1.0)
                cost += 1.0
                raise
            ok = any(r.status in ('ok', 'empty', 'cache') for r in batch.runs)
            meter.settle(handle, 1.0 if ok else 0.0)
            cost += 1.0 if ok else 0.0
            batches.append(batch)
            for run in batch.runs:
                if run.status == 'error':
                    note_failure(run, [run.name])
            errors.extend(str(r.error) for r in batch.runs if r.status == 'error')
            return batch

        first = run(plan.queries[0], plan.providers)
        if first:
            papers = self._constraints(first.papers, task)
        if allow_refine and len(plan.queries) > 1 and not self._coverage(papers, task):
            second = run(plan.queries[1], plan.providers)
            if second:
                papers = self._constraints(merge_papers(papers + second.papers), task)
        if (allow_refine and plan.deepseek_allowed and not self._coverage(papers, task)
                and getattr(self.source, 'deepseek_calls', 0) < 1):
            third = run(plan.queries[0], ['deepseek'])
            if third:
                papers = self._constraints(merge_papers(papers + third.papers), task)

        ranked, scores = self.rank(papers, task, query)
        # Apply the assessment budget only after all providers participated in
        # ranking; provider order must not decide which candidates survive.
        ranked = ranked[:limit]
        return RetrievalResult(ranked, plan, batches, scores, cost, errors,
                               tool_cost_unknown=tool_cost_unknown,
                               paid_followups_blocked=paid_followups_blocked,
                               remote_outcomes=remote_outcomes)

    def _research_retrieve(self, task: dict, query: str, limit: int, meter: CostMeter,
                           plan: RetrievalPlan, *, allow_complement: bool) -> RetrievalResult:
        # Prefetch is used only when the source path is direct. ToolRegistry
        # remains the governed execution path; no worker may bypass it.
        can_prefetch = (allow_complement and len(plan.steps) > 1
                        and getattr(self.source, 'store', None) is not None
                        and self.tools is None)
        if can_prefetch:
            from source_parallel import DirectedPrefetch
            with DirectedPrefetch(self.source, plan.steps, limit, settle=meter.settle) as prefetch:
                admitted = []
                try:
                    for index, step in enumerate(plan.steps[:2]):
                        budget = getattr(self.source, 'budget', None)
                        if budget is not None:
                            if budget.cancel_token.requested:
                                break
                            try:
                                budget.remaining()
                            except GuideError:
                                break
                        if not getattr(self.source, 'available', lambda _: True)(step['provider']):
                            continue
                        handle = meter.reserve('search', 1.0, {'research': True, **step})
                        admitted.append((index, step, handle))
                    for index, step, handle in admitted:
                        prefetch.submit(index, step, handle)
                except BaseException:
                    submitted = set(prefetch.futures)
                    for index, _step, handle in admitted:
                        if index not in submitted:
                            meter.settle(handle, 0.0)
                    raise
                result = self._execute_research(task, query, limit, meter, plan,
                                                allow_complement=True, prefetch=prefetch)
            result.cost += sum(float(record['units']) for record in prefetch.drain_records)
            if any(record['index'] < len(plan.steps) and
                   plan.steps[record['index']].get('provider') in _PAID_PROVIDERS
                   for record in prefetch.drain_records):
                result.tool_cost_unknown = True
                result.paid_followups_blocked = True
                result.remote_outcomes.append({'providers': [], 'remote_outcome': 'unknown',
                                               'retry_safe': False})
            return result
        return self._execute_research(task, query, limit, meter, plan,
                                      allow_complement=allow_complement)

    def _execute_research(self, task: dict, query: str, limit: int, meter: CostMeter,
                          plan: RetrievalPlan, *, allow_complement: bool, prefetch=None) -> RetrievalResult:
        papers, batches, errors, cost = [], [], list(plan.notes), 0.0
        executions = []
        tool_cost_unknown = False
        paid_followups_blocked = False
        remote_outcomes = []

        def note_failure(value, provider):
            nonlocal tool_cost_unknown, paid_followups_blocked
            outcome = _paid_failure_outcome(value, [provider])
            if outcome is not None:
                remote_outcomes.append({'providers': [provider], 'remote_outcome': outcome,
                                        'retry_safe': False})
                if outcome != 'not_started':
                    tool_cost_unknown = True
                    paid_followups_blocked = True
            return outcome

        def retain_partial(exc, step, index, handle, already_started, execution, target):
            nonlocal cost, papers
            batch = retained_partial_batch(exc, step['query'], [step['provider']], limit)
            if batch is None:
                return False
            if already_started:
                if prefetch.settle(index, 1.0):
                    cost += 1.0
            else:
                meter.settle(handle, 1.0)
                cost += 1.0
            batches.append(batch)
            if target:
                papers, identity_matches = self._enrich_verified_abstract(papers, target, batch.papers)
                execution['identity_matches'] = identity_matches
            else:
                incoming = batch.papers
                pool = step['filters'].get('preferred_venues') or []
                if pool:
                    incoming = [paper for paper in incoming if preferred_venue_matches(paper, pool)]
                papers = merge_papers(papers + incoming)
            execution.update(status='partial', raw_candidates=len(batch.papers),
                             stop_reason=batch.stop_reason)
            return True

        def gap() -> str:
            """A recall/evidence signal, never a claim of semantic coverage.

            A snippet cannot authorize assessment. Nor can a preprint borrow a
            conference label to clear a hard publication requirement. Merging
            happens before this check so complementary metadata/abstracts from
            the same work can jointly form an eligible candidate.
            """
            if not papers:
                answered = any(run.status in ('ok', 'empty', 'cache')
                               for batch in batches for run in batch.runs)
                return 'empty_candidates' if answered else 'source_failure'
            eligible = [paper for paper in papers if publication_matches(paper, task)]
            if not eligible:
                return 'publication_evidence_gap'
            usable = sum(bool(paper.title.strip() and paper.abstract.strip()) for paper in eligible)
            if not usable:
                return 'missing_abstracts'
            if usable < int(task.get('minimum_count') or task.get('target_count') or 5):
                return 'usable_candidate_shortfall'
            return ''

        # Two complementary providers are independent of query rewriting. Only
        # an unresolved evidence gap authorizes the one advertised fallback.
        # A single-pass caller still authorizes just the primary step.
        steps = (plan.steps + plan.fallback_steps if allow_complement else plan.steps[:1])
        for index, step in enumerate(steps):
            fallback = index >= len(plan.steps)
            trigger = gap() if fallback else ('primary' if index == 0 else 'complementary_source')
            if fallback and not trigger and step.get('soft_preference_fallback'):
                usable = sum(bool(paper.title.strip() and paper.abstract.strip())
                             and publication_matches(paper, task) for paper in papers)
                if usable < int(task.get('target_count') or 5):
                    trigger = 'usable_candidate_shortfall'
            if fallback and not trigger:
                break
            step = dict(step)
            target = None
            if fallback and trigger == 'missing_abstracts' and step['provider'] == 'dblp':
                enrichment = step.get('abstract_enrichment')
                if enrichment:
                    verified = [paper for paper in papers if publication_records(paper)
                                and publication_matches(paper, task)
                                and paper.title.strip() and not paper.abstract.strip()]
                    ranked, _ = self.rank(verified, task, query)
                    target = ranked[0] if ranked else None
                if not target:
                    errors.append('No authorized abstract-capable fallback can enrich the verified candidates.')
                    break
                step.update(provider=enrichment['provider'], query=_clean_query(target.title),
                            intent='verified_work_abstract_enrichment', filters={},
                            target_id=target.id, hard_filters=dict(step['filters']),
                            purpose='Retrieve an abstract for an existing verified work; identity and final '
                                    'publication gates remain mandatory.')
            budget = getattr(self.source, 'budget', None)
            if budget is not None:
                if budget.cancel_token.requested:
                    break
                try:
                    budget.remaining()
                except GuideError as exc:
                    errors.append(str(exc))
                    break
            execution = {'provider': step['provider'], 'query': step['query'],
                         'intent': step['intent'], 'trigger': trigger,
                         'raw_candidates': 0, 'eligible_candidates': 0, 'abstract_candidates': 0}
            if step.get('soft_preference_fallback'):
                execution.update(soft_preference_fallback=True,
                                 from_preferred_venues=list(step['from_preferred_venues']))
            if step['filters'].get('preferred_venues'):
                execution['preferred_venues'] = list(step['filters']['preferred_venues'])
            if target:
                execution.update(target_id=target.id, hard_filters=dict(step['hard_filters']),
                                 lookup_filters=dict(step['filters']))
            executions.append(execution)
            # Health can change while the preceding provider is running. An
            # unavailable source is observable but spends no reservation.
            already_started = prefetch is not None and prefetch.is_submitted(index)
            if paid_followups_blocked and step['provider'] in _PAID_PROVIDERS:
                if not already_started:
                    execution.update(status='not_started', reason='unresolved_paid_outcome')
                    continue
                # A prefetched operation may already be in flight and cannot be
                # undone. Consume its result without claiming it was prevented.
                execution['prior_paid_outcome_unconfirmed'] = True
            if not already_started and not getattr(self.source, 'available', lambda _: True)(step['provider']):
                execution['status'] = 'unavailable'
                errors.append(f'{step["provider"]}: source became unavailable before execution.')
                continue
            if already_started:
                handle = prefetch.handle(index)
            else:
                handle = meter.reserve('search', 1.0, {'research': True, **step})
            try:
                if already_started:
                    batch = prefetch.take(index)
                elif self.tools is not None:
                    observation = self.tools.search(step['query'], limit,
                        providers=[step['provider']], filters=step['filters'])
                    if not observation.ok:
                        raise _observation_error(observation)
                    batch = observation.data
                else:
                    batch = self.source.search_batch(step['query'], limit,
                        providers=[step['provider']], filters=step['filters'])
                ok = any(run.status in ('ok', 'empty', 'cache') for run in batch.runs)
            except RunCancelled as exc:
                outcome = note_failure(exc, step['provider'])
                if outcome is not None:
                    execution.update(remote_outcome=outcome, retry_safe=False)
                if not retain_partial(exc, step, index, handle, already_started, execution, target):
                    if already_started:
                        prefetch.settle(index, 0.0)
                    else:
                        meter.settle(handle, 0.0)
                    execution['status'] = 'cancelled'
                break
            except GuideError as exc:
                outcome = note_failure(exc, step['provider'])
                if outcome is not None:
                    execution.update(remote_outcome=outcome, retry_safe=False)
                partial = retain_partial(exc, step, index, handle, already_started, execution, target)
                if not partial:
                    if already_started:
                        prefetch.settle(index, 0.0)
                    else:
                        meter.settle(handle, 0.0)
                    execution['status'] = 'error'
                execution['error'] = str(exc)
                if isinstance(exc, BudgetExceeded) or (budget is not None and budget.cancel_token.requested):
                    break
                errors.append(f'{step["provider"]}: {exc}')
                continue
            except Exception as exc:
                outcome = note_failure(exc, step['provider'])
                if outcome is not None:
                    execution.update(remote_outcome=outcome, retry_safe=False)
                # The request may have reached a remote service before an
                # unclassified worker/adapter failure. Conservatively settle
                # the full unit and expose the uncertainty to the caller.
                if already_started:
                    prefetch.settle(index, 1.0)
                else:
                    meter.settle(handle, 1.0)
                cost += 1.0
                execution.update(status='unknown_error', error=f'{type(exc).__name__}: {exc}',
                                 settlement='conservative_unknown')
                errors.append(f'{step["provider"]}: unknown execution failure ({type(exc).__name__})')
                continue
            except BaseException:
                if already_started:
                    prefetch.settle(index, 1.0)
                else:
                    meter.settle(handle, 1.0)
                cost += 1.0
                raise
            settled = 1.0 if ok else 0.0
            if already_started:
                if prefetch.settle(index, settled):
                    cost += settled
            else:
                meter.settle(handle, settled)
                cost += settled

            batches.append(batch)
            for run in batch.runs:
                if run.status == 'error':
                    outcome = note_failure(run, run.name)
                    if outcome is not None:
                        execution.update(remote_outcome=outcome, retry_safe=False)
            errors.extend(f'{run.name}: {run.error}' for run in batch.runs if run.status == 'error')
            if target:
                papers, identity_matches = self._enrich_verified_abstract(papers, target, batch.papers)
                execution.update(identity_matches=identity_matches,
                                 enriched_candidates=len(identity_matches),
                                 ignored_candidates=len(batch.papers) - len(identity_matches))
            else:
                incoming = batch.papers
                pool = step['filters'].get('preferred_venues') or []
                if pool:
                    incoming = [paper for paper in incoming if preferred_venue_matches(paper, pool)]
                    execution['outside_pool_candidates'] = len(batch.papers) - len(incoming)
                    resolution = getattr(self.source, 'preference_resolution', {}).get(step['provider'])
                    if resolution:
                        execution['preference_resolution'] = dict(resolution)
                papers = merge_papers(papers + incoming)
            eligible_batch = [paper for paper in batch.papers if publication_matches(paper, task)]
            execution.update(status=('ok' if batch.papers else 'empty') if ok else 'error',
                             raw_candidates=len(batch.papers),
                             eligible_candidates=len(eligible_batch),
                             abstract_candidates=sum(bool(paper.title.strip() and paper.abstract.strip())
                                                     for paper in eligible_batch),
                             remaining_gap=gap())
        eligible = [paper for paper in papers if publication_matches(paper, task)]
        ranked, scores = self.rank(eligible, task, query)
        return RetrievalResult(ranked[:limit], plan, batches, scores, cost, errors, executions, gap(),
                               tool_cost_unknown=tool_cost_unknown,
                               paid_followups_blocked=paid_followups_blocked,
                               remote_outcomes=remote_outcomes)

    @staticmethod
    def _enrich_verified_abstract(papers: list[Paper], target: Paper,
                                  candidates: list[Paper]) -> tuple[list[Paper], list[dict]]:
        """Copy only an identified work's abstract, never its publication facts.

        The unconstrained title request may return other works. Shared IDs are
        strongest; an exact substantial title also requires the complete same
        author set and no contradictory identifiers. This deliberately does
        not weaken general deduplication's rule against title-only acceptance
        transfer or introduce unrelated results through an enrichment request.
        """
        matches = []
        target_doi, target_arxiv = normalize_doi(target.doi), arxiv_id(target.arxiv_id)
        target_title = title_key(target.title)
        target_authors = {title_key(author) for author in target.authors if title_key(author)}
        for candidate in candidates:
            if not candidate.abstract.strip() or not publication_matches(candidate, {}):
                continue
            candidate_doi, candidate_arxiv = normalize_doi(candidate.doi), arxiv_id(candidate.arxiv_id)
            if ((target_doi and candidate_doi and target_doi != candidate_doi)
                    or (target_arxiv and candidate_arxiv and target_arxiv != candidate_arxiv)):
                continue
            identity = 'doi' if target_doi and target_doi == candidate_doi else \
                'arxiv_id' if target_arxiv and target_arxiv == candidate_arxiv else ''
            if not identity and (len(target_title.split()) >= 5 and target_title == title_key(candidate.title)
                    and target_authors and target_authors == {
                        title_key(author) for author in candidate.authors if title_key(author)}):
                identity = 'strict_title_authors'
            if identity:
                matches.append((candidate, identity))
        if not matches:
            return papers, []
        candidate, identity = max(matches, key=lambda item: len(item[0].abstract))
        enriched = replace(target, abstract=candidate.abstract,
            abstract_source=candidate.abstract_source or candidate.source, metadata_only=False,
            sources=list(dict.fromkeys(target.sources + candidate.sources)),
            from_query=list(dict.fromkeys(target.from_query + candidate.from_query)),
            open_access_url=target.open_access_url or candidate.open_access_url,
            arxiv_id=target.arxiv_id or candidate.arxiv_id)
        return ([enriched if paper.id == target.id else paper for paper in papers],
                [{'paper_id': target.id, 'source_id': candidate.id, 'identity': identity}])

    def observation(self) -> dict:
        return self.last_plan.as_dict() if self.last_plan else {}
