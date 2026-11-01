"""Small shared types, configuration and request budget. No agent framework required."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('SG_DATA_DIR') or ROOT).expanduser()
RUNS_DIR = Path(os.environ.get('SG_RUNS_DIR') or DATA_DIR / 'runs').expanduser()

# Bounded facts, not an authorization to repeat a remote operation.
REMOTE_OUTCOMES = ('not_started', 'unknown', 'response_received', 'error', 'completed')


class GuideError(Exception):
    """An expected failure which is safe to describe in a run report."""


class BudgetExceeded(GuideError):
    pass


class RunCancelled(GuideError):
    """A cooperative user stop; in-flight requests are allowed to finish."""
    code = 'user_cancelled'

    def __init__(self):
        super().__init__('Stopped by the user; verified results were retained.')


class CancellationToken:
    """Thread-safe admission token. Requesting stop never touches persisted state."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self) -> bool:
        with self._lock:
            first = not self._event.is_set()
            self._event.set()
            return first

    def check(self) -> None:
        if self.requested:
            raise RunCancelled()

    def wait(self, seconds: float) -> None:
        self.check()
        if self._event.wait(max(0, seconds)):
            raise RunCancelled()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env(path: Path | None = None) -> None:
    """Read literal KEY=VALUE lines; shell interpolation is deliberately unsupported."""
    if path is None:
        path = Path(os.environ.get('SG_ENV_FILE') or ROOT / '.env').expanduser()
    if path == ROOT / '.env' and os.environ.get('SG_SKIP_PROJECT_ENV') == '1':
        return
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not key.strip().replace('_', '').isalnum():
            raise GuideError('Invalid .env entry; expected KEY=VALUE.')
        os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def resolve_api_key(value: str) -> str:
    """Optional, explicit local credential reference. Never copy the key into project files."""
    if not value.startswith('@pi:'):
        return value
    provider = value[4:]
    if not provider or not all(c.isalnum() or c in '-_' for c in provider):
        raise GuideError('Invalid @pi:provider credential reference.')
    path = Path(os.getenv('PI_CODING_AGENT_DIR', str(Path.home() / '.pi' / 'agent'))) / 'auth.json'
    try:
        auth = json.loads(path.read_text(encoding='utf-8'))[provider]
        if auth.get('type') != 'api_key' or not isinstance(auth.get('key'), str) or not auth['key']:
            raise ValueError
        return auth['key']
    except (OSError, KeyError, ValueError, TypeError):
        raise GuideError('Referenced pi API key is unavailable; set SG_API_KEY explicitly on this machine.') from None


SOURCE_NAMES = ('arxiv', 'openalex', 'semantic_scholar', 'dblp',
                'openreview', 'acl_anthology', 'deepseek', 'lune')
SOURCE_ALIASES = {
    's2': 'semantic_scholar', 'semanticscholar': 'semantic_scholar',
    'acl': 'acl_anthology', 'anthology': 'acl_anthology',
    'open_review': 'openreview', 'dblp': 'dblp',
}
DEFAULT_MULTI = ('lune', 'arxiv', 'openalex', 'semantic_scholar', 'dblp', 'openreview', 'acl_anthology')
MIN_PAPERS, MAX_PAPERS, DEFAULT_PAPERS = 1, 10, 5


def source_names(value: str) -> list[str]:
    """Parse a single source, a comma list, or the ``multi`` preset."""
    value = (value or 'arxiv').strip().lower()
    if value in ('multi', 'all'):
        return list(DEFAULT_MULTI)
    names = []
    for raw in value.replace('+', ',').split(','):
        name = SOURCE_ALIASES.get(raw.strip(), raw.strip())
        if name and name not in names:
            names.append(name)
    return names or ['arxiv']


