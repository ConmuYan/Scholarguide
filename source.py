"""Academic source providers, bounded HTTP, cache and canonical deduplication.

One ``AcademicSource`` can query a single provider (the main arXiv experiment) or
a list of providers. Every provider returns the same ``Paper`` shape, with
provenance in ``sources`` and ``abstract_source``. Network failures are contained
per provider: a multi-source search degrades to the providers that answered.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from contextlib import contextmanager, nullcontext
from functools import wraps
import socket
import threading
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request as HttpRequest
import xml.etree.ElementTree as ET

from common import (Budget, BudgetExceeded, Config, GuideError, Paper, REMOTE_OUTCOMES, RunCancelled, SOURCE_NAMES, source_names,
                    merge_publication_records, requested_venues, utc_now, venue_matches, year_matches)
import hashlib
from execution_events import operation
from network import source_urlopen as urlopen, TransportDeadlineExceeded
from vendor.gptr_arxiv import ArxivSearch, arxiv_id

DEFAULT_MULTI = ('arxiv', 'openalex', 'semantic_scholar', 'dblp', 'openreview', 'acl_anthology')
ARXIV_RATE_PAUSE_SECONDS = 15 * 60
DEEPSEEK_SEARCH_URL = 'https://api.deepseek.com/v1/tools/search'
VERBATIM_SEARCH_URL = 'https://verbatim.krlabs.eu/api/v1/papers/search'
S2_SEARCH_URL = 'https://api.semanticscholar.org/graph/v1/paper/search'
S2_BULK_URL = 'https://api.semanticscholar.org/graph/v1/paper/search/bulk'
DBLP_SEARCH_URL = 'https://dblp.org/search/publ/api'
OPENREVIEW_SEARCH_URL = 'https://api2.openreview.net/notes/search'
OPENREVIEW_GROUPS_URL = 'https://api2.openreview.net/groups'
OPENALEX_WORKS_URL = 'https://api.openalex.org/works'
OPENALEX_SOURCES_URL = 'https://api.openalex.org/sources'
ACL_BASE = 'https://aclanthology.org'
OPENREVIEW_BASE = 'https://openreview.net'


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    kind: str
    needs_key: bool = False
    rate_seconds: float = 0.0
    description: str = ''
    supports_venue: bool = False
    supports_year: bool = False


PROVIDERS = {
    'arxiv': ProviderSpec('arxiv', 'arXiv', 'preprint', rate_seconds=4.0,
                          description='Atom API; abstracts and PDFs.', supports_year=True),
    'openalex': ProviderSpec('openalex', 'OpenAlex', 'scholarly', rate_seconds=0.2,
                             description='Works + inverted abstracts; optional key.',
                             supports_venue=True, supports_year=True),
    'semantic_scholar': ProviderSpec('semantic_scholar', 'Semantic Scholar', 'scholarly',
                                     rate_seconds=1.1,
                                     description='Graph API; abstract, venue, OA PDF.',
                                     supports_venue=True, supports_year=True),
    'dblp': ProviderSpec('dblp', 'DBLP', 'metadata', rate_seconds=1.0,
                         description='Venue/author metadata; no abstract.',
                         supports_venue=True, supports_year=True),
    'openreview': ProviderSpec('openreview', 'OpenReview', 'venue', rate_seconds=0.6,
                               description='ICLR/NeurIPS/ICML notes; abstract + PDF.',
                               supports_venue=True, supports_year=True),
    'acl_anthology': ProviderSpec('acl_anthology', 'ACL Anthology', 'venue', rate_seconds=0.6,
                                  description='ACL/EMNLP/NAACL papers; metadata page abstract.',
                                  supports_venue=True, supports_year=True),
    'lune': ProviderSpec('lune', 'Lune Research', 'scholarly', needs_key=True,
                         rate_seconds=1.0, description='MCP conference corpus; abstract and full-text discovery.',
                         supports_venue=True, supports_year=True),
    'deepseek': ProviderSpec('deepseek', 'DeepSeek Search', 'web', needs_key=True,
                              rate_seconds=0.2,
                              description='Official web search; discovery fallback.'),
}


def provider_capabilities(name: str) -> dict:
    spec = PROVIDERS.get(name)
    if spec is None:
        return {}
    return {'name': spec.name, 'label': spec.label, 'kind': spec.kind,
            'supports_venue': spec.supports_venue, 'supports_year': spec.supports_year,
            'needs_key': spec.needs_key, 'description': spec.description}


def curated_preferences(value, publication_type: str = 'any') -> list[str]:
    """Bounded soft discovery hints, never arbitrary query syntax or hard facts."""
    from research_policy import curated_venue_catalog, venue_identity
    if not isinstance(value, list) or publication_type == 'preprint':
        return []
    catalog = {entry['name']: entry['type'] for entry in curated_venue_catalog()['venues']}
    names = []
    for raw in value[:16]:
        if not isinstance(raw, str):
            continue
        name = venue_identity(raw)
        if (name in catalog and name not in names
                and (publication_type not in ('journal', 'conference') or catalog[name] == publication_type)):
            names.append(name)
            if len(names) == 4:
                break
    return names


def preferred_venue_matches(paper: Paper, names: list[str]) -> bool:
    """Discovery membership only; acceptance still needs publication_matches.

    A source venue filter can return unrelated or repository records. Verify
    exact catalogue identities client-side, including imported display aliases,
    without promoting S2's unknown acceptance to a verified publication event.
    """
    from research_policy import venue_identity
    values = [paper.venue]
    values.extend(record.get('venue_name') for record in _publication_records(paper.publication))
    return any(venue_identity(value) in names for value in values if value)


def _preprint_repository(publication: dict, journal: dict, display: str, doi: str) -> str:
    names = [display, publication.get('name'), journal.get('name')]
    names.extend(_as_list(publication.get('alternate_names')))
    repositories = {'arxiv': 'arXiv', 'arxiv org': 'arXiv', 'biorxiv': 'bioRxiv',
                    'medrxiv': 'medRxiv', 'ssrn': 'SSRN', 'research square': 'Research Square'}
    for value in names:
        identity = ' '.join(re.findall(r'[a-z0-9]+', str(value or '').casefold()))
        if identity in repositories:
            return repositories[identity]
    try:
        host = urlsplit(str(publication.get('url') or '')).hostname
    except ValueError:
        host = ''
    if host in ('arxiv.org', 'export.arxiv.org', 'www.arxiv.org') or doi.startswith('10.48550/arxiv.'):
        return 'arXiv'
    if publication.get('type') == 'repository':
        return str(publication.get('name') or 'preprint repository')
    return ''


class SourceTransportError(GuideError):
    """A safe public failure with the transport category preserved."""
    def __init__(self, host, error):
        reason = getattr(error, 'reason', error)
        self.original_type = type(reason).__name__
        lowered = str(reason).casefold()
        if isinstance(reason, TimeoutError) or 'timed out' in lowered or lowered == 'timeout':
            self.code, detail = 'timeout', 'request timed out'
        elif isinstance(reason, socket.gaierror) or 'gaierror' in lowered or 'dns' in lowered or 'name resolution' in lowered or 'getaddrinfo' in lowered:
            self.code, detail = 'dns_error', 'DNS resolution failed'
        elif isinstance(reason, ConnectionError) or any(word in lowered for word in ('refused', 'reset', 'aborted', 'connectionerror')):
            self.code, detail = 'connection_error', 'connection failed'
        else:
            self.code, detail = 'transport', 'network transport failed'
        super().__init__(f'{host}: {detail} ({self.original_type}).')


_LOCAL_REQUEST_GATES = {}
_LOCAL_GATE_LOCK = threading.Lock()
_LOCAL_SLOT_CONTEXT = ContextVar('local_source_slot_context', default=())


def _reset_local_slots_after_fork():
    global _LOCAL_REQUEST_GATES, _LOCAL_GATE_LOCK, _LOCAL_SLOT_CONTEXT
    _LOCAL_REQUEST_GATES = {}
    _LOCAL_GATE_LOCK = threading.Lock()
    _LOCAL_SLOT_CONTEXT = ContextVar('local_source_slot_context', default=())


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_local_slots_after_fork)


def classify_source_error(error: str) -> str:
    code = getattr(error, 'code', None)
    if code in ('dns_error', 'connection_error', 'transport', 'timeout',
                'quota_no_snapshot', 'quota_snapshot_expired', 'quota_exhausted',
                'quota_pending_reserved', 'quota_snapshot_corrupt', 'quota_ledger_unavailable',
                'quota_store_closed'):
        return code
    text = str(error or '').casefold()
    if 'dns resolution failed' in text or 'gaierror' in text or 'getaddrinfo' in text or 'name resolution' in text:
        return 'dns_error'
    if 'connection failed' in text or 'connection refused' in text or 'connection reset' in text:
        return 'connection_error'
    if 'network transport failed' in text:
        return 'transport'
    if '402' in text or 'quota_exhausted' in text or 'lune_request_budget_exhausted' in text:
        return 'quota_exhausted'
    if ('429' in text or 'rate limit' in text or 'rate exceeded' in text
            or 'too many requests' in text):
        return 'rate_limited'
    if 'challenge' in text or 'bot protection' in text or 'captcha' in text:
        return 'bot_challenge'
    if 'timeout' in text or 'timed out' in text or 'exceeded' in text:
        return 'timeout'
    if 'could not resolve venue' in text or 'invalid venue' in text:
        return 'venue_resolve'
    if '401' in text or '403' in text or 'unauthorized' in text or 'missing key' in text:
        return 'auth'
    if 'invalid' in text or 'parse' in text or 'json' in text:
        return 'invalid_response'
    if '404' in text or 'not found' in text:
        return 'not_found'
    return 'source_error'


@dataclass
class SourceRun:
    name: str
    status: str
    returned: int = 0
    error: str = ''
    elapsed_ms: int = 0
    cost_cny: float | None = 0.0
    kind: str = 'ok'
    code: str = ''
    remote_outcome: str | None = None
    retry_safe: bool | None = None
    audit_status: str | None = None

    def as_dict(self) -> dict:
        result = {'name': self.name, 'status': self.status, 'returned': self.returned,
                  'error': self.error, 'elapsed_ms': self.elapsed_ms, 'cost_cny': self.cost_cny,
                  'kind': self.kind, 'code': self.code}
        result.update(_safe_outcome(self))
        if self.audit_status == 'failed':
            result['audit_status'] = 'failed'
        return result


def _safe_outcome(value):
    result = {}
    remote = getattr(value, 'remote_outcome', None)
    if isinstance(remote, str) and remote in REMOTE_OUTCOMES:
        result['remote_outcome'] = remote
    retry = getattr(value, 'retry_safe', None)
    if type(retry) is bool:
        result['retry_safe'] = retry
    return result


def _unknown_post(error):
    error.remote_outcome = 'unknown'
    error.retry_safe = False
    return error


@dataclass
class SearchBatch:
    query: str
    providers: list[str]
    papers: list[Paper]
    runs: list[SourceRun]
    stop_reason: str = ''

    def summary(self) -> dict:
        result = {'query': self.query, 'providers': list(self.providers),
                  'returned': len(self.papers), 'runs': [r.as_dict() for r in self.runs],
                  'errors': [r.as_dict() for r in self.runs if r.status == 'error'],
                  'skipped': [r.name for r in self.runs if r.status == 'skipped']}
        if self.stop_reason:
            result['stop_reason'] = self.stop_reason
        return result


def retained_partial_batch(error, query: str, providers, limit: int) -> SearchBatch | None:
    """Accept only a bounded, source-produced partial batch for this request."""
    batch = getattr(error, 'partial_batch', None)
    if not isinstance(providers, (list, tuple)) or not all(isinstance(name, str) for name in providers):
        return None
    allowed = set(providers)
    if (not isinstance(batch, SearchBatch) or batch.query != query or not allowed
            or batch.stop_reason not in ('user_cancelled', 'budget_exceeded', 'source_error', 'tool_timeout')
            or not isinstance(batch.providers, list)
            or not all(isinstance(name, str) for name in batch.providers)
            or not set(batch.providers) <= allowed
            or not isinstance(batch.runs, list) or not batch.runs
            or not isinstance(batch.papers, list)
            or len(batch.papers) > limit * max(1, len(allowed))
            or not all(isinstance(run, SourceRun) and run.name in allowed for run in batch.runs)
            or not all(isinstance(paper, Paper) for paper in batch.papers)):
        return None
    final = batch.runs[-1]
    completed_prior = any(run.status in ('ok', 'empty', 'cache')
                          and run.remote_outcome == 'completed' and run.retry_safe is False
                          for run in batch.runs[:-1])
    ordered_runs = ([run.name for run in batch.runs] == batch.providers[:len(batch.runs)]
                    and len(set(batch.providers)) == len(batch.providers))
    interrupted_not_started = (batch.stop_reason in ('user_cancelled', 'budget_exceeded')
        and ordered_runs and completed_prior and final.status == 'not_started'
        and final.remote_outcome == 'not_started' and type(final.retry_safe) is bool
        and getattr(error, 'remote_outcome', None) == final.remote_outcome
        and getattr(error, 'retry_safe', None) is final.retry_safe)
    interrupted_partial = (batch.stop_reason in ('user_cancelled', 'budget_exceeded')
        and ordered_runs and completed_prior and final.status == 'partial'
        and final.remote_outcome in ('unknown', 'error', 'response_received')
        and type(final.retry_safe) is bool
        and getattr(error, 'remote_outcome', None) == final.remote_outcome
        and getattr(error, 'retry_safe', None) is final.retry_safe)
    local_cancelled = (batch.stop_reason == 'user_cancelled' and ordered_runs
        and any(run.status in ('ok', 'empty', 'cache') and run.remote_outcome == 'completed'
                and run.retry_safe is False for run in batch.runs)
        and all(run.status in ('ok', 'empty', 'cache', 'skipped')
                and run.remote_outcome in (None, 'completed', 'not_started')
                for run in batch.runs))
    if not (interrupted_not_started or interrupted_partial or local_cancelled
            or (final.status == 'partial' and final.remote_outcome in ('unknown', 'error', 'response_received')
             and final.retry_safe is False)
            or (batch.stop_reason == 'source_error' and ordered_runs
                and final.name == 'lune' and final.status in ('ok', 'empty')
                and final.audit_status == 'failed' and final.remote_outcome == 'completed'
                and final.retry_safe is False
                and getattr(error, 'remote_outcome', None) == 'completed'
                and getattr(error, 'retry_safe', None) is False)
            or (batch.stop_reason == 'budget_exceeded' and final.status in ('ok', 'empty')
                and final.remote_outcome == 'completed' and final.retry_safe is False)
            or (batch.stop_reason == 'tool_timeout' and any(
                run.status in ('ok', 'empty', 'cache') and run.remote_outcome == 'completed'
                and run.retry_safe is False for run in batch.runs))):
        return None
    return deepcopy(batch)


def _authority(value):
    try:
        number = float(value or 0)
        return max(0., min(number, 4.)) if number == number else 0.
    except (TypeError, ValueError):
        return 0.


def normalize_doi(value: str) -> str:
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', str(value or '').strip(), flags=re.I).lower()
    value = value.split('?', 1)[0].split('#', 1)[0].rstrip('.,;:)')
    return value if re.fullmatch(r'10\.\d{4,9}/\S+', value) else ''


def title_key(value: str) -> str:
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', str(value or '')).casefold()))


def _clean(value) -> str:
    text = unescape(str(value or ''))
    text = re.sub(r'<[^>]+>', ' ', text)
    return ' '.join(text.split())


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _year(value) -> int | None:
    match = re.search(r'(?:19|20)\d{2}', str(value or ''))
    return int(match.group(0)) if match else None


def _value(content: dict, key: str):
    item = (content or {}).get(key)
    return item.get('value') if isinstance(item, dict) else item


def _issns(values) -> list[str]:
    """Canonical print/electronic ISSNs; names and arbitrary strings are not IDs."""
    result = []
    for value in _as_list(values):
        value = str(value or '').upper().replace('-', '').strip()
        if re.fullmatch(r'\d{7}[\dX]', value):
            canonical = value[:4] + '-' + value[4:]
            if canonical not in result:
                result.append(canonical)
    return result


def _date(value, year=None) -> str:
    text = str(value or '')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        try:
            datetime.strptime(text, '%Y-%m-%d')
            return text
        except ValueError:
            pass
    return str(year) if isinstance(year, int) and 1900 <= year <= 2100 else ''


def _ms_date(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return ''


def _publication(records: list[dict], conflicts: list | None = None) -> dict:
    return merge_publication_records(records, conflicts)


def _publication_records(publication: dict) -> list[dict]:
    if not isinstance(publication, dict) or not publication:
        return []
    records = publication.get('records')
    return records if isinstance(records, list) else [publication]


def _evidence(source: str, url: str, field: str, value) -> dict:
    return {'source': source, 'url': url, 'field': field, 'value': value}


def _record(kind='unknown', status='unknown', **facts) -> dict:
    record = {'type': kind, 'status': status, 'presentation': 'unknown', 'venue_id': '',
            'venue_name': '', 'date': '', 'issn': [], 'evidence': [], 'verified': False,
            'identity_verified': True, 'observed_at': utc_now(), **facts}
    record.setdefault('date_precision', 'day' if len(str(record['date'])) == 10
                      else 'year' if len(str(record['date'])) == 4 else 'unknown')
    return record


def merge_papers(papers: list[Paper]) -> list[Paper]:
    """Join shared identifiers with provenance, preferring the richest abstract."""
    groups: list[tuple[Paper, set[str]]] = []
    for original in papers:
        p = replace(original, sources=list(original.sources), from_query=list(original.from_query),
                    publication=deepcopy(original.publication))
        identifiers = {p.id} if p.id else set()
        normalized_arxiv, normalized_doi = arxiv_id(p.arxiv_id), normalize_doi(p.doi)
        if normalized_arxiv:
            identifiers.add('arxiv:' + normalized_arxiv)
        if normalized_doi:
            identifiers.add('doi:' + normalized_doi)
        matches = []
        for i, (old, keys) in enumerate(groups):
            same_title = bool(title_key(p.title)) and title_key(old.title) == title_key(p.title)
            conflict = (old.arxiv_id and p.arxiv_id and arxiv_id(old.arxiv_id) != arxiv_id(p.arxiv_id))
            conflict = conflict or (old.doi and p.doi and normalize_doi(old.doi) != normalize_doi(p.doi))
            # A title alone cannot transfer an official acceptance or a journal
            # identity to another work. Keep those candidates separate until a
            # shared DOI/arXiv/forum identity establishes the link.
            publication_identity = any(r.get('verified') for r in
                _publication_records(old.publication) + _publication_records(p.publication))
            if identifiers & keys or (same_title and not conflict and not publication_identity):
                matches.append(i)
        if not matches:
            groups.append((p, identifiers))
            continue
        first = matches[0]
        old, keys = groups[first]
        others = [groups[i][0] for i in matches[1:]] + [p]
        for i in matches:
            keys |= groups[i][1]
        keys |= identifiers
        for item in others:
            citation_identity = (old.id == item.id or
                bool(old.doi and item.doi and normalize_doi(old.doi) == normalize_doi(item.doi)) or
                bool(old.arxiv_id and item.arxiv_id and arxiv_id(old.arxiv_id) == arxiv_id(item.arxiv_id)))
            old.sources = list(dict.fromkeys(old.sources + item.sources))
            old.publication = _publication(_publication_records(old.publication)
                + _publication_records(item.publication),
                list(old.publication.get('conflicts') or []) + list(item.publication.get('conflicts') or [])) \
                if old.publication or item.publication else {}
            if old.publication.get('conflicts'):
                old.provenance_conflict = True
            old.from_query = list(dict.fromkeys(old.from_query + item.from_query))
            old.doi = old.doi or item.doi
            old.arxiv_id = old.arxiv_id or item.arxiv_id
            old.year = old.year or item.year
            # A preprint's abstract and an accepted paper's venue can describe
            # the same work. Prefer publication metadata over the arXiv label,
            # including its publication year, without losing the abstract.
            if (not old.venue or 'arxiv' in old.venue.casefold()) and item.venue:
                if not old.venue or 'arxiv' not in item.venue.casefold():
                    old.venue = item.venue
                    old.year = item.year or old.year
            old.open_access_url = old.open_access_url or item.open_access_url
            if (citation_identity and item.citation_count is not None and
                (old.citation_count is None or (item.citation_source == 'openalex' and old.citation_source != 'openalex'))):
                old.citation_count = item.citation_count
                old.citation_source = item.citation_source
                old.citation_observed_at = item.citation_observed_at
            old.authority = max(float(old.authority or 0), float(item.authority or 0))
            old.snippet = old.snippet or item.snippet
            if (old.abstract and item.abstract
                    and ' '.join(old.abstract.split()) != ' '.join(item.abstract.split())):
                old.provenance_conflict = True
            if len(item.abstract) > len(old.abstract):
                old.abstract = item.abstract
                old.abstract_source = item.abstract_source
            old.source = old.source or item.source
            old.metadata_only = not bool(old.abstract)
            if old.publication.get('verified') and old.publication.get('status') in ('accepted', 'published'):
                old.venue = old.publication.get('venue_name') or old.venue
                old.year = _year(old.publication.get('date')) or old.year
        for i in reversed(matches[1:]):
            del groups[i]
    return [paper for paper, _ in groups]


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta: dict[str, str] = {}
        self.authors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'meta':
            return
        data = {k.lower(): v for k, v in attrs}
        name, content = data.get('name'), data.get('content')
        if not name or content is None:
            return
        name = name.lower()
        if name == 'citation_author':
            self.authors.append(_clean(content))
        else:
            self.meta.setdefault(name, _clean(content))


def _acl_html(html: str) -> dict:
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    abstract = ''
    match = re.search(r'<div[^>]*class=["\'][^"\']*acl-abstract[^"\']*["\'][^>]*>(.*?)</div>',
                      html, re.I | re.S)
    if match:
        span = re.search(r'<span[^>]*>(.*?)</span>', match.group(1), re.I | re.S)
        abstract = _clean(span.group(1) if span else match.group(1))
    return {'meta': parser.meta, 'authors': parser.authors, 'abstract': abstract}


def _extract_ids(*values: str) -> tuple[str, str]:
    text = ' '.join(str(v or '') for v in values)
    match = re.search(r'10\.\d{4,9}/[^\s"<>?#]+', text)
    doi = normalize_doi(match.group(0)) if match else ''
    arx = ''
    for pattern in (r'arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})',
                    r'arxiv[:\s]+([0-9]{4}\.[0-9]{4,5})'):
        found = re.search(pattern, text, re.I)
        if found:
            arx = arxiv_id(found.group(1))
            break
    return doi, arx


ACADEMIC_HOSTS = (
    'arxiv.org', 'doi.org', 'aclanthology.org', 'openreview.net', 'mlr.press',
    'proceedings.mlr.press', 'papers.nips.cc', 'proceedings.neurips.cc',
    'openaccess.thecvf.com', 'jmlr.org', 'usenix.org', 'ieee.org', 'acm.org',
    'springer.com', 'sciencedirect.com', 'nature.com', 'science.org',
)


def _looks_academic(url: str, title: str, site_name: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    if any(host == item or host.endswith('.' + item) for item in ACADEMIC_HOSTS):
        return True
    text = f'{title} {site_name}'.lower()
    return any(word in text for word in ('arxiv', 'openreview', 'acl anthology', 'neurips',
                                         'icml', 'iclr', 'cvpr', 'ieee', 'acm ', 'springer'))


def _source_budget_scope(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        store = self.store
        scope = getattr(store, 'budget_scope', None)
        with scope(self.budget) if callable(scope) else nullcontext():
            return method(self, *args, **kwargs)
    return wrapped


class AcademicSource:
    supports_search_filters = True
    def __init__(self, config: Config, budget: Budget):
        self.config, self.budget = config, budget
        self.last_arxiv_call = 0.0
        self.last_call: dict[str, float] = {}
        self.mode = 'live'
        self.providers = source_names(config.source)
        self.store = getattr(config, 'source_store', None)
        if self.store is not None and config.source in ('multi', 'all'):
            scope = getattr(self.store, 'budget_scope', None)
            with scope(budget) if callable(scope) else nullcontext():
                self.providers = self.store.enabled_sources(self.providers)
        self._lock = threading.RLock()
        self.last_batch: SearchBatch | None = None
        self.deepseek_calls = 0
        self.last_errors: dict[str, str] = {}
        self.cooldown: dict[str, float] = {}
        self._venue_ids: dict[str, str] = {}
        self.preference_resolution: dict[str, dict] = {}
        self.health: dict[str, dict] = {}

    def _provider_key(self, provider: str) -> str:
        if provider == 'semantic_scholar':
            return self.config.s2_key
        if provider == 'deepseek':
            return self.config.deepseek_search_key
        if provider == 'openalex':
            return self.config.openalex_key
        return ''

    def _deepseek_target_key(self, target: str) -> str:
        key = self._provider_key('deepseek')
        endpoint = getattr(self.config, 'deepseek_search_endpoint', DEEPSEEK_SEARCH_URL)
        from source_store import _origin
        target_origin, search_origin = _origin(target), _origin(endpoint)
        authorized = bool(target_origin and target_origin[0] == 'https' and target_origin == search_origin)
        if authorized and self.store is not None:
            authorized = self.store.deepseek_key_authorized(key, endpoint, target)
        elif authorized:
            # A standalone Config carries no evidence that a custom target and key
            # were explicitly bound together; environment-derived keys look identical.
            authorized = target_origin == _origin(DEEPSEEK_SEARCH_URL)
        if key and not authorized:
            error = GuideError('DeepSeek credential target is not authorized.')
            error.code = 'unauthorized_credential_target'
            raise error
        return key

    @_source_budget_scope
    def available(self, provider: str) -> bool:
        if provider not in PROVIDERS:
            return False
        if self.store is not None:
            states = self.store.public_state()['sources']
            saved = next((row for row in states if row['name'] == provider), None)
            if saved is not None and (not saved['enabled'] or not saved['configured']):
                return False
            if self.store.cooldown(provider) > time.time():
                return False
        if provider == 'lune':
            from mcp_lune import install
            return install().ready() or bool(self.store and self.store.credentials('lune').get('api_key'))
        with self._lock:
            if self.cooldown.get(provider, 0) > time.monotonic():
                return False
        if provider == 'deepseek':
            return bool(self._provider_key(provider))
        return True

    def begin_turn(self) -> None:
        """Reset per-turn DeepSeek budget; the router may make several searches."""
        self.deepseek_calls = 0

    def _local_write_failed(self, provider, action='source_audit_failed'):
        try:
            from execution_events import emit
            emit(action, source=provider, status='unknown', reason='source_store_unavailable')
        except BaseException:
            pass

    def record_run(self, run: SourceRun) -> None:
        if self.store is not None:
            try:
                scope = getattr(self.store, 'budget_scope', None)
                with scope(Budget(.25)) if callable(scope) else nullcontext():
                    self.store.record_run(run)
            except Exception:
                self._local_write_failed(run.name)
        if run.status in ('cache', 'skipped'):
            return
        state = self.health.setdefault(run.name, {
            'name': run.name, 'failures': 0, 'last_status': '', 'last_code': '',
            'last_error': '', 'last_at': ''})
        state['last_status'] = run.status
        state['last_at'] = utc_now()
        if run.status == 'error':
            state['failures'] = int(state.get('failures', 0)) + 1
            state['last_code'] = run.code or classify_source_error(run.error)
            state['last_error'] = run.error[:500]
        elif run.status in ('ok', 'empty', 'cache'):
            state['failures'] = 0
            state['last_code'] = ''
            state['last_error'] = ''

    def health_snapshot(self) -> dict[str, dict]:
        return {name: dict(value) for name, value in self.health.items()}

    def _wait_for(self, provider: str) -> None:
        if self.store is not None:
            self.store.wait_for_rate(provider, self.budget)
            return
        if provider == 'arxiv':
            wait = max(0, 4 - (time.monotonic() - self.last_arxiv_call))
            self.budget.wait(wait)
            self.last_arxiv_call = time.monotonic()
            return
        interval = PROVIDERS.get(provider, ProviderSpec(provider, provider, '')).rate_seconds
        last = self.last_call.get(provider)
        if last and interval:
            self.budget.wait(max(0, interval - (time.monotonic() - last)))
        self.last_call[provider] = time.monotonic()

    @contextmanager
    def _source_slot(self, provider):
        if self.store is not None and provider in PROVIDERS:
            completed = False
            try:
                with self.store.request_slot(provider, self.budget):
                    yield
                    completed = True
            except GuideError as exc:
                if not completed or getattr(exc, 'code', '') != 'source_slot_release_unknown':
                    raise
                # The store already emitted source_release_deferred; the
                # protected request succeeded and must not be retried.
            return
        # Standalone clients without persistent settings still share a local
        # provider cap; the Web/API path uses the cross-process SQLite slots.
        from source_store import _MAX_INFLIGHT
        active = _LOCAL_SLOT_CONTEXT.get()
        if provider in active:
            error = GuideError('Nested requests to the same source slot are not allowed.')
            error.code = 'source_slot_reentrant'
            raise error
        with _LOCAL_GATE_LOCK:
            gate = _LOCAL_REQUEST_GATES.setdefault(provider, threading.BoundedSemaphore(_MAX_INFLIGHT.get(provider, 1)))
        context_var = _LOCAL_SLOT_CONTEXT
        context_token = context_var.set(active + (provider,))
        owner_pid = os.getpid()
        acquired = False
        try:
            while not acquired:
                self.budget.check_cancelled()
                acquired = gate.acquire(timeout=min(.05, self.budget.remaining()))
            self.budget.check_cancelled()
            yield
        finally:
            # A child must never release or reset its parent's in-flight slot.
            if os.getpid() == owner_pid:
                try:
                    if acquired:
                        gate.release()
                finally:
                    context_var.reset(context_token)

    def _wait_for_cooldown(self, provider):
        # This last observation cannot be atomic with network dispatch. A newer
        # cooldown may still arrive after it; requests already in flight continue.
        waited = False
        while True:
            self.budget.check_cancelled()
            self.budget.remaining()
            with self._lock:
                local = max(0., self.cooldown.get(provider, 0) - time.monotonic())
            persistent = (max(0., self.store.cooldown(provider) - time.time())
                          if self.store is not None and provider in PROVIDERS else 0.)
            delay = max(local, persistent)
            if delay <= 0:
                return waited
            waited = True
            self.budget.wait(delay)

    def _request_bytes(self, url, provider, headers, data, method, attempt):
        host = urlsplit(url).hostname or ''
        deepseek_endpoint = getattr(self.config, 'deepseek_search_endpoint', DEEPSEEK_SEARCH_URL)
        deepseek_auth = next((value for name, value in (headers or {}).items()
                              if name.lower() == 'authorization'), None) if provider == 'deepseek' else None
        deepseek_row = self.store._row('deepseek') if deepseek_auth is not None and self.store is not None else None
        with self._source_slot(provider):
            while True:
                self._wait_for(provider)
                # A cooldown introduced after a rate reservation invalidates its
                # timing: reacquire a rate slot after waiting instead of letting
                # multiple old reservations dispatch together on expiry.
                if not self._wait_for_cooldown(provider):
                    break
            timeout = min(getattr(self.config, 'source_timeout', 15),
                          self.config.request_timeout, self.budget.remaining())
            request_headers = {'User-Agent': 'ScholarGuide/1.0 (academic research)',
                               'Accept': 'application/json, text/html, application/atom+xml, */*'}
            request_headers.update(headers or {})
            req = HttpRequest(url, data=data, headers=request_headers, method=method)
            with operation('http.request', source=provider, category='source', host=host, attempt=attempt) as measured:
                try:
                    self.budget.check_cancelled()
                    self.budget.remaining()
                    if provider == 'deepseek' and deepseek_auth is not None:
                        current_row = self.store._row('deepseek') if self.store is not None else None
                        unchanged = (getattr(self.config, 'deepseek_search_endpoint', DEEPSEEK_SEARCH_URL)
                                     == deepseek_endpoint and
                                     deepseek_auth == 'Bearer ' + self._provider_key('deepseek'))
                        if deepseek_row is not None:
                            unchanged = (unchanged and
                                         (deepseek_row['endpoint'] or DEEPSEEK_SEARCH_URL) == deepseek_endpoint and
                                         all(current_row[field] == deepseek_row[field]
                                             for field in ('endpoint', 'api_key', 'enabled')))
                            authorized = unchanged and self.store.deepseek_key_authorized(
                                self._provider_key('deepseek'), deepseek_endpoint, url, row=current_row)
                        else:
                            try:
                                authorized = unchanged and bool(self._deepseek_target_key(url))
                            except (RunCancelled, BudgetExceeded):
                                raise
                            except GuideError:
                                authorized = False
                        self.budget.check_cancelled()
                        self.budget.remaining()
                        if not authorized:
                            error = GuideError('DeepSeek credential target is not authorized.')
                            error.code = 'unauthorized_credential_target'
                            error.remote_outcome, error.retry_safe = 'not_started', False
                            raise error from None
                    timeout = min(getattr(self.config, 'source_timeout', 15),
                                  self.config.request_timeout, self.budget.remaining())
                    self.budget.search_http_calls += 1
                    is_post = req.get_method().upper() == 'POST'
                    try:
                        with urlopen(req, timeout=timeout) as response:
                            body = response.read(4_000_001)
                            measured['http_status'] = getattr(response, 'status', 200)
                        self.budget.check_cancelled()
                    except (RunCancelled, BudgetExceeded, TransportDeadlineExceeded) as exc:
                        if is_post:
                            _unknown_post(exc)
                        raise
                    except HTTPError:
                        raise
                    except (URLError, TimeoutError, OSError) as exc:
                        try:
                            self.budget.check_cancelled()
                        except RunCancelled as cancelled:
                            if is_post:
                                _unknown_post(cancelled)
                            raise
                        error = SourceTransportError(host, exc)
                        if is_post:
                            _unknown_post(error)
                        raise error from None
                except HTTPError:
                    raise
            if len(body) > 4_000_000:
                error = GuideError(f'{host}: response exceeded 4 MB.')
                if is_post:
                    _unknown_post(error)
                raise error
            return body

    def _pause(self, provider: str, seconds: float, code: str) -> None:
        with self._lock:
            self.cooldown[provider] = max(self.cooldown.get(provider, 0),
                                          time.monotonic() + max(1, seconds))
        if self.store is not None:
            try:
                self.store.cooldown(provider, time.time() + max(1, seconds), errorcode=code)
            except RunCancelled:
                raise
            except Exception:
                # Preserve the provider failure without hiding the local write failure.
                self._local_write_failed(provider if provider in PROVIDERS else 'unknown',
                                         'source_cooldown_failed')

    @_source_budget_scope
    def _fetch(self, url: str, *, provider: str = '', headers: dict | None = None,
               data: bytes | None = None, method: str | None = None) -> bytes:
        host = urlsplit(url).hostname or ''
        # arXiv's API is explicitly one request per three seconds. A timeout
        # may get one retry, but a 429 must not be retried immediately; the
        # source-level cooldown owns recovery.
        is_post = (method or ('POST' if data is not None else 'GET')).upper() == 'POST'
        attempts = 1 if provider == 'dblp' or is_post else 2
        for attempt in range(attempts):
            self.budget.check_cancelled()
            try:
                body = self._request_bytes(url, provider, headers, data, method, attempt + 1)
                if provider == 'arxiv' and b'rate exceeded' in body[:256].lower():
                    self._pause(provider, ARXIV_RATE_PAUSE_SECONDS, 'rate_limited')
                    raise GuideError(
                        f'{host}: arXiv rate exceeded; API paused for '
                        f'{ARXIV_RATE_PAUSE_SECONDS // 60} minutes.')
                with self._lock:
                    if self.cooldown.get(provider, 0) <= time.monotonic():
                        self.cooldown.pop(provider, None)
                if self.store is not None:
                    try:
                        scope = getattr(self.store, 'budget_scope', None)
                        with scope(Budget(.25)) if callable(scope) else nullcontext():
                            clear_expired = getattr(self.store, 'clear_expired_cooldown', None)
                            if callable(clear_expired):
                                clear_expired(provider)
                    except Exception:
                        self._local_write_failed(provider, 'source_cooldown_failed')
                return body
            except HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
                exc.close()
                try:
                    self.budget.check_cancelled()
                except RunCancelled as cancelled:
                    if is_post:
                        _unknown_post(cancelled)
                    raise
                wait = min(2 ** (attempt + 1), 8)
                try:
                    wait = max(0., float(retry_after)) if retry_after else wait
                except (TypeError, ValueError):
                    try:
                        wait = max(0., parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
                pause = (ARXIV_RATE_PAUSE_SECONDS if provider == 'arxiv' and code == 429
                         else max(wait, 1))
                if code in (429, 503):
                    try:
                        self._pause(provider, pause,
                                    'rate_limited' if code == 429 else 'server_error')
                    except RunCancelled as cancelled:
                        if is_post:
                            _unknown_post(cancelled)
                        raise
                if (attempt + 1 < attempts and code in (408, 429, 500, 502, 503, 504)
                        and not (provider == 'arxiv' and code == 429)):
                    if wait < self.budget.remaining() and wait <= 15:
                        self.budget.wait(wait)
                        continue
                error = GuideError(f'{host}: HTTP {code}; retry_after={round(wait, 1)}s.')
                if is_post:
                    _unknown_post(error)
                raise error from None
            except SourceTransportError as exc:
                try:
                    self.budget.check_cancelled()
                except RunCancelled as cancelled:
                    if is_post:
                        _unknown_post(cancelled)
                    raise
                if attempt + 1 < attempts and self.budget.remaining() > 2:
                    self.budget.wait(1)
                    continue
                try:
                    self._pause(provider, 30, exc.code)
                except RunCancelled as cancelled:
                    if is_post:
                        _unknown_post(cancelled)
                    raise
                raise
        raise GuideError('Source request failed.')

    def _json(self, url: str, *, provider: str, headers: dict | None = None,
              data: dict | None = None) -> dict:
        body = None
        if data is not None:
            body = json.dumps(data).encode('utf-8')
            headers = dict(headers or {})
            headers.setdefault('Content-Type', 'application/json')
        raw = self._fetch(url, provider=provider, headers=headers, data=body,
                          method='POST' if body is not None else None)
        def invalid_response(message):
            error = GuideError(message)
            if body is not None:
                _unknown_post(error)
            return error
        if raw.lstrip()[:1] == b'<':
            raise invalid_response(f'{PROVIDERS[provider].label} returned an HTML challenge '
                                   '(bot protection); use an API key or a cached browser session.')
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise invalid_response(f'{PROVIDERS[provider].label} returned invalid JSON.') from None
        if not isinstance(parsed, dict):
            raise invalid_response(f'{PROVIDERS[provider].label} returned an invalid payload.')
        return parsed

    # -- providers -------------------------------------------------------
    def _arxiv(self, query: str, limit: int, lookup: bool = False) -> list[Paper]:
        client = ArxivSearch(query, lambda url: self._fetch(url, provider='arxiv'))
        try:
            rows = client.lookup(query) if lookup else client.search(limit)
        except (ValueError, ET.ParseError):
            raise GuideError('arXiv returned invalid metadata.') from None
        return [Paper(id='arxiv:' + r['arxiv_id'], title=r['title'], abstract=r['body'],
                      url=r['href'], year=r['year'], authors=r['authors'],
                      arxiv_id=r['arxiv_id'], doi=normalize_doi(r['doi']),
                      venue='arXiv', open_access_url='https://arxiv.org/pdf/' + r['arxiv_id'],
                      sources=['arxiv'], source='arxiv', abstract_source='arxiv',
                      authority=0.75, fetched_at=utc_now(), from_query=[query],
                      publication=_publication([_record('preprint', venue_name='arXiv',
                          venue_id='arxiv', date=_date('', r['year']), verified=True,
                          evidence=[_evidence('arxiv', r['href'], 'repository', 'arxiv')])])) for r in rows]

    def _venue_id(self, venue: str, publication_type: str = 'any') -> str:
        key = publication_type + ':' + ' '.join(venue.casefold().split())
        if key in self._venue_ids:
            return self._venue_ids[key]
        params = {'search': venue, 'per-page': 10}
        if self.config.openalex_key:
            params['api_key'] = self.config.openalex_key
        try:
            data = self._json(OPENALEX_SOURCES_URL + '?' + urlencode(params), provider='openalex')
            rows = data.get('results') or []
        except GuideError:
            raise
        candidates = []
        for row in rows:
            source = row.get('display_name') or ''
            from research_policy import venue_identity
            if venue_identity(source) == venue_identity(venue) and (publication_type not in ('journal', 'conference')
                                                or row.get('type') == publication_type):
                candidates.append((int(row.get('works_count') or 0), row.get('id') or ''))
        if not candidates:
            raise GuideError(f'OpenAlex could not resolve venue "{venue}".')
        identifier = max(candidates)[1].rsplit('/', 1)[-1]
        if not re.fullmatch(r'S\d+', identifier):
            raise GuideError(f'OpenAlex returned an invalid venue id for "{venue}".')
        self._venue_ids[key] = identifier
        return identifier

    def _openalex(self, query: str, limit: int, venue: str = '',
                  year_from: int | None = None, year_to: int | None = None,
                  constraints: dict | None = None) -> list[Paper]:
        constraints = constraints or {}
        params = {'search': query, 'per-page': min(max(limit, 1), 50)}
        filters = []
        kind = constraints.get('publication_type', 'any')
        preferred = curated_preferences(constraints.get('preferred_venues'), kind) if not venue else []
        from research_policy import journal_issns_for_venue
        journal_issns = _issns(constraints.get('journal_issns') or
                              journal_issns_for_venue(venue))[:10]
        # A known publisher ISSN is a stronger journal identity than a fuzzy
        # name search. Alias resolution must not veto an exact ISSN lookup.
        venues = requested_venues(venue)
        if len(venues) > 1:
            resolved, unresolved, errors = [], [], []
            for name in venues:
                self.budget.check_cancelled()
                try:
                    resolved.append(self._venue_id(name, kind) if kind in ('journal', 'conference')
                                    else self._venue_id(name))
                except GuideError as exc:
                    self.budget.check_cancelled()
                    unresolved.append(name)
                    errors.append(f'{name}: {exc}')
            self.preference_resolution['openalex'] = {'requested_venues': venues,
                'resolved_venues': [v for v in venues if v not in unresolved],
                'unresolved_venues': unresolved, 'source_ids': list(dict.fromkeys(resolved)),
                'errors': errors}
            # A partial source-id filter silently loses unresolved venues. Search
            # broadly instead, and apply the full whitelist to returned records.
            if resolved and not unresolved:
                filters.append('primary_location.source.id:' + '|'.join(dict.fromkeys(resolved)))
        elif venue and not journal_issns:
            filters.append('primary_location.source.id:' + (self._venue_id(venue, kind)
                if kind in ('journal', 'conference') else self._venue_id(venue)))
        elif preferred:
            from research_policy import curated_venue_catalog
            catalog = {entry['name']:entry['type'] for entry in curated_venue_catalog()['venues']}
            resolved, unresolved, source_ids, errors = [], [], [], []
            for name in preferred:
                self.budget.check_cancelled()
                self.budget.remaining()
                try:
                    identifier = self._venue_id(name, catalog[name])
                except GuideError as exc:
                    self.budget.check_cancelled()
                    unresolved.append(name)
                    errors.append(f'{name}: {exc}')
                    continue
                resolved.append(name)
                source_ids.append(identifier)
            self.preference_resolution['openalex'] = {'requested_venues':list(preferred),
                'resolved_venues':resolved, 'unresolved_venues':unresolved, 'errors':errors,
                'source_ids':list(dict.fromkeys(source_ids))}
            if not source_ids:
                raise GuideError('OpenAlex could not resolve the preferred venue pool: ' + '; '.join(errors))
            filters.append('primary_location.source.id:' + '|'.join(dict.fromkeys(source_ids)))
        if journal_issns:
            filters.append('primary_location.source.issn:' + '|'.join(journal_issns))
        if kind == 'journal' or journal_issns:
            filters.append('primary_location.source.type:journal')
        elif kind == 'preprint':
            filters.append('type:preprint')
        elif kind == 'conference':
            filters.append('primary_location.source.type:conference')
        if year_from and year_to:
            filters.append(f'publication_year:{year_from}-{year_to}')
        elif year_from:
            filters.append(f'publication_year:>{year_from - 1}')
        elif year_to:
            filters.append(f'publication_year:<{year_to + 1}')
        if filters:
            params['filter'] = ','.join(filters)
        if self.config.openalex_key:
            params['api_key'] = self.config.openalex_key
        try:
            data = self._json(OPENALEX_WORKS_URL + '?' + urlencode(params), provider='openalex')
            rows = data['results']
            if not isinstance(rows, list):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise GuideError('OpenAlex returned invalid metadata.') from None
        papers = []
        for row in rows:
            identifier = (row.get('id') or '').rsplit('/', 1)[-1]
            if not re.fullmatch(r'W\d+', identifier) or not row.get('title'):
                continue
            index = row.get('abstract_inverted_index') or {}
            positions = {pos: word for word, offsets in index.items() for pos in offsets}
            abstract = ' '.join(positions[pos] for pos in sorted(positions))
            doi = normalize_doi(row.get('doi') or '')
            ax = arxiv_id((row.get('ids') or {}).get('arxiv') or '')
            location = row.get('primary_location') or {}
            source_info = location.get('source') or {}
            source_issns = _issns(_as_list(source_info.get('issn')) + _as_list(source_info.get('issn_l')))
            if journal_issns and not set(journal_issns).intersection(source_issns):
                continue
            venue = source_info.get('display_name') or ''
            oa = (row.get('open_access') or {}).get('oa_url') or ''
            best = row.get('best_oa_location') or {}
            oa = oa or best.get('pdf_url') or best.get('landing_page_url') or ''
            link = f'https://doi.org/{doi}' if doi else f'https://openalex.org/{identifier}'
            record_type = {'journal': 'journal', 'conference': 'conference',
                           'repository': 'preprint'}.get(source_info.get('type'), 'unknown')
            work_type = str(row.get('type') or '')
            if work_type == 'preprint':
                record_type = 'preprint'
            retracted = row.get('is_retracted') is True
            published = (bool(doi) and not retracted and location.get('is_published') is not False
                         and location.get('version') not in ('submittedVersion', 'acceptedVersion')
                         and ((record_type == 'journal' and bool(source_issns)
                               and work_type in ('article', 'review'))
                              or (record_type == 'conference' and work_type == 'conference-paper')))
            evidence_url = 'https://api.openalex.org/works/' + identifier
            publication = _publication([_record(record_type, 'retracted' if retracted else 'published' if published else 'unknown',
                venue_id=str(source_info.get('id') or ''), venue_name=venue,
                date=_date(row.get('publication_date'), row.get('publication_year')),
                issn=source_issns, doi=doi, document_type=work_type,
                verified=retracted or published or record_type == 'preprint', retracted=retracted,
                evidence=[_evidence('openalex', evidence_url, 'primary_location.source', source_info),
                          _evidence('openalex', evidence_url, 'type', work_type),
                          _evidence('openalex', evidence_url, 'doi', doi),
                          _evidence('openalex', evidence_url, 'is_retracted', retracted)])])
            papers.append(Paper(id='openalex:' + identifier, title=row['title'], abstract=abstract,
                url=link, year=row.get('publication_year'), doi=doi, arxiv_id=ax,
                authors=[a['author']['display_name'] for a in row.get('authorships', [])
                         if a.get('author', {}).get('display_name')],
                venue=venue, open_access_url=oa, sources=['openalex'], source='openalex',
                abstract_source='openalex' if abstract else '', authority=0.65,
                metadata_only=not bool(abstract), fetched_at=utc_now(), from_query=[query],
                publication=publication,
                citation_count=row.get('cited_by_count') if type(row.get('cited_by_count')) is int and row['cited_by_count'] >= 0 else None,
                citation_source='openalex' if type(row.get('cited_by_count')) is int and row['cited_by_count'] >= 0 else '',
                citation_observed_at=utc_now() if type(row.get('cited_by_count')) is int and row['cited_by_count'] >= 0 else ''))
        return papers

    def _semantic_scholar(self, query: str, limit: int, filters: dict | None = None) -> list[Paper]:
        filters = filters or {}
        # The relevance API treats long keyword lists as a narrow conjunction.
        # For an explicit venue list, preserve a compact topical seed and let
        # the API parameters enforce year and venue; never append venue names as
        # extra topic terms. The full original query remains in provenance.
        names = requested_venues(filters.get('venue') or '')
        search_query = query
        if len(names) > 1:
            venue_tokens = {name.casefold() for name in names}
            words = re.findall(r"[\w]+(?:[-'][\w]+)*", query)
            topical = [word for word in words if word.casefold() not in venue_tokens
                       and not re.fullmatch(r'(?:19|20)\d{2}', word)]
            if topical:
                search_query = ' '.join(topical[:4] if len(topical) > 5 else topical)
        params = {
            'query': search_query,
            'limit': min(max(limit, 1), 100),
            'fields': 'title,abstract,url,year,venue,authors,externalIds,openAccessPdf,publicationVenue,citationCount,publicationTypes,publicationDate,journal',
        }
        year_from, year_to = filters.get('year_from'), filters.get('year_to')
        if year_from or year_to:
            params['year'] = f'{year_from or ""}-{year_to or ""}'
        if filters.get('venue'):
            params['venue'] = ','.join(requested_venues(filters['venue']))
        else:
            preferred = curated_preferences(filters.get('preferred_venues'), filters.get('publication_type', 'any'))
            if preferred:
                # The documented venue parameter is a comma-separated OR list,
                # so a small pool needs one request rather than N venue calls.
                params['venue'] = ','.join(preferred)
        if filters.get('publication_type') == 'journal' or filters.get('journal_issns'):
            params['publicationTypes'] = 'JournalArticle'
        elif filters.get('publication_type') == 'conference':
            params['publicationTypes'] = 'Conference'
        headers = {'x-api-key': self.config.s2_key} if self.config.s2_key else {}
        try:
            data = self._json(S2_SEARCH_URL + '?' + urlencode(params), provider='semantic_scholar',
                              headers=headers)
        except GuideError as exc:
            if '429' not in str(exc):
                raise
            # The relevance endpoint is the most aggressively throttled one. Its
            # bulk sibling is a useful last resort for discovery; cap client-side.
            bulk = {key: value for key, value in params.items() if key != 'limit'}
            data = self._json(S2_BULK_URL + '?' + urlencode(bulk), provider='semantic_scholar',
                              headers=headers)
        rows = data.get('data') or []
        if not isinstance(rows, list):
            raise GuideError('Semantic Scholar returned an invalid result list.')
        rows = rows[:max(limit, 1)]
        papers = []
        for row in rows:
            if not isinstance(row, dict) or not row.get('title'):
                continue
            ext = row.get('externalIds') or {}
            doi = normalize_doi(ext.get('DOI') or '')
            ax = arxiv_id(ext.get('ArXiv') or '')
            paper_id = row.get('paperId') or doi or ax or hashlib.sha1(
                str(row.get('title')).encode()).hexdigest()[:16]
            abstract = _clean(row.get('abstract') or '')
            venue = row.get('venue') or ''
            publication = row.get('publicationVenue') or {}
            publication = publication if isinstance(publication, dict) else {}
            if not venue and isinstance(publication, dict):
                venue = publication.get('name') or ''
            oa = (row.get('openAccessPdf') or {}).get('url') or ''
            types = [str(t) for t in _as_list(row.get('publicationTypes')) if t]
            normalized_types = {re.sub(r'\W', '', t).casefold() for t in types}
            journal = row.get('journal') if isinstance(row.get('journal'), dict) else {}
            source_issns = _issns(_as_list(publication.get('issn'))
                + _as_list(publication.get('alternate_issns')) + _as_list(journal.get('issn')))
            wanted_issns = _issns(filters.get('journal_issns'))
            if wanted_issns and not set(wanted_issns).intersection(source_issns):
                continue
            kind = publication.get('type') if publication.get('type') in ('journal', 'conference') else 'unknown'
            if kind == 'unknown' and journal.get('name') and 'journalarticle' in normalized_types:
                kind = 'journal'
            # A Conference label supplies type, not an official acceptance decision.
            if kind == 'unknown' and 'conference' in normalized_types:
                kind = 'conference'
            repository = _preprint_repository(publication, journal, venue, doi)
            if repository:
                # Generic JournalArticle labels (including an arXiv ISSN and
                # preprint DOI) are not formal journal-publication evidence.
                kind = 'preprint'
            published = (kind == 'journal' and 'journalarticle' in normalized_types and bool(doi)
                         and bool(source_issns) and bool(publication.get('name') or journal.get('name')))
            evidence_url = 'https://api.semanticscholar.org/graph/v1/paper/' + quote(str(paper_id), safe='')
            facts = _publication([_record(kind, 'published' if published else 'unknown',
                venue_id=str(publication.get('id') or ''),
                venue_name=publication.get('name') or journal.get('name') or venue,
                date=_date(row.get('publicationDate'), row.get('year')), issn=source_issns,
                doi=doi, document_type='preprint' if repository else 'review' if 'review' in normalized_types else
                    ('article' if 'journalarticle' in normalized_types else 'unknown'),
                publication_types=types, verified=published or bool(repository),
                evidence=[_evidence('semantic_scholar', evidence_url, 'publicationVenue', publication),
                          _evidence('semantic_scholar', evidence_url, 'publicationTypes', types),
                          _evidence('semantic_scholar', evidence_url, 'journal', journal),
                          _evidence('semantic_scholar', evidence_url, 'externalIds.DOI', doi)])])
            papers.append(Paper(id='s2:' + str(paper_id), title=_clean(row['title']),
                abstract=abstract, url=row.get('url') or (f'https://doi.org/{doi}' if doi else ''),
                year=row.get('year'), doi=doi, arxiv_id=ax,
                authors=[a.get('name') for a in row.get('authors') or [] if a.get('name')],
                venue=venue, open_access_url=oa, sources=['semantic_scholar'],
                source='semantic_scholar', abstract_source='semantic_scholar' if abstract else '',
                authority=0.7, metadata_only=not bool(abstract), fetched_at=utc_now(),
                from_query=[query], publication=facts,
                citation_count=row.get('citationCount') if type(row.get('citationCount')) is int and row['citationCount'] >= 0 else None,
                citation_source='semantic_scholar' if type(row.get('citationCount')) is int and row['citationCount'] >= 0 else '',
                citation_observed_at=utc_now() if type(row.get('citationCount')) is int and row['citationCount'] >= 0 else ''))
        return papers

    def _dblp(self, query: str, limit: int) -> list[Paper]:
        params = {'q': query, 'format': 'json', 'h': min(max(limit, 1), 100)}
        data = self._json(DBLP_SEARCH_URL + '?' + urlencode(params), provider='dblp')
        hits = ((data.get('result') or {}).get('hits') or {}).get('hit') or []
        if isinstance(hits, dict):
            hits = [hits]
        papers = []
        for hit in hits:
            info = hit.get('info') if isinstance(hit, dict) else None
            if not isinstance(info, dict) or not info.get('title'):
                continue
            authors_raw = (info.get('authors') or {}).get('author') or []
            authors = [_clean(a.get('text') if isinstance(a, dict) else a) for a in _as_list(authors_raw)]
            ee = info.get('ee') or ''
            links = [_clean(x.get('text') if isinstance(x, dict) else x) for x in _as_list(ee)]
            link = next((x for x in links if x.startswith('http')), '') or info.get('url') or ''
            open_link = next((x for x in links if 'pdf' in x.lower()), '')
            venue = info.get('venue') or ''
            if isinstance(venue, list):
                venue = ', '.join(str(v) for v in venue)
            papers.append(Paper(id='dblp:' + str(info.get('key') or hashlib.sha1(str(info['title']).encode()).hexdigest()[:16]),
                title=_clean(info['title']).rstrip('.'), abstract='', url=link,
                year=_year(info.get('year')), doi=normalize_doi(info.get('doi') or ''),
                authors=authors, venue=_clean(venue), open_access_url=open_link,
                sources=['dblp'], source='dblp', authority=0.6, metadata_only=True,
                fetched_at=utc_now(), from_query=[query]))
        return papers

    def _openreview_group(self, venue_id: str) -> dict:
        cache = getattr(self, '_openreview_groups', None)
        if cache is None:
            self._openreview_groups = cache = {}
        entry = cache.get(venue_id)
        if entry and time.monotonic() - entry['stored_at'] < (3600 if entry['group'] else 60):
            return entry['group']
        if self.budget.cancel_token.requested:
            return {}
        params = {'id': venue_id, 'select': 'id,content'}
        try:
            data = self._json(OPENREVIEW_GROUPS_URL + '?' + urlencode(params), provider='openreview')
            groups = data.get('groups') or []
            group = next((g for g in groups if isinstance(g, dict) and g.get('id') == venue_id), {})
        except RunCancelled:
            # The topic search may have completed just as Stop was requested.
            # Keep its successful notes as unverified facts without another HTTP call.
            return {}
        except GuideError:
            group = {}
        cache[venue_id] = {'group': group, 'stored_at': time.monotonic()}
        return group

    def _openreview_publication(self, note: dict, target: str = '') -> dict:
        content = note.get('content') or {}
        venue_id = _clean(_value(content, 'venueid'))
        venue_name = _clean(_value(content, 'venue'))
        invitations = _as_list(note.get('invitations') or note.get('invitation'))
        domain = str(note.get('domain') or '')
        if not domain:
            invitation = next((str(i) for i in invitations if '/-/Submission' in str(i)), '')
            domain = invitation.split('/-/Submission', 1)[0]
        # A targeted venue is authoritative only after the official group resolves.
        canonical = target or domain
        if not canonical and venue_id:
            canonical = re.sub(r'/(?:Submission|Rejected_Submission|Withdrawn_Submission|Desk_Rejected_Submission)$', '', venue_id)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+', canonical):
            canonical = ''
        group = self._openreview_group(canonical) if canonical else {}
        settings = group.get('content') or {}
        official = bool(group.get('id') == canonical and _value(settings, 'submission_venue_id'))
        status = 'unknown'
        official_state = False
        status_fields = [('withdrawn_venue_id', 'withdrawn'), ('desk_rejected_venue_id', 'desk_rejected'),
                         ('rejected_venue_id', 'rejected'), ('submission_venue_id', 'submitted')]
        for field_name, candidate in status_fields:
            if venue_id and venue_id == _value(settings, field_name):
                status = candidate
                official_state = True
                break
        if status == 'unknown' and official and venue_id == canonical:
            status = 'accepted'
            official_state = True
        # This text can explain an unverified negative state; it cannot prove acceptance.
        if status == 'unknown':
            low = (venue_id + ' ' + venue_name).casefold()
            if 'withdrawn' in low:
                status = 'withdrawn'
            elif 'desk_rejected' in low or 'desk rejected' in low:
                status = 'desk_rejected'
            elif 'rejected' in low:
                status = 'rejected'
            elif 'submitted' in low or venue_id.endswith('/Submission'):
                status = 'submitted'
        decision = ''
        decision_verified = False
        decisions = []
        decision_field = str(_value(settings, 'decision_field_name') or 'decision')
        details = note.get('details') or {}
        replies = details.get('replies') or details.get('directReplies') or []
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            reply_invitations = _as_list(reply.get('invitations') or reply.get('invitation'))
            trusted = any(str(i).startswith(canonical + '/') and str(i).endswith('/-/Decision')
                          for i in reply_invitations) if canonical else False
            signatures = [str(s) for s in _as_list(reply.get('signatures'))]
            trusted = trusted and any((s == canonical or s.startswith(canonical + '/'))
                and not any(x in s.casefold() for x in ('/authors', '/reviewer', '/anonreviewer'))
                for s in signatures)
            if trusted and reply.get('forum') == (note.get('forum') or note.get('id')):
                value = _clean(_value(reply.get('content') or {}, decision_field))
                if value and not reply.get('ddate'):
                    decisions.append({'value': value, 'id': str(reply.get('id') or ''),
                        'invitations': [str(i) for i in reply_invitations], 'signatures': signatures,
                        'updated_at': _ms_date(reply.get('tmdate') or reply.get('mdate') or reply.get('cdate'))})
        if decisions:
            decisions.sort(key=lambda d: d['updated_at'], reverse=True)
            decision = decisions[0]['value']
            decision_verified = True
        current_status = status
        heading_map = _value(settings, 'decision_heading_map') or {}
        if not decision and isinstance(heading_map, dict):
            decision = str(heading_map.get(venue_name) or '')
        if decision:
            low = decision.casefold()
            if 'conditional' in low:
                status = 'unknown'
            elif (low.startswith('accept') and official and (decision_verified or status == 'accepted')
                  and status not in ('rejected', 'withdrawn', 'desk_rejected')):
                status = 'accepted'
                official_state = True
            elif decision_verified and 'withdraw' in low:
                status = 'withdrawn'
                official_state = True
            elif decision_verified and 'desk' in low and 'reject' in low:
                status = 'desk_rejected'
                official_state = True
            elif decision_verified and low.startswith('reject'):
                status = 'rejected'
                official_state = True
        def decision_status(value):
            low = value.casefold()
            if 'conditional' in low:
                return 'unknown'
            if low.startswith('accept'):
                return 'accepted'
            if 'withdraw' in low:
                return 'withdrawn'
            if 'reject' in low:
                return 'rejected'
            return 'unknown'
        decision_states = {decision_status(d['value']) for d in decisions}
        contradictory = bool(decision_states & {'accepted'} and decision_states & {'rejected', 'withdrawn'})
        if decision_verified:
            current_positive = current_status == 'accepted'
            current_negative = current_status in ('rejected', 'withdrawn', 'desk_rejected')
            contradictory = contradictory or (current_positive and decision_status(decision) in ('rejected', 'withdrawn'))
            contradictory = contradictory or (current_negative and decision_status(decision) == 'accepted')
        if contradictory:
            status, official_state = 'unknown', False
        deleted = bool(note.get('ddate'))
        verified = bool(official and official_state and status != 'unknown' and not deleted)
        presentation = 'unknown'
        if verified and status == 'accepted':
            label = (decision or venue_name).casefold()
            presentation = next((p for p in ('oral', 'spotlight', 'poster')
                                 if re.search(r'\b' + p + r'\b', label)), 'unknown')
        forum = str(note.get('forum') or note.get('id') or '')
        url = OPENREVIEW_BASE + '/forum?id=' + quote(forum, safe='')
        group_url = OPENREVIEW_GROUPS_URL + '?' + urlencode({'id': canonical})
        edition = _year(canonical) or _year(venue_name)
        accepted_at = _ms_date(note.get('pdate'))
        return _publication([_record('conference' if official else 'unknown', status,
            venue_id=canonical or venue_id, venue_name=venue_name, presentation=presentation,
            date=str(edition) if edition else accepted_at[:10],
            date_precision='year' if edition else ('day' if accepted_at else 'unknown'),
            accepted_at=accepted_at, first_public_at=_ms_date(note.get('odate')),
            decision=decision, decisions=decisions, verified=verified, deleted=deleted,
            verification_reason='' if verified else ('official_group_unavailable' if not official
                else 'conflicting_official_decisions' if contradictory
                else 'conditional_decision' if 'conditional' in decision.casefold() else 'unknown_or_deleted'),
            evidence=[_evidence('openreview', url, 'content.venueid', venue_id),
                      _evidence('openreview', url, 'content.venue', venue_name),
                      _evidence('openreview', group_url, 'group.id', group.get('id') or ''),
                      _evidence('openreview', url, 'decision', decision)])])

    def _openreview(self, query: str, limit: int, filters: dict | None = None) -> list[Paper]:
        filters = filters or {}
        targets = [str(v) for v in _as_list(filters.get('openreview_venue_ids'))
                   if re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+', str(v))][:3]
        notes = []
        per_target = max(1, min(max(limit, 1), 50) // max(1, len(targets)))
        for target in targets or ['']:
            params = {'term': query, 'limit': per_target, 'source': 'forum'}
            if target:
                params['venueid'] = target
            data = self._json(OPENREVIEW_SEARCH_URL + '?' + urlencode(params), provider='openreview')
            rows = data.get('notes') or []
            if not isinstance(rows, list):
                raise GuideError('OpenReview returned an invalid result list.')
            notes.extend((note, target) for note in rows[:per_target])
        papers = []
        seen = set()
        for note, target in notes:
            if not isinstance(note, dict):
                continue
            if note.get('id') and note.get('forum') and note['id'] != note['forum']:
                continue
            content = note.get('content') or {}
            if target and _value(content, 'venueid') != target:
                continue
            title = _clean(_value(content, 'title'))
            abstract = _clean(_value(content, 'abstract'))
            venue = _clean(_value(content, 'venue')) or _clean(_value(content, 'venueid'))
            if not title or not abstract:
                continue
            forum = note.get('forum') or note.get('id') or ''
            if not forum or forum in seen:
                continue
            publication = self._openreview_publication(note, target)
            # Discovery defaults retain the historical exclusion of known negative
            # states, while status facts remain available to merge/audit callers.
            if publication.get('status') in ('submitted', 'rejected', 'withdrawn', 'desk_rejected'):
                continue
            seen.add(forum)
            pdf = _value(content, 'pdf') or ''
            if isinstance(pdf, str) and pdf.startswith('/'):
                pdf = OPENREVIEW_BASE + pdf
            html_link = _value(content, 'html') or ''
            link = f'{OPENREVIEW_BASE}/forum?id={forum}'
            doi = normalize_doi(_value(content, 'doi') or '')
            ax = arxiv_id(str(_value(content, 'arxiv') or _value(content, 'arxiv_id') or ''))
            papers.append(Paper(id='openreview:' + str(forum), title=title, abstract=abstract,
                url=link, year=_year(publication.get('date')) or _year(venue), doi=doi, arxiv_id=ax,
                authors=[_clean(a) for a in _as_list(_value(content, 'authors')) if a],
                venue=venue, open_access_url=str(pdf or html_link or ''), sources=['openreview'],
                source='openreview', abstract_source='openreview', authority=0.8,
                fetched_at=utc_now(), from_query=[query], publication=publication))
        return papers

    def _acl_page(self, url: str) -> dict:
        raw = self._fetch(url, provider='acl_anthology')
        try:
            html = raw.decode('utf-8', errors='replace')
        except Exception:
            return {}
        return _acl_html(html)

    def verify_openreview_paper(self, paper: Paper) -> Paper:
        """Verify an exact candidate against its official OpenReview edition.

        A database conference label alone is not an acceptance decision. Search
        the specific edition, require an exact normalized title and year, and
        retain only an independently verified published/accepted record.
        """
        from research_policy import venue_identity, publication_records
        venue = venue_identity(paper.venue or '')
        if venue not in {'ICLR', 'ICML', 'NeurIPS'} or not paper.year:
            return paper
        edition = f'{venue}.cc/{paper.year}/Conference'
        try:
            candidates = self._openreview(paper.title, 3, {'openreview_venue_ids': [edition]})
        except RunCancelled:
            raise
        except GuideError:
            return paper
        key = lambda value: ''.join(re.findall(r'\w+', (value or '').casefold()))
        matches = [p for p in candidates if key(p.title) == key(paper.title) and p.year == paper.year
                   and paper.authors and p.authors and key(paper.authors[0]) == key(p.authors[0])]
        if len(matches) != 1:
            return paper
        records = [r for r in publication_records(matches[0]) if r.get('verified')
                   and r.get('status') in ('accepted', 'published')]
        if not records:
            return paper
        existing = (paper.publication or {}).get('records') or []
        return replace(paper, publication=merge_publication_records([*existing, *records]))

    def verify_acl_paper(self, paper: Paper) -> Paper:
        """Add publication evidence from the exact official Anthology page.

        A Lune conference label or a DOI prefix by itself is not acceptance
        evidence. This only considers main ACL/EMNLP proceedings, and requires
        the official page's DOI, title and publication date to agree.
        """
        doi = normalize_doi(paper.doi)
        match = re.fullmatch(r'10\.18653/(?:v1/)?((?:19|20)\d{2}\.(?:acl|emnlp)-(?:long|short|main)\.\d+)', doi)
        if not match:
            return paper
        url = 'https://aclanthology.org/' + match.group(1) + '/'
        try:
            page = self._acl_page(url)
        except RunCancelled:
            raise
        except GuideError:
            return paper
        meta = page.get('meta') or {}
        title = _clean(meta.get('citation_title') or '')
        def title_key(value):
            return ''.join(re.findall(r'\w+', value.casefold()))
        official_year = _year(meta.get('citation_publication_date'))
        venue = _clean(meta.get('citation_conference_title') or '')
        from research_policy import venue_identity
        expected = 'ACL' if '.acl-' in match.group(1) else 'EMNLP'
        if (normalize_doi(meta.get('citation_doi') or '') != doi or not title
                or title_key(title) != title_key(paper.title)
                or not official_year or official_year != int(match.group(1)[:4])
                or venue_identity(venue) != expected):
            return paper
        record = _record('conference', 'published', venue_id=match.group(1).rsplit('.', 1)[0],
            venue_name=venue, date=str(official_year), doi=doi, verified=True,
            evidence=[_evidence('acl_anthology', url, 'citation_conference_title', venue),
                      _evidence('acl_anthology', url, 'citation_doi', doi),
                      _evidence('acl_anthology', url, 'citation_title', title)])
        existing = (paper.publication or {}).get('records') or []
        return replace(paper, publication=merge_publication_records([*existing, record]))

    def _acl_anthology(self, query: str, limit: int) -> list[Paper]:
        params = {'query': query, 'limit': min(max(limit, 1), 50), 'collection_ids': 'anthology'}
        data = self._json(VERBATIM_SEARCH_URL + '?' + urlencode(params), provider='acl_anthology')
        items = data.get('items') or []
        if not isinstance(items, list):
            raise GuideError('ACL Anthology index returned an invalid result list.')
        papers = []
        for item in items[:max(1, min(limit, 10))]:
            if not isinstance(item, dict) or not item.get('title') or not item.get('url'):
                continue
            url = item['url']
            page = {}
            try:
                page = self._acl_page(url)
            except RunCancelled:
                break
            except GuideError:
                page = {}
            meta = page.get('meta') or {}
            abstract = page.get('abstract') or ''
            doi = normalize_doi(meta.get('citation_doi') or '')
            pdf = meta.get('citation_pdf_url') or (url.rstrip('/') + '.pdf')
            venue = _clean(meta.get('citation_conference_title') or item.get('booktitle') or '')
            authors = page.get('authors') or [_clean(a) for a in item.get('authors') or []]
            anthology_id = str(item.get('id') or '')
            year = _year(meta.get('citation_publication_date') or item.get('year'))
            official_page = urlsplit(str(url)).hostname == 'aclanthology.org' and bool(meta)
            publication = _publication([_record('conference', 'published' if official_page and doi else 'unknown',
                venue_id=anthology_id.rsplit('.', 1)[0], venue_name=venue,
                date=str(year) if year else '', doi=doi, verified=official_page and bool(doi),
                evidence=[_evidence('acl_anthology', url, 'citation_conference_title', venue),
                          _evidence('acl_anthology', url, 'citation_doi', doi)])])
            papers.append(Paper(id='acl:' + str(item.get('id') or hashlib.sha1(str(url).encode()).hexdigest()[:16]),
                title=_clean(meta.get('citation_title') or item.get('title')), abstract=abstract,
                url=url, year=year,
                doi=doi, authors=[a for a in authors if a], venue=venue,
                open_access_url=pdf, sources=['acl_anthology'], source='acl_anthology',
                abstract_source='acl_anthology' if abstract else '', authority=0.8,
                metadata_only=not bool(abstract), fetched_at=utc_now(), from_query=[query], publication=publication))
        return papers

    def _deepseek_search(self, query: str, limit: int) -> list[Paper]:
        endpoint = getattr(self.config, 'deepseek_search_endpoint', DEEPSEEK_SEARCH_URL)
        key = self._deepseek_target_key(endpoint)
        if not key:
            raise GuideError('DeepSeek Search key is not configured (DEEPSEEK_SEARCH_KEY or DeepSeek API key).')
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            raise GuideError('DeepSeek Search query must contain 1–128 characters.')
        if self.deepseek_calls >= 4:
            raise GuideError('DeepSeek Search is limited to four bounded requests per turn.')
        self.deepseek_calls += 1
        data = self._json(getattr(self.config, 'deepseek_search_endpoint', DEEPSEEK_SEARCH_URL), provider='deepseek',
                          headers={'Authorization': 'Bearer ' + key},
                          data={'query': query, 'count': min(max(limit, 1), 10),
                                'need_content': False, 'ori_question': query})
        rows = data.get('results')
        if not isinstance(rows, list):
            raise _unknown_post(GuideError('DeepSeek Search returned an invalid result list.'))
        papers = []
        for row in rows:
            if not isinstance(row, dict) or not row.get('url') or not row.get('title'):
                continue
            title = _clean(row['title'])
            url = str(row['url'])
            if not _looks_academic(url, title, str(row.get('site_name') or '')):
                continue
            doi, ax = _extract_ids(url, title, str(row.get('summary') or ''), str(row.get('snippet') or ''))
            if doi:
                paper_id = 'doi:' + doi
            elif ax:
                paper_id = 'arxiv:' + ax
            else:
                paper_id = 'web:' + hashlib.sha1(url.encode()).hexdigest()[:16]
            papers.append(Paper(id=paper_id, title=title, abstract='', url=url,
                year=_year(row.get('date')), doi=doi, arxiv_id=ax, venue=_clean(row.get('site_name') or ''),
                open_access_url='', sources=['deepseek'], source='deepseek',
                authority=_authority(row.get('authority')),
                metadata_only=True, snippet=_clean(row.get('summary') or row.get('snippet') or ''),
                fetched_at=utc_now(), from_query=[query]))
        return papers

    def fetch(self, url: str) -> dict:
        """Fetch Markdown for explicit reading; it is never relabeled an abstract."""
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise GuideError('Fetch requires an HTTP(S) URL without embedded credentials.')
        key = self._deepseek_target_key('https://api.deepseek.com/v1/tools/fetch')
        if not key:
            raise GuideError('DeepSeek Fetch key is not configured.')
        result = self._json('https://api.deepseek.com/v1/tools/fetch', provider='deepseek',
                            headers={'Authorization': 'Bearer ' + key}, data={'url': url})
        if not isinstance(result.get('content'), str) or not result['content']:
            raise _unknown_post(GuideError('DeepSeek Fetch returned no readable content.'))
        return {'title': str(result.get('title') or ''), 'content': result['content'],
                'url': url, 'source': 'deepseek', 'evidence_level': 'fetched_page', 'fetched_at': utc_now()}

    # -- orchestration ---------------------------------------------------
    def _search_one(self, name: str, query: str, limit: int, filters: dict | None = None) -> list[Paper]:
        filters = filters or {}
        if name == 'arxiv':
            return self._arxiv(query, limit)
        if name == 'openalex':
            return self._openalex(query, limit, str(filters.get('venue') or ''),
                                  filters.get('year_from'), filters.get('year_to'), filters)
        if name == 'semantic_scholar':
            return self._semantic_scholar(query, limit, filters)
        if name == 'dblp':
            return self._dblp(query, limit)
        if name == 'openreview':
            return self._openreview(query, limit, filters)
        if name == 'acl_anthology':
            return self._acl_anthology(query, limit)
        if name == 'deepseek':
            return self._deepseek_search(query, limit)
        if name == 'lune':
            from mcp_lune import budgeted_lock, install
            from source_store import SourceStore
            provider = install()
            # The MCP adapter translates the product's inclusive year keys.
            native_filters = {**filters, 'year_min': filters.get('year_from'), 'year_max': filters.get('year_to')}
            with self._source_slot('lune'), SourceStore._mcp_lock(self.budget), budgeted_lock(provider._lock, self.budget):
                key = getattr(self.config, 'lune_key', '')
                endpoint = getattr(self.config, 'lune_endpoint', '')
                client_override = None
                if key or endpoint:
                    from mcp_client import McpClient, load_servers
                    server = load_servers(provider.config_path, on_missing='drop')['lune']
                    from mcp_lune_policy import (lune_override_headers, saved_key_matches_snapshot,
                                                 validate_lune_auth_target)
                    headers = {k: v for k, v in lune_override_headers(
                        server.headers, endpoint or server.url, server.url).items()
                        if k.lower() != 'authorization'}
                    store = getattr(self.config, 'source_store', None)
                    explicit_key = (bool(key) if store is None else
                                    saved_key_matches_snapshot(key, endpoint or '', store._row('lune')))
                    validate_lune_auth_target(endpoint or server.url, server.url,
                                              require_same_origin=not explicit_key)
                    if key:
                        headers['Authorization'] = 'Bearer ' + key
                    else:
                        from mcp_lune import access_token
                        headers['Authorization'] = 'Bearer ' + access_token(budget=self.budget)
                    client_override = McpClient(replace(server, headers=headers,
                                                        url=endpoint or server.url), budget=self.budget)
                def before_page():
                    while True:
                        self._wait_for('lune')
                        if not self._wait_for_cooldown('lune'):
                            return
                def before_send():
                    scope = getattr(self.store, 'budget_scope', None)
                    with scope(self.budget) if callable(scope) else nullcontext():
                        while self._wait_for_cooldown('lune'):
                            self._wait_for('lune')
                options = {'before_page': before_page, 'before_send': before_send}
                if client_override is not None:
                    options['_client_override'] = client_override
                return provider.search(query, limit, native_filters, budget=self.budget, **options)
        raise GuideError(f'Unknown source: {name}')

    def _cache_path(self, query: str, limit: int, providers: list[str],
                    filters: dict | None = None) -> Path:
        # Preserve the frozen legacy experiment key, while research never reuses
        # records from the former S2 repository-as-journal mapping.
        version = 'v10-product-provenance-count10'
        key = hashlib.sha256(json.dumps([version, self.config.research_mode, providers, query, limit, filters or {}],
                                        sort_keys=True).encode()).hexdigest()
        return self.config.cache_dir / (key + '.json')

    @_source_budget_scope
    def search_batch(self, query: str, limit: int = 10,
                     providers: list[str] | None = None,
                     filters: dict | None = None) -> SearchBatch:
        """Fuse at most ``limit`` records per provider before caller ranking.

        The router applies the final assessment-pool cap after it has ranked
        every source's candidates. Capping here would favor provider order.
        """
        self.budget.check_cancelled()
        self.budget.remaining()
        limit = min(max(limit, 1), 10)
        selected = [p for p in (providers if providers is not None else self.providers) if p in PROVIDERS]
        if not selected:
            raise GuideError('No supported source selected.')
        selected = list(dict.fromkeys(selected))
        path = self._cache_path(query, limit, selected, filters)
        # Existing cache keys do not bind Lune results to an account/endpoint.
        # Preserve old files, but do not read or write unscoped Lune batches.
        cache_allowed = self.config.cache and 'lune' not in selected
        if cache_allowed and path.exists():
            try:
                cached = json.loads(path.read_text(encoding='utf-8'))
                # Publication decisions can change (for example withdrawal).
                ttl = (300 if not cached.get('papers') else
                       3600 if any(p.get('publication') for p in cached.get('papers', [])) else 86400)
                if cached.get('schema') == 2 and time.time() - cached['stored_at'] < ttl:
                    papers = [Paper(**p) for p in cached['papers']]
                    self.budget.cache_hits += 1
                    runs = [SourceRun(**{**raw, 'status': 'cache', 'kind': 'cache', 'code': 'cache'})
                            for raw in cached.get('runs', [])]
                    # Historical provider attribution is retained. A cache hit
                    # never clears a real connection failure or asserts live health.
                    batch = SearchBatch(query, selected, papers, runs)
                    self.budget.check_cancelled()
                    self.budget.remaining()
                    self.last_batch = batch
                    return batch
            except (OSError, ValueError, KeyError, TypeError):
                pass

        runs: list[SourceRun] = []
        collected: list[Paper] = []

        def filtered_papers(items):
            venue = str((filters or {}).get('venue') or '')
            year_from = (filters or {}).get('year_from')
            year_to = (filters or {}).get('year_to')
            papers = merge_papers([p for p in items if p.title and
                                   (p.url or normalize_doi(p.doi) or arxiv_id(p.arxiv_id)
                                    or (p.id and p.id.startswith('lune:')))])
            def matches_version(paper):
                if not venue and year_from is None and year_to is None:
                    return True
                if paper.publication and paper.publication.get('records'):
                    from research_policy import publication_matches
                    return publication_matches(paper, {'venue': venue, 'year_from': year_from,
                                                         'year_to': year_to})
                return venue_matches(paper.venue, venue) and year_matches(paper.year, year_from, year_to)
            papers = [p for p in papers if matches_version(p)]
            preferred = curated_preferences((filters or {}).get('preferred_venues'),
                                             (filters or {}).get('publication_type', 'any')) if not venue else []
            return [p for p in papers if preferred_venue_matches(p, preferred)] if preferred else papers

        def retain_lune_partial(exc, started, reason):
            partial = [p for p in getattr(exc, 'partial_papers', [])[:limit]
                       if isinstance(p, Paper) and p.title and
                       (p.url or normalize_doi(p.doi) or arxiv_id(p.arxiv_id)
                        or (p.id and p.id.startswith('lune:')))]
            detail = getattr(exc, 'partial_reason', '')
            if detail not in ('quota_finalization_failed', 'lune_ledger_unavailable',
                              'nonadvancing_offset', 'invalid_offset', 'max_pages', 'empty_page'):
                detail = reason
            run = SourceRun('lune', 'partial', len(partial),
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            cost_cny=None, kind='partial', code=detail,
                            audit_status='failed' if getattr(exc, 'audit_status', None) == 'failed' else None,
                            **_safe_outcome(exc))
            self.last_batch = SearchBatch(query, selected, filtered_papers(collected + partial),
                                          runs + [run], stop_reason=reason)
            exc.partial_batch = self.last_batch

        def retain_lune_completed(exc, started, reason):
            partial = [p for p in getattr(exc, 'partial_papers', [])[:limit]
                       if isinstance(p, Paper) and p.title and
                       (p.url or normalize_doi(p.doi) or arxiv_id(p.arxiv_id)
                        or (p.id and p.id.startswith('lune:')))]
            run = SourceRun('lune', 'ok' if partial else 'empty', len(partial),
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            cost_cny=None, kind='ok' if partial else 'empty', code='audit_interrupted',
                            remote_outcome='completed', retry_safe=False,
                            audit_status='failed' if getattr(exc, 'audit_status', None) == 'failed' else None)
            self.last_batch = SearchBatch(query, selected, filtered_papers(collected + partial),
                                          runs + [run], stop_reason=reason)
            exc.partial_batch = self.last_batch

        def retain_received_budget(exc):
            if (not runs or runs[-1].status not in ('ok', 'empty')
                    or getattr(exc, 'remote_outcome', None) == 'unknown'):
                return
            completed = deepcopy(runs)
            completed[-1].remote_outcome, completed[-1].retry_safe = 'completed', False
            exc.remote_outcome, exc.retry_safe = 'completed', False
            exc.partial_batch = SearchBatch(query, selected, filtered_papers(collected),
                                            completed, stop_reason='budget_exceeded')
            self.last_batch = exc.partial_batch

        def retain_prior_unknown(exc, started, name, reason):
            if not collected or hasattr(exc, 'partial_batch'):
                return
            remote = getattr(exc, 'remote_outcome', None)
            if remote not in ('not_started', 'unknown', 'error', 'response_received'):
                remote = 'unknown'
                exc.remote_outcome, exc.retry_safe = remote, False
            retry = getattr(exc, 'retry_safe', None)
            if type(retry) is not bool:
                retry = False
                exc.retry_safe = retry
            completed = deepcopy(runs)
            for run in completed:
                if run.status in ('ok', 'empty', 'cache'):
                    run.remote_outcome, run.retry_safe = 'completed', False
            status = 'not_started' if remote == 'not_started' else 'partial'
            interrupted = SourceRun(name, status, elapsed_ms=int((time.monotonic() - started) * 1000),
                                    cost_cny=None if name in ('lune', 'deepseek') and remote != 'not_started' else 0.0,
                                    kind=status, code=reason, remote_outcome=remote, retry_safe=retry)
            exc.partial_batch = SearchBatch(query, selected, filtered_papers(collected),
                                            completed + [interrupted], stop_reason=reason)
            self.last_batch = exc.partial_batch

        stopped = False
        paid_followups_blocked = False
        for name in selected:
            if self.budget.cancel_token.requested and any(r.status in ('ok', 'empty') for r in runs):
                stopped = True
                break
            self.budget.check_cancelled()
            try:
                self.budget.remaining()
            except BudgetExceeded as exc:
                retain_received_budget(exc)
                raise
            started = time.monotonic()
            if paid_followups_blocked and name in ('lune', 'deepseek'):
                skipped = SourceRun(name, 'skipped', error='prior paid outcome unresolved',
                                    kind='skipped', code='unresolved_paid_outcome')
                runs.append(skipped)
                self.record_run(skipped)
                continue
            # Check persistent cooldown explicitly: available() also covers
            # missing credentials, whose legacy single-source behavior differs.
            with self._lock:
                in_cooldown = self.cooldown.get(name, 0) > time.monotonic()
            if self.store is not None:
                in_cooldown = self.store.cooldown(name) > time.time() or in_cooldown
            if in_cooldown or not self.available(name):
                if len(selected) == 1 and in_cooldown:
                    error = GuideError(f'{PROVIDERS[name].label} is temporarily unavailable (cooldown).')
                    error.code = 'unavailable'
                    error.remote_outcome, error.retry_safe = 'not_started', False
                    raise error
                skipped = SourceRun(name, 'skipped', error='missing credentials or cooldown',
                                    kind='skipped', code='unavailable')
                runs.append(skipped)
                self.record_run(skipped)
                self.last_errors[name] = 'missing credentials or cooldown'
                continue
            try:
                with operation('search', source=name, category='source', limit=limit) as measured:
                    response = self._search_one(name, query, limit, filters)
                    if name == 'lune':
                        from mcp_lune import LuneSearchResult
                        if (isinstance(response, LuneSearchResult) and not response.partial
                                and response.audit_status == 'failed'):
                            error = GuideError('Lune search completed, but local audit failed; candidates were retained.')
                            error.remote_outcome, error.retry_safe = 'completed', False
                            error.completed_audit_failure = True
                            error.partial_papers = list(response)[:limit]
                            error.audit_status = 'failed'
                            raise error
                        if isinstance(response, LuneSearchResult) and response.partial:
                            error = GuideError('Lune search stopped early; prior candidates were retained.')
                            error.remote_outcome, error.retry_safe = 'unknown', False
                            error.partial_search = True
                            error.partial_papers = list(response)[:limit]
                            error.partial_reason = response.stop_reason
                            error.audit_status = response.audit_status
                            raise error
                    items = response[:limit]
                    # Once received, retain the bounded response before checking the deadline.
                    try:
                        if not self.budget.cancel_token.requested:
                            self.budget.remaining()
                    except BudgetExceeded as exc:
                        success = SourceRun(name, 'ok' if items else 'empty', len(items),
                                            elapsed_ms=int((time.monotonic() - started) * 1000),
                                            cost_cny=None if name == 'lune' else (0.005 if name == 'deepseek' else 0.0),
                                            kind='ok' if items else 'empty', code='budget_exceeded',
                                            remote_outcome='completed', retry_safe=False)
                        exc.remote_outcome, exc.retry_safe = 'completed', False
                        exc.partial_batch = SearchBatch(query, selected, filtered_papers(collected + items),
                                                        runs + [success], stop_reason='budget_exceeded')
                        self.last_batch = exc.partial_batch
                        raise
                    measured.update(returned=len(items), status='ok' if items else 'empty')
                status = 'ok' if items else 'empty'
                cost_cny = None if name == 'lune' else (0.005 if name == 'deepseek' else 0.0)
                success = SourceRun(name, status, len(items),
                                    elapsed_ms=int((time.monotonic() - started) * 1000),
                                    cost_cny=cost_cny, kind=status, code=status)
                runs.append(success)
                self.record_run(success)
                collected.extend(items)
                self.last_errors.pop(name, None)
                if self.budget.cancel_token.requested:
                    success.remote_outcome, success.retry_safe = 'completed', False
                    stopped = True
                    break
            except (RunCancelled, BudgetExceeded) as exc:
                if name == 'lune' and getattr(exc, 'partial_search', False):
                    retain_lune_partial(exc, started, 'user_cancelled'
                                        if isinstance(exc, RunCancelled) else 'budget_exceeded')
                elif (name == 'lune' and getattr(exc, 'remote_outcome', None) == 'completed'
                      and isinstance(getattr(exc, 'partial_papers', None), list)):
                    retain_lune_completed(exc, started, 'user_cancelled'
                                          if isinstance(exc, RunCancelled) else 'budget_exceeded')
                else:
                    retain_prior_unknown(exc, started, name, 'user_cancelled'
                                         if isinstance(exc, RunCancelled) else 'budget_exceeded')
                    if isinstance(exc, BudgetExceeded) and not hasattr(exc, 'partial_batch'):
                        retain_received_budget(exc)
                raise
            except (GuideError, ValueError, TypeError, KeyError) as exc:
                if name == 'lune' and getattr(exc, 'completed_audit_failure', False):
                    retain_lune_completed(exc, started, 'source_error')
                    raise
                if name == 'lune' and getattr(exc, 'partial_search', False):
                    retain_lune_partial(exc, started, 'source_error')
                    raise
                try:
                    self.budget.check_cancelled()
                except RunCancelled as cancelled:
                    for key, value in _safe_outcome(exc).items():
                        setattr(cancelled, key, value)
                    retain_prior_unknown(cancelled, started, name, 'user_cancelled')
                    raise
                code = classify_source_error(exc)
                if name in ('lune', 'deepseek') and getattr(exc, 'remote_outcome', None) != 'not_started':
                    paid_followups_blocked = True
                    if getattr(exc, 'remote_outcome', None) not in REMOTE_OUTCOMES:
                        exc.remote_outcome, exc.retry_safe = 'unknown', False
                failed = SourceRun(name, 'error', error=str(exc), kind='error', code=code,
                                   elapsed_ms=int((time.monotonic() - started) * 1000),
                                   cost_cny=None if name in ('lune', 'deepseek') else 0.0,
                                   **_safe_outcome(exc))
                runs.append(failed)
                try:
                    self.record_run(failed)
                except RunCancelled:
                    raise
                except Exception:
                    # Audit is best effort on an already failed request.
                    pass
                self.last_errors[name] = str(exc)
        if self.budget.cancel_token.requested and any(r.status in ('ok', 'empty') for r in runs):
            stopped = True
        if not stopped:
            self.budget.check_cancelled()
            # A bounded audit can consume the last task time. Do not replace an
            # already observed single-provider failure with that audit's deadline.
            if not (len(selected) == 1 and any(run.status == 'error' for run in runs)):
                try:
                    self.budget.remaining()
                except BudgetExceeded as exc:
                    retain_received_budget(exc)
                    raise
        # Merge before publication filtering so distinct versions of the same
        # work can contribute their verified metadata and abstract.
        papers = filtered_papers(collected)
        batch = SearchBatch(query, selected, papers, runs,
                            stop_reason='user_cancelled' if stopped else '')
        self.last_batch = batch
        failures = [r for r in runs if r.status == 'error']
        if len(selected) == 1 and failures and not papers:
            error = GuideError(failures[0].error or 'Source request failed.')
            error.code = failures[0].code
            for key, value in _safe_outcome(failures[0]).items():
                setattr(error, key, value)
            raise error
        if (cache_allowed and not stopped and not self.budget.cancel_token.requested
                and runs and all(row.status in ('ok', 'empty') for row in runs)):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                                 prefix=path.stem, suffix='.tmp', delete=False) as handle:
                    tmp = Path(handle.name)
                    json.dump({'schema': 2, 'stored_at': time.time(),
                               'runs': [row.as_dict() for row in runs],
                               'papers': [p.to_dict() for p in papers]}, handle, ensure_ascii=False)
                tmp.replace(path)
            except OSError:
                pass
        return batch

    def search(self, query: str, limit: int = 10, *, filters: dict | None = None) -> list[Paper]:
        return self.search_batch(query, limit, filters=filters).papers

    @_source_budget_scope
    def enrich_openalex_citations(self, papers: list[Paper], *, max_lookups: int = 10) -> tuple[list[Paper], dict]:
        """Fill missing citations by exact DOI only, without changing discovery facts.

        OpenAlex must be among the caller's selected and enabled providers. Failed,
        invalid or unmatched lookups stay unknown; no search/title fallback is used.
        The caller controls when to invoke this bounded, potentially billable operation.
        """
        if type(max_lookups) is not int or not 0 <= max_lookups <= 20:
            raise ValueError('max_lookups must be an integer between 0 and 20')
        if 'openalex' not in self.providers or not self.available('openalex'):
            raise GuideError('OpenAlex must be selected and enabled for citation enrichment.')
        cache = {}
        stats = {'attempted': 0, 'updated': 0, 'unresolved': 0}
        output = []
        for paper in papers:
            self.budget.check_cancelled()
            self.budget.remaining()
            identifier = normalize_doi(paper.doi)
            if paper.citation_count is not None or not identifier:
                output.append(paper)
                continue
            if identifier not in cache and stats['attempted'] < max_lookups:
                stats['attempted'] += 1
                params = {'api_key': self.config.openalex_key} if self.config.openalex_key else {}
                endpoint = OPENALEX_WORKS_URL + '/doi:' + quote(identifier, safe='')
                if params:
                    endpoint += '?' + urlencode(params)
                try:
                    work = self._json(endpoint, provider='openalex')
                    count = work.get('cited_by_count') if isinstance(work, dict) else None
                    returned_doi = normalize_doi(work.get('doi') or '') if isinstance(work, dict) else ''
                    work_id = str(work.get('id') or '') if isinstance(work, dict) else ''
                    valid = (returned_doi == identifier and re.fullmatch(r'https://openalex\.org/W\d+', work_id)
                             and type(count) is int and count >= 0)
                    cache[identifier] = (count, utc_now()) if valid else None
                except (RunCancelled, BudgetExceeded):
                    raise
                except GuideError:
                    cache[identifier] = None
            found = cache.get(identifier)
            if found is None:
                stats['unresolved'] += 1
                output.append(paper)
            else:
                stats['updated'] += 1
                output.append(replace(paper, citation_count=found[0], citation_source='openalex',
                                      citation_observed_at=found[1]))
        return output, stats

    @_source_budget_scope
    def lookup(self, identifier: str) -> list[Paper]:
        if not arxiv_id(identifier):
            raise GuideError('Exact lookup currently supports arXiv IDs/URLs only; no fuzzy fallback.')
        return self._arxiv(identifier, 1, lookup=True)
