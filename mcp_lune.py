"""Lune Research as the seventh retrieval provider (via the MCP client).

What this adapter owns
----------------------
* field mapping from Lune's ``search_papers`` payload to the frozen ``Paper`` shape
* a request budget (Lune bills per request, not per token) with a ledger on disk
* error translation so ``source.classify_source_error`` keeps working: an MCP
  ``rate_limited``/``auth`` failure must show up as such in the batch manifest

Field mapping is deliberately tolerant. The exact JSON shape of ``search_papers``
is not published field-by-field, and a key is not available in this checkout, so
the adapter accepts the plausible spellings and records what it actually saw. The
first real call (``scripts/lune_probe.py``) is what confirms the shape; until then
this is "works against a recorded shape", not "works against Lune".
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from common import Budget, BudgetExceeded, GuideError, Paper, REMOTE_OUTCOMES, RunCancelled, utc_now
from mcp_client import McpClient, McpError, McpResult, ServerConfig, load_servers, structured
from source import ProviderSpec

import mcp_plugin

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get('SG_MCP_CONFIG') or ROOT / 'mcp_servers.json')
RUN_DIR = Path(os.environ.get('SG_MCP_RUN_DIR') or ROOT / 'runs' / 'mcp-20261024').expanduser()
LEDGER_PATH = Path(os.environ.get('SG_LUNE_LEDGER') or Path.home() / '.scholarguide' / 'lune-ledger.json')
TOKEN_PATH = Path(os.environ.get('SG_MCP_TOKEN_FILE')
                  or Path.home() / '.scholarguide' / 'lune-oauth.json')
ISSUER = 'https://api.luneresearch.com'
RESOURCE = 'https://mcp.luneresearch.com'
# Cloudflare in front of api.luneresearch.com refuses the default Python UA.
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')

PROVIDER = 'lune'
# Lune bills per day. The ledger accumulates across processes, so the *default* budget
# must be "what is left today", not a per-process constant (a stale 120 blocked a live
# search with "used 241 of 120"). An explicit --max-lune-requests still means
# "additional requests for this run".
_allowance = os.environ.get('LUNE_DAILY_ALLOWANCE')
DAILY_ALLOWANCE = int(_allowance) if _allowance else None
MAX_SEARCH_LIMIT = 10
REMOTE_PAGE_SIZE = 5
MAX_SEARCH_PAGES = 3
PAPER_KEYS = ('papers', 'results', 'items', 'data', 'hits', 'documents')
_ID_KEYS = ('paper_id', 'id', 'key', 'doc_id', 'slug', 'openreview_id', 'arxiv_id', 'doi')
_TITLE_KEYS = ('title', 'name', 'paper_title')
_ABSTRACT_KEYS = ('abstract', 'abstract_text', 'abstractText')
_SNIPPET_KEYS = ('summary', 'tldr', 'snippet', 'highlight')
_URL_KEYS = ('url', 'link', 'source_url', 'paper_url', 'pdf_url')
_VENUE_KEYS = ('venue', 'conference', 'publication_venue', 'venue_name')
_YEAR_KEYS = ('year', 'published_year', 'publication_year')
_ARXIV_KEYS = ('arxiv_id', 'arxiv', 'arxivId')
_TOKEN_LOCK = threading.RLock()
_TOKEN_CACHE: tuple[str, int, dict] | None = None


class LuneSearchResult(list):
    """List-compatible results carrying host-owned search completion metadata."""

    def __init__(self, papers, *, stop_reason, audit_status='recorded', partial=True):
        super().__init__(papers)
        self.partial = partial
        self.stop_reason = stop_reason
        self.audit_status = audit_status
        self.retry_safe = False


class _InvalidSearchResponse(Exception):
    pass


@contextmanager
def budgeted_lock(lock, budget):
    if budget is None:
        with lock:
            yield
        return
    while True:
        budget.check_cancelled()
        if lock.acquire(timeout=min(.05, budget.remaining())):
            break
    try:
        budget.check_cancelled()
        budget.remaining()
        yield
    finally:
        lock.release()


@contextmanager
def _token_process_lock(budget):
    """Cooperate with project processes using this token path, not third parties."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + '.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            def acquire():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            def release():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire(): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release(): fcntl.flock(fd, fcntl.LOCK_UN)
        while True:
            if budget is not None:
                budget.check_cancelled()
                delay = min(.05, budget.remaining())
            else:
                delay = .05
            try:
                acquire()
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                time.sleep(delay)
        if budget is not None:
            budget.check_cancelled()
            budget.remaining()
        yield
    finally:
        try:
            if acquired:
                release()
        finally:
            os.close(fd)