@dataclass
class Config:
    provider: str = 'openai'
    base_url: str = 'https://api.deepseek.com'
    model: str = 'deepseek-flash'
    api_key: str = field(default='', repr=False)
    source: str = 'arxiv'
    agent_runtime: str = 'langgraph'
    request_timeout: float = 90
    source_timeout: float = 15
    task_timeout: float = 600
    max_output_tokens: int = 3000
    protocol: str = 'chat_completions'
    profile_id: str = 'env'
    profile_name: str = 'Environment / default'
    input_price: float | None = None
    output_price: float | None = None
    currency: str = 'CNY'
    per_search: int = 10
    batch_size: int = 5
    cache: bool = True
    cache_dir: Path = ROOT / '.cache'
    openalex_key: str = field(default='', repr=False)
    s2_key: str = field(default='', repr=False)
    deepseek_search_key: str = field(default='', repr=False)
    lune_key: str = field(default='', repr=False)
    lune_endpoint: str = ''
    deepseek_search_endpoint: str = 'https://api.deepseek.com/v1/tools/search'
    # Research discovery is enabled by the product entry point; legacy CLI and
    # the frozen arXiv experiment keep their original bounded workflow.
    research_mode: bool = False
    source_store: object | None = field(default=None, repr=False, compare=False)
    action_protocol: str = 'json'
    native_fallback: str = 'disabled'

    @classmethod
    def from_env(cls) -> Config:
        load_env()
        try:
            config = cls(
                provider=os.getenv('SG_PROVIDER', 'openai'),
                base_url=os.getenv('SG_BASE_URL', 'https://api.deepseek.com').rstrip('/'),
                model=os.getenv('SG_MODEL', 'deepseek-flash'),
                api_key=resolve_api_key(os.getenv('SG_API_KEY', '')),
                source=os.getenv('SG_SOURCE', 'arxiv'),
                agent_runtime=os.getenv('SG_AGENT_RUNTIME', 'langgraph'),
                request_timeout=float(os.getenv('SG_REQUEST_TIMEOUT', '90')),
                source_timeout=float(os.getenv('SG_SOURCE_TIMEOUT', '15')),
                task_timeout=float(os.getenv('SG_TASK_TIMEOUT', '600')),
                max_output_tokens=int(os.getenv('SG_MAX_OUTPUT_TOKENS', '3000')),
                protocol=os.getenv('SG_PROTOCOL', 'chat_completions'),
                action_protocol=os.getenv('SG_ACTION_PROTOCOL', 'json').strip().lower(),
                native_fallback=os.getenv('SG_NATIVE_FALLBACK', 'disabled').strip().lower(),
                profile_id=os.getenv('SG_PROFILE_ID', 'env'),
                profile_name=os.getenv('SG_PROFILE_NAME', 'Environment / default'),
                input_price=float(os.getenv('SG_INPUT_PRICE')) if os.getenv('SG_INPUT_PRICE') not in (None, '') else None,
                output_price=float(os.getenv('SG_OUTPUT_PRICE')) if os.getenv('SG_OUTPUT_PRICE') not in (None, '') else None,
                currency=os.getenv('SG_CURRENCY', 'CNY'),
                cache_dir=Path(os.getenv('SG_CACHE_DIR', str(DATA_DIR / '.cache'))).expanduser(),
                openalex_key=os.getenv('OPENALEX_API_KEY', ''),
                s2_key=os.getenv('S2_API_KEY', os.getenv('SEMANTIC_SCHOLAR_API_KEY', '')),
                deepseek_search_key=os.getenv('DEEPSEEK_SEARCH_KEY', ''),
                research_mode=os.getenv('SG_RESEARCH_MODE', '').strip().lower() in ('1', 'true', 'yes'),
            )
        except ValueError as exc:
            raise GuideError('Timeout settings must be numbers.') from exc
        config._validate_action_protocol()
        return config

    def _validate_action_protocol(self) -> None:
        if self.action_protocol not in ('json', 'responses_native'):
            raise GuideError('action_protocol must be json or responses_native.')
        if self.native_fallback not in ('disabled', 'json_on_unsupported'):
            raise GuideError('native_fallback must be disabled or json_on_unsupported.')
        if (self.action_protocol == 'responses_native'
                and (self.provider != 'openai' or self.protocol != 'responses')):
            raise GuideError('responses_native requires the openai provider and responses protocol.')
        if self.native_fallback == 'json_on_unsupported' and self.action_protocol != 'responses_native':
            raise GuideError('json_on_unsupported requires responses_native.')

    def validate(self) -> None:
        self._validate_action_protocol()
        if type(self.research_mode) is not bool:
            raise GuideError('research_mode must be a boolean.')
        if self.provider not in ('ollama', 'openai'):
            raise GuideError('SG_PROVIDER must be ollama or openai.')
        if self.agent_runtime not in ('legacy', 'langgraph'):
            raise GuideError('SG_AGENT_RUNTIME must be legacy or langgraph.')
        if self.protocol not in ('chat_completions', 'responses'):
            raise GuideError('SG_PROTOCOL must be chat_completions or responses.')
        if self.input_price is not None and (not math.isfinite(self.input_price) or self.input_price < 0):
            raise GuideError('Input price must be non-negative or empty.')
        if self.output_price is not None and (not math.isfinite(self.output_price) or self.output_price < 0):
            raise GuideError('Output price must be non-negative or empty.')
        if len(self.currency or '') != 3 or not (self.currency or '').isalpha():
            raise GuideError('Currency must be a 3-letter code.')
        unknown = [name for name in source_names(self.source) if name not in SOURCE_NAMES]
        if unknown:
            raise GuideError('Unknown source(s): ' + ', '.join(unknown))
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in self.base_url):
            raise GuideError('Model base URL must not contain whitespace or control characters.')
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in self.api_key):
            raise GuideError('API key must not contain control characters.')
        try:
            url = urlsplit(self.base_url)
            port = url.port
        except ValueError:
            raise GuideError('Model base URL is not a valid URL.') from None
        if (url.scheme not in ('http', 'https') or not url.hostname or port == 0
                or url.username or url.password or url.query or url.fragment):
            raise GuideError('Model base URL must be HTTP(S), without embedded credentials/query.')
        if not self.model or any(not math.isfinite(t) or t <= 0 for t in (
                self.request_timeout, self.source_timeout, self.task_timeout)):
            raise GuideError('A model and positive timeouts are required.')
        if not 256 <= self.max_output_tokens <= 100000:
            raise GuideError('max_output_tokens must be between 256 and 100000.')
        if not 1 <= self.per_search <= 10 or not 1 <= self.batch_size <= 5:
            raise GuideError('per_search must be 1–10; batch_size must be 1–5.')

    def public(self) -> dict:
        # Do not persist API keys, model endpoint or environment variables.
        return {k: getattr(self, k) for k in (
            'provider', 'model', 'source', 'agent_runtime', 'request_timeout', 'source_timeout', 'task_timeout',
            'per_search', 'batch_size', 'cache', 'max_output_tokens', 'protocol', 'action_protocol',
            'native_fallback', 'profile_id',
            'profile_name', 'input_price', 'output_price', 'currency', 's2_enabled',
            'deepseek_search_enabled', 'research_mode')}

    @property
    def s2_enabled(self) -> bool:
        return True

    @property
    def deepseek_search_enabled(self) -> bool:
        return bool(self.deepseek_search_key)


