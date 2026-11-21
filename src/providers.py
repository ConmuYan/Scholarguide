"""Local, server-side provider profiles for replaceable LLM endpoints.

Profiles are deliberately not stored in result.json, SQLite event payloads or the
browser. The JSON file contains the key on the local machine (0600 where the OS
supports it); API responses expose only a masked hint.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from common import Budget, Config, GuideError
from costing import cost_breakdown


PROTOCOLS = ('chat_completions', 'responses')
ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
CURRENCY_RE = re.compile(r'^[A-Za-z]{3}$')


def _synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


def default_store_path() -> Path:
    value = os.getenv('SG_PROVIDER_STORE', '').strip()
    if value:
        return Path(value).expanduser()
    return Path.home() / '.scholarguide' / 'providers.json'


def mask_key(value: str) -> str:
    value = value or ''
    if not value:
        return ''
    return '••••' + value[-4:] if len(value) > 4 else '••••'


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    protocol: str
    base_url: str
    model: str
    api_key: str = field(default='', repr=False)
    input_price: float | None = None
    output_price: float | None = None
    max_output_tokens: int = 3000
    currency: str = 'CNY'
    source: str = 'custom'

    def public(self) -> dict[str, Any]:
        return {'id': self.id, 'name': self.name, 'protocol': self.protocol,
                'base_url': self.base_url, 'model': self.model,
                'input_price': self.input_price, 'output_price': self.output_price,
                'max_output_tokens': self.max_output_tokens,
                'currency': self.currency, 'source': self.source,
                'has_key': bool(self.api_key), 'masked_key': mask_key(self.api_key)}

    def config(self, base: Config | None = None) -> Config:
        base = base or Config.from_env()
        if self.source == 'env':
            config = replace(base, protocol=self.protocol, profile_id='env', profile_name=self.name,
                             input_price=self.input_price, output_price=self.output_price,
                             max_output_tokens=self.max_output_tokens,
                             currency=self.currency, api_key=self.api_key)
        else:
            config = replace(base, provider='openai', base_url=self.base_url, model=self.model,
                             api_key=self.api_key, protocol=self.protocol, profile_id=self.id,
                             profile_name=self.name,
                             input_price=self.input_price, output_price=self.output_price,
                             max_output_tokens=self.max_output_tokens,
                             currency=self.currency)
        config._validate_action_protocol()
        return config


def _env_base() -> Config:
    try:
        return Config.from_env()
    except GuideError:
        return Config()


def _env_profile(base: Config | None = None) -> ProviderProfile:
    base = base or _env_base()
    return ProviderProfile('env', 'Environment / default', base.protocol, base.base_url,
                           base.model, base.api_key, base.input_price, base.output_price,
                           base.max_output_tokens, base.currency, 'env')


def _price(value: Any, label: str) -> float | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise GuideError(f'{label} must be a non-negative number.')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise GuideError(f'{label} must be a non-negative number.') from None
    if not math.isfinite(number) or number < 0:
        raise GuideError(f'{label} must be finite and non-negative.')
    return number


def validate_profile(data: dict[str, Any], *, existing: ProviderProfile | None = None) -> ProviderProfile:
    if not isinstance(data, dict):
        raise GuideError('Profile must be an object.')
    name = str(data.get('name') or '').strip()
    if not name or len(name) > 80:
        raise GuideError('Profile name must be 1–80 characters.')
    protocol = str(data.get('protocol') or 'chat_completions').strip().lower()
    if protocol not in PROTOCOLS:
        raise GuideError('Protocol must be chat_completions or responses.')
    base_url = str(data.get('base_url') or '').strip().rstrip('/')
    if any(ch.isspace() or ord(ch) < 32 for ch in base_url):
        raise GuideError('Base URL must not contain whitespace or control characters.')
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise GuideError('Base URL is not a valid URL.') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or port == 0 or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise GuideError('Base URL must be HTTP(S), with a host and no credentials/query/fragment.')
    model = str(data.get('model') or '').strip()
    if not model or len(model) > 200:
        raise GuideError('Model must be 1–200 characters.')
    currency = str(data.get('currency') or 'CNY').strip().upper()
    if not CURRENCY_RE.fullmatch(currency):
        raise GuideError('Currency must be a 3-letter code.')
    input_price = _price(data.get('input_price'), 'Input price')
    output_price = _price(data.get('output_price'), 'Output price')
    raw_tokens = data.get('max_output_tokens')
    if raw_tokens in (None, ''):
        raw_tokens = 3000
    try:
        if isinstance(raw_tokens, bool) or not re.fullmatch(r'[0-9]+', str(raw_tokens)):
            raise ValueError
        max_output_tokens = int(raw_tokens)
    except (TypeError, ValueError, OverflowError):
        raise GuideError('Max output tokens must be an integer.') from None
    if not 256 <= max_output_tokens <= 100000:
        raise GuideError('Max output tokens must be between 256 and 100000.')
    profile_id = str(data.get('id') or (existing.id if existing else '')).strip().lower()
    if not profile_id:
        profile_id = 'p-' + re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:18]
        profile_id = (profile_id + '-' + uuid.uuid4().hex[:8])[:32]
    if profile_id == 'env':
        raise GuideError('The environment profile is read-only; use a custom profile id.')
    if not ID_RE.fullmatch(profile_id):
        raise GuideError('Profile id must be a short lowercase slug.')
    key = data.get('api_key')
    if key is None or key == '':
        key = existing.api_key if existing else ''
    elif not isinstance(key, str) or len(key) > 4096:
        raise GuideError('API key is invalid.')
    elif any(ord(ch) < 32 or ord(ch) == 127 for ch in key):
        raise GuideError('API key must not contain control characters.')
    elif key.startswith('••••'):
        # A browser may echo the masked hint; never replace the stored secret with it.
        key = existing.api_key if existing else ''
    source = 'custom'
    return ProviderProfile(profile_id, name, protocol, base_url, model, key,
                           input_price, output_price, max_output_tokens, currency, source)


class ProviderStore:
    def __init__(self, path: str | Path | None = None):
        self._lock = threading.RLock()
        self.path = Path(path).expanduser() if path else default_store_path()
        self._profiles: list[ProviderProfile] = []
        self._active = 'env'
        self._load()

    def _load(self) -> None:
        raw: dict[str, Any] = {}
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            raw = value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            raw = {}
        rows = raw.get('profiles') if isinstance(raw.get('profiles'), list) else []
        loaded = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            try:
                loaded.append(validate_profile(item, existing=None))
            except GuideError:
                continue
        self._profiles = loaded
        active = str(raw.get('active') or 'env')
        if active in {p.id for p in loaded}:
            self._active = active
        elif 'env' in {p.id for p in loaded}:
            self._active = 'env'
        else:
            self._active = 'env'

    def _write(self, profiles: list[ProviderProfile], active: str) -> None:
        payload = {'version': 1, 'active': active,
                   'profiles': [{**asdict(p), 'api_key': p.api_key}
                                for p in profiles if p.source == 'custom']}
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=self.path.name + '-', suffix='.tmp',
                                        dir=self.path.parent)
            tmp = Path(name)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                self._restrict(tmp)
                stream.write(json.dumps(payload, ensure_ascii=False, indent=2,
                                        allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(self.path)
        except OSError:
            raise GuideError('Provider settings could not be saved. Check local disk permissions and try again.') from None
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass  # Preserve the original storage error if cleanup is denied.

    @_synchronized
    def profiles(self) -> list[ProviderProfile]:
        return [_env_profile(), *self._profiles]

    @_synchronized
    def get(self, profile_id: str) -> ProviderProfile | None:
        profile_id = (profile_id or '').lower()
        if profile_id == 'env':
            return _env_profile()
        return next((p for p in self._profiles if p.id == profile_id), None)

    @_synchronized
    def active(self) -> ProviderProfile:
        return self.get(self._active) or _env_profile()

    @staticmethod
    def _restrict(path: Path) -> None:
        """Best-effort local-only permissions; Windows chmod alone is not enough."""
        try:
            if os.name == 'nt':
                user = os.getenv('USERNAME') or ''
                if user:
                    subprocess.run(['icacls', str(path), '/inheritance:r',
                                    '/grant:r', f'{user}:F'], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            os.chmod(path, 0o600)
        except (OSError, subprocess.SubprocessError):
            pass

    @_synchronized
    def public_state(self) -> dict[str, Any]:
        return {'active': self.active().id,
                'profiles': [p.public() for p in self.profiles()]}

    @_synchronized
    def config(self, base: Config | None = None) -> Config:
        return self.active().config(base)

    @_synchronized
    def upsert(self, data: dict[str, Any], *, activate: bool = False) -> ProviderProfile:
        existing = self.get(str(data.get('id') or '')) if data.get('id') else None
        profile = validate_profile(data, existing=existing if existing and existing.source == 'custom' else None)
        if profile.source == 'env':
            raise GuideError('The environment profile is read-only; create a custom profile.')
        updated = [profile if item.id == profile.id else item for item in self._profiles]
        if not any(item.id == profile.id for item in self._profiles):
            updated.append(profile)
        # Saving a profile is not activation. The settings checkbox (or an
        # explicit activate call) owns that decision; env remains the fallback.
        active = profile.id if activate else self._active
        self._write(updated, active)
        self._profiles, self._active = updated, active
        return profile

    @_synchronized
    def activate(self, profile_id: str) -> ProviderProfile:
        profile = self.get(profile_id)
        if profile is None:
            raise GuideError('Provider profile not found.')
        self._write(self._profiles, profile.id)
        self._active = profile.id
        return profile

    @_synchronized
    def delete(self, profile_id: str) -> None:
        profile = self.get(profile_id)
        if profile is None:
            raise GuideError('Provider profile not found.')
        if profile.source == 'env':
            raise GuideError('The environment profile cannot be deleted.')
        updated = [p for p in self._profiles if p.id != profile_id]
        active = self._active
        if active == profile_id:
            active = updated[0].id if updated else 'env'
        self._write(updated, active)
        self._profiles, self._active = updated, active

    def test(self, data: dict[str, Any]) -> dict[str, Any]:
        existing = self.get(str(data.get('id') or '')) if data.get('id') else None
        if existing is not None and existing.source == 'env':
            # Test the env fallback without letting the read-only profile be saved.
            test_data = dict(data)
            test_data['id'] = 'env-test'
            test_data['api_key'] = test_data.get('api_key') or existing.api_key
            profile = validate_profile(test_data, existing=None)
        else:
            profile = validate_profile(data, existing=existing if existing and existing.source == 'custom' else None)
        config = profile.config()
        config = replace(config, request_timeout=min(max(config.request_timeout, 1), 30),
                         source_timeout=min(max(config.source_timeout, 1), 30))
        started = time.monotonic()
        try:
            from llm import JsonLLM
            budget = Budget(min(config.request_timeout, 30))
            llm = JsonLLM(config, budget)
            schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                      'required': ['ok'], 'additionalProperties': False}
            def validate(value):
                if not isinstance(value, dict) or value.get('ok') is not True:
                    raise GuideError('Probe response did not contain {"ok": true}.')
            llm.generate('probe', 'Return exactly {"ok": true} as JSON.', {'probe': True},
                         schema, validate)
            elapsed = int((time.monotonic() - started) * 1000)
            metrics = budget.metrics()
            cost = cost_breakdown(config, metrics, [])
            return {'ok': True, 'status': 'ok', 'http_status': llm.last_http_status,
                    'model': llm.last_model, 'protocol': config.protocol,
                    'profile_id': profile.id, 'latency_ms': elapsed,
                    'usage': {'input_tokens': metrics.get('input_tokens'),
                              'output_tokens': metrics.get('output_tokens')}, 'cost': cost,
                    'message': 'Model endpoint is reachable and returned structured JSON.'}
        except GuideError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            metrics = budget.metrics()
            message = str(exc)
            lower = message.lower()
            if 'http 401' in lower or 'http 403' in lower:
                status = 'auth'
            elif 'http 404' in lower:
                status = 'model_or_endpoint'
            elif 'timed out' in lower or 'timeout' in lower:
                status = 'timeout'
            elif 'connection refused' in lower or 'refused' in lower:
                status = 'connection'
            elif 'invalid json' in lower or 'unexpected response' in lower or 'empty' in lower:
                status = 'protocol'
            elif ('http 400' in lower or 'http 422' in lower) and any(
                    word in lower for word in ('schema', 'json_schema', 'response_format',
                                               'text.format', 'strict', 'parameter',
                                               'required')):
                # The provider rejected the request shape, not the credential.
                status = 'protocol'
            else:
                status = 'error'
            return {'ok': False, 'status': status, 'http_status': getattr(locals().get('llm', None), 'last_http_status', None),
                    'model': getattr(locals().get('llm', None), 'last_model', None),
                    'protocol': config.protocol, 'profile_id': profile.id,
                    'latency_ms': elapsed, 'error': message[:400],
                    'usage': {'input_tokens': metrics.get('input_tokens'),
                              'output_tokens': metrics.get('output_tokens')},
                    'cost': cost_breakdown(config, metrics, []),
                    'message': 'Model endpoint test failed.'}
        except Exception as exc:  # noqa: BLE001 - probe must never crash settings
            return {'ok': False, 'status': 'error', 'protocol': config.protocol,
                    'profile_id': profile.id,
                    'latency_ms': int((time.monotonic() - started) * 1000),
                    'error': f'{type(exc).__name__}', 'message': 'Model endpoint test failed.'}