def _first(item: dict, keys: tuple) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, '', []):
            return value
    return ''


def token_path() -> Path:
    return Path(os.environ.get('SG_MCP_TOKEN_FILE') or TOKEN_PATH)


def _save_tokens(data: dict) -> None:
    global _TOKEN_CACHE
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=1) + chr(10))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    try:
        _TOKEN_CACHE = (str(path), path.stat().st_mtime_ns, dict(data))
    except OSError:
        _TOKEN_CACHE = None


def _read_token_data() -> dict:
    global _TOKEN_CACHE
    path = token_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        _TOKEN_CACHE = None
        return {}
    cached = _TOKEN_CACHE
    if cached and cached[0] == str(path) and cached[1] == stamp:
        return dict(cached[2])
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    _TOKEN_CACHE = (str(path), stamp, dict(data))
    return dict(data)


def _local_credentials() -> bool:
    """Check credentials without refreshing or making any network request."""
    if os.environ.get('LUNE_API_KEY'):
        return True
    data = _read_token_data()
    return bool(data.get('access_token') or data.get('refresh_token'))


def urlopen(request, *, timeout):
    """Reject redirects so a token-bearing HTTP attempt cannot multiply or leak."""
    from network import _SafeRedirectHandler
    return build_opener(_SafeRedirectHandler(reject_all=True)).open(request, timeout=timeout)


@contextmanager
def _oauth_quota(budget=None):
    """Conservatively meter each OAuth token HTTP attempt as account usage."""
    from lune_quota import LuneQuotaStore, default_quota_path
    with LuneQuotaStore(default_quota_path()) as store:
        reservation = store.reserve('oauth/token', budget=budget)
        try:
            yield
        except BaseException:
            try:
                store.finish(reservation, 'unknown', budget=budget)
            except Exception:
                pass  # The pending reservation is still consumed.
            raise
        else:
            store.finish(reservation, 'response_received', budget=budget)


def _refresh(data: dict, *, budget=None) -> str:
    """Exchange the refresh token; Lune rotates it, so the new one is what we keep."""
    payload = {'grant_type': 'refresh_token', 'refresh_token': data.get('refresh_token') or '',
               'client_id': data.get('client_id') or '', 'resource': RESOURCE}
    request = Request(f'{ISSUER}/oauth/token', data=urlencode(payload).encode(), method='POST',
                      headers={'Content-Type': 'application/x-www-form-urlencoded',
                               'Accept': 'application/json', 'User-Agent': _UA})
    if budget is not None:
        budget.check_cancelled()
    with _oauth_quota(budget):
        if budget is not None:
            budget.check_cancelled()
        timeout = min(30, budget.remaining()) if budget is not None else 30
        with urlopen(request, timeout=timeout) as response:
            body = response.read(128_001)
        if len(body) > 128_000:
            raise ValueError('OAuth response exceeds limit')
        fresh = json.loads(body or b'{}')
        if not isinstance(fresh, dict):
            raise ValueError('OAuth response is not an object')
        access = fresh.get('access_token')
        if not isinstance(access, str) or not access:
            raise ValueError('OAuth response omitted access token')
        data.update({'access_token': access,
                     'refresh_token': fresh.get('refresh_token') or data.get('refresh_token') or '',
                     'expires_at': int(time.time()) + int(fresh.get('expires_in') or 3600)})
        # Rotated tokens must be persisted even when cancellation arrived during
        # the response; access_token checks cancellation after this returns.
        try:
            _save_tokens(data)
        except (OSError, ValueError):
            error = GuideError('Rotated OAuth credentials could not be saved; reauthorization may be required.')
            error.code = 'oauth_rotation_persist_failed'
            raise error from None
        return access