@dataclass
class Budget:
    seconds: float
    started: float = field(default_factory=time.monotonic)
    search_http_calls: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_calls: int = 0
    usage_observed: bool = False
    cancel_token: CancellationToken = field(default_factory=CancellationToken, repr=False)

    def check_cancelled(self) -> None:
        self.cancel_token.check()

    def remaining(self) -> float:
        remaining = self.seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise BudgetExceeded('Task time budget exhausted.')
        return remaining

    def wait(self, seconds: float) -> None:
        self.check_cancelled()
        if seconds >= self.remaining():
            raise BudgetExceeded('Not enough task time remaining for retry/rate limit wait.')
        self.cancel_token.wait(seconds)

    def metrics(self) -> dict:
        # A partial usage report is not a known cost. Require every LLM call
        # in this task/turn to have returned usage before exposing token totals.
        complete = self.llm_calls > 0 and self.llm_calls == self.usage_calls
        return {
            'elapsed_seconds': round(time.monotonic() - self.started, 3),
            'search_http_calls': self.search_http_calls, 'llm_calls': self.llm_calls,
            'cache_hits': self.cache_hits,
            'input_tokens': self.input_tokens if complete else None,
            'output_tokens': self.output_tokens if complete else None,
            'usage_complete': complete,
            'usage_calls': self.usage_calls,
        }


VENUE_ALIASES = {
    'cvpr': ('cvpr', 'computer vision and pattern recognition'),
    'iccv': ('iccv', 'international conference on computer vision'),
    'eccv': ('eccv', 'european conference on computer vision'),
    'neurips': ('neurips', 'nips', 'neural information processing systems'),
    'nips': ('nips', 'neurips', 'neural information processing systems'),
    'icml': ('icml', 'international conference on machine learning'),
    'iclr': ('iclr', 'international conference on learning representations'),
    'aaai': ('aaai', 'aaai conference on artificial intelligence'),
    'ijcai': ('ijcai', 'international joint conference on artificial intelligence'),
    'acl': ('acl', 'association for computational linguistics'),
    'emnlp': ('emnlp', 'empirical methods in natural language processing'),
    'naacl': ('naacl', 'north american chapter of the association for computational linguistics'),
    'kdd': ('kdd', 'knowledge discovery and data mining'),
    'sigir': ('sigir', 'research and development in information retrieval'),
    'miccai': ('miccai', 'medical image computing and computer assisted intervention'),
    'tmlr': ('tmlr', 'transactions on machine learning research'),
}


