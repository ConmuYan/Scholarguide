"""Explicit, transient diagnostics for the fixed official OpenAI OAuth profile.

Only the documented models catalog and JsonLLM's official Responses transport
are used. Provider-controlled errors, identity fields and credentials never form
part of the public result. Nothing is saved or activated here.
"""
from __future__ import annotations

from dataclasses import replace
import json
import threading
import time
import unicodedata
from urllib.error import HTTPError
from urllib.request import Request

from scholarguide.common import Budget, Config, GuideError, utc_now
from scholarguide.llm import JsonLLM
from scholarguide import network
from scholarguide import openai_oauth

PROFILE_ID = 'openai-oauth'
BASE_URL = 'https://api.openai.com/v1'
MODELS_URL = BASE_URL + '/models'
MANAGE_URL = 'https://chatgpt.com/settings/usage'
TOTAL_SECONDS = 45.0
CATALOG_SECONDS = 15.0
# Account catalogs include model instructions/capabilities, not just slugs.
# The live official catalog is ~650 KB; expose only names but bound the input
# below the shared transport's 4 MB limit.
MAX_CATALOG_BYTES = 2_000_000
MAX_MODELS = 1000


class OAuthDiagnosticsBusy(GuideError):
    pass


def _safe_string(value):
    return (isinstance(value, str) and 0 < len(value) <= 200
            and bool(value.strip())
            and not any(unicodedata.category(ch).startswith('C') for ch in value))


def _valid_model(value):
    return _safe_string(value) and not any(ch.isspace() for ch in value)


def _http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


class OpenAIOAuthDiagnostics:
    """One nonblocking single-flight service per local server; dependencies injectable."""

    def __init__(self, providers, *, opener=None, llm_factory=None, config_factory=None):
        self.providers = providers
        self._opener = opener
        self._llm_factory = llm_factory
        self._config_factory = config_factory
        self._lock = threading.Lock()

    def test(self, raw):
        if not isinstance(raw, dict) or set(raw) - {'model'}:
            raise GuideError('OAuth diagnostics accepts only an optional model string.')
        if 'model' in raw and not _valid_model(raw['model']):
            raise GuideError('Model must be a nonempty slug of at most 200 characters without whitespace or controls.')
        if not self._lock.acquire(blocking=False):
            raise OAuthDiagnosticsBusy('OpenAI OAuth diagnostics is already in progress.')
        try:
            started = time.monotonic()
            deadline = started + TOTAL_SECONDS
            profile = self.providers.get(PROFILE_ID)
            if profile is None:
                raise GuideError('Save the official OpenAI OAuth profile before testing.')
            if (profile.id != PROFILE_ID or profile.base_url != BASE_URL
                    or profile.protocol != 'responses' or profile.api_key):
                raise GuideError('The reserved openai-oauth profile conflicts with the official OAuth profile.')
            model = raw.get('model', profile.model)
            if not _valid_model(model):
                raise GuideError('The saved OAuth model is invalid; choose a valid model slug.')
            try:
                base = (self._config_factory or Config.from_env)()
                config = profile.config(base)
                config = replace(config, model=model, max_output_tokens=256,
                                 request_timeout=min(config.request_timeout, 30.0))
            except Exception:
                raise GuideError('OpenAI OAuth diagnostic configuration is unavailable.') from None
            # The transport scope also caps DNS, reads and any structured repair
            # at the same absolute deadline. Catalog failure leaves probe time.
            with network.execution_context(timeout_s=max(0, deadline - time.monotonic())):
                models, catalog = self._catalog(min(deadline, time.monotonic() + CATALOG_SECONDS))
                probe = self._probe(config, deadline)
            return {'checked_at': utc_now(), 'models': models, 'catalog': catalog,
                    'probe': probe,
                    'account_usage': {'available': False, 'manage_url': MANAGE_URL}}
        finally:
            self._lock.release()

    def _catalog(self, deadline):
        status = None
        try:
            cancelled = lambda: time.monotonic() >= deadline
            token = openai_oauth.access_token(deadline=deadline, cancelled=cancelled)
            proxy = openai_oauth.proxy_url(deadline=deadline, cancelled=cancelled)
            if (not isinstance(token, str) or not token
                    or any(ord(ch) < 33 or ord(ch) > 126 for ch in token)):
                raise ValueError('invalid token')
            request = Request(MODELS_URL, headers={'Authorization': 'Bearer ' + token,
                                                   'Accept': 'application/json'}, method='GET')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with network.execution_context(timeout_s=remaining, cancelled=cancelled):
                with (self._opener or network.urlopen)(
                        request, timeout=remaining, reject_redirects=True, model_proxy=proxy) as response:
                    status = _http_status(response.status)
                    body = response.read(MAX_CATALOG_BYTES + 1)
            if status != 200 or len(body) > MAX_CATALOG_BYTES or time.monotonic() >= deadline:
                raise ValueError('invalid catalog response')
            value = json.loads(body)
            rows = value.get('models') if isinstance(value, dict) else None
            if not isinstance(rows, list) or len(rows) > MAX_MODELS:
                raise ValueError('invalid catalog')
            models = []
            for row in rows:
                if not isinstance(row, dict) or row.get('visibility') != 'list':
                    continue
                slug, name = row.get('slug'), row.get('display_name')
                if _valid_model(slug) and _safe_string(name):
                    models.append({'slug': slug, 'display_name': name})
            return models, {'ok': True, 'http_status': status}
        except HTTPError as exc:
            status = _http_status(exc.code)
            exc.close()
        except Exception:
            pass
        return [], {'ok': False, 'http_status': status,
                    'error': 'OpenAI OAuth model catalog could not be retrieved.'}

    def _probe(self, config, deadline):
        started = time.monotonic()
        budget = Budget(max(0, deadline - started), started=started)
        llm = None
        ok = False
        try:
            budget.remaining()
            llm = (self._llm_factory or JsonLLM)(config, budget)
            schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                      'required': ['ok'], 'additionalProperties': False}

            def validate(value):
                if not isinstance(value, dict) or set(value) != {'ok'} or value['ok'] is not True:
                    raise GuideError('Probe must return exactly {"ok": true}.')

            llm.generate('probe', 'Return exactly {"ok": true} as JSON.', {}, schema, validate)
            budget.remaining()
            ok = True
        except Exception:
            pass
        # Include known usage even for failed/incomplete SSE responses; never
        # mistake missing usage for a free request or expose model output.
        metrics = budget.metrics()
        # A repair call can lack usage even when the first call reported it.
        # Only present a total when every dispatched model call is accounted for.
        usage = {key: metrics.get(key) for key in ('input_tokens', 'output_tokens')}
        result = {'ok': ok, 'model': config.model,
                  'usage_complete': metrics.get('usage_complete', False),
                  'model_calls': budget.llm_calls,
                  'http_status': _http_status(getattr(llm, 'last_http_status', None)),
                  'latency_ms': max(0, int((time.monotonic() - started) * 1000)),
                  'usage': usage}
        if not ok:
            result['error'] = 'OpenAI OAuth model probe failed.'
        return result