def access_token(*, force_refresh: bool = False, budget=None, rejected_token=None) -> str:
    """Refresh under a cooperative per-path process lock when required."""
    global _TOKEN_CACHE
    budget = budget if budget is not None else Budget(30)
    with budgeted_lock(_TOKEN_LOCK, budget):
        data = _read_token_data()
        current = str(data.get('access_token') or '')
        expires_at = int(data.get('expires_at') or 0)
        if current and not force_refresh and expires_at - 120 > int(time.time()):
            return current
        if data.get('refresh_token'):
            try:
                with _token_process_lock(budget):
                    _TOKEN_CACHE = None  # Force a disk read after acquiring the lock.
                    latest = _read_token_data()
                    latest_access = str(latest.get('access_token') or '')
                    baseline = rejected_token if force_refresh and rejected_token is not None else current
                    if latest_access and (latest_access != baseline or
                                          latest.get('refresh_token') != data.get('refresh_token')):
                        budget.check_cancelled()
                        budget.remaining()
                        return latest_access
                    if not force_refresh and latest_access and int(latest.get('expires_at') or 0) - 120 > int(time.time()):
                        budget.check_cancelled()
                        budget.remaining()
                        return latest_access
                    current = latest_access
                    refreshed = _refresh(latest, budget=budget) if latest.get('refresh_token') else ''
                    if budget is not None:
                        budget.check_cancelled()
                        budget.remaining()
                    if refreshed:
                        return refreshed
            except HTTPError as exc:
                exc.close()
                error = GuideError('OAuth refresh was rejected; reauthorization may be required.')
                error.code = 'oauth_refresh_failed'
                raise error from None
            except (OSError, ValueError, RecursionError):
                error = GuideError('OAuth refresh outcome is unknown; do not retry automatically.')
                error.code = 'oauth_refresh_unknown'
                raise error from None
        if budget is not None:
            budget.check_cancelled()
            budget.remaining()
        return current


def papers_from(payload: dict) -> list[dict]:
    """Find the paper list inside a tool result, whatever the wrapper key is."""
    for key in PAPER_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if isinstance(payload.get('results'), dict):
        nested = payload['results'].get('papers')
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    return []


def _authors(item: dict) -> list[str]:
    raw = item.get('authors') or item.get('author') or []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(',') if part.strip()]
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            out.append(entry.strip())
        elif isinstance(entry, dict):
            name = entry.get('name') or entry.get('full_name')
            if name:
                out.append(str(name).strip())
    return [name for name in out if name]


def _year(item: dict) -> int | None:
    value = _first(item, _YEAR_KEYS)
    try:
        year = int(str(value)[:4])
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