def split_venue_year(venue: str, year_from: int | None = None,
                     year_to: int | None = None) -> tuple[str, int | None, int | None]:
    """Separate a year embedded in a venue label from the venue name.

    LLMs commonly emit ``venue='CVPR 2026'`` even when the schema has separate
    year fields.  OpenAlex resolves a venue source by name, so passing the year
    verbatim makes a valid venue look unavailable.  Keep explicit year fields
    authoritative; infer them only when the label contains exactly one year.
    """
    raw = str(venue or '').strip()
    years = list(re.finditer(r'(?<!\d)((?:19|20)\d{2})(?!\d)', raw))
    if len(years) != 1:
        return raw, year_from, year_to
    year = int(years[0].group(1))
    cleaned = (raw[:years[0].start()] + ' ' + raw[years[0].end():]).strip(' ,-/')
    cleaned = ' '.join(cleaned.split())
    if not cleaned:
        return raw, year_from, year_to
    if year_from is None and year_to is None:
        year_from = year_to = year
    return cleaned, year_from, year_to


def normalize_venue(value: str) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', str(value or '').casefold()))


def venue_matches(venue: str, requested: str) -> bool:
    """Match a requested venue against metadata, including common aliases."""
    requested = normalize_venue(requested)
    if not requested:
        return True
    haystack = normalize_venue(venue)
    if not haystack:
        return False
    if requested in haystack:
        return True
    aliases = set()
    for token in requested.split():
        aliases.update(VENUE_ALIASES.get(token, ()))
    return any(alias and alias in haystack for alias in aliases)


def year_matches(year: int | None, year_from: int | None, year_to: int | None) -> bool:
    if year_from is None and year_to is None:
        return True
    if year is None:
        return False
    if year_from is not None and year < year_from:
        return False
    if year_to is not None and year > year_to:
        return False
    return True


