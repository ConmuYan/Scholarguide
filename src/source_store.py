"""Durable, local-only source settings and observed provider health.

Listing settings is offline. Credentials never enter public state, attempts, or
error reports; model credentials are deliberately unrelated to this service.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
import json
import hmac
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

from common import Budget, BudgetExceeded, GuideError, REMOTE_OUTCOMES, RunCancelled, utc_now

CANONICAL_SOURCES = ('arxiv', 'openalex', 'semantic_scholar', 'dblp',
                     'openreview', 'acl_anthology', 'deepseek', 'lune')
_LABELS = dict(zip(CANONICAL_SOURCES, ('arXiv', 'OpenAlex', 'Semantic Scholar',
    'DBLP', 'OpenReview', 'ACL Anthology', 'DeepSeek Search', 'Lune Research')))
_KEY_FIELDS = {'openalex': 'openalex_key', 'semantic_scholar': 's2_key',
               'deepseek': 'deepseek_search_key', 'lune': 'lune_key'}
_KEY_ENV = {'openalex': ('OPENALEX_API_KEY',),
            'semantic_scholar': ('S2_API_KEY', 'SEMANTIC_SCHOLAR_API_KEY'),
            'deepseek': ('DEEPSEEK_SEARCH_KEY', 'DEEPSEEK_SEARCH_API_KEY'),
            'lune': ('LUNE_API_KEY', 'LUNE_TOKEN')}
_RATE = {'arxiv': 4.0, 'openalex': .2, 'semantic_scholar': 1.1, 'dblp': 1.,
         'openreview': .6, 'acl_anthology': .6, 'deepseek': .2, 'lune': 1.}
_MAX_INFLIGHT = {'arxiv': 1, 'openalex': 2, 'semantic_scholar': 1, 'dblp': 1,
                 'openreview': 2, 'acl_anthology': 2, 'deepseek': 2, 'lune': 1}
_DB_BUDGET = ContextVar('source_store_budget', default=None)
_SLOT_CONTEXT = ContextVar('source_store_slot_context', default=())
_MCP_LOCK = threading.RLock()
# Process-wide, not store-instance-wide: a failed SQLite release remains
# recoverable only after the protected operation has actually finished.
_SLOT_LOCK = threading.Lock()
_SLOT_PID = os.getpid()
_SLOT_INSTANCE = uuid.uuid4().hex
_SLOT_STATES = {}
_SLOT_DATABASES = {}


def _reset_slot_registry_after_fork():
    global _SLOT_LOCK, _SLOT_PID, _SLOT_INSTANCE, _SLOT_STATES, _SLOT_DATABASES
    _SLOT_LOCK = threading.Lock()
    _SLOT_PID = os.getpid()
    _SLOT_INSTANCE = uuid.uuid4().hex
    _SLOT_STATES = {}
    _SLOT_DATABASES = {}


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_slot_registry_after_fork)
_TOOL_ALIASES = {'search': 'search_papers', 'related': 'search_related_papers',
                 'fulltext': 'get_paper_fulltext', 'citations': 'get_paper_citations',
                 'find_related_papers': 'search_related_papers', 'get_citations': 'get_paper_citations'}
_TOOL_SCHEMAS = {
    'search_papers': {'query': 'text', 'limit': 'limit', 'offset': 'offset',
                      'venues': 'texts', 'year_from': 'year', 'year_to': 'year'},
    'search_related_papers': {'paper_id': 'text', 'limit': 'limit'},
    'get_paper_fulltext': {'paper_id': 'text'},
    'get_paper_citations': {'paper_id': 'text', 'limit': 'limit', 'offset': 'offset'},
    'list_conferences': {},
}
_REQUIRED = {'search_papers': ('query',), 'search_related_papers': ('paper_id',),
             'get_paper_fulltext': ('paper_id',), 'get_paper_citations': ('paper_id',),
             'list_conferences': ()}


def _error(code: str, message: str) -> GuideError:
    exc = GuideError(message)
    exc.code = code
    return exc


def _name(value) -> str:
    if not isinstance(value, str) or value not in CANONICAL_SOURCES:
        raise _error('unknown_source', 'Unknown source; use a canonical provider name.')
    return value


def _timestamp(value) -> float:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            try:
                value = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                raise _error('invalid_cooldown', 'Cooldown must be an epoch or ISO timestamp.') from None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise _error('invalid_cooldown', 'Cooldown must be a finite, nonnegative epoch timestamp.')
    return float(value)


_DEEPSEEK_DEFAULT = 'https://api.deepseek.com/v1/tools/search'
_DEEPSEEK_BINDING = 'sg-deepseek-binding-v1:'


def _origin(endpoint):
    try:
        url = urlsplit(endpoint)
        if url.scheme.lower() not in ('https', 'http') or not url.hostname or url.username or url.password:
            return None
        return (url.scheme.lower(), url.hostname.lower(), url.port or (443 if url.scheme.lower() == 'https' else 80))
    except (ValueError, TypeError):
        return None


def _deepseek_unwrap(value):
    if isinstance(value, str) and value.startswith(_DEEPSEEK_BINDING):
        try:
            data = json.loads(value[len(_DEEPSEEK_BINDING):])
            if isinstance(data, dict) and isinstance(data.get('key'), str) and isinstance(data.get('endpoint'), str):
                return data['key'], data['endpoint']
        except (ValueError, TypeError):
            pass
        return '', None
    return value, None


class RetainedSourceResult(dict):
    """Host-redacted response retained when local accounting is interrupted."""


class SourceStore:
    """SQLite service shared safely by request threads and other processes."""

    def __init__(self, path=None, *, quota_path=None):
        selected = path if path is not None else os.environ.get('SG_SOURCE_STORE')
        self.quota_path = Path(quota_path).expanduser() if quota_path is not None else None
        self.path = Path(selected or Path.home() / '.scholarguide' / 'sources.sqlite3').expanduser()
        memory = str(selected) == ':memory:'
        if not memory:
            existed = self.path.parent.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not existed:
                self.path.parent.chmod(0o700)
            # Create with private permissions before SQLite can write credentials.
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        self._owner_pid, self._owner_instance = _SLOT_PID, _SLOT_INSTANCE
        self._slot_database = object() if memory else str(self.path.resolve())
        self._lock = threading.RLock()
        self._db_depth = 0
        self._db_rollback_only = False
        self._db_unusable = False
        self._secrets = set()
        self._connection = sqlite3.connect(':memory:' if memory else str(self.path),
                                            timeout=10, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._db() as db:
            db.executescript('''
                PRAGMA busy_timeout=10000;
                PRAGMA secure_delete=ON;
                CREATE TABLE IF NOT EXISTS sources (
                    name TEXT PRIMARY KEY, enabled INTEGER, priority INTEGER,
                    api_key TEXT, endpoint TEXT, health TEXT, quota TEXT,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    next_request_at REAL NOT NULL DEFAULT 0,
                    rate_version INTEGER NOT NULL DEFAULT 0,
                    cooldown_code TEXT NOT NULL DEFAULT '', updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    at TEXT NOT NULL, status TEXT NOT NULL, code TEXT NOT NULL,
                    error TEXT NOT NULL, returned INTEGER NOT NULL,
                    elapsed_ms INTEGER NOT NULL, cost_cny REAL NOT NULL,
                    cached INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS attempts_source ON attempts(name, id);
                CREATE TABLE IF NOT EXISTS source_inflight (
                    token TEXT PRIMARY KEY, name TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS source_inflight_name ON source_inflight(name, expires_at);
            ''')
            # Serialize inspection and ALTER so simultaneous initializers can
            # migrate an existing database without racing duplicate columns.
            db.execute('BEGIN IMMEDIATE')
            source_columns = {row['name'] for row in db.execute('PRAGMA table_info(sources)')}
            if 'rate_version' not in source_columns:
                db.execute('ALTER TABLE sources ADD COLUMN rate_version INTEGER NOT NULL DEFAULT 0')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(source_inflight)')}
            if 'owner_pid' not in columns:
                db.execute('ALTER TABLE source_inflight ADD COLUMN owner_pid INTEGER')
            if 'owner_instance' not in columns:
                db.execute('ALTER TABLE source_inflight ADD COLUMN owner_instance TEXT')
            db.executemany('INSERT OR IGNORE INTO sources(name) VALUES (?)',
                           [(name,) for name in CANONICAL_SOURCES])

    def _check_process(self):
        if (os.getpid() != self._owner_pid or _SLOT_PID != self._owner_pid
                or _SLOT_INSTANCE != self._owner_instance):
            raise _error('source_store_forked', 'Create a new source store in the child process.')

    @contextmanager
    def budget_scope(self, budget):
        token = _DB_BUDGET.set(budget)
        try:
            yield
        finally:
            _DB_BUDGET.reset(token)

    @contextmanager
    def _locked(self, budget=None):
        budget = budget if budget is not None else _DB_BUDGET.get()
        if budget is None:
            with self._lock:
                yield
            return
        while True:
            budget.check_cancelled()
            if self._lock.acquire(timeout=min(.05, budget.remaining())):
                break
        try:
            budget.check_cancelled()
            budget.remaining()
            yield
        finally:
            self._lock.release()

    @contextmanager
    def _transaction(self):
        connection = self._connection
        connection.__enter__()
        try:
            yield
        except BaseException:
            original = sys.exc_info()
            try:
                connection.__exit__(*original)
            except BaseException:
                self._db_unusable = True
            raise
        else:
            try:
                connection.__exit__(None, None, None)
            except BaseException:
                self._db_unusable = True
                raise

    @contextmanager
    def _db(self, budget=None):
        self._check_process()  # Before touching an inherited lock or connection.
        budget = budget if budget is not None else _DB_BUDGET.get()
        with self._locked(budget):
            if self._db_unusable:
                raise _error('source_store_unusable', 'Source store connection is unusable; create a new store.')
            if self._db_depth:
                if self._db_rollback_only:
                    raise _error('source_transaction_rollback_only', 'Source transaction must roll back.')
                if budget is not None:
                    budget.check_cancelled()
                    budget.remaining()
                # A savepoint must not become the outermost transaction: its
                # RELEASE would otherwise commit the caller's work.
                if not self._connection.in_transaction:
                    self._connection.execute('BEGIN')
                savepoint = f'sg_nested_{self._db_depth}'
                self._connection.execute(f'SAVEPOINT {savepoint}')
                self._db_depth += 1
                try:
                    yield self._connection
                except BaseException:
                    try:
                        self._connection.execute(f'ROLLBACK TO {savepoint}')
                        self._connection.execute(f'RELEASE {savepoint}')
                    except BaseException:
                        self._db_rollback_only = True
                    raise
                else:
                    try:
                        self._connection.execute(f'RELEASE {savepoint}')
                    except BaseException:
                        self._db_rollback_only = True
                        raise
                finally:
                    self._db_depth -= 1
                return
            self._db_depth = 1
            self._db_rollback_only = False
            outcome = 'not_started'
            try:
                if budget is None:
                    try:
                        with self._transaction():
                            yield self._connection
                            if self._db_rollback_only:
                                raise _error('source_transaction_rollback_only', 'Source transaction must roll back.')
                    except BaseException:
                        if self._connection.in_transaction:
                            self._db_unusable = True
                        raise
                    return
                try:
                    # Contextmanager bodies cannot be replayed after a busy error.
                    while True:
                        budget.check_cancelled()
                        wait_ms = max(1, min(50, int(budget.remaining() * 1000)))
                        self._connection.execute(f'PRAGMA busy_timeout={wait_ms}')
                        try:
                            self._connection.execute('BEGIN IMMEDIATE')
                            break
                        except sqlite3.OperationalError as exc:
                            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                                raise
                    outcome = 'unknown'
                    try:
                        with self._transaction():
                            budget.check_cancelled()
                            budget.remaining()
                            yield self._connection
                            if self._db_rollback_only:
                                raise _error('source_transaction_rollback_only', 'Source transaction must roll back.')
                    except BaseException:
                        if self._connection.in_transaction:
                            self._db_unusable = True
                        else:
                            outcome = 'rolled_back_or_unknown'
                        raise
                    outcome = 'committed'
                finally:
                    active_error = sys.exc_info()[0] is not None
                    try:
                        self._connection.execute('PRAGMA busy_timeout=10000')
                    except BaseException:
                        self._db_unusable = True
                        if not active_error:
                            error = _error('source_store_cleanup_failed',
                                           'Source store cleanup failed; transaction outcome is available on this error; do not retry blindly.')
                            error.transaction_outcome = outcome
                            raise error from None
            finally:
                self._db_depth = 0

    def close(self):
        self._check_process()
        with self._lock:
            self._connection.close()

    def _row(self, name):
        with self._db() as db:
            return dict(db.execute('SELECT * FROM sources WHERE name=?', (_name(name),)).fetchone())

    def _key(self, name, row=None):
        row = self._row(name) if row is None else row
        # NULL means never provided; an explicit clear is the empty string.
        if row['api_key'] is not None:
            return _deepseek_unwrap(row['api_key'])[0] if name == 'deepseek' else row['api_key']
        key = next((os.environ[key] for key in _KEY_ENV.get(name, ()) if os.environ.get(key)), '')
        return key

    def deepseek_key_authorized(self, key, search_endpoint, target, *, row=None):
        row = self._row('deepseek') if row is None else row
        search_origin, target_origin = _origin(search_endpoint), _origin(target)
        effective = self._key('deepseek', row)
        if (not isinstance(key, str) or not key or not isinstance(effective, str) or not effective
                or not hmac.compare_digest(key.encode('utf-8'), effective.encode('utf-8'))
                or not search_origin or not target_origin or target_origin[0] != 'https'
                or search_origin != _origin(row['endpoint'] or _DEEPSEEK_DEFAULT)):
            return False
        saved, bound = _deepseek_unwrap(row['api_key'])
        if bound is not None and saved and hmac.compare_digest(key.encode('utf-8'), saved.encode('utf-8')):
            return (search_origin == _origin(bound) and target_origin == _origin(bound))
        return search_origin == _origin(_DEEPSEEK_DEFAULT) and target_origin == _origin(_DEEPSEEK_DEFAULT)

    @staticmethod
    def _oauth_configured():
        # No import, token reads, refresh, network calls, or token-path disclosure.
        token_file = os.environ.get('SG_MCP_TOKEN_FILE')
        return Path(token_file or Path.home() / '.scholarguide' / 'lune-oauth.json').expanduser().is_file()

    def _configured(self, name, row=None):
        row = self._row(name) if row is None else row
        key = self._key(name, row)
        auth = bool(key)
        if name == 'deepseek':
            endpoint = row['endpoint'] or _DEEPSEEK_DEFAULT
            return bool(auth and self.deepseek_key_authorized(key, endpoint, endpoint, row=row)), auth
        if name == 'lune':
            auth = auth or self._oauth_configured()
        return (auth if name == 'lune' else True), auth

    @staticmethod
    def _capabilities(name):
        scholarly = name not in ('arxiv', 'deepseek')
        modes = {'venue': 'native' if scholarly else 'unsupported',
                 'year': 'native' if name in ('openalex', 'semantic_scholar') else 'client',
                 'publication_type': 'enrich' if scholarly else 'unsupported',
                 'accepted_only': 'enrich' if scholarly else 'unsupported',
                 'presentation': 'native' if name == 'openreview' else 'unsupported'}
        return {'filters': modes, 'abstract': name not in ('dblp', 'deepseek'),
                'publication': scholarly, 'presentation': name == 'openreview',
                'fulltext': name in ('lune', 'deepseek'),
                'related': name == 'lune', 'citations': name == 'lune',
                'skills': name == 'lune'}

    def _public(self, row):
        name = row['name']
        configured, auth = self._configured(name, row)
        enabled = bool(row['enabled']) if row['enabled'] is not None else (
            name not in ('deepseek', 'lune') or configured)
        priority = row['priority']
        if priority is None:
            priority = 0 if name == 'lune' else 10 * (CANONICAL_SOURCES.index(name) + 1)
        return {'name': name, 'label': _LABELS[name], 'enabled': enabled,
                'priority': priority, 'configured': configured, 'auth_configured': auth,
                'requires_key': name in ('deepseek', 'lune'),
                'key_recommended': name in ('openalex', 'semantic_scholar'),
                'setup_hint': ('配置独立来源 key 后启用' if name == 'deepseek' else
                               '推荐配置 API key，避免匿名限流' if name in ('semantic_scholar', 'openalex') else ''),
                'capabilities': self._capabilities(name),
                'concurrency': self.concurrency(name),
                'endpoint': row['endpoint'] or ({'lune':'https://mcp.luneresearch.com', 'deepseek':'https://api.deepseek.com/v1/tools/search'}.get(name, '')),
                'health': json.loads(row['health']) if row['health'] else None,
                'quota': json.loads(row['quota']) if row['quota'] else None,
                'cooldown_until': row['cooldown_until'], 'cooldown_code': row['cooldown_code']}

    def public_state(self):
        with self._db() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM sources')]
        sources = [self._public(row) for row in rows]
        sources.sort(key=lambda item: (item['priority'], CANONICAL_SOURCES.index(item['name'])))
        now = time.time()
        routing = [item['name'] for item in sources
                   if item['enabled'] and item['configured'] and item['cooldown_until'] <= now]
        cooling_down = [item['name'] for item in sources
                        if item['enabled'] and item['configured'] and item['cooldown_until'] > now]
        return {'sources': sources, 'default_source': 'multi', 'routing': routing,
                'cooling_down': cooling_down}

    def upsert(self, data):
        if not isinstance(data, dict):
            raise _error('invalid_source', 'Source settings must be an object.')
        name = _name(data.get('name'))
        unknown = set(data) - {'name', 'enabled', 'priority', 'api_key', 'endpoint', 'clear_key'}
        if unknown:
            raise _error('invalid_source', 'Unsupported source settings: ' + ', '.join(sorted(unknown)))
        updates = {}
        if 'enabled' in data:
            if type(data['enabled']) is not bool:
                raise _error('invalid_enabled', 'enabled must be a boolean.')
            updates['enabled'] = int(data['enabled'])
        if 'priority' in data:
            if type(data['priority']) is not int or not 0 <= data['priority'] <= 100:
                raise _error('invalid_priority', 'priority must be an integer from 0 to 100.')
            updates['priority'] = data['priority']
        if 'clear_key' in data and type(data['clear_key']) is not bool:
            raise _error('invalid_api_key', 'clear_key must be a boolean.')
        if 'api_key' in data:
            key = data['api_key']
            if not isinstance(key, str) or len(key) > 16384 or any(ord(ch) < 32 or ord(ch) == 127 for ch in key):
                raise _error('invalid_api_key', 'api_key must be text without control characters.')
            if key.strip():
                updates['api_key'] = key.strip()
        if data.get('clear_key'):
            updates['api_key'] = ''
        if 'endpoint' in data:
            endpoint = data['endpoint']
            try:
                url = urlsplit(endpoint) if isinstance(endpoint, str) else None
                valid = (url and len(endpoint) <= 2048 and url.scheme in ('https', 'http')
                         and url.hostname and not url.username and not url.password
                         and not url.query and not url.fragment and url.port != 0
                         and not any(ch.isspace() or ord(ch) < 32 for ch in endpoint))
            except ValueError:
                valid = False
            if not valid:
                raise _error('invalid_endpoint', 'endpoint must be HTTP(S) without credentials, query, or fragment.')
            if name not in ('deepseek', 'lune'):
                raise _error('unsupported_endpoint', 'This source uses its canonical public API endpoint.')
            updates['endpoint'] = endpoint.rstrip('/')
        if updates:
            updates['updated_at'] = utc_now()
            with self._db() as db:
                if name == 'deepseek':
                    db.execute('UPDATE sources SET endpoint=endpoint WHERE name=?', (name,))
                    current = db.execute('SELECT endpoint,api_key FROM sources WHERE name=?', (name,)).fetchone()
                    old_endpoint = current['endpoint'] or _DEEPSEEK_DEFAULT
                    new_endpoint = updates.get('endpoint', old_endpoint)
                    if (current['api_key'] and _origin(new_endpoint) != _origin(old_endpoint)
                            and 'api_key' not in updates):
                        raise _error('credential_confirmation_required',
                                     'Re-enter or clear the DeepSeek key when changing its endpoint.')
                    if updates.get('api_key'):
                        updates['api_key'] = _DEEPSEEK_BINDING + json.dumps(
                            {'key': updates['api_key'], 'endpoint': new_endpoint}, separators=(',', ':'))
                if name == 'lune' and 'endpoint' in updates:
                    # Lock before reading to bind endpoint changes to the same
                    # credential decision across application processes.
                    db.execute('UPDATE sources SET endpoint=endpoint WHERE name=?', (name,))
                    current = db.execute('SELECT endpoint,api_key FROM sources WHERE name=?', (name,)).fetchone()
                    if (current['api_key'] and updates['endpoint'] != current['endpoint']
                            and 'api_key' not in updates):
                        raise _error('credential_confirmation_required',
                                     'Re-enter or clear the Lune key when changing its endpoint.')
                db.execute('UPDATE sources SET ' + ','.join(key + '=?' for key in updates)
                           + ' WHERE name=?', [*updates.values(), name])
        return self._public(self._row(name))

    def configure(self, config):
        if not is_dataclass(config):
            raise _error('invalid_config', 'Source configuration requires a dataclass Config.')
        available = {field.name for field in fields(config)}
        self._check_process()
        with self._locked():
            self._secrets.update(str(getattr(config, field, '') or '')
                                 for field in ('api_key', *_KEY_FIELDS.values()))
        updates = {}
        for name, field in _KEY_FIELDS.items():
            row = self._row(name)
            key = self._key(name, row)
            if field in available and (row['api_key'] is not None or key):
                updates[field] = key
            endpoint_fields = {'deepseek': ('deepseek_search_url', 'deepseek_search_endpoint'),
                               'lune': ('lune_endpoint',)}.get(name, ())
            for endpoint_field in endpoint_fields:
                if endpoint_field in available and row['endpoint']:
                    updates[endpoint_field] = row['endpoint']
        if 'source_store' in available:
            updates['source_store'] = self
        result = replace(config, **updates)
        # Compatibility while the owning worker adds the optional Config field.
        if 'source_store' not in available:
            result.source_store = self
        return result

    def enabled_sources(self, configured=None):
        """Return enabled, authenticated sources in saved priority order.

        An optional iterable is an allow-list (normally the installed provider
        registry). Explicit source selection remains the caller's responsibility.
        Cooldown is exposed separately so a scheduler can wait rather than hide it.
        """
        if isinstance(configured, str):
            configured = configured.replace('+', ',').split(',')
        allowed = set(configured) if configured is not None else set(CANONICAL_SOURCES)
        return [item['name'] for item in self.public_state()['sources']
                if item['name'] in allowed and item['enabled'] and item['configured']]

    def _redact(self, value, config=None):
        self._check_process()
        with self._locked():
            secrets = list(self._secrets)
        secrets.extend(self._key(name) for name in CANONICAL_SOURCES)
        if config is not None:
            secrets.extend(str(getattr(config, field, '') or '')
                           for field in ('api_key', *_KEY_FIELDS.values()))
        # OAuth contents are only inspected on active requests, never listings.
        token_file = Path(os.environ.get('SG_MCP_TOKEN_FILE') or
                          Path.home() / '.scholarguide' / 'lune-oauth.json').expanduser()
        try:
            tokens = json.loads(token_file.read_text(encoding='utf-8'))
            secrets.extend(str(tokens.get(key) or '') for key in ('access_token', 'refresh_token', 'client_secret'))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        def scrub(item):
            if isinstance(item, dict):
                return {str(key): scrub(val) for key, val in item.items()
                        if not any(token in str(key).lower() for token in
                                   ('api_key', 'access_token', 'refresh_token', 'authorization', 'password', 'client_secret'))}
            if isinstance(item, (list, tuple)):
                return [scrub(part) for part in item]
            if isinstance(item, str):
                for secret in sorted(set(secrets), key=len, reverse=True):
                    if secret:
                        item = item.replace(secret, '[redacted]')
                item = re.sub(r'(?i)(bearer\s+)[^\s,;"\']+', r'\1[redacted]', item)
                item = re.sub(r'(?i)((?:api[_-]?key|access_token|refresh_token|client_secret)=)[^&\s]+',
                              r'\1[redacted]', item)
                return item
            return item
        return scrub(value)

    def credentials(self, name):
        """Internal adapter configuration; never serialize this into API output.

        Source adapters may use this instead of borrowing model credentials.
        OAuth refresh remains the MCP adapter's responsibility.
        """
        name = _name(name)
        row = self._row(name)
        return {'api_key': self._key(name, row), 'endpoint': row['endpoint']}

    def record_run(self, run):
        data = run if isinstance(run, dict) else run.as_dict()
        name = _name(data.get('name'))
        status = str(data.get('status') or 'error')[:40]
        code = str(data.get('code') or '')[:120]
        error = str(self._redact(str(data.get('error') or '')))[:500]
        if status == 'error' and not code:
            from source import classify_source_error
            code = classify_source_error(error)
        code = str(self._redact(code))
        elapsed = max(0, int(data.get('elapsed_ms') or 0))
        returned = max(0, int(data.get('returned') or 0))
        # Legacy schema requires a non-null REAL. A negative sentinel means
        # unknown, never a negative charge or a verified zero-cost request.
        raw_cost = data.get('cost_cny')
        try:
            cost = float(raw_cost) if raw_cost is not None and not isinstance(raw_cost, bool) else -1.
        except (TypeError, ValueError, OverflowError):
            cost = -1.
        if (not math.isfinite(cost) or cost < 0
                or name == 'lune' and status in ('ok', 'empty', 'error', 'partial', 'unknown')
                or name == 'deepseek' and cost == 0 and status in ('ok', 'empty', 'error', 'partial')):
            cost = -1.
        cached = status in ('cache', 'cached')
        now = utc_now()
        now_seconds = time.time()
        with self._db() as db:
            db.execute('UPDATE sources SET updated_at=updated_at WHERE name=?', (name,))
            row = dict(db.execute('SELECT * FROM sources WHERE name=?', (name,)).fetchone())
            previous = json.loads(row['health']) if row['health'] else {}
            failures = int(previous.get('failures') or 0)
            # Cache hits/skips are audit entries, not fresh connectivity evidence.
            health = dict(previous)
            # A late success has no request-order proof and must not overwrite
            # a newer active failure/cooldown. Manual probing can clear it first.
            preserve_failure = status in ('ok', 'empty') and row['cooldown_until'] > now_seconds
            if status not in ('cache', 'cached', 'skipped') and not preserve_failure:
                health = {'name': name, 'status': status, 'last_status': status,
                          'code': code, 'last_code': code, 'error': error, 'last_error': error,
                          'elapsed_ms': elapsed, 'returned': returned, 'last_at': now,
                          'failures': failures + 1 if status == 'error' else 0}
            db.execute('INSERT INTO attempts(name,at,status,code,error,returned,elapsed_ms,cost_cny,cached) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       (name, now, status, code, error, returned, elapsed, cost, int(cached)))
            db.execute('UPDATE sources SET health=?, updated_at=? WHERE name=?',
                       (json.dumps(health) if health else None, now, name))
            quota = data.get('quota')
            if isinstance(quota, dict):
                # Persist observed counters only, never arbitrary upstream payloads.
                safe_quota = {key: value for key, value in quota.items()
                              if key in ('limit', 'remaining', 'used', 'requests', 'daily_allowance',
                                         'max_requests', 'reset_at', 'day')
                              and (value is None or (type(value) in (int, float) and math.isfinite(value))
                                   or (key in ('reset_at', 'day') and isinstance(value, str)
                                       and re.fullmatch(r'[0-9T:Z+. -]{1,40}', value)))}
                db.execute('UPDATE sources SET quota=? WHERE name=?', (json.dumps(safe_quota), name))
            if status == 'error' and code in ('rate_limited', 'timeout', 'dns_error', 'connection_error', 'transport', 'auth', 'quota_exhausted', 'lune_request_budget_exhausted', 'bot_challenge'):
                if code == 'bot_challenge':
                    seconds = 6 * 3600
                elif code == 'rate_limited':
                    seconds = min(3600, 60 * (2 ** min(failures, 5)))
                elif code in ('auth', 'quota_exhausted', 'lune_request_budget_exhausted'):
                    seconds = 3600
                else:
                    seconds = 30
                deadline = now_seconds + seconds
                db.execute('UPDATE sources SET cooldown_code=CASE WHEN ?>=cooldown_until THEN ? '
                           'ELSE cooldown_code END, cooldown_until=MAX(cooldown_until,?) WHERE name=?',
                           (deadline, code, deadline, name))
            elif status in ('ok', 'empty'):
                db.execute("UPDATE sources SET cooldown_until=0,cooldown_code='' "
                           "WHERE name=? AND cooldown_until<=?", (name, now_seconds))
        return self.health_snapshot().get(name)

    def cooldown(self, name, until=None, errorcode='', **kwargs):
        name = _name(name)
        if until is None:
            return self._row(name)['cooldown_until']
        code = kwargs.get('error_code', errorcode)
        if not isinstance(code, str) or len(code) > 120:
            raise _error('invalid_error_code', 'Cooldown error code must be short text.')
        code = self._redact(code)
        deadline = _timestamp(until)
        with self._db() as db:
            if deadline == 0:
                # Explicit administrative clear, used by a user-requested probe.
                db.execute("UPDATE sources SET cooldown_until=0,cooldown_code='',updated_at=? WHERE name=?",
                           (utc_now(), name))
            else:
                db.execute('UPDATE sources SET cooldown_code=CASE WHEN ?>=cooldown_until THEN ? '
                           'ELSE cooldown_code END, cooldown_until=MAX(cooldown_until,?), updated_at=? WHERE name=?',
                           (deadline, code, deadline, utc_now(), name))
        return self._row(name)['cooldown_until']

    def clear_expired_cooldown(self, name):
        """Clear only expired cooldowns; a concurrent newer failure wins."""
        name = _name(name)
        with self._db() as db:
            db.execute("UPDATE sources SET cooldown_until=0,cooldown_code='',updated_at=? "
                       "WHERE name=? AND cooldown_until<=?", (utc_now(), name, time.time()))

    def health_snapshot(self):
        with self._db() as db:
            rows = [dict(row) for row in db.execute('SELECT name,health,cooldown_until,cooldown_code,next_request_at FROM sources')]
        return {row['name']: {**(json.loads(row['health']) if row['health'] else
                                {'name': row['name'], 'status': 'unknown', 'last_status': 'unknown'}),
                             'cooldown_until': row['cooldown_until'],
                             'cooldown_code': row['cooldown_code'],
                             'next_request_at': row['next_request_at']} for row in rows}

    def reserve(self, name, interval=None, now=None, *, budget=None, _ticket=False):
        """Atomically reserve a rate slot; return delay seconds (wall-clock based).

        The scheduler must wait with Budget.wait(delay) before issuing a request.
        This method never sleeps or performs network I/O.
        """
        name = _name(name)
        interval = _RATE[name] if interval is None else interval
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 0:
            raise _error('invalid_rate', 'Rate interval must be finite and nonnegative.')
        fixed_now = None if now is None else _timestamp(now)
        while True:
            may_have_updated = False
            try:
                with self._db(budget=budget) as db:
                    # Acquire a write lock before reading, including across processes.
                    db.execute('UPDATE sources SET next_request_at=next_request_at WHERE name=?', (name,))
                    current = time.time() if fixed_now is None else fixed_now
                    if budget is not None:
                        budget.check_cancelled()
                        budget.remaining()
                    row = db.execute('SELECT cooldown_until,next_request_at,rate_version FROM sources WHERE name=?', (name,)).fetchone()
                    slot = max(current, row['cooldown_until'], row['next_request_at'])
                    if budget is not None and slot - current >= budget.remaining():
                        raise BudgetExceeded('Not enough task time remaining for source rate wait.')
                    may_have_updated = True
                    db.execute('UPDATE sources SET next_request_at=?, rate_version=rate_version+1 WHERE name=?',
                               (slot + interval, name))
                delay = max(0., slot - current)
                return (delay, (name, row['next_request_at'], slot + interval,
                                row['rate_version'] + 1)) if _ticket else delay
            except sqlite3.OperationalError as exc:
                if may_have_updated:
                    # Commit may have succeeded; retrying would allocate an
                    # additional slot without returning a usable ticket.
                    raise _error('rate_reservation_unknown',
                                 'Source rate reservation outcome is unknown; retry explicitly.') from None
                if budget is None or ('locked' not in str(exc).lower() and 'busy' not in str(exc).lower()):
                    raise
                budget.check_cancelled()
                budget.remaining()

    def wait_for_rate(self, name, budget):
        """Reserve and wait; undo only a still-latest slot cancelled before dispatch."""
        reservation = self.reserve(name, budget=budget, _ticket=True)
        if not isinstance(reservation, tuple):
            # Compatibility with host-supplied/test reservation implementations.
            return budget.wait(reservation)
        delay, (name, previous, issued, version) = reservation
        try:
            budget.wait(delay)
        except BaseException:
            try:
                with self._db(budget=Budget(.25)) as db:
                    db.execute('UPDATE sources SET next_request_at=?, rate_version=rate_version+1 '
                               'WHERE name=? AND next_request_at=? AND rate_version=?',
                               (previous, name, issued, version))
            except Exception:
                # Another reservation or an unavailable database is safely
                # conservative; never mask cancellation or an unknown outcome.
                pass
            raise

    def concurrency(self, name):
        name = _name(name)
        with self._db() as db:
            active = db.execute('SELECT COUNT(*) FROM source_inflight WHERE name=?',
                                (name,)).fetchone()[0]
        return {'limit': _MAX_INFLIGHT[name], 'in_flight': active}

    def legacy_inflight_status(self, name):
        name = _name(name)
        with self._db() as db:
            count = db.execute('SELECT COUNT(*) FROM source_inflight WHERE name=? '
                               'AND owner_pid IS NULL AND owner_instance IS NULL', (name,)).fetchone()[0]
        return {'name': name, 'legacy': count, 'remote_outcome_unknown': True}

    def reconcile_legacy_inflight(self, name, data):
        """Remove only ownerless pre-upgrade slots after explicit operator confirmation."""
        name = _name(name)
        if (not isinstance(data, dict) or set(data) != {'acknowledged', 'legacy_clients_stopped'}
                or data['acknowledged'] is not True or data['legacy_clients_stopped'] is not True):
            raise _error('confirmation_required', 'Confirm all legacy clients are stopped and acknowledge remote outcomes may remain unknown.')
        with self._db() as db:
            db.execute('UPDATE sources SET updated_at=updated_at WHERE name=?', (name,))
            removed = db.execute('DELETE FROM source_inflight WHERE name=? '
                                 'AND owner_pid IS NULL AND owner_instance IS NULL', (name,)).rowcount
            db.execute('INSERT INTO attempts(name,at,status,code,error,returned,elapsed_ms,cost_cny,cached) '
                       'VALUES(?,?,?,?,?,?,?,?,?)',
                       (name, utc_now(), 'reconciled', 'legacy_inflight_reconciled', '', removed, 0, 0., 0))
        return {'name': name, 'removed': removed, 'remote_outcome_unknown': True}

    def _acquire_request_slot(self, name, token, expires_at, *, budget=None):
        self._check_process()
        cleaned = set()
        admitted = False
        with self._db(budget=budget) as db:
            # Acquire the SQLite write lock before the count, including when
            # different application processes share this source store.
            db.execute('UPDATE sources SET next_request_at=next_request_at WHERE name=?', (name,))
            # Only a write-locked snapshot can prove a finished token is absent:
            # an uncertain writer cannot subsequently commit behind this view.
            present = {row[0] for row in db.execute('SELECT token FROM source_inflight')}
            with _SLOT_LOCK:
                cleaned.update(key for key, state in _SLOT_STATES.items()
                               if state == 'finished' and _SLOT_DATABASES.get(key) == self._slot_database
                               and key not in present)
            from lune_quota import _process_alive
            rows = db.execute('SELECT token,owner_pid,owner_instance FROM source_inflight WHERE name=?',
                              (name,)).fetchall()
            for row in rows:
                pid, instance = row['owner_pid'], row['owner_instance']
                if type(pid) is not int or pid <= 0 or not instance:
                    continue  # Legacy or malformed owner: fail closed.
                if pid == _SLOT_PID and instance == _SLOT_INSTANCE:
                    with _SLOT_LOCK:
                        reclaim = _SLOT_STATES.get(row['token']) == 'finished'
                else:
                    try:
                        reclaim = _process_alive(pid) is False
                    except Exception:
                        reclaim = False
                if reclaim:
                    db.execute('DELETE FROM source_inflight WHERE token=?', (row['token'],))
                    if pid == _SLOT_PID and instance == _SLOT_INSTANCE:
                        cleaned.add(row['token'])
            active = db.execute('SELECT COUNT(*) FROM source_inflight WHERE name=?', (name,)).fetchone()[0]
            with _SLOT_LOCK:
                own_active = (_SLOT_STATES.get(token) == 'active' and
                              _SLOT_DATABASES.get(token) == self._slot_database)
            existing_own = any(row['token'] == token and row['owner_pid'] == _SLOT_PID and
                               row['owner_instance'] == _SLOT_INSTANCE for row in rows)
            if existing_own and own_active:
                # Retry after an unknown commit outcome: the same live owner
                # already holds this exact slot; never insert it a second time.
                admitted = True
            elif token in present:
                raise _error('source_slot_conflict', 'Source slot ownership cannot be confirmed.')
            elif active < _MAX_INFLIGHT[name]:
                with _SLOT_LOCK:
                    _SLOT_STATES[token] = 'active'
                    _SLOT_DATABASES[token] = self._slot_database
                db.execute('INSERT INTO source_inflight(token,name,expires_at,owner_pid,owner_instance) '
                           'VALUES(?,?,?,?,?)', (token, name, expires_at, _SLOT_PID, _SLOT_INSTANCE))
                admitted = True
        # Do not discard evidence on rollback or an uncertain commit.
        with _SLOT_LOCK:
            for key in cleaned:
                if _SLOT_STATES.get(key) == 'finished':
                    _SLOT_STATES.pop(key, None)
                    _SLOT_DATABASES.pop(key, None)
        return admitted

    @contextmanager
    def request_slot(self, name, budget):
        """Reject same-context recursion before entering the shared slot queue."""
        name = _name(name)
        self._check_process()
        key = (_SLOT_PID, self._slot_database, name)
        active = _SLOT_CONTEXT.get()
        if key in active:
            raise _error('source_slot_reentrant', 'Nested requests to the same source slot are not allowed.')
        context_token = _SLOT_CONTEXT.set(active + (key,))
        try:
            with self._request_slot_impl(name, budget) as token:
                yield token
        finally:
            _SLOT_CONTEXT.reset(context_token)

    @contextmanager
    def _request_slot_impl(self, name, budget):
        """Bound outstanding requests across tasks, store instances and processes.

        A slot is held through response-body consumption and released on every
        normal/error/cancel exit. Expiry never proves the operation stopped;
        recovery requires a locally finished token or a confirmed dead process.
        Rate reservations remain a separate constraint inside the acquired slot.
        """
        name = _name(name)
        self._check_process()
        owner_pid, owner_instance = _SLOT_PID, _SLOT_INSTANCE
        token, queued, acquired = uuid.uuid4().hex, False, False
        started = time.monotonic()
        try:
            while not acquired:
                budget.check_cancelled()
                remaining = budget.remaining()
                try:
                    acquired = self._acquire_request_slot(name, token, time.time() + remaining + 5.,
                                                          budget=budget)
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                        raise
                    budget.check_cancelled()
                    budget.remaining()
                    continue
                if not acquired:
                    if not queued:
                        from execution_events import emit
                        emit('source_wait', source=name, reason='concurrency_limit',
                             max_inflight=_MAX_INFLIGHT[name], status='waiting')
                        queued = True
                    budget.wait(min(.05, remaining))
            budget.check_cancelled()
            if queued:
                from execution_events import emit
                emit('source_ready', source=name, reason='concurrency_slot_acquired',
                     duration_ms=round((time.monotonic() - started) * 1000, 1))
            yield token
        finally:
            # A fork inherits this generator's locals, but not ownership.
            is_owner = (os.getpid() == owner_pid == _SLOT_PID
                        and owner_instance == _SLOT_INSTANCE)
            # Never touch the inherited store, lock, or parent's token in a child.
            if is_owner:
                original_error = sys.exc_info()[0] is not None
                try:
                    with _SLOT_LOCK:
                        if token in _SLOT_STATES:
                            _SLOT_STATES[token] = 'finished'
                    if acquired:
                        release_budget = Budget(.25)
                        released = False
                        while not released:
                            try:
                                with self._db(budget=release_budget) as db:
                                    deleted = db.execute('DELETE FROM source_inflight WHERE token=? AND owner_pid=? '
                                                         'AND owner_instance=?', (token, owner_pid, owner_instance)).rowcount
                                    if deleted != 1:
                                        raise _error('source_slot_release_unknown', 'Source slot ownership cannot be confirmed.')
                                released = True
                                with _SLOT_LOCK:
                                    _SLOT_STATES.pop(token, None)
                                    _SLOT_DATABASES.pop(token, None)
                            except sqlite3.OperationalError as exc:
                                if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                                    raise
                                release_budget.remaining()
                except BaseException as release_error:
                    # The finished token can be reclaimed by a later admission;
                    # never mask the original failure.
                    try:
                        from execution_events import emit
                        emit('source_release_deferred', source=name, status='unknown',
                             reason='source_store_unavailable')
                    except BaseException:
                        pass
                    if not original_error:
                        if not isinstance(release_error, Exception):
                            raise
                        raise _error('source_slot_release_unknown',
                                     'Source slot release failed; recovery will be attempted later.') from None

    reserve_request = reserve

    def _ensure_ready(self, name, config):
        configured = self.configure(config)
        public = self._public(self._row(name))
        field = _KEY_FIELDS.get(name)
        has_config_key = bool(field and getattr(configured, field, ''))
        if not public['configured'] and not has_config_key:
            raise _error('source_not_configured', 'Source credentials are not configured.')
        return configured

    def probe(self, name, config, query='retrieval augmented generation'):
        """Explicit recovery probe globally clears this source's persistent cooldown."""
        name = _name(name)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise _error('invalid_query', 'Probe query must contain 1 to 500 characters.')
        started = time.monotonic()
        outcome = {}
        try:
            configured = self._ensure_ready(name, config)
            from source import AcademicSource
            budget = Budget(30)
            configured = self.configure(replace(configured, source=name, cache=False, per_search=1,
                                 request_timeout=min(float(configured.request_timeout), 30),
                                 source_timeout=min(float(configured.source_timeout), 30)))
            # An explicit test is also the recovery action after a network
            # change, so it may retry a source that is still cooling down.
            self.cooldown(name, 0)
            with self._db() as db:
                before = db.execute('SELECT COALESCE(MAX(id),0) FROM attempts WHERE name=?', (name,)).fetchone()[0]
            batch = AcademicSource(configured, budget).search_batch(query.strip(), 1)
            with self._db() as db:
                recorded = [dict(row) for row in db.execute(
                    'SELECT status,code,error,returned,elapsed_ms FROM attempts WHERE name=? AND id>?', (name, before))]
            runs = list(getattr(batch, 'runs', []))
            if not runs:
                raise _error('invalid_response', 'Source returned no observed attempt.')
            observed = [run for run in runs if (run.get('name') if isinstance(run, dict) else run.name) == name]
            if not observed:
                raise _error('invalid_response', 'Source returned no attempt for the requested provider.')
            for run in observed:
                safe = dict(run if isinstance(run, dict) else run.as_dict())
                if safe.get('remote_outcome') in REMOTE_OUTCOMES:
                    outcome['remote_outcome'] = safe['remote_outcome']
                if type(safe.get('retry_safe')) is bool:
                    outcome['retry_safe'] = safe['retry_safe']
                safe['error'] = self._redact(str(safe.get('error') or ''), configured)
                # AcademicSource can already be wired to record_run. Match its
                # actual attempt rather than append the same run a second time.
                duplicate = next((row for row in recorded
                    if row['status'] == safe.get('status') and row['code'] == (safe.get('code') or '')
                    and row['returned'] == (safe.get('returned') or 0)
                    and row['elapsed_ms'] == (safe.get('elapsed_ms') or 0)), None)
                if duplicate is not None:
                    recorded.remove(duplicate)
                else:
                    self.record_run(safe)
        except RunCancelled:
            raise
        except Exception as exc:
            from source import classify_source_error, _safe_outcome
            outcome = _safe_outcome(exc)
            message = str(self._redact(str(exc), config))[:500]
            self.record_run({'name': name, 'status': 'error', 'error': message,
                             'code': getattr(exc, 'code', '') or classify_source_error(message),
                             'elapsed_ms': int((time.monotonic() - started) * 1000)})
        state = self._public(self._row(name))
        return {**state, **outcome, 'duration_ms': int((time.monotonic() - started) * 1000),
                'ok': bool(state['health'] and state['health']['status'] in ('ok', 'empty'))}

    def skills(self, name, config=None):
        name = _name(name)
        if name != 'lune':
            return {'name': name, 'skills': [], 'supported': False,
                    'capabilities': self._capabilities(name)}
        return {'name': name, 'supported': True, 'skills': [
            {'name': tool, 'tool': tool, 'required': list(_REQUIRED[tool]),
             'parameters': dict(schema), 'network': True}
            for tool, schema in _TOOL_SCHEMAS.items()]}

    @staticmethod
    def _arguments(tool, args):
        if not isinstance(args, dict):
            raise _error('invalid_skill_arguments', 'Skill arguments must be an object.')
        schema = _TOOL_SCHEMAS[tool]
        if set(args) - set(schema):
            raise _error('invalid_skill_arguments', 'Skill arguments include unsupported fields.')
        clean = {}
        for key, value in args.items():
            kind = schema[key]
            valid = False
            if kind == 'text':
                valid = isinstance(value, str) and 0 < len(value.strip()) <= 2000 and not any(ord(ch) < 32 for ch in value)
                value = value.strip() if isinstance(value, str) else value
            elif kind == 'texts':
                valid = (isinstance(value, list) and 0 < len(value) <= 10
                         and all(isinstance(v, str) and 0 < len(v.strip()) <= 200 for v in value))
            elif kind in ('limit', 'offset', 'year'):
                lower, upper = {'limit': (1, 10), 'offset': (0, 10000), 'year': (1900, 2100)}[kind]
                valid = type(value) is int and lower <= value <= upper
            if not valid:
                raise _error('invalid_skill_arguments', 'Invalid bounded skill argument: ' + key)
            clean[key] = value
        if any(key not in clean for key in _REQUIRED[tool]):
            raise _error('invalid_skill_arguments', 'A required skill argument is missing.')
        if clean.get('year_from', 1900) > clean.get('year_to', 2100):
            raise _error('invalid_skill_arguments', 'year_from must not exceed year_to.')
        if tool == 'search_papers':
            for public, native in (('year_from', 'year_min'), ('year_to', 'year_max')):
                if public in clean:
                    clean[native] = clean.pop(public)
        if 'paper_id' in clean and clean['paper_id'].startswith('lune:'):
            clean['paper_id'] = clean['paper_id'][5:]
        return clean

    def lune_quota_status(self):
        """Read local accounting without contacting Lune or claiming account balance."""
        from lune_quota import LuneQuotaStore, default_quota_path
        with LuneQuotaStore(self.quota_path or default_quota_path()) as ledger:
            return {**ledger.status(), 'pending': ledger.pending()}

    def confirm_lune_quota(self, data):
        """Record only a user-confirmed snapshot; never infer a reset."""
        if (not isinstance(data, dict) or set(data) != {'remaining', 'expires_at', 'acknowledged'}
                or data.get('acknowledged') is not True):
            raise _error('invalid_quota_confirmation', 'Explicit quota confirmation is required.')
        from lune_quota import LuneQuotaStore, default_quota_path
        with LuneQuotaStore(self.quota_path or default_quota_path()) as ledger:
            ledger.confirm(data['remaining'], data['expires_at'])
            return {**ledger.status(), 'pending': ledger.pending()}

    def reconcile_lune_quota(self, data):
        """Mark one explicitly acknowledged unfinished attempt unknown; never refund."""
        if (not isinstance(data, dict) or set(data) != {'id', 'acknowledged'}
                or data.get('acknowledged') is not True):
            raise _error('invalid_quota_reconciliation', 'Explicit reconciliation acknowledgement is required.')
        from lune_quota import LuneQuotaStore, default_quota_path
        with LuneQuotaStore(self.quota_path or default_quota_path()) as ledger:
            ledger.reconcile_unknown(data['id'])
            return {**ledger.status(), 'pending': ledger.pending()}

    @staticmethod
    @contextmanager
    def _mcp_lock(budget):
        while True:
            budget.check_cancelled()
            if _MCP_LOCK.acquire(timeout=min(0.1, budget.remaining())):
                break
        try:
            budget.check_cancelled()
            budget.remaining()
            yield
        finally:
            _MCP_LOCK.release()

    @staticmethod
    def _local_failure_event(action):
        try:
            from execution_events import emit
            emit(action, source='lune', status='unknown', reason='source_store_unavailable')
        except BaseException:
            pass

    def _call_lune(self, tool, arguments, config, *, budget=None):
        budget = budget if budget is not None else Budget(30)
        budget.check_cancelled()
        budget.remaining()
        result = None
        with self.budget_scope(budget):
            try:
                with self.request_slot('lune', budget):
                    result = self._call_lune_reserved(tool, arguments, config, budget)
            except GuideError as exc:
                if result is None or getattr(exc, 'code', '') != 'source_slot_release_unknown':
                    raise
                result['release_status'] = 'deferred'
            return result

    def _call_lune_reserved(self, tool, arguments, config, budget):
        from lune_quota import default_quota_path
        if self.quota_path is not None and self.quota_path.resolve() != default_quota_path().resolve():
            raise _error('quota_ledger_mismatch',
                         'Quota status and MCP transport must use the same shared ledger.')
        configured = self._ensure_ready('lune', config)
        import mcp_lune
        from mcp_client import McpError, McpResult, McpToolError, structured
        started = time.monotonic()
        remote_success = False
        finish_failed = False
        finish_interrupted = None
        outcome = None
        try:
            self.wait_for_rate('lune', budget)
            with self._mcp_lock(budget), ExitStack() as locks:
                provider = mcp_lune.install()
                # All built-in Lune paths acquire MCP before the provider lock;
                # request-specific search clients never replace the shared one.
                provider_lock = getattr(provider, '_lock', None)
                if provider_lock is not None:
                    locks.enter_context(mcp_lune.budgeted_lock(provider_lock, budget))
                row = self._row('lune')
                if row['api_key'] is not None or row['endpoint']:
                    from mcp_client import McpClient, load_servers
                    server = load_servers(provider.config_path, on_missing='drop').get('lune')
                    if server is None:
                        raise _error('source_not_configured', 'Lune MCP server is not configured.')
                    from mcp_lune_policy import lune_override_headers, validate_lune_auth_target
                    headers = {k: v for k, v in lune_override_headers(
                        server.headers, row['endpoint'] or server.url, server.url).items()
                        if k.lower() != 'authorization'}
                    explicit_key = self._key('lune', row)
                    validate_lune_auth_target(row['endpoint'] or server.url, server.url,
                                              require_same_origin=not bool(row['api_key']))
                    token = explicit_key or mcp_lune.access_token(budget=budget)
                    if token:
                        headers['Authorization'] = 'Bearer ' + token
                    server = replace(server, headers=headers, url=row['endpoint'] or server.url,
                                     timeout=min(server.timeout, budget.remaining()))
                    client = McpClient(server, budget=budget)
                else:
                    client = provider.client(budget=budget)
                    from mcp_client import McpClient
                    if isinstance(client, McpClient):
                        # A tool invoked from Sources must not change a running
                        # search's shared client budget or timeout.
                        shared = client
                        client = McpClient(shared.config, transport=shared._transport, budget=budget)
                        if shared._auth_provider is not None:
                            def provide_auth(force, *, budget=None, rejected_token=None):
                                return mcp_lune.access_token(
                                    force_refresh=force, budget=budget,
                                    rejected_token=rejected_token if force else None)
                            client.set_auth_provider(provide_auth, budgeted=True)
                from mcp_client import McpClient
                # A shared installed client must not retain this operation's
                # expired Budget or shortened timeout after the request ends.
                original_config = client.config if isinstance(client, McpClient) else None
                original_budget = client.budget if isinstance(client, McpClient) else None
                try:
                    if isinstance(client, McpClient):
                        if not client.config.permits(tool):
                            raise _error('unsupported_skill', 'This tool is not enabled by the Lune MCP allow-list.')
                        client.config = replace(client.config, timeout=min(client.config.timeout, budget.remaining()))
                        client.budget = budget
                    # Recheck after MCP-lock wait and again inside the real
                    # client's transport immediately before dispatch.
                    def before_send():
                        while True:
                            budget.check_cancelled()
                            budget.remaining()
                            delay = max(0., self.cooldown('lune') - time.time())
                            if not delay:
                                return
                            budget.wait(delay)
                            self.wait_for_rate('lune', budget)
                    before_send()
                    # The adapter owns OAuth and the paid ledger. Charge before
                    # dispatch: a failed provider request still consumed a slot.
                    attempt_index = None
                    if hasattr(provider, '_charge'):
                        attempt_index = provider._charge(tool, {})
                    outcome = 'unknown'
                    try:
                        budget.check_cancelled()
                        budget.remaining()
                        if isinstance(client, McpClient):
                            result = client.call_tool(tool, arguments, _before_send=before_send)
                        else:
                            # Legacy host adapters do not expose their transport boundary.
                            result = client.call_tool(tool, arguments)
                        if isinstance(result, McpResult) and result.quota_finalization_failed:
                            finish_failed = True
                        if isinstance(result, dict) and result.get('isError') is True:
                            outcome = 'error'
                        elif (isinstance(result, dict)
                              and (result.get('isError') is False or
                                   (result.get('isError') is None and
                                    ('content' in result or 'structuredContent' in result)))):
                            outcome = 'completed'
                            remote_success = True
                    except McpToolError:
                        outcome = 'error'
                        raise
                    except McpError as exc:
                        # Preserve explicit remote rejection, known non-dispatch,
                        # and received-but-unavailable responses without replay.
                        if getattr(exc, 'remote_outcome', None) in REMOTE_OUTCOMES:
                            outcome = exc.remote_outcome
                        raise
                    except (RunCancelled, BudgetExceeded) as exc:
                        retained = getattr(exc, 'mcp_result', None)
                        if (isinstance(client, McpClient) and isinstance(retained, McpResult)
                                and getattr(exc, 'remote_outcome', None) == 'response_received'
                                and retained.get('isError') is not True
                                and ('content' in retained or 'structuredContent' in retained)):
                            result = retained
                            outcome = 'completed'
                            remote_success = True
                            finish_failed = True
                            exc.remote_outcome, exc.retry_safe = 'completed', False
                            try:
                                with self.budget_scope(Budget(.25)):
                                    payload = self._redact(structured(retained), configured)
                                if not isinstance(payload, dict):
                                    payload = {'result': payload}
                                exc.partial_source_result = RetainedSourceResult({
                                    'name': 'lune', 'skill': tool, 'result': payload,
                                    'audit_status': 'failed'})
                            except BaseException:
                                self._local_failure_event('source_audit_failed')
                        elif getattr(exc, 'remote_outcome', None) == 'not_started':
                            outcome = 'not_started'
                        raise
                    finally:
                        if attempt_index is not None and hasattr(provider, '_finish'):
                            original_error = sys.exc_info()[0] is not None
                            try:
                                provider._finish(attempt_index, ok=outcome == 'completed', status=outcome)
                            except BaseException as finish_error:
                                finish_failed = True
                                self._local_failure_event('source_audit_failed')
                                if not original_error and isinstance(finish_error, (RunCancelled, BudgetExceeded)):
                                    finish_interrupted = finish_error
                                    if remote_success:
                                        try:
                                            with self.budget_scope(Budget(.25)):
                                                payload = self._redact(structured(result), configured)
                                            if not isinstance(payload, dict):
                                                payload = {'result': payload}
                                            finish_error.partial_source_result = RetainedSourceResult({
                                                'name': 'lune', 'skill': tool, 'result': payload,
                                                'audit_status': 'failed'})
                                        except BaseException:
                                            self._local_failure_event('source_audit_failed')
                                    raise
                                if not isinstance(finish_error, Exception) and not original_error:
                                    raise
                    if outcome == 'unknown':
                        error = _error('remote_outcome_unknown', 'Lune response outcome is unknown; do not retry automatically.')
                        error.remote_outcome, error.retry_safe = 'unknown', False
                        raise error
                    if outcome == 'error':
                        raise _error('source_error', 'Lune tool returned an error response.')
                    try:
                        usage = provider.usage() if hasattr(provider, 'usage') else None
                    except RunCancelled:
                        raise
                    except Exception:
                        usage = None
                        finish_failed = True
                        self._local_failure_event('source_audit_failed')
                finally:
                    if isinstance(client, McpClient):
                        client.config = original_config
                        client.budget = original_budget
            if isinstance(result, dict) and result.get('isError'):
                raise _error('source_error', 'Lune tool returned an error response.')
            with self.budget_scope(Budget(.25)):
                payload = self._redact(structured(result), configured)
            if not isinstance(payload, dict):
                payload = {'result': payload}
            audit_status = 'failed' if finish_failed else 'recorded'
            try:
                with self.budget_scope(Budget(.25)):
                    self.record_run({'name': 'lune', 'status': 'ok', 'quota': usage,
                                     'elapsed_ms': int((time.monotonic() - started) * 1000)})
            except RunCancelled:
                raise
            except Exception:
                audit_status = 'failed'
                self._local_failure_event('source_audit_failed')
            return {'name': 'lune', 'skill': tool, 'result': payload, 'audit_status': audit_status,
                    'duration_ms': int((time.monotonic() - started) * 1000)}
        except RunCancelled as exc:
            if remote_success:
                exc.remote_outcome = 'completed'
                exc.retry_safe = False
                try:
                    from execution_events import emit
                    emit('source_remote_completed', source='lune', status='completed',
                         reason='local_processing_cancelled', retry_safe=False)
                except BaseException:
                    pass
            elif outcome in ('unknown', 'not_started'):
                exc.remote_outcome, exc.retry_safe = outcome, False
            raise
        except BudgetExceeded as exc:
            if remote_success and exc is finish_interrupted:
                exc.remote_outcome, exc.retry_safe = 'completed', False
                try:
                    from execution_events import emit
                    emit('source_remote_completed', source='lune', status='completed',
                         reason='local_processing_cancelled', retry_safe=False)
                except BaseException:
                    pass
                raise
            if remote_success and hasattr(exc, 'partial_source_result'):
                exc.remote_outcome, exc.retry_safe = 'completed', False
                raise
            if remote_success:
                self._local_failure_event('source_audit_failed')
                error = _error('remote_success_local_failure',
                               'Lune completed, but local processing failed; do not retry automatically.')
                error.remote_outcome, error.retry_safe = 'completed', False
                raise error from None
            if outcome in ('unknown', 'not_started'):
                exc.remote_outcome, exc.retry_safe = outcome, False
            raise
        except Exception as exc:
            if remote_success:
                self._local_failure_event('source_audit_failed')
                error = _error('remote_success_local_failure',
                               'Lune completed, but local processing failed; do not retry automatically.')
                error.remote_outcome, error.retry_safe = 'completed', False
                raise error from None
            from source import classify_source_error
            code = str(getattr(exc, 'kind', '') or getattr(exc, 'code', '') or
                       classify_source_error(str(exc)))
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code):
                code = 'source_error'
            # Do not reflect arbitrary remote text or wait on the settings DB
            # merely to redact it while handling an already-failed request.
            message = f'Lune request failed ({code}).'
            try:
                with self.budget_scope(Budget(.25)):
                    self.record_run({'name': 'lune', 'status': 'error', 'code': code, 'error': message,
                                     'elapsed_ms': int((time.monotonic() - started) * 1000)})
            except BaseException:
                self._local_failure_event('source_audit_failed')
            error = _error(str(code), message)
            if outcome in ('unknown', 'not_started', 'error', 'response_received'):
                error.remote_outcome, error.retry_safe = outcome, False
            raise error from None

    def catalog(self, name, config, *, budget=None):
        name = _name(name)
        if name != 'lune':
            raise _error('unsupported_catalog', 'Conference catalog is only supported by Lune.')
        result = self._call_lune('list_conferences', {}, config, budget=budget)
        return {**result, 'catalog': result['result']}

    def invoke(self, name, skill, args, config, *, budget=None):
        name = _name(name)
        if name != 'lune':
            raise _error('unsupported_skill', 'This source does not expose executable skills.')
        if not isinstance(skill, str):
            raise _error('unsupported_skill', 'Unknown source skill.')
        tool = _TOOL_ALIASES.get(skill, skill)
        if tool not in _TOOL_SCHEMAS:
            raise _error('unsupported_skill', 'Unknown or unsupported source skill.')
        arguments = self._arguments(tool, args)
        return self._call_lune(tool, arguments, config, budget=budget)