def _normalise_doi(value: Any) -> str:
    text = str(value or '').strip()
    text = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', text, flags=re.I)
    text = re.sub(r'^doi:\s*', '', text, flags=re.I).strip().rstrip('.,;')
    if not re.fullmatch(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', text, flags=re.I):
        return ''
    return text.lower()


def _normalise_arxiv(value: Any) -> str:
    text = str(value or '').strip()
    text = re.sub(r'^https?://arxiv\.org/(?:abs|pdf)/', '', text, flags=re.I)
    text = re.sub(r'^arxiv:\s*', '', text, flags=re.I).split('?', 1)[0]
    text = re.sub(r'\.pdf$', '', text, flags=re.I)
    if not re.fullmatch(r'(?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[a-z-]+)?/\d{7}(?:v\d+)?)',
                        text, flags=re.I):
        return ''
    return text


def to_paper(item: dict) -> Paper:
    """Map one record, retaining only a genuine abstract as grounding text."""
    native_id = _first(item, _ID_KEYS[:-2])
    title = str(_first(item, _TITLE_KEYS) or '').strip()
    if not native_id and not title and not item.get('doi') and not _first(item, _ARXIV_KEYS):
        raise GuideError('Lune returned a record with neither id nor title.')
    abstract = str(_first(item, _ABSTRACT_KEYS) or '').strip()
    snippet = str(_first(item, _SNIPPET_KEYS) or '').strip()
    doi = _normalise_doi(item.get('doi'))
    arxiv_id = _normalise_arxiv(_first(item, _ARXIV_KEYS))
    raw_id = native_id or arxiv_id or doi
    if raw_id:
        identifier = str(raw_id).strip()
    else:
        stable = '|'.join((title, str(_year(item) or ''), str(_first(item, _VENUE_KEYS) or '')))
        digest = hashlib.sha256(stable.encode('utf-8')).hexdigest()[:12]
        identifier = f'{title} [{digest}]'
    identifier = identifier[:180]
    venue = str(_first(item, _VENUE_KEYS) or '')
    year = _year(item)
    publication = {}
    if native_id and title and venue:
        # This verifies the corpus's bibliographic observation, not acceptance.
        # Decisions and oral/poster classes require their own original records.
        record = {'type': 'conference', 'status': 'unknown', 'presentation': 'unknown',
                  'venue_name': venue, 'venue_id': '', 'date': str(year) if year else '',
                  'date_precision': 'year' if year else 'unknown', 'doi': doi,
                  'verified': True, 'identity_verified': True, 'observed_at': utc_now(),
                  'evidence': [{'source': 'lune', 'url': RESOURCE, 'field': 'search_papers.conference',
                                'value': venue, 'paper_id': str(native_id)},
                               {'source': 'lune', 'url': RESOURCE, 'field': 'search_papers.year',
                                'value': year, 'paper_id': str(native_id)}],
                  'evidence_level': 'corpus_metadata', 'acceptance_verified': False}
        publication = {**record, 'records': [record]}
    return Paper(
        id=f'{PROVIDER}:{identifier}',
        title=title or identifier,
        abstract=abstract,
        snippet=snippet,
        url=str(_first(item, _URL_KEYS) or (f'https://doi.org/{doi}' if doi else
                                         f'https://arxiv.org/abs/{arxiv_id}' if arxiv_id else '')),
        year=year,
        authors=_authors(item),
        doi=doi,
        arxiv_id=arxiv_id,
        venue=venue, publication=publication,
        source=PROVIDER,
        sources=[PROVIDER],
        abstract_source='lune' if abstract else '',
        metadata_only=not abstract,
        fetched_at=utc_now(),
    )


class LuneProvider:
    """Owns the MCP client, the request budget and the on-disk ledger."""

    def __init__(self, *, config_path: Path | str = CONFIG_PATH,
                 max_requests: int | None = None,
                 ledger_path: Path | str = LEDGER_PATH,
                 client: McpClient | None = None,
                 transport: Callable | None = None):
        self.config_path = Path(config_path)
        self.ledger_path = Path(ledger_path)
        requested = None if max_requests is None else int(max_requests)
        if requested is not None and requested < 0:
            raise GuideError('--max-lune-requests must be non-negative')
        self._client = client
        self._transport = transport
        self._lock = threading.RLock()
        self.requests = 0
        self.by_tool: dict[str, int] = {}
        self.entries: list[dict] = []
        self.day = time.strftime('%Y-%m-%d')
        self._load_ledger()
        self.additional_budget = requested
        # explicit N = N more requests this run; default = whatever today has left
        self.max_requests = (self.requests + requested if requested is not None else
                             (self.requests + DAILY_ALLOWANCE - self.requests
                              if DAILY_ALLOWANCE is not None else None))

    # ------------------------------------------------------------------ ledger
    def _load_ledger(self) -> None:
        if not self.ledger_path.is_file():
            return
        try:
            data = json.loads(self.ledger_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        raw_entries = data.get('entries') or []
        self.entries = [entry for entry in raw_entries if isinstance(entry, dict)]
        recorded_day = str(data.get('day') or '')
        same_day = recorded_day == self.day
        self.requests = int(data.get('requests_used') or 0) if same_day else 0
        self.by_tool = dict(data.get('by_tool') or {}) if same_day else {}
        todays = [e for e in self.entries if str(e.get('at') or '').startswith(self.day)]
        if len(todays) > self.requests:
            self.requests = len(todays)
            self.by_tool = {}
            for entry in todays:
                tool = str(entry.get('tool') or '')
                self.by_tool[tool] = self.by_tool.get(tool, 0) + 1

    def _save_ledger(self) -> None:
        with self._lock:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {'provider': PROVIDER, 'day': self.day, 'requests_used': self.requests,
                       'daily_allowance': DAILY_ALLOWANCE,
                       'max_requests': self.max_requests, 'by_tool': dict(self.by_tool),
                       'updated_at': utc_now(), 'entries': self.entries[-2000:]}
            fd, tmp_name = tempfile.mkstemp(prefix=f'.{self.ledger_path.name}.',
                                            suffix='.tmp', dir=str(self.ledger_path.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, indent=1) + '\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self.ledger_path)
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass

    def remaining(self) -> int | None:
        with self._lock:
            return None if self.max_requests is None else max(0, self.max_requests - self.requests)

    def _charge(self, tool: str, detail: dict) -> int:
        with self._lock:
            today = time.strftime('%Y-%m-%d')
            if today != self.day:
                self.day, self.requests, self.by_tool = today, 0, {}
                self.max_requests = self.additional_budget if self.additional_budget is not None else DAILY_ALLOWANCE
            if self.max_requests is not None and self.requests + 1 > self.max_requests:
                allowance = DAILY_ALLOWANCE if DAILY_ALLOWANCE is not None else 'unknown'
                raise GuideError(f'lune_request_budget_exhausted: used {self.requests} of '
                                 f'{self.max_requests} (daily allowance {allowance})')
            self.requests += 1
            self.by_tool[tool] = self.by_tool.get(tool, 0) + 1
            attempt = max((int(entry.get('attempt') or 0) for entry in self.entries), default=0) + 1
            index = len(self.entries)
            self.entries.append({'attempt': attempt, 'at': utc_now(), 'tool': tool,
                                 'requests': 1, 'status': 'reserved', **detail})
            self._save_ledger()
            return index

    def _finish(self, index: int, **detail) -> None:
        with self._lock:
            if 0 <= index < len(self.entries):
                self.entries[index].update(detail)
            self._save_ledger()

    def _finish_safely(self, index: int, *, _preserve_exception=False, **detail) -> bool:
        # _finish mutates memory before writing; restore it if persistence fails.
        previous = dict(self.entries[index]) if 0 <= index < len(self.entries) else None
        try:
            self._finish(index, **detail)
            return True
        except BaseException as exc:
            if previous is not None:
                self.entries[index] = previous
            try:
                from execution_events import emit
                emit('source_audit_failed', source=PROVIDER, status='unknown',
                     reason='lune_ledger_unavailable')
            except BaseException:
                pass
            if not _preserve_exception and (isinstance(exc, (RunCancelled, BudgetExceeded))
                                            or not isinstance(exc, Exception)):
                if isinstance(exc, (RunCancelled, BudgetExceeded)):
                    exc.args = ('Lune ledger finalization interrupted.',)
                raise
            return False

    # ------------------------------------------------------------------ client
    def client(self, *, budget=None) -> McpClient:
        if self._client is not None:
            return self._client
        servers = load_servers(self.config_path,
                               on_missing='drop' if _local_credentials() else 'raise')
        if PROVIDER not in servers:
            raise GuideError(f'{self.config_path} has no "{PROVIDER}" server')
        config: ServerConfig = servers[PROVIDER]
        from mcp_lune_policy import validate_lune_auth_target
        validate_lune_auth_target(config.url, config.url, require_same_origin=True)
        token = access_token(budget=budget)
        has_oauth = bool(token or _read_token_data().get('refresh_token'))
        if has_oauth:
            headers = {k: v for k, v in config.headers.items()
                       if k.lower() != 'authorization'}
            if token:
                headers['Authorization'] = f'Bearer {token}'
            config = replace(config, headers=headers)
        # Only pass a transport when one was injected: passing None would override
        # McpClient's own default and fail at the first call.
        self._client = (McpClient(config, transport=self._transport) if self._transport
                        else McpClient(config))
        if has_oauth and hasattr(self._client, 'set_auth_provider'):
            def provide_auth(force, *, budget=None, rejected_token=None):
                return access_token(force_refresh=force, budget=budget,
                                    rejected_token=rejected_token if force else None)
            self._client.set_auth_provider(provide_auth, budgeted=True)
        return self._client

    def ready(self) -> bool:
        """Return local readiness only; this never opens the MCP or OAuth endpoint."""
        if self._client is not None:
            return True
        try:
            servers = load_servers(self.config_path, on_missing='drop')
        except (OSError, ValueError, McpError):
            return False
        if PROVIDER not in servers:
            return False
        config = servers[PROVIDER]
        try:
            from mcp_lune_policy import validate_lune_auth_target
            validate_lune_auth_target(config.url, config.url, require_same_origin=True)
        except GuideError:
            return False
        configured = any(key.lower() == 'authorization' and value for key, value in config.headers.items())
        return bool(_local_credentials() or configured)

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 10, filters: dict | None = None,
               budget=None, *, before_page=None, before_send=None, _client_override=None) -> list[Paper]:
        # Use the same lock order as source skills, including reentrant calls.
        from source_store import _MCP_LOCK
        with budgeted_lock(_MCP_LOCK, budget), budgeted_lock(self._lock, budget):
            client = _client_override if _client_override is not None else self.client(budget=budget)
            previous = getattr(client, 'budget', None)
            try:
                if hasattr(client, 'set_budget'):
                    client.set_budget(budget)
                return self._search(query, limit, filters, budget=budget,
                                    before_page=before_page, before_send=before_send, client=client)
            finally:
                if hasattr(client, 'set_budget'):
                    client.set_budget(previous)

    def _search(self, query: str, limit: int = 10, filters: dict | None = None,
                budget=None, *, before_page=None, before_send=None, client) -> list[Paper]:
        state = {'papers': [], 'pages_received': 0}
        try:
            return self._search_pages(query, limit, filters, budget=budget,
                                      before_page=before_page, before_send=before_send,
                                      state=state, client=client)
        except BaseException as exc:
            if state['pages_received']:
                exc.partial_papers = list(state['papers'])[:MAX_SEARCH_LIMIT]
            if state['pages_received'] and not state.get('complete'):
                # A page-level not_started/completed cannot describe the entire
                # interrupted search, including any later in-flight request.
                exc.remote_outcome = 'unknown'
                exc.retry_safe = False
                exc.partial_search = True
            raise

    def _search_pages(self, query: str, limit: int = 10, filters: dict | None = None,
                      budget=None, *, before_page=None, before_send=None, state, client) -> list[Paper]:
        filters = dict(filters or {})
        for old, new in (('year_from', 'year_min'), ('year_to', 'year_max')):
            if filters.get(old) is not None and filters.get(new) is None:
                filters[new] = filters[old]
        target = min(max(int(limit), 1), MAX_SEARCH_LIMIT)
        arguments_base: dict = {'query': str(query), 'limit': min(target, REMOTE_PAGE_SIZE), 'offset': 0}
        venues = filters.get('venues')
        if isinstance(venues, str):
            venues = [venues]
        elif isinstance(venues, (tuple, set)):
            venues = list(venues)
        if not venues and filters.get('venue'):
            venues = [filters['venue']]
        if venues:
            arguments_base['venues'] = [str(value).strip() for value in venues if str(value).strip()]
        for key in ('year_min', 'year_max'):
            if filters.get(key) is not None:
                arguments_base[key] = int(filters[key])
        if filters.get('offset') is not None:
            arguments_base['offset'] = max(0, int(filters['offset']))
        papers: list[Paper] = state['papers']
        seen: set[str] = set()
        skipped = 0
        visible_items = 0
        offset = arguments_base['offset']
        for page in range(MAX_SEARCH_PAGES):
            if before_page is not None:
                before_page()
            arguments = dict(arguments_base)
            arguments['offset'] = offset
            remaining = target - len(papers)
            arguments['limit'] = min(remaining, REMOTE_PAGE_SIZE)
            index = self._charge('search_papers', {'query_length': len(str(query)),
                                                    'limit': arguments['limit'], 'offset': offset})
            finalization_error = None
            try:
                try:
                    if before_send is not None and isinstance(client, McpClient):
                        result = client.call_tool('search_papers', arguments, _before_send=before_send)
                    else:
                        # Legacy host adapters lack a transport-level admission hook.
                        if before_send is not None:
                            before_send()
                        result = client.call_tool('search_papers', arguments)
                except (RunCancelled, BudgetExceeded) as exc:
                    retained = getattr(exc, 'mcp_result', None)
                    if not isinstance(client, McpClient) or not isinstance(retained, McpResult):
                        raise
                    finalization_error, result = exc, retained
                if not isinstance(result, dict) or result.get('isError') is True:
                    raise _InvalidSearchResponse('invalid search response')
                try:
                    payload = structured(result)
                except (TypeError, ValueError, AttributeError):
                    raise _InvalidSearchResponse('invalid search response') from None
                valid_list = any(isinstance(payload.get(key), list) for key in PAPER_KEYS)
                nested = payload.get('results')
                valid_list = valid_list or (isinstance(nested, dict) and
                                            isinstance(nested.get('papers'), list))
                if not valid_list:
                    raise _InvalidSearchResponse('invalid search response')
            except BaseException as exc:
                remote = getattr(exc, 'remote_outcome', None)
                if (not isinstance(remote, str) or remote not in REMOTE_OUTCOMES
                        or isinstance(exc, McpError) and exc.kind == 'timeout'):
                    remote = 'unknown'
                # The provider explicitly reporting an error is distinct from an
                # ambiguous transport or a received but unavailable response.
                ledger_status = remote if remote in ('error', 'response_received') else 'unknown'
                self._finish_safely(index, _preserve_exception=True,
                                    ok=False, status=ledger_status, returned=0)
                if isinstance(exc, McpError):
                    error = GuideError(f'lune {exc.kind} {exc.status or ""}'.strip())
                elif isinstance(exc, (TimeoutError, _InvalidSearchResponse)):
                    error = GuideError('lune timeout or invalid response; remote outcome unknown')
                    remote = 'unknown'
                else:
                    raise
                error.remote_outcome = remote
                error.retry_safe = False
                raise error from None
            state['pages_received'] += 1
            items = papers_from(payload)
            visible_items += len(items)
            for item in items:
                try:
                    paper = to_paper(item)
                except GuideError:
                    skipped += 1
                    continue
                year = paper.year
                low, high = filters.get('year_min'), filters.get('year_max')
                if low is not None and (year is None or year < int(low)):
                    continue
                if high is not None and (year is None or year > int(high)):
                    continue
                if paper.id not in seen:
                    seen.add(paper.id)
                    papers.append(paper)
                if len(papers) >= target:
                    break
            has_more = bool(payload.get('has_more'))
            pagination = payload.get('pagination')
            if isinstance(pagination, dict):
                has_more = has_more or bool(pagination.get('has_more'))
            state['complete'] = len(papers) >= target or not has_more
            try:
                audit_recorded = self._finish_safely(
                    index, _preserve_exception=finalization_error is not None,
                    status='completed', ok=True, returned=len(papers),
                    visible_items=visible_items, skipped=skipped,
                    no_identity=sum(not (p.url or p.doi or p.arxiv_id) for p in papers))
            except (RunCancelled, BudgetExceeded) as exc:
                exc.remote_outcome, exc.retry_safe = 'completed', False
                exc.audit_status = 'failed'
                raise
            if finalization_error is not None:
                finalization_error.remote_outcome, finalization_error.retry_safe = 'completed', False
                raise finalization_error
            quota_failed = isinstance(result, McpResult) and result.quota_finalization_failed
            if quota_failed or not audit_recorded:
                return LuneSearchResult(papers[:target],
                    stop_reason='quota_finalization_failed' if quota_failed else 'lune_ledger_unavailable',
                    audit_status='failed', partial=not state['complete'])
            if len(papers) >= target or not has_more:
                return papers[:target]
            if not items:
                return LuneSearchResult(papers[:target], stop_reason='empty_page')
            next_offset = payload.get('next_offset')
            if isinstance(pagination, dict):
                next_offset = pagination.get('next_offset', next_offset)
            if next_offset is None:
                offset += len(items)
            elif type(next_offset) is int and 0 <= next_offset <= 10000:
                offset = next_offset
            elif (isinstance(next_offset, str) and 0 < len(next_offset) <= 5
                  and next_offset.isascii() and next_offset.isdecimal()
                  and int(next_offset) <= 10000):
                offset = int(next_offset)
            else:
                return LuneSearchResult(papers[:target], stop_reason='invalid_offset')
            if offset <= arguments['offset']:
                return LuneSearchResult(papers[:target], stop_reason='nonadvancing_offset')
        return LuneSearchResult(papers[:target], stop_reason='max_pages')

    # ------------------------------------------------------------------- usage
    def usage(self) -> dict:
        try:
            ledger = str(self.ledger_path.relative_to(ROOT))
        except ValueError:
            ledger = str(self.ledger_path)
        with self._lock:
            return {'provider': PROVIDER, 'day': self.day, 'requests': self.requests,
                    'max_requests': self.max_requests, 'remaining': self.remaining(),
                    'daily_allowance': DAILY_ALLOWANCE,
                    'additional_budget': self.additional_budget,
                    'by_tool': dict(self.by_tool), 'ledger': ledger}


_INSTALLED: LuneProvider | None = None


def install(*, config_path: Path | str = CONFIG_PATH, max_requests: int | None = None,
            ledger_path: Path | str = LEDGER_PATH, client: McpClient | None = None,
            transport: Callable | None = None) -> LuneProvider:
    """Register Lune once and return the provider that owns its budget."""
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
    provider = LuneProvider(config_path=config_path, max_requests=max_requests,
                            ledger_path=ledger_path, client=client, transport=transport)
    spec = ProviderSpec(
        PROVIDER, 'Lune Research', 'scholarly', needs_key=True, rate_seconds=1.0,
        description='A/A* conference corpus via MCP; hybrid full-text search.',
        supports_venue=True, supports_year=True)
    mcp_plugin.register(PROVIDER, spec, provider.search)
    mcp_plugin.activate()
    _INSTALLED = provider
    return provider


def installed() -> LuneProvider | None:
    return _INSTALLED


def reset_for_tests() -> None:
    global _INSTALLED
    _INSTALLED = None