@dataclass
class Request:
    topic: str
    category: str = 'none'
    minimum_category_count: int = 0
    target_count: int = 5
    unsupported: list[str] = field(default_factory=list)
    # v2: explicit task fields the updater may change. Empty defaults keep the
    # single-pass workflow bit-for-bit compatible.
    purpose: str = ''
    excluded_paper_ids: list[str] = field(default_factory=list)
    prefer_recent: bool = False
    venue: str = ''
    year_from: int | None = None
    year_to: int | None = None
    reading_goal: str = ''
    venue_scope: str = 'any'
    publication_type: str = 'any'
    accepted_only: bool = False
    presentation_types: list[str] = field(default_factory=list)
    minimum_count: int | None = None
    maximum_count: int | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Request:
        if not isinstance(data, dict):
            raise GuideError('Request must be a JSON object.')
        topic = data.get('topic')
        category = data.get('category', 'none')
        minimum = data.get('minimum_category_count', 0)
        target = data.get('target_count', 5)
        unsupported = data.get('unsupported', [])
        purpose = data.get('purpose', '')
        excluded = data.get('excluded_paper_ids', [])
        prefer_recent = data.get('prefer_recent', False)
        venue = data.get('venue') or ''
        year_from = data.get('year_from')
        year_to = data.get('year_to')
        reading_goal = data.get('reading_goal') or ''
        venue_scope = data.get('venue_scope', 'any')
        publication_type = data.get('publication_type', 'any')
        accepted_only = data.get('accepted_only', False)
        presentation_types = data.get('presentation_types', [])
        if venue_scope not in ('any', 'preferred', 'preferred_only'):
            raise GuideError('venue_scope must be any, preferred or preferred_only.')
        if publication_type not in ('any', 'conference', 'journal', 'preprint'):
            raise GuideError('publication_type must be any, conference, journal or preprint.')
        if type(accepted_only) is not bool:
            raise GuideError('accepted_only must be a boolean.')
        if (not isinstance(presentation_types, list) or len(presentation_types) > 3
                or any(value not in ('oral', 'spotlight', 'poster') for value in presentation_types)):
            raise GuideError('presentation_types must contain oral, spotlight or poster.')
        presentation_types = list(dict.fromkeys(presentation_types))
        if presentation_types:
            accepted_only = True
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 500:
            raise GuideError('Topic must be nonempty and at most 500 characters.')
        if category not in ('none', 'evaluation', 'survey', 'research'):
            raise GuideError('Supported categories: none, evaluation, survey.')
        if type(minimum) is not int or not 0 <= minimum <= MAX_PAPERS:
            raise GuideError('Category quota must be an integer between 0 and 10.')
        if type(target) is not int or not MIN_PAPERS <= target <= MAX_PAPERS or (category == 'none' and minimum):
            raise GuideError('每次推荐支持 1–10 篇，未指定时默认 5 篇。')
        low, high = data.get('minimum_count'), data.get('maximum_count')
        if (low is None) != (high is None):
            raise GuideError('minimum_count and maximum_count must be supplied together.')
        if low is not None:
            if type(low) is not int or type(high) is not int or not 1 <= low <= high <= MAX_PAPERS:
                raise GuideError('推荐数量区间须在 1–10 篇内，且下限不高于上限。')
            if 'target_count' not in data:
                target = min(high, max(low, DEFAULT_PAPERS))
            if not low <= target <= high:
                raise GuideError('target_count must be inside the requested count interval.')
        if minimum > target:
            raise GuideError('类别最低篇数不能超过推荐总数；请调整数量或类别要求。')
        if not isinstance(unsupported, list) or not all(isinstance(x, str) for x in unsupported):
            raise GuideError('unsupported must be a list of strings.')
        if not isinstance(purpose, str) or len(purpose) > 500:
            raise GuideError('purpose must be a string of at most 500 characters.')
        if not isinstance(excluded, list) or not all(isinstance(x, str) for x in excluded):
            raise GuideError('excluded_paper_ids must be a list of strings.')
        if not isinstance(prefer_recent, bool):
            raise GuideError('prefer_recent must be a boolean.')
        if not isinstance(venue, str) or len(venue) > 120:
            raise GuideError('venue must be a string of at most 120 characters.')
        venue, year_from, year_to = split_venue_year(venue, year_from, year_to)
        if len(venue) > 120:
            raise GuideError('venue must be a string of at most 120 characters.')
        if reading_goal not in ('', 'auto', 'general', 'orientation', 'comparison', 'frontier'):
            raise GuideError('reading_goal must be empty, general, orientation, comparison or frontier.')
        for label, value in (('year_from', year_from), ('year_to', year_to)):
            if value is not None and (type(value) is not int or not 1900 <= value <= 2100):
                raise GuideError(f'{label} must be an integer year between 1900 and 2100.')
        if year_from is not None and year_to is not None and year_from > year_to:
            raise GuideError('year_from must not exceed year_to.')
        return cls(topic.strip(), category, minimum, target, unsupported, purpose.strip(),
                   list(excluded), prefer_recent, venue.strip(), year_from, year_to,
                   str(reading_goal).strip(), venue_scope, publication_type,
                   accepted_only, presentation_types, minimum_count=low, maximum_count=high)


@dataclass
class Paper:
    id: str
    title: str
    abstract: str
    url: str
    year: int | None = None
    authors: list[str] = field(default_factory=list)
    doi: str = ''
    arxiv_id: str = ''
    sources: list[str] = field(default_factory=list)
    from_query: list[str] = field(default_factory=list)
    fetched_at: str = ''
    # Cross-source provenance. Defaults keep old caches and fixtures readable.
    source: str = ''
    venue: str = ''
    open_access_url: str = ''
    retrieved_at: str = ''
    authority: float = 0.0
    abstract_source: str = ''
    metadata_only: bool = False
    snippet: str = ''
    provenance_conflict: bool = False
    # Publication facts are distinct from abstract-based relevance/category judgements.
    # An empty mapping means unknown, including records loaded from older caches.
    publication: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.retrieved_at and not self.fetched_at:
            self.fetched_at = self.retrieved_at
        if self.fetched_at and not self.retrieved_at:
            self.retrieved_at = self.fetched_at
        if not self.source and self.sources:
            self.source = self.sources[0]

    def to_dict(self) -> dict:
        return asdict(self)


