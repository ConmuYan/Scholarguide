"""Generic Model Context Protocol client over Streamable HTTP (stdlib only).

Scope
-----
This is a *client*, not a Lune wrapper: it speaks JSON-RPC 2.0 to any MCP server
declared in ``mcp_servers.json``, discovers tools with ``tools/list`` and calls
them with ``tools/call``. Lune is only the first entry in that file.

Transport facts it implements (from the MCP Streamable HTTP profile):
  * one JSON-RPC message per ``POST``; ``Content-Type: application/json``
  * ``Accept: application/json, text/event-stream`` — a server may answer with a
    single SSE ``event: message`` whose ``data:`` line is the JSON-RPC response
  * no session state: ``GET``/``DELETE`` are not used
  * ``initialize`` is optional for stateless servers, so it is opt-in

Failures are classified rather than stringified: every error carries a stable
``kind`` (``auth``/``forbidden``/``rate_limited``/``timeout``/``protocol``/
``client_error``/``server_error``/``transport``) plus the raw HTTP status when
there was one. Callers branch on ``kind``, never on message text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from http.client import HTTPException
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from common import Budget, BudgetExceeded, GuideError, REMOTE_OUTCOMES, RunCancelled
from network import execution_context, source_urlopen as urlopen, TransportDeadlineExceeded
from execution_events import operation
from mcp_lune_policy import LUNE_PROMPT_SCHEMAS, LUNE_TOOL_SCHEMAS
from tool_governance import (audit_arguments, redact_text, validate_arguments,
                             validate_remote_schema, ToolGovernanceError)

PROTOCOL_VERSION = '2025-11-25'
SUPPORTED_SESSION_PROTOCOLS = frozenset(('2025-11-25', '2025-03-26'))
MAX_BODY_BYTES = 4_000_000
MAX_DISCOVERY_PAGES = 20
MAX_DISCOVERY_ENTRIES = 5000
MAX_DISCOVERY_BYTES = 8_000_000
CALL_ALIASES = {
    'listconferences': 'list_conferences',
    'fulltext': 'get_paper_fulltext',
    'related': 'search_related_papers',
    'citations': 'get_paper_citations',
}
KINDS = ('auth', 'forbidden', 'rate_limited', 'timeout', 'protocol',
         'client_error', 'server_error', 'transport')

_ENV_PATTERN = re.compile(r'\$\{([A-Z0-9_]+)\}')


class McpError(GuideError):
    """A classified MCP failure. ``kind`` is stable; ``message`` is for humans."""

    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 code: int | None = None, server: str = '', raw: str = ''):
        if kind not in KINDS:
            raise ValueError(f'unknown MCP error kind: {kind}')
        super().__init__(f'{server + ": " if server else ""}{kind}: {message}')
        self.kind, self.status, self.code, self.server = kind, status, code, server
        self.remote_outcome = None
        self.retry_safe = None
        self.quota_finalization_failed = False
        if raw:
            self.raw = '[HTML response omitted]' if str(raw).lstrip().startswith('<') else '[remote response omitted]'
        else:
            self.raw = ''

    def as_dict(self) -> dict:
        result = {'kind': self.kind, 'status': self.status, 'code': self.code,
                  'server': self.server, 'message': str(self), 'raw': self.raw}
        if isinstance(self.remote_outcome, str) and self.remote_outcome in REMOTE_OUTCOMES:
            result['remote_outcome'] = self.remote_outcome
        if self.retry_safe is False:
            result['retry_safe'] = False
        if self.quota_finalization_failed:
            result['quota_finalization_failed'] = True
        return result


def _validate_session_protocol(version):
    """Accept only the locally declared session protocol versions."""
    if not isinstance(version, str) or version not in SUPPORTED_SESSION_PROTOCOLS:
        raise McpError('protocol', 'unsupported session protocol version')
    return version


def _validate_session_id(value):
    """Validate the opaque HTTP header without exposing its contents."""
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(not 0x21 <= ord(char) <= 0x7e for char in value)):
        raise McpError('protocol', 'invalid session identifier')
    return value


class McpResult(dict):
    """Decoded remote result with host-owned accounting metadata."""

    def __init__(self, result, *, quota_finalization_failed=False):
        super().__init__(result)
        self.quota_finalization_failed = quota_finalization_failed
        self.audit_status = 'failed' if quota_finalization_failed else 'recorded'
        self.retry_safe = False


class McpToolError(McpError):
    """The server responded explicitly that a tool invocation failed."""


@dataclass(frozen=True)
class ServerConfig:
    name: str
    url: str
    headers: dict = field(default_factory=dict, repr=False)
    timeout: float = 45.0
    allow: tuple = ()
    protocol: str = PROTOCOL_VERSION
    allow_all: bool = False
    allow_prompts: tuple = ()
    local_tool_schemas: dict = field(default_factory=dict, repr=False)
    local_prompt_schemas: dict = field(default_factory=dict, repr=False)

    def permits(self, tool: str) -> bool:
        return self.allow_all is True or bool(self.allow and tool in self.allow)

    def permits_prompt(self, prompt: str) -> bool:
        return self.allow_all is True or bool(self.allow_prompts and prompt in self.allow_prompts)


def _resolve(value: str) -> str:
    """Expand ``${VAR}`` from the environment; never echo the resolved secret."""
    def repl(match):
        name = match.group(1)
        found = os.getenv(name)
        if not found:
            raise McpError('auth', f'environment variable {name} is not set')
        return found
    return _ENV_PATTERN.sub(repl, str(value))


def load_servers(path: Path | str, *, on_missing: str = 'raise') -> dict[str, ServerConfig]:
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    servers = raw.get('servers') if isinstance(raw, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise McpError('protocol', f'{path}: no "servers" object')
    out: dict[str, ServerConfig] = {}
    for name, entry in servers.items():
        if not isinstance(entry, dict) or not entry.get('url'):
            raise McpError('protocol', f'{name}: entry needs "url"')
        if not isinstance(name, str) or not name:
            raise McpError('protocol', f'{name}: entry needs a name')
        parsed_url = __import__('urllib.parse', fromlist=['urlsplit']).urlsplit(str(entry['url']))
        if (parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment):
            raise McpError('protocol', f'{name}: URL may not contain credentials, query or fragment')
        headers = {}
        for key, value in (entry.get('headers') or {}).items():
            try:
                headers[str(key)] = _resolve(value)
            except McpError:
                # A caller that supplies the credential another way (OAuth token file)
                # can drop the placeholder header instead of failing the whole config.
                if on_missing == 'raise':
                    raise

        allow_value = entry.get('allow', [])
        prompt_value = entry.get('allow_prompts', [])
        allow_all = entry.get('allow_all', False)
        if (not isinstance(allow_value, list) or not isinstance(prompt_value, list)
                or any(not isinstance(t, str) or not t for t in allow_value + prompt_value)
                or type(allow_all) is not bool):
            raise McpError('protocol', f'{name}: invalid tool or prompt allow-list')
        allow = tuple(dict.fromkeys(allow_value))
        allow_prompts = tuple(dict.fromkeys(prompt_value))
        out[name] = ServerConfig(name=str(name), url=str(entry['url']).rstrip('/'),
                                 headers=headers, timeout=float(entry.get('timeout') or 45.0),
                                 allow=allow, allow_all=allow_all, allow_prompts=allow_prompts,
                                 protocol=str(entry.get('protocol') or PROTOCOL_VERSION))
    return out


def _parse_messages(body: bytes, server: str) -> list[dict]:
    """Parse JSON or every JSON event in an SSE stream.

    Streamable HTTP permits progress/notification messages before the response.
    Keeping them as separate messages lets the caller select the response by its
    JSON-RPC id instead of accidentally joining unrelated JSON documents.
    """
    text = body.decode('utf-8', 'replace').strip()
    if not text:
        raise McpError('protocol', 'empty response body', server=server)
    if not text.startswith(('data:', 'event:', ':')):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpError('protocol', f'response is not JSON: {exc.msg}',
                           server=server, raw=text) from None
        values = payload if isinstance(payload, list) else [payload]
        if not all(isinstance(value, dict) for value in values):
            raise McpError('protocol', 'JSON-RPC message is not an object', server=server)
        return values

    events: list[str] = []
    data_lines: list[str] = []

    def flush() -> None:
        if data_lines:
            events.append('\n'.join(data_lines))
            data_lines.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
            continue
        if line.startswith(':') or line.startswith('event:') or line.startswith('id:'):
            continue
        if line.startswith('data:'):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(' ') else value)
    flush()
    if not events:
        raise McpError('protocol', 'not JSON and no SSE data event', server=server, raw=text)

    messages: list[dict] = []
    for payload in events:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise McpError('protocol', f'response is not JSON: {exc.msg}',
                           server=server, raw=payload) from None
        if not isinstance(message, dict):
            raise McpError('protocol', 'JSON-RPC message is not an object', server=server)
        messages.append(message)
    return messages


def _parse_body(body: bytes, server: str) -> dict:
    """Backward-compatible single-message view of a JSON/SSE response."""
    messages = _parse_messages(body, server)
    return messages[-1]


def _header_secrets(headers: dict) -> tuple[str, ...]:
    """Keep actual credential values in memory solely for response redaction."""
    excluded = {'content-type', 'accept', 'user-agent', 'mcp-protocol-version'}
    values = []
    for key, value in headers.items():
        if str(key).lower() in excluded or not isinstance(value, str) or not value:
            continue
        values.append(value)
        if value.lower().startswith('bearer '):
            values.append(value[7:].strip())
    return tuple(sorted({value for value in values if value}, key=len, reverse=True))


def _redact_remote(value, secrets: tuple[str, ...]):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[redacted]')
        return value
    if isinstance(value, dict):
        return {_redact_remote(key, secrets): _redact_remote(item, secrets)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_remote(item, secrets) for item in value]
    return value


def _contains_cursor(value, cursors: set[str]) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if any(cursor in item for cursor in cursors):
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _kind_for_status(status: int) -> str:
    if status in (401, 407):
        return 'auth'
    if status == 403:
        return 'forbidden'
    if status == 429:
        return 'rate_limited'
    if 400 <= status < 500:
        return 'client_error'
    return 'server_error'


def _mcp_transport(request, *, timeout):
    # A redirect is another HTTP request and must not bypass quota admission.
    return urlopen(request, timeout=timeout, reject_redirects=True)


class McpClient:
    """One server, one JSON-RPC stream. Counts calls so a quota can be enforced."""

    def __init__(self, config: ServerConfig, *, transport=_mcp_transport, budget=None,
                 quota_store=None, stateful=False):
        if type(stateful) is not bool:
            raise McpError('client_error', 'stateful option must be boolean', server=config.name)
        if stateful:
            _validate_session_protocol(config.protocol)
            if any(str(key).casefold() in ('mcp-session-id', 'mcp-protocol-version') for key in config.headers):
                raise McpError('client_error', 'reserved session header in configuration', server=config.name)
        self._stateful = stateful
        self._session_lock = threading.RLock()
        self._session_status = 'new'
        self._session_id = None
        self._session_protocol = None
        self._session_identity = None
        self._session_initialization = None
        self.config = config
        self._transport = transport
        self.budget = budget
        # An injected store is useful for isolated clients/tests. Production
        # Lune clients otherwise use the same local cross-process database.
        self._quota_store = quota_store
        self.calls = 0
        self.http_attempts = 0
        self.calls_by_tool: dict[str, int] = {}
        self._tools = None
        self._tool_schemas: dict[str, dict | None] = {}
        self._prompts: list[dict] | None = None
        self._prompt_schemas: dict[str, dict | None] = {}
        self._discovery_generation = {'tools': 0, 'prompts': 0}
        self._discovery_invalid = {'tools': False, 'prompts': False}
        # The shipped Lune route has reviewed host-side bounds. These are not
        # claims about current remote advertisement and require no hidden I/O.
        self._local_tool_schemas = deepcopy(
            LUNE_TOOL_SCHEMAS if config.name == 'lune' else config.local_tool_schemas)
        self._local_prompt_schemas = deepcopy(
            LUNE_PROMPT_SCHEMAS if config.name == 'lune' else config.local_prompt_schemas)
        self._next_id = 0
        self._lock = threading.RLock()
        self._auth_provider: Callable | None = None
        self._auth_provider_budgeted = False

    def set_budget(self, budget) -> None:
        """Adopt the caller's current request budget for subsequent calls."""
        with self._lock:
            self.budget = budget

    def set_auth_provider(self, provider: Callable | None, *, budgeted: bool = False) -> None:
        """Install a token provider; budget-aware hosts receive the request budget."""
        with self._lock:
            self._auth_provider = provider
            self._auth_provider_budgeted = budgeted is True

    @contextmanager
    def _quota_attempt(self, method: str, state: dict, budget=None):
        """Reserve each Lune HTTP attempt; unknown outcomes never refund."""
        host = (urlsplit(self.config.url).hostname or '').casefold()
        if self.config.name.casefold() != 'lune' and host != 'mcp.luneresearch.com':
            yield
            return
        from lune_quota import LuneQuotaStore, default_quota_path
        owned = self._quota_store is None
        store = LuneQuotaStore(default_quota_path()) if owned else self._quota_store
        try:
            # Production stores support cancellable admission. Legacy injected
            # test/host stores retain their previous interface and must enforce
            # their own waiting policy.
            bounded = isinstance(store, LuneQuotaStore)
            reservation = store.reserve(method, budget=budget) if bounded else store.reserve(method)
            try:
                yield
            except BaseException:
                try:
                    if bounded:
                        store.finish(reservation, 'unknown', budget=budget)
                    else:
                        store.finish(reservation, 'unknown')
                except BaseException:
                    # The reservation remains consumed, even if finalization failed.
                    state['quota_finalization_failed'] = True
                raise
            else:
                try:
                    if bounded:
                        store.finish(reservation, 'response_received', budget=budget)
                    else:
                        store.finish(reservation, 'response_received')
                except (RunCancelled, BudgetExceeded) as exc:
                    state['quota_finalization_failed'] = True
                    exc.args = ('Lune quota finalization interrupted.',)
                    state['finalization_exception'] = exc
                except Exception:
                    state['quota_finalization_failed'] = True
        finally:
            if owned:
                original_error = sys.exc_info()[0] is not None
                try:
                    store.close()
                except (RunCancelled, BudgetExceeded) as exc:
                    state['quota_finalization_failed'] = True
                    if not original_error and 'finalization_exception' not in state:
                        exc.args = ('Lune quota finalization interrupted.',)
                        state['finalization_exception'] = exc
                except Exception:
                    state['quota_finalization_failed'] = True
                except BaseException:
                    state['quota_finalization_failed'] = True
                    if not original_error:
                        raise

    # ---------------------------------------------------------------- transport
    @contextmanager
    def _session_guard(self, budget):
        active_budget = self.budget if budget is None else budget
        deadline = time.monotonic() + self.config.timeout
        while True:
            if active_budget is not None:
                active_budget.check_cancelled()
                remaining = min(active_budget.remaining(), deadline - time.monotonic())
            else:
                remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError('timeout', 'session admission deadline exceeded', server=self.config.name)
            if self._session_lock.acquire(timeout=min(0.05, remaining)):
                break
        try:
            if active_budget is not None:
                active_budget.check_cancelled()
                active_budget.remaining()
            yield
        finally:
            self._session_lock.release()

    def _post(self, message: dict, *, budget=None, _before_send=None, _discovery=None, _admission=None) -> dict:
        if not self._stateful:
            return self._post_once(message, budget=budget, _before_send=_before_send,
                                   _discovery=_discovery, _admission=_admission)
        # A client owns a single session and serializes its operations. Any
        # ambiguous initialization or request failure poisons this instance;
        # it never guesses whether a remote operation can be replayed.
        business_state = {'sent': False, 'quota_finalization_failed': False}
        try:
            guard = self._session_guard(budget)
            guard.__enter__()
        except (McpError, RunCancelled, BudgetExceeded) as exc:
            if message.get('method') == 'tools/call':
                exc.remote_outcome, exc.retry_safe = 'not_started', False
            raise
        try:
            try:
                if self._session_status == 'failed':
                    raise McpError('client_error', 'session unavailable; create a new client after inspecting prior outcome',
                                   server=self.config.name)
                if self._session_status == 'new':
                    self._session_status = 'initializing'
                    with self._lock:
                        self._next_id += 1
                        initialization_id = self._next_id
                    init = {'jsonrpc': '2.0', 'id': initialization_id, 'method': 'initialize',
                            'params': {'protocolVersion': self.config.protocol, 'capabilities': {},
                                       'clientInfo': {'name': 'scholarguide', 'version': '0.1'}}}
                    init_state = {'sent': False, 'quota_finalization_failed': False}
                    result = self._post_once(init, budget=budget, _state=init_state)
                    negotiated = _validate_session_protocol(result.get('protocolVersion'))
                    if (not isinstance(result.get('capabilities'), dict)
                            or not isinstance(result.get('serverInfo'), dict)
                            or not isinstance(result['serverInfo'].get('name'), str)
                            or not result['serverInfo']['name']
                            or not isinstance(result['serverInfo'].get('version'), str)
                            or not result['serverInfo']['version']):
                        raise McpError('protocol', 'invalid initialization response', server=self.config.name)
                    if init_state.get('quota_finalization_failed'):
                        raise McpError('client_error', 'initialization audit failed', server=self.config.name)
                    self._session_id = _validate_session_id(init_state.get('response_session'))
                    self._session_protocol = negotiated
                    self._session_initialization = result
                    self._session_status = 'notifying'
                    notification = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
                    notified = self._post_once(notification, budget=budget)
                    if isinstance(notified, McpResult) and notified.quota_finalization_failed:
                        raise McpError('client_error', 'initialization audit failed', server=self.config.name)
                    self._session_status = 'ready'
                if message.get('method') == 'initialize':
                    return deepcopy(self._session_initialization)
                return self._post_once(message, budget=budget, _before_send=_before_send,
                                       _discovery=_discovery, _admission=_admission, _state=business_state)
            except BaseException as exc:
                if message.get('method') == 'tools/call' and not business_state['sent']:
                    exc.remote_outcome, exc.retry_safe = 'not_started', False
                self._session_status = 'failed'
                self._session_id = self._session_protocol = self._session_identity = None
                with self._lock:
                    self._tools = self._prompts = None
                    for key in ('tools', 'prompts'):
                        self._discovery_generation[key] += 1
                        self._discovery_invalid[key] = True
                raise
        finally:
            guard.__exit__(None, None, None)

    def _post_once(self, message: dict, *, budget=None, _before_send=None, _discovery=None, _admission=None,
                   _state=None) -> dict:
        state = _state if _state is not None else {'sent': False, 'quota_finalization_failed': False}
        try:
            result = self._post_impl(message, budget=budget, state=state, _before_send=_before_send,
                                     _discovery=_discovery, _admission=_admission)
        except McpError as exc:
            if message.get('method') == 'tools/call':
                if exc.remote_outcome is None:
                    exc.remote_outcome = 'unknown' if state['sent'] else 'not_started'
                exc.retry_safe = False
            exc.quota_finalization_failed = state['quota_finalization_failed']
            raise
        except (RunCancelled, BudgetExceeded) as exc:
            if message.get('method') == 'tools/call':
                exc.remote_outcome = 'unknown' if state['sent'] else 'not_started'
                exc.retry_safe = False
                exc.quota_finalization_failed = state['quota_finalization_failed']
            raise
        if self._stateful and message.get('method') == 'tools/call':
            content = result.get('content')
            structured = result.get('structuredContent')
            valid_content = (('content' in result or 'structuredContent' in result)
                             and ('content' not in result or (isinstance(content, list)
                                  and all(isinstance(item, dict) and isinstance(item.get('type'), str)
                                          and bool(item['type']) for item in content)))
                             and ('structuredContent' not in result or isinstance(structured, dict)))
            valid_error_flag = 'isError' not in result or type(result['isError']) is bool
            if not valid_content or not valid_error_flag:
                exc = McpError('protocol', 'invalid tool result', server=self.config.name)
                exc.remote_outcome, exc.retry_safe = 'response_received', False
                exc.quota_finalization_failed = state['quota_finalization_failed']
                raise exc
        if message.get('method') == 'tools/call' and result.get('isError') is True:
            exc = McpToolError('protocol', 'tool reported an error', server=self.config.name)
            exc.remote_outcome, exc.retry_safe = 'error', False
            exc.quota_finalization_failed = state['quota_finalization_failed']
            # Retain the sanitized received error result for a trusted caller;
            # it remains a tool error, not a successful result or recommendation.
            exc.mcp_result = McpResult(result, quota_finalization_failed=state['quota_finalization_failed'])
            raise exc
        if state.get('finalization_exception') is not None:
            from execution_events import _terminal_emit
            _terminal_emit('source_audit_failed', source=self.config.name, status='unknown',
                           reason='lune_quota_finalization_failed')
            exc = state['finalization_exception']
            if message.get('method') == 'tools/call':
                exc.remote_outcome, exc.retry_safe = 'response_received', False
                exc.quota_finalization_failed = True
                exc.audit_status = 'failed'
                exc.mcp_result = McpResult(result, quota_finalization_failed=True)
            raise exc
        if state['quota_finalization_failed']:
            from execution_events import _terminal_emit
            _terminal_emit('source_audit_failed', source=self.config.name, status='unknown',
                           reason='lune_quota_finalization_failed')
            if message.get('method') in ('tools/list', 'prompts/list'):
                exc = McpError('client_error', 'discovery audit failed; results not published', server=self.config.name)
                exc.remote_outcome, exc.retry_safe = 'response_received', False
                exc.quota_finalization_failed = True
                raise exc
            return McpResult(result, quota_finalization_failed=True)
        return result

    def _post_impl(self, message: dict, *, budget=None, state: dict, _before_send=None, _discovery=None,
                   _admission=None) -> dict:
        active_budget = self.budget if budget is None else budget
        if active_budget is not None:
            active_budget.check_cancelled()
        base_headers = {'Content-Type': 'application/json',
                       'Accept': 'application/json, text/event-stream',
                       'MCP-Protocol-Version': self.config.protocol,
                       'User-Agent': 'ScholarGuide-mcp/0.1'}
        base_headers.update(self.config.headers)
        if self._stateful:
            if self._session_status == 'initializing':
                base_headers.pop('MCP-Protocol-Version', None)
            else:
                base_headers['MCP-Protocol-Version'] = self._session_protocol
                base_headers['MCP-Session-Id'] = self._session_id
        data = json.dumps(message, ensure_ascii=False).encode('utf-8')
        request_id = message.get('id')
        rejected_token = None
        for attempt in range(2):
            if active_budget is not None:
                active_budget.check_cancelled()
            headers = dict(base_headers)
            with self._lock:
                provider = self._auth_provider
                budgeted_provider = self._auth_provider_budgeted
            if provider is not None:
                token = (provider(attempt == 1, budget=active_budget, rejected_token=rejected_token)
                         if budgeted_provider else provider(attempt == 1))
                if token:
                    headers['Authorization'] = f'Bearer {token}'
            if self._stateful:
                identity_headers = sorted((str(k).casefold(), str(v)) for k, v in headers.items()
                                          if str(k).casefold() not in ('mcp-session-id', 'mcp-protocol-version'))
                identity = hashlib.sha256(repr((self.config.url, identity_headers)).encode('utf-8')).digest()
                if self._session_status == 'initializing':
                    self._session_identity = identity
                elif identity != self._session_identity:
                    raise McpError('auth', 'session authentication identity changed', server=self.config.name)
            request = Request(self.config.url, data=data, headers=headers, method='POST')
            try:
                timeout = min(self.config.timeout, active_budget.remaining()) if active_budget is not None else self.config.timeout
                with self._quota_attempt(str(message.get('method') or ''), state, active_budget):
                    if active_budget is not None:
                        active_budget.check_cancelled()
                        active_budget.remaining()
                    # Host-only hook, after authentication and quota reservation.
                    # The reservation is already committed and is never refunded.
                    if _before_send is not None:
                        try:
                            _before_send()
                        except (RunCancelled, BudgetExceeded):
                            raise
                        except Exception:
                            raise McpError('client_error', 'local dispatch admission failed',
                                           server=self.config.name) from None
                    if _discovery is not None:
                        identity = (request.full_url, tuple(sorted(request.header_items())), provider)
                        previous = _discovery.get('identity')
                        if previous is not None and (identity[:2] != previous[:2] or provider is not previous[2]):
                            raise McpError('client_error', 'discovery identity changed', server=self.config.name)
                    if active_budget is not None:
                        active_budget.check_cancelled()
                        timeout = min(self.config.timeout, active_budget.remaining())
                    cancel = (lambda: active_budget.cancel_token.requested) if (
                        active_budget is not None and hasattr(active_budget, 'cancel_token')) else None
                    with execution_context(timeout_s=timeout, cancelled=cancel):
                        if _admission is not None:
                            _admission()
                        if active_budget is not None:
                            active_budget.search_http_calls += 1
                        with self._lock:
                            self.http_attempts += 1
                        state['sent'] = True
                        with self._transport(request, timeout=timeout) as response:
                            body = response.read(MAX_BODY_BYTES + 1)
                            if self._stateful:
                                state['response_status'] = getattr(response, 'status', None)
                                if self._session_status == 'initializing':
                                    response_headers = getattr(response, 'headers', {})
                                    if hasattr(response_headers, 'get_all'):
                                        values = response_headers.get_all('Mcp-Session-Id') or []
                                    else:
                                        values = [v for k, v in response_headers.items()
                                                  if str(k).casefold() == 'mcp-session-id']
                                    if len(values) != 1:
                                        raise McpError('protocol', 'invalid session response header', server=self.config.name)
                                    state['response_session'] = _validate_session_id(values[0])
            except HTTPError as exc:
                status = int(exc.code)
                raw = b''
                try:
                    raw = exc.read(2048)
                except OSError:
                    pass
                finally:
                    try:
                        exc.close()
                    except OSError:
                        pass
                if status == 401 and message.get('method') == 'tools/call':
                    raise McpError('auth', 'tool invocation outcome unknown; automatic retry disabled',
                                   status=401, server=self.config.name) from None
                if (not self._stateful and status == 401 and attempt == 0 and provider is not None
                        and message.get('method') in ('initialize', 'ping', 'tools/list', 'prompts/list')):
                    rejected_token = token
                    continue
                detail = {}
                readable = raw.decode('utf-8', 'replace')
                for header, secret in headers.items():
                    if secret and header.lower() not in ('content-type', 'accept', 'user-agent'):
                        for candidate in (str(secret), str(secret).removeprefix('Bearer ').strip()):
                            if len(candidate) >= 4:
                                readable = readable.replace(candidate, '[redacted]')
                readable = redact_text(readable)
                raw = readable.encode('utf-8')
                if raw.strip():
                    try:
                        detail_messages = _parse_messages(raw, self.config.name)
                        detail = detail_messages[-1] if detail_messages else {}
                    except McpError:
                        detail = {}
                error = detail.get('error') if isinstance(detail, dict) else None
                valid_error = (isinstance(error, dict) and type(error.get('code')) is int
                               and isinstance(error.get('message'), str)
                               and detail.get('jsonrpc') == '2.0'
                               and type(detail.get('id')) is type(request_id)
                               and detail.get('id') == request_id and 'result' not in detail)
                exc = McpError(_kind_for_status(status), f'HTTP {status}; remote details withheld',
                               status=status, code=error['code'] if valid_error else None,
                               server=self.config.name)
                if valid_error:
                    exc.remote_outcome = 'error'
                raise exc from None
            except (socket.timeout, TimeoutError, TransportDeadlineExceeded):
                raise McpError('timeout', 'request deadline exceeded; remote outcome may be unknown',
                               server=self.config.name) from None
            except URLError as exc:
                reason = getattr(exc, 'reason', exc)
                kind = 'timeout' if isinstance(reason, (socket.timeout, TimeoutError)) else 'transport'
                raise McpError(kind, 'MCP transport failed; details withheld',
                               server=self.config.name) from None
            except (OSError, HTTPException):
                raise McpError('transport', 'MCP transport failed; details withheld',
                               server=self.config.name) from None
            if len(body) > MAX_BODY_BYTES:
                raise McpError('protocol', 'response exceeded 4 MB', server=self.config.name)
            if self._stateful and message.get('method') == 'notifications/initialized':
                if state.get('response_status') != 202 or body:
                    raise McpError('protocol', 'invalid initialization notification response', server=self.config.name)
                return {}
            if message.get('method') in ('tools/list', 'prompts/list'):
                if _discovery is not None:
                    _discovery['bytes'] = _discovery.get('bytes', 0) + len(body)
                    raw_size = _discovery['bytes']
                    if _discovery.get('identity') is None:
                        _discovery['identity'] = identity
                else:
                    raw_size = len(body)
                if raw_size > MAX_DISCOVERY_BYTES:
                    raise McpError('protocol', 'discovery limit exceeded', server=self.config.name)
            if self._stateful and state.get('response_status') != 200:
                raise McpError('protocol', 'unexpected session response status', server=self.config.name)
            messages = _parse_messages(body, self.config.name)
            if self._stateful:
                notifications = [item for item in messages if 'method' in item]
                if any(item.get('jsonrpc') != '2.0'
                       or item.get('method') != 'notifications/progress'
                       or 'id' in item or 'result' in item or 'error' in item
                       or not isinstance(item.get('params'), dict)
                       for item in notifications):
                    exc = McpError('protocol', 'unsupported server-initiated message', server=self.config.name)
                    responses = [item for item in messages if 'method' not in item
                                 and type(item.get('id')) is type(request_id) and item.get('id') == request_id]
                    if (message.get('method') == 'tools/call' and len(responses) == 1
                            and responses[0].get('jsonrpc') == '2.0' and 'error' not in responses[0]
                            and isinstance(responses[0].get('result'), dict)):
                        received = _redact_remote(responses[0]['result'], _header_secrets(headers))
                        exc.remote_outcome, exc.retry_safe = 'response_received', False
                        exc.mcp_result = McpResult(received, quota_finalization_failed=state['quota_finalization_failed'])
                    raise exc
                messages = [item for item in messages if 'method' not in item]
                if len(messages) != 1:
                    raise McpError('protocol', 'expected one session response', server=self.config.name)
            matches = [item for item in messages if type(item.get('id')) is type(request_id)
                       and item.get('id') == request_id]
            if len(matches) != 1:
                raise McpError('protocol',
                               f'no unique response for JSON-RPC id {request_id}',
                               server=self.config.name)
            original = matches[0]
            if message.get('method') in ('tools/list', 'prompts/list'):
                key = 'tools' if message['method'] == 'tools/list' else 'prompts'
                raw_result = original.get('result')
                if isinstance(raw_result, dict) and isinstance(raw_result.get(key), list):
                    if len(raw_result[key]) > MAX_DISCOVERY_ENTRIES:
                        raise McpError('protocol', 'discovery limit exceeded', server=self.config.name)
            secrets = _header_secrets(headers)
            if self._stateful and state.get('response_session'):
                secrets = (*secrets, state['response_session'])
            message_in = _redact_remote(original, secrets)
            if (message.get('method') in ('tools/list', 'prompts/list')
                    and isinstance(original.get('result'), dict)
                    and isinstance(message_in.get('result'), dict)
                    and 'nextCursor' in original['result']):
                message_in['result']['nextCursor'] = original['result']['nextCursor']
            if (message_in.get('jsonrpc') != '2.0'
                    or ('result' in message_in) == ('error' in message_in)):
                raise McpError('protocol', 'invalid JSON-RPC response', server=self.config.name)
            if 'error' in message_in:
                error = message_in['error']
                if (not isinstance(error, dict) or type(error.get('code')) is not int
                        or not isinstance(error.get('message'), str)):
                    raise McpError('protocol', 'invalid JSON-RPC error', server=self.config.name)
                exc = McpError('protocol', 'JSON-RPC error; remote details withheld',
                               code=error['code'], server=self.config.name)
                exc.remote_outcome = 'error'
                raise exc
            result = message_in.get('result')
            if not isinstance(result, dict):
                raise McpError('protocol', 'response has no object "result"', server=self.config.name)
            return result
        raise McpError('auth', 'authentication refresh failed', server=self.config.name)

    def _checked_tool_arguments(self, name: str, arguments: object, *, budget=None) -> dict:
        with self._lock:
            if not isinstance(name, str) or not name or not self.config.permits(name):
                raise McpError('client_error', 'tool is not in the server allow-list',
                               server=self.config.name)
            if self._discovery_invalid['tools']:
                raise McpError('client_error', 'tool discovery is invalid', server=self.config.name)
            generation = self._discovery_generation['tools']
            if self._tools is not None and name not in self._tool_schemas:
                raise McpError('client_error', 'tool was not advertised by the server',
                               server=self.config.name)
            local_schema = self._local_tool_schemas.get(name)
            remote_schema = self._tool_schemas.get(name)
        if not isinstance(local_schema, (dict, bool)):
            raise McpError('client_error', 'tool has no reviewed local input schema',
                           server=self.config.name)
        try:
            checked = validate_arguments({} if arguments is None else arguments)
            if isinstance(local_schema, (dict, bool)):
                validate_arguments(checked, local_schema)
            if isinstance(remote_schema, (dict, bool)):
                validate_remote_schema(checked, remote_schema, budget=budget)
            return checked, generation
        except (BudgetExceeded, RunCancelled):
            raise
        except ToolGovernanceError as exc:
            raise McpError('client_error', str(exc), server=self.config.name) from None

    def _checked_prompt_arguments(self, name: str, arguments: object, *, budget=None) -> dict:
        with self._lock:
            if not isinstance(name, str) or not name or not self.config.permits_prompt(name):
                raise McpError('client_error', 'prompt is not in the server allow-list',
                               server=self.config.name)
            if self._discovery_invalid['prompts']:
                raise McpError('client_error', 'prompt discovery is invalid', server=self.config.name)
            generation = self._discovery_generation['prompts']
            if self._prompts is not None and name not in self._prompt_schemas:
                raise McpError('client_error', 'prompt was not advertised by the server',
                               server=self.config.name)
            local_schema = self._local_prompt_schemas.get(name)
            remote_schema = self._prompt_schemas.get(name)
        if not isinstance(local_schema, (dict, bool)):
            raise McpError('client_error', 'prompt has no reviewed local input schema',
                           server=self.config.name)
        try:
            checked = validate_arguments({} if arguments is None else arguments)
            if isinstance(local_schema, (dict, bool)):
                validate_arguments(checked, local_schema)
            if isinstance(remote_schema, (dict, bool)):
                validate_remote_schema(checked, remote_schema, budget=budget)
            return checked, generation
        except (BudgetExceeded, RunCancelled):
            raise
        except ToolGovernanceError as exc:
            raise McpError('client_error', str(exc), server=self.config.name) from None

    def request(self, method: str, params: dict | None = None, *, budget=None, _before_send=None) -> dict:
        if _before_send is not None and (method != 'tools/call' or not callable(_before_send)):
            raise McpError('client_error', 'invalid local dispatch admission', server=self.config.name)
        supported = {'initialize', 'ping', 'tools/list', 'tools/call',
                     'prompts/list', 'prompts/get'}
        if not isinstance(method, str) or method not in supported:
            raise McpError('client_error', 'unsupported MCP request method',
                           server=self.config.name)
        active_budget = self.budget if budget is None else budget
        if active_budget is None and method in ('tools/list', 'prompts/list'):
            active_budget = Budget(30)
        if active_budget is not None:
            try:
                active_budget.check_cancelled()
            except (RunCancelled, BudgetExceeded) as exc:
                if method == 'tools/call':
                    exc.remote_outcome, exc.retry_safe = 'not_started', False
                raise
        if not isinstance(params, (dict, type(None))):
            exc = McpError('client_error', 'request parameters must be an object', server=self.config.name)
            if method == 'tools/call':
                exc.remote_outcome, exc.retry_safe = 'not_started', False
            raise exc
        params = dict(params or {})
        audit_name = None
        if method in ('tools/call', 'prompts/get'):
            name = params.get('name')
            if method == 'tools/call':
                try:
                    checked, generation = self._checked_tool_arguments(name, params.get('arguments'), budget=active_budget)
                except (McpError, RunCancelled, BudgetExceeded) as exc:
                    exc.remote_outcome, exc.retry_safe = 'not_started', False
                    raise
            else:
                checked, generation = self._checked_prompt_arguments(name, params.get('arguments'), budget=active_budget)
            if set(params) - {'name', 'arguments'}:
                exc = McpError('client_error', 'unsupported invocation parameter', server=self.config.name)
                if method == 'tools/call':
                    exc.remote_outcome, exc.retry_safe = 'not_started', False
                raise exc
            params = {'name': name, 'arguments': checked}
            audit_name = 'mcp.' + ('prompt.' if method == 'prompts/get' else '') + name
        else:
            try:
                params = validate_arguments(params)
            except ToolGovernanceError as exc:
                raise McpError('client_error', str(exc), server=self.config.name) from None
            expected = ({'protocolVersion': self.config.protocol, 'capabilities': {},
                         'clientInfo': {'name': 'scholarguide', 'version': '0.1'}}
                        if method == 'initialize' else {})
            if params != expected:
                raise McpError('client_error', 'unsupported protocol parameters', server=self.config.name)
            audit_name = 'mcp.protocol.' + method.replace('/', '.')
        raw_key = ('tools' if method == 'tools/list' else
                   'prompts' if method == 'prompts/list' else None)
        with self._lock:
            if raw_key is not None:
                self._discovery_generation[raw_key] += 1
                raw_generation = self._discovery_generation[raw_key]
                self._discovery_invalid[raw_key] = True
            self._next_id += 1
            request_id = self._next_id
            self.calls += 1
            if method == 'tools/call':
                self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        message = {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}
        with operation(audit_name, source=self.config.name, category='mcp',
                       arguments=audit_arguments(params.get('arguments', params))):
            if method in ('tools/call', 'prompts/get'):
                key = 'tools' if method == 'tools/call' else 'prompts'
                def admission():
                    with self._lock:
                        if self._discovery_invalid[key] or self._discovery_generation[key] != generation:
                            raise McpError('client_error', 'discovery changed before dispatch', server=self.config.name)
                result = self._post(message, budget=active_budget, _before_send=_before_send,
                                    _admission=admission)
            else:
                result = self._post(message, budget=active_budget)
            if method in ('tools/list', 'prompts/list'):
                self._check_discovery_page(result, 'tools' if method == 'tools/list' else 'prompts')
                if 'nextCursor' in result:
                    raise McpError('protocol', 'discovery requires explicit pagination', server=self.config.name)
                key = 'tools' if method == 'tools/list' else 'prompts'
                permit = self.config.permits if method == 'tools/list' else self.config.permits_prompt
                items = result.get(key)
                if not isinstance(items, list):
                    raise McpError('protocol', 'discovery returned no list', server=self.config.name)
                items = deepcopy([item for item in items if isinstance(item, dict)
                                  and isinstance(item.get('name'), str) and item['name']
                                  and permit(item['name'])])
                if active_budget is not None:
                    active_budget.check_cancelled()
                    active_budget.remaining()
                self._publish_discovery(key, items, raw_generation)
                result = {**result, key: deepcopy(items)}
            return result

    def _publish_discovery(self, key, entries, generation):
        schemas = {}
        for entry in entries:
            name = entry['name']
            if name in schemas:
                raise McpError('protocol', 'duplicate discovery name', server=self.config.name)
            if key == 'tools':
                value = entry.get('inputSchema')
                schemas[name] = value if isinstance(value, (dict, bool)) else None
            else:
                arguments = entry.get('arguments') or []
                if not isinstance(arguments, list):
                    raise McpError('protocol', 'invalid prompt arguments', server=self.config.name)
                names = [item['name'] for item in arguments if isinstance(item, dict)
                         and isinstance(item.get('name'), str) and item['name']]
                if len(names) != len(set(names)):
                    raise McpError('protocol', 'duplicate prompt argument', server=self.config.name)
                schemas[name] = {'type': 'object',
                    'properties': {argument: {'type': 'string'} for argument in names},
                    'required': [item['name'] for item in arguments if isinstance(item, dict)
                                 and isinstance(item.get('name'), str) and item['name']
                                 and item.get('required') is True],
                    'additionalProperties': False}
        with self._lock:
            if generation != self._discovery_generation[key]:
                raise McpError('client_error', 'discovery superseded', server=self.config.name)
            if key == 'tools':
                self._tools, self._tool_schemas = entries, schemas
            else:
                self._prompts, self._prompt_schemas = entries, schemas
            self._discovery_invalid[key] = False

    def _check_discovery_page(self, result, key):
        try:
            size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        except (TypeError, ValueError, RecursionError):
            raise McpError('protocol', 'invalid discovery response', server=self.config.name) from None
        items = result.get(key)
        if size > MAX_DISCOVERY_BYTES or (isinstance(items, list) and len(items) > MAX_DISCOVERY_ENTRIES):
            raise McpError('protocol', 'discovery limit exceeded', server=self.config.name)
        return size

    def _discover(self, method: str, key: str, paginate: bool, budget) -> list[dict]:
        if type(paginate) is not bool:
            raise McpError('client_error', 'invalid pagination option', server=self.config.name)
        entries, seen, size = [], set(), 0
        identity = {} if paginate else None
        cursor = None
        permit = self.config.permits if key == 'tools' else self.config.permits_prompt
        for page in range(MAX_DISCOVERY_PAGES):
            if budget is not None:
                budget.check_cancelled()
            with self._lock:
                self._next_id += 1
                request_id = self._next_id
                self.calls += 1
            params = {} if cursor is None else {'cursor': cursor}
            message = {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}
            with operation('mcp.protocol.' + method.replace('/', '.'), source=self.config.name,
                           category='mcp', arguments=audit_arguments({})):
                if identity is None:
                    result = self._post(message, budget=budget)
                else:
                    result = self._post(message, budget=budget, _discovery=identity)
            items = result.get(key)
            if not isinstance(items, list):
                raise McpError('protocol', 'discovery returned no list', server=self.config.name)
            size += self._check_discovery_page(result, key)
            if size > MAX_DISCOVERY_BYTES or len(items) + len(entries) > MAX_DISCOVERY_ENTRIES:
                raise McpError('protocol', 'discovery limit exceeded', server=self.config.name)
            entries.extend(items)
            if 'nextCursor' not in result:
                if budget is not None:
                    budget.check_cancelled()
                    budget.remaining()
                public_entries = deepcopy([item for item in entries if isinstance(item, dict)
                                           and isinstance(item.get('name'), str) and item['name']
                                           and permit(item['name'])])
                if seen and _contains_cursor(public_entries, seen):
                    raise McpError('protocol', 'discovery metadata contains cursor', server=self.config.name)
                return public_entries
            cursor = result['nextCursor']
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise McpError('protocol', 'invalid discovery cursor', server=self.config.name)
            if not paginate:
                raise McpError('protocol', 'discovery requires explicit pagination', server=self.config.name)
            seen.add(cursor)
        raise McpError('protocol', 'discovery page limit exceeded', server=self.config.name)

    # ------------------------------------------------------------------- tools
    def initialize(self) -> dict:
        """Optional for stateless servers; safe to skip and safe to call twice."""
        return self.request('initialize', {
            'protocolVersion': self.config.protocol,
            'capabilities': {},
            'clientInfo': {'name': 'scholarguide', 'version': '0.1'}})

    def list_tools(self, *, refresh: bool = False, paginate: bool = False, budget=None) -> list[dict]:
        with self._lock:
            needs_fetch = self._tools is None or refresh
            active_budget = self.budget if budget is None else budget
            if needs_fetch:
                self._discovery_generation['tools'] += 1
                generation = self._discovery_generation['tools']
                self._discovery_invalid['tools'] = True
        if needs_fetch:
            active_budget = active_budget if active_budget is not None else Budget(30)
            tools = self._discover('tools/list', 'tools', paginate, active_budget)
            if active_budget is not None:
                active_budget.check_cancelled()
                active_budget.remaining()
            self._publish_discovery('tools', tools, generation)
        with self._lock:
            if self._discovery_invalid['tools']:
                raise McpError('client_error', 'tool discovery is invalid', server=self.config.name)
            return deepcopy([tool for tool in self._tools
                             if self.config.permits(str(tool.get('name') or ''))])

    def tool_names(self, *, refresh: bool = False) -> list[str]:
        return [tool['name'] for tool in self.list_tools(refresh=refresh)
                if self.config.permits(str(tool.get('name') or ''))]

    def call_tool(self, name: str, arguments: dict | None = None, *, budget=None, _before_send=None) -> dict:
        return self.request('tools/call', {'name': name, 'arguments': arguments},
                            budget=budget, _before_send=_before_send)

    def call(self, tool: str, arguments: dict | None = None, *, budget=None) -> dict:
        """Call one of the four stable product aliases."""
        alias = str(tool).strip().lower()
        name = CALL_ALIASES.get(alias)
        if name is None:
            raise McpError('client_error', 'unsupported MCP call alias', server=self.config.name)
        return self.call_tool(name, arguments, budget=budget)

    # ------------------------------------------------------------------ prompts
    def list_prompts(self, *, refresh: bool = False, paginate: bool = False, budget=None) -> list[dict]:
        with self._lock:
            needs_fetch = self._prompts is None or refresh
            active_budget = self.budget if budget is None else budget
            if needs_fetch:
                self._discovery_generation['prompts'] += 1
                generation = self._discovery_generation['prompts']
                self._discovery_invalid['prompts'] = True
        if needs_fetch:
            active_budget = active_budget if active_budget is not None else Budget(30)
            prompts = self._discover('prompts/list', 'prompts', paginate, active_budget)
            if active_budget is not None:
                active_budget.check_cancelled()
                active_budget.remaining()
            self._publish_discovery('prompts', prompts, generation)
        with self._lock:
            if self._discovery_invalid['prompts']:
                raise McpError('client_error', 'prompt discovery is invalid', server=self.config.name)
            return deepcopy([prompt for prompt in self._prompts or []
                             if self.config.permits_prompt(str(prompt['name']))])

    def get_prompt(self, name: str, arguments: dict | None = None, *, budget=None) -> dict:
        return self.request('prompts/get', {'name': name, 'arguments': arguments}, budget=budget)

    # -------------------------------------------------------------------- usage
    def usage(self) -> dict:
        return {'server': self.config.name, 'requests': self.calls,
                'logical_requests': self.calls, 'http_attempts': self.http_attempts,
                'by_tool': dict(self.calls_by_tool)}


def _content_text(result: dict) -> str:
    """Flatten the text blocks of a tools/call result into one string."""
    parts = []
    for block in result.get('content') or []:
        if isinstance(block, dict) and block.get('type') == 'text':
            parts.append(str(block.get('text') or ''))
    return '\n'.join(part for part in parts if part).strip()


def structured(result: dict) -> dict:
    """Return a tool result as a dict: structuredContent when present, else JSON text."""
    if isinstance(result.get('structuredContent'), dict):
        return result['structuredContent']
    text = _content_text(result)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {'text': text}
    return parsed if isinstance(parsed, dict) else {'text': text}