def verified_retraction(record: dict) -> bool:
    """Only an explicit official work observation can revoke a publication.

    An unknown status, a display label, or a rumor from another domain cannot
    override prior verified metadata. Keep the underlying observation durable.
    """
    if (record.get('verified') is not True or record.get('identity_verified') is False
            or record.get('status') != 'retracted' or record.get('retracted') is not True):
        return False
    for evidence in record.get('evidence') or []:
        if not isinstance(evidence, dict):
            continue
        try:
            url = urlsplit(str(evidence.get('url') or ''))
        except ValueError:
            continue
        if (evidence.get('source') == 'openalex' and evidence.get('field') == 'is_retracted'
                and evidence.get('value') is True and url.scheme == 'https'
                and url.hostname == 'api.openalex.org' and not url.username and not url.password
                and re.fullmatch(r'/works/W\d+', url.path)):
            return True
    return False


def _retraction_applies(negative: dict, record: dict) -> bool:
    def doi(value):
        return re.sub(r'^https?://(?:dx\.)?doi\.org/', '', str(value or '').strip(), flags=re.I).casefold()
    negative_doi, record_doi = doi(negative.get('doi')), doi(record.get('doi'))
    if negative_doi and record_doi:
        return negative_doi == record_doi
    # Within a strongly identified work, match the exact publication edition
    # when one source lacks a DOI. Never revoke a distinct known DOI/version.
    venue = str(negative.get('venue_id') or '')
    year = str(negative.get('date') or '')[:4]
    return bool(venue and year.isdigit() and venue == record.get('venue_id')
                and year == str(record.get('date') or '')[:4])


def merge_publication_records(records: list[dict], conflicts: list | None = None) -> dict:
    """Merge venue-scoped facts without upgrading contradictory or weak claims."""
    from copy import deepcopy
    unique: dict[str, dict] = {}
    for value in records:
        if not isinstance(value, dict):
            continue
        record = deepcopy(value)
        # Re-observing one fact updates freshness; distinct decisions retain evidence.
        identity = {k: v for k, v in record.items()
                    if k not in ('observed_at', 'verified_at')}
        key = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        if key not in unique or str(record.get('observed_at') or '') >= str(unique[key].get('observed_at') or ''):
            unique[key] = record
    records = list(unique.values())
    conflicts = [] if conflicts is None else deepcopy(list(conflicts))
    conflicts = list({json.dumps(c, ensure_ascii=False, sort_keys=True): c for c in conflicts}.values())
    groups: dict[str, list[dict]] = {}
    for record in records:
        key = str(record.get('venue_id') or '')
        if not key and record.get('issn'):
            key = 'issn:' + '|'.join(sorted(str(i).replace('-', '').upper() for i in record['issn']))
        if not key and record.get('venue_name') and str(record.get('date') or '')[:4].isdigit():
            # A missing source ID must not merge different editions, but opposite
            # statuses for the same named edition still need an audit conflict.
            key = 'name:' + '|'.join((str(record.get('type') or ''),
                normalize_venue(record['venue_name']), str(record['date'])[:4]))
        if key:
            groups.setdefault(key, []).append(record)
    for key, versions in groups.items():
        # An unverified label cannot invalidate official venue/decision facts.
        states = {r.get('status') for r in versions if r.get('verified')}
        if states & {'accepted', 'published'} and states & {'rejected', 'withdrawn', 'desk_rejected'}:
            conflict = {'venue_id': key, 'statuses': sorted(str(s) for s in states)}
            if conflict not in conflicts:
                conflicts.append(conflict)
            for record in versions:
                record['verified'] = False
                record['verification_reason'] = 'conflicting_publication_status'
    # Conflicts cannot disappear when one competing claim is re-observed.
    for conflict in conflicts:
        if isinstance(conflict, dict):
            for record in groups.get(str(conflict.get('venue_id') or ''), []):
                record['verified'] = False
                record['verification_reason'] = 'conflicting_publication_status'
    retractions = [record for record in records if verified_retraction(record)]
    for record in records:
        if not verified_retraction(record) and any(_retraction_applies(item, record) for item in retractions):
            record['verified'] = False
            record['verification_reason'] = 'retracted_publication'
    def priority(record):
        return (bool(record.get('verified')), record.get('status') in ('accepted', 'published'),
                record.get('type') in ('journal', 'conference'), str(record.get('date') or ''))
    selected = max(records, key=priority) if records else {}
    publication = {k: deepcopy(v) for k, v in selected.items()}
    publication.update(records=records, conflicts=conflicts, schema_version=1)
    return publication
