"""Single-user OIDC authorization code client, deliberately fail-closed.

Credentials and the stable host identifier are stored in an approved OS keyring.
Refresh and login are serialized across processes. Before a refresh request the
record is durably marked pending; an uncertain outcome requires reauthorization
instead of risking a replay of a rotated refresh token.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from scholarguide.common import GuideError, RunCancelled

ISSUER = 'https://auth.openai.com'
DISCOVERY = ISSUER + '/.well-known/openid-configuration'
SERVICE = 'scholarguide.openai.oauth'
ACCOUNT = 'single-user'
HOST_ACCOUNT = 'host-id'
PROXY_ACCOUNT = 'local-proxy'
PENDING_CLIENT_ACCOUNT = 'pending-client-id'
DYNAMIC_CLIENT = 'dynamic_agent_client'
RESOURCE = 'https://api.openai.com/v1'
TOKEN_ENDPOINT = ISSUER + '/api/accounts/oauth/token'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
_MAX_BODY = 128_000


def _service():
    # Keyring identity and cross-process lock must have the same canonical
    # home-directory scope, even when HOME or directory aliases differ.
    home = os.path.normcase(str(Path.home().resolve()))
    return SERVICE + '.' + hashlib.sha256(home.encode('utf-8')).hexdigest()


class _AccessDenied(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _validated_proxy(value):
    if not value:
        return ''
    parsed = urlsplit(value)
    try:
        valid = (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
                 and parsed.port is not None and parsed.port > 0 and not parsed.username
                 and not parsed.password and parsed.path in ('', '/') and not parsed.query
                 and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise GuideError('OpenAI OAuth proxy must be an HTTP loopback URL without credentials.')
    return value.rstrip('/')


def configure_system_proxy():
    """Explicitly save the current loopback HTTPS proxy for OpenAI OAuth only."""
    value = os.getenv('HTTPS_PROXY') or os.getenv('https_proxy') or ''
    proxy = _validated_proxy(value)
    if not proxy:
        raise GuideError('No system HTTPS proxy is configured.')
    try:
        store = _keyring()
        store.set_password(_service(), PROXY_ACCOUNT, proxy)
        if store.get_password(_service(), PROXY_ACCOUNT) != proxy:
            raise ValueError('proxy readback failed')
    except Exception:
        raise GuideError('Could not save the OpenAI OAuth proxy configuration.') from None


def _cancel_requested(cancelled):
    return bool(cancelled is not None and cancelled())


def _remaining(deadline):
    """Seconds left before an absolute time.monotonic() deadline, or None."""
    if deadline is None:
        return None
    try:
        left = float(deadline) - time.monotonic()
    except (TypeError, ValueError):
        raise GuideError('Invalid OpenAI OAuth deadline.') from None
    if not math.isfinite(left):
        raise GuideError('Invalid OpenAI OAuth deadline.')
    return left


def _check_stop(deadline, cancelled, what='token acquisition'):
    """Honor a user Stop or an exhausted absolute deadline before any new work."""
    if _cancel_requested(cancelled):
        raise RunCancelled()
    left = _remaining(deadline)
    if left is not None and left <= 0:
        raise GuideError(f'OpenAI OAuth {what} exceeded its deadline; no further request was started.')


def _bounded_local(call, *, deadline, cancelled):
    """Run a local blocking call (keyring/file inspection) within the deadline.

    A broken OS keyring can block indefinitely. The caller's absolute deadline
    and Stop are polled while the call runs, so an unattended read cannot pin a
    task past its budget. Only reads use this helper: credential writes stay
    synchronous so an interrupted write never loses refresh_pending intent.
    """
    if deadline is None and cancelled is None:
        return call()
    _check_stop(deadline, cancelled)
    box = {}

    def worker():
        try:
            box['value'] = call()
        except BaseException as exc:  # Local backend failure reaches the caller.
            box['error'] = exc

    thread = threading.Thread(target=worker, name='OpenAI OAuth local call', daemon=True)
    thread.start()
    while thread.is_alive():
        left = _remaining(deadline)
        _check_stop(deadline, cancelled)
        thread.join(0.05 if left is None else min(0.05, max(0.0, left)))
    if 'error' in box:
        raise box['error']
    return box.get('value')


def proxy_url(deadline=None, cancelled=None):
    """Return only an explicitly authorized, validated local proxy or direct mode."""
    try:
        value = _bounded_local(lambda: _store().get_password(_service(), PROXY_ACCOUNT) or '',
                               deadline=deadline, cancelled=cancelled)
        return _validated_proxy(value)
    except RunCancelled:
        raise
    except Exception:
        raise GuideError('OpenAI OAuth proxy configuration is unavailable.') from None


def _request(url, data=None, json_body=False, *, deadline=None, cancelled=None):
    left = _remaining(deadline)
    if _cancel_requested(cancelled):
        raise RunCancelled()
    if left is not None and left <= 0:
        raise GuideError('OpenAI OAuth request deadline exceeded; no request was sent.')
    timeout = 15.0 if left is None else min(15.0, left)
    try:
        headers = {'Accept': 'application/json'}
        body = None
        if data is not None:
            headers['Content-Type'] = 'application/json' if json_body else 'application/x-www-form-urlencoded'
            body = (json.dumps(data).encode() if json_body else urlencode(data).encode())
        req = Request(url, data=body, headers=headers)
        proxy = proxy_url(deadline=deadline, cancelled=cancelled)
        with build_opener(ProxyHandler({'https': proxy} if proxy else {}), _NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(_MAX_BODY + 1)
        if len(raw) > _MAX_BODY:
            raise ValueError('oversize response')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('invalid response')
        left = _remaining(deadline)
        if left is not None and left <= 0:
            raise GuideError('OpenAI OAuth request deadline exceeded.')
        return value
    except GuideError:
        raise
    except HTTPError as exc:
        code = ''
        try:
            payload = json.loads(exc.read(4096).decode('utf-8', 'replace'))
            error = payload.get('error')
            code = (error.get('code') if isinstance(error, dict) else error) or ''
            if not isinstance(code, str) or len(code) > 80:
                code = ''
            code = ''.join(ch for ch in code if 32 <= ord(ch) < 127)
        except Exception:
            code = ''
        raise GuideError('OpenAI OAuth request was rejected'
                         + (f' ({code})' if code else '')
                         + '; please retry login.') from None
    except Exception:
        raise GuideError('OpenAI OAuth request failed; please retry login.') from None


def _endpoint(value):
    if not isinstance(value, str):
        raise ValueError('missing endpoint')
    p = urlsplit(value)
    if p.scheme != 'https' or p.netloc != 'auth.openai.com' or p.fragment or p.username or p.password:
        raise ValueError('untrusted endpoint')
    return value


def _discovery():
    d = _request(DISCOVERY)
    try:
        if d.get('issuer') != ISSUER:
            raise ValueError('issuer')
        for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            _endpoint(d.get(name))
        if 'S256' not in d.get('code_challenge_methods_supported', []):
            raise ValueError('pkce')
        if 'RS256' not in d.get('id_token_signing_alg_values_supported', []):
            raise ValueError('algorithm')
        return d
    except (ValueError, TypeError):
        raise GuideError('OpenAI does not advertise the required OIDC security capabilities.') from None


def _keyring():
    try:
        import keyring
        backend = keyring.get_keyring()
        # Refuse plaintext, fallback, third-party and chained backends.
        approved = {'keyring.backends.Windows.WinVaultKeyring',
                    'keyring.backends.macOS.Keyring',
                    'keyring.backends.SecretService.Keyring'}
        name = type(backend).__module__ + '.' + type(backend).__name__
        if name not in approved or backend.priority <= 0:
            raise ValueError('unapproved keyring')
        return backend
    except Exception:
        raise GuideError('A supported OS keyring is required for OpenAI OAuth; no token was saved.') from None


def _verify_id_token(token, client_id, nonce, jwks_uri):
    try:
        import jwt
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str) or not header['kid']:
            raise ValueError('header')
        jwks = _request(_endpoint(jwks_uri))
        keys = [k for k in jwks.get('keys', []) if isinstance(k, dict) and k.get('kid') == header['kid']
                and k.get('kty') == 'RSA' and k.get('use', 'sig') == 'sig' and k.get('alg', 'RS256') == 'RS256']
        if len(keys) != 1:
            raise ValueError('key')
        key = jwt.PyJWK.from_dict(keys[0], algorithm='RS256').key
        claims = jwt.decode(token, key, algorithms=['RS256'], audience=client_id, issuer=ISSUER,
                            leeway=30,  # Bounded clock skew, including iat/nbf/exp.
                            options={'require': ['iss', 'sub', 'aud', 'exp', 'iat', 'nonce']})
        if not isinstance(claims.get('sub'), str) or not claims['sub']:
            raise ValueError('subject')
        if not isinstance(claims.get('nonce'), str) or not secrets.compare_digest(claims['nonce'], nonce):
            raise ValueError('nonce')
        aud = claims['aud']
        if isinstance(aud, list) and len(aud) > 1 and claims.get('azp') != client_id:
            raise ValueError('azp')
        if 'azp' in claims and claims['azp'] != client_id:
            raise ValueError('azp')
        return claims
    except Exception as exc:
        # Report only fixed categories, never the token, claims, JWKS, or exception text.
        categories = {
            'InvalidSignatureError': 'signature', 'ExpiredSignatureError': 'expired',
            'ImmatureSignatureError': 'not-yet-valid', 'InvalidIssuerError': 'issuer',
            'InvalidAudienceError': 'audience', 'MissingRequiredClaimError': 'required-claim',
            'InvalidIssuedAtError': 'issued-at', 'InvalidTokenError': 'token-format',
            'PyJWKError': 'verification-key', 'InvalidKeyError': 'verification-key',
            'ModuleNotFoundError': 'missing-dependency', 'GuideError': 'jwks-request',
            'ValueError': 'claim-or-key-selection',
        }
        category = categories.get(type(exc).__name__, 'verification-error')
        raise GuideError('OpenAI identity verification failed (' + category + '); please retry login.') from None


def _parse_callback(path, expected_state, expected_client=None):
    p = urlsplit(path)
    if p.path != '/auth/callback' or p.scheme or p.netloc or p.fragment or len(path) > 8192:
        raise ValueError('callback')
    q = parse_qs(p.query, keep_blank_values=True, max_num_fields=20)
    if any(len(v) != 1 for v in q.values()):
        raise ValueError('duplicate parameter')
    state = q.get('state', [''])[0]
    if not state or not state.isascii() or not secrets.compare_digest(state, expected_state):
        raise ValueError('state')
    if q.get('error') == ['access_denied']:
        raise _AccessDenied()
    code = q.get('code', [''])[0]
    if not code or 'error' in q or len(code) > 4096:
        raise ValueError('authorization failed')
    client = q.get('client_id', [None])[0]
    if client is not None and (not client or len(client) > 4096 or client == DYNAMIC_CLIENT
                               or any(ord(c) < 33 or ord(c) > 126 for c in client)):
        raise ValueError('invalid client')
    if expected_client and client is not None and client != expected_client:
        raise ValueError('conflicting client')
    if not expected_client and not client:
        raise ValueError('missing client')
    return code, client or expected_client


def _callback_server(state, expected_client=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            # A local peer must not hold the serial login/refresh lock forever
            # by sending request headers slowly or never completing them.
            def interrupt():
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            remaining = max(0.01, min(2.0, self.server.deadline - time.monotonic()))
            self.connection.settimeout(remaining)
            self._timer = threading.Timer(remaining, interrupt)
            self._timer.daemon = True
            self._timer.start()

        def finish(self):
            try:
                super().finish()
            finally:
                self._timer.cancel()

        def do_GET(self):
            # A local peer may disconnect at any point (the deadline timer, a
            # cancelled browser tab). That is not an authorization failure, so
            # it must not escape as an unhandled server traceback.
            try:
                self._respond()
            except OSError:
                return

        def _respond(self):
            try:
                code, client = _parse_callback(self.path, state, expected_client)
            except _AccessDenied:
                self.server.denied = True
                self.send_response(200)
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
            except (ValueError, UnicodeError, TypeError):
                self.send_response(400)
                self.end_headers()
                return
            self.server.code, self.server.client_id = code, client
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(b'Authorization received. You may close this window.')

    class QuietServer(HTTPServer):
        """Local single-user callback: a peer resetting mid-request is not an error."""

        def handle_error(self, request, client_address):
            failure = sys.exc_info()[1]
            if isinstance(failure, (OSError, ConnectionError)):
                return
            super().handle_error(request, client_address)

    server = QuietServer(('127.0.0.1', 0), Handler)
    server.code = None
    server.client_id = None
    server.denied = False
    server.deadline = time.monotonic() + 180
    return server


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _lock(timeout):
    from filelock import FileLock
    directory = Path.home().resolve() / '.scholarguide'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return FileLock(str(directory / 'openai-oauth-login.lock'), timeout=timeout)


@contextmanager
def _acquired_lock(timeout, deadline=None, cancelled=None):
    """Acquire the login/refresh lock while honoring Stop and the deadline.

    A plain ``FileLock`` only knows its own timeout, so a request cancelled
    during a long wait would still block for the whole lock timeout. Polling a
    short acquire keeps the same serialization with a responsive stop.
    """
    lock = _lock(timeout)
    if deadline is None and cancelled is None:
        with lock:
            yield lock
        return
    from filelock import Timeout as LockTimeout
    while True:
        _check_stop(deadline, cancelled)
        try:
            lock.acquire(timeout=0.05)
            break
        except LockTimeout:
            continue
    try:
        yield lock
    finally:
        lock.release()


_CHUNK_SIZE = 800
_MAX_CHUNKS = 64


def _load_record(store):
    raw = store.get_password(_service(), ACCOUNT)
    if raw is None:
        return None
    pointer = json.loads(raw)
    if not isinstance(pointer, dict):
        raise ValueError('invalid credential record')
    if pointer.get('version') != 2:
        return pointer  # Existing pre-chunk credential, if any.
    slot, count, digest = pointer.get('slot'), pointer.get('count'), pointer.get('sha256')
    if (slot not in ('a', 'b') or isinstance(count, bool) or not isinstance(count, int)
            or not 1 <= count <= _MAX_CHUNKS or not isinstance(digest, str) or len(digest) != 64):
        raise ValueError('invalid credential pointer')
    chunks = [store.get_password(_service(), f'credentials-{slot}-{i}') for i in range(count)]
    if any(not isinstance(chunk, str) or len(chunk) > _CHUNK_SIZE for chunk in chunks):
        raise ValueError('missing credential chunk')
    encoded = ''.join(chunks)
    if not secrets.compare_digest(hashlib.sha256(encoded.encode('ascii')).hexdigest(), digest):
        raise ValueError('credential digest mismatch')
    result = json.loads(encoded)
    if not isinstance(result, dict):
        raise ValueError('invalid credential record')
    return result


def _save_record(store, record):
    # Windows Credential Manager limits each credential blob. Write an inactive
    # slot first, then switch one small pointer; a crash leaves either the old
    # record or the complete new record, never a mixture. The caller holds _lock.
    encoded = json.dumps(record, ensure_ascii=True)
    chunks = [encoded[i:i + _CHUNK_SIZE] for i in range(0, len(encoded), _CHUNK_SIZE)]
    if not chunks or len(chunks) > _MAX_CHUNKS:
        raise ValueError('credential record too large')
    raw = store.get_password(_service(), ACCOUNT)
    old = json.loads(raw) if raw is not None else {}
    old_slot = old.get('slot') if isinstance(old, dict) and old.get('version') == 2 else None
    slot = 'b' if old_slot == 'a' else 'a'
    for i, chunk in enumerate(chunks):
        store.set_password(_service(), f'credentials-{slot}-{i}', chunk)
        if store.get_password(_service(), f'credentials-{slot}-{i}') != chunk:
            raise ValueError('credential chunk readback failed')
    pointer = json.dumps({'version': 2, 'slot': slot, 'count': len(chunks),
                          'sha256': hashlib.sha256(encoded.encode('ascii')).hexdigest()})
    store.set_password(_service(), ACCOUNT, pointer)
    if store.get_password(_service(), ACCOUNT) != pointer or _load_record(store) != record:
        raise ValueError('credential pointer readback failed')
    if old_slot in ('a', 'b'):
        # Best effort: obsolete tokens must not be needed for recovery.
        for i in range(min(old.get('count', 0), _MAX_CHUNKS)):
            try:
                store.delete_password(_service(), f'credentials-{old_slot}-{i}')
            except Exception:
                pass


def _credential_response(response, started, *, prior_scope=None):
    token, refresh, lifetime = response.get('access_token'), response.get('refresh_token'), response.get('expires_in')
    if (not isinstance(token, str) or not token or len(token) > 16384
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
            or not isinstance(refresh, str) or not refresh or len(refresh) > 16384
            or any(ord(c) < 33 or ord(c) > 126 for c in refresh)
            or str(response.get('token_type', '')).lower() != 'bearer'
            or isinstance(lifetime, bool) or not isinstance(lifetime, (int, float))
            or not math.isfinite(lifetime) or lifetime <= 60 or lifetime > 86400):
        raise GuideError('OpenAI returned invalid OAuth credentials.')
    scope = response.get('scope', prior_scope)
    if not isinstance(scope, str) or not {'chatgpt.tokens.use.direct', 'resource.invoke'} <= set(scope.split()):
        raise GuideError('OpenAI did not grant the required ChatGPT plan permission.')
    return {'access_token': token, 'refresh_token': refresh,
            'expires_at': min(started + lifetime - 60, started + 3600), 'scope': scope,
            'refresh_pending': False}


def login():
    """Serialize sign-ins with refreshes and retain the prior verified account on failure."""
    from filelock import Timeout
    try:
        with _lock(0):
            return _login()
    except Timeout:
        raise GuideError('An OpenAI OAuth operation is already in progress.') from None
    except OSError:
        raise GuideError('Unable to acquire the OpenAI OAuth login lock.') from None


def _login():
    store = _store()
    try:
        host = store.get_password(_service(), HOST_ACCOUNT)
        if host is None:
            host = 'urn:uuid:' + str(uuid.uuid4())
            store.set_password(_service(), HOST_ACCOUNT, host)
            if store.get_password(_service(), HOST_ACCOUNT) != host:
                raise ValueError('host readback')
        if not isinstance(host, str) or not host.startswith('urn:uuid:') or str(uuid.UUID(host[9:])) != host[9:]:
            raise ValueError('host')
        previous = _load_record(store)
        if previous is not None:
            if (not isinstance(previous, dict) or not isinstance(previous.get('client_id'), str)
                    or not previous['client_id'] or previous['client_id'] == DYNAMIC_CLIENT
                    or not isinstance(previous.get('subject'), str) or not previous['subject']):
                raise ValueError('invalid saved identity')
        pending_client = store.get_password(_service(), PENDING_CLIENT_ACCOUNT)
        if pending_client is not None and (not isinstance(pending_client, str) or not pending_client
                or pending_client == DYNAMIC_CLIENT or len(pending_client) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in pending_client)):
            raise ValueError('invalid pending registration')
        saved_client = previous['client_id'] if previous is not None else pending_client
    except Exception:
        raise GuideError('Could not securely load OpenAI registration; no login was started.') from None
    d = _discovery()
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    server = _callback_server(state, saved_client)
    try:
        redirect = 'http://127.0.0.1:%d/auth/callback' % server.server_port
        params = {'client_id': saved_client or DYNAMIC_CLIENT, 'ext_agent_host_id': host,
            'redirect_uri': redirect, 'response_type': 'code', 'scope': SCOPES,
            'resource': RESOURCE, 'state': state, 'nonce': nonce,
            'code_challenge_method': 'S256',
            'code_challenge': _b64(hashlib.sha256(verifier.encode('ascii')).digest())}
        if not saved_client:
            params['agent_name_hint'] = 'ScholarGuide'
        url = d['authorization_endpoint'] + '?' + urlencode(params)
        if not webbrowser.open(url):
            raise GuideError('Could not open a browser for OpenAI login.')
        deadline = server.deadline
        while server.code is None and not server.denied and time.monotonic() < deadline:
            server.timeout = min(1, max(0.01, deadline - time.monotonic()))
            server.handle_request()
        if server.denied:
            raise GuideError('OpenAI authorization was declined.')
        if server.code is None:
            raise GuideError('OpenAI login timed out.')
        client = server.client_id
        if (not isinstance(client, str) or not client or client == DYNAMIC_CLIENT
                or (saved_client and client != saved_client)):
            raise GuideError('OpenAI returned an invalid client registration.')
        if previous is None:
            # Preserve the issued registration for retries, but never treat it
            # as a verified account until the ID token has passed validation.
            try:
                store.set_password(_service(), PENDING_CLIENT_ACCOUNT, client)
                if store.get_password(_service(), PENDING_CLIENT_ACCOUNT) != client:
                    raise ValueError('pending client readback failed')
            except Exception:
                raise GuideError('Could not save OpenAI client registration; please retry login.') from None
        started = time.time()
        response = _request(d['token_endpoint'], {'grant_type': 'authorization_code',
            'client_id': client, 'redirect_uri': redirect, 'code': server.code,
            'code_verifier': verifier, 'resource': RESOURCE})
        credentials = _credential_response(response, started)
        claims = _verify_id_token(response.get('id_token'), client, nonce, d['jwks_uri'])
        if previous is not None and claims['sub'] != previous['subject']:
            raise GuideError('OpenAI account identity changed; existing credentials were not replaced.')
        record = {**credentials, 'client_id': client, 'subject': claims['sub']}
        try:
            _save_record(store, record)
        except Exception:
            raise GuideError('Could not securely save OpenAI credentials; please retry login.') from None
        # The verified record takes precedence; stale pending registration is harmless.
    finally:
        server.server_close()


def access_token(deadline=None, cancelled=None):
    """Get an access token, serializing refresh and failing closed on uncertain rotation.

    ``deadline`` is an absolute ``time.monotonic()`` value, not a duration;
    ``cancelled`` is polled for a cooperative user Stop. Both are optional so
    existing callers keep their behaviour. The lock wait, keyring reads and the
    refresh HTTP timeout are all bounded by the remaining deadline.
    """
    try:
        _check_stop(deadline, cancelled)
        left = _remaining(deadline)
        timeout = 30.0 if left is None else min(30.0, left)
        with _acquired_lock(timeout, deadline, cancelled):
            _check_stop(deadline, cancelled)
            store = _bounded_local(_store, deadline=deadline, cancelled=cancelled)
            record = _bounded_local(lambda: _load_record(store), deadline=deadline, cancelled=cancelled)
            _check_stop(deadline, cancelled)
            if record is None:
                raise ValueError('no saved credentials')
            token, expiry = record['access_token'], record['expires_at']
            if (not isinstance(token, str) or not token or len(token) > 16384
                    or any(ord(c) < 33 or ord(c) > 126 for c in token)
                    or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                    or not math.isfinite(expiry) or record.get('refresh_pending')):
                raise ValueError('invalid or uncertain credentials')
            if time.time() < expiry:
                return token
            refresh, client, scope = record['refresh_token'], record['client_id'], record['scope']
            if (not isinstance(refresh, str) or not refresh or len(refresh) > 16384
                    or any(ord(c) < 33 or ord(c) > 126 for c in refresh)
                    or not isinstance(client, str) or not client or client == DYNAMIC_CLIENT
                    or not isinstance(scope, str)):
                raise ValueError('invalid refresh credentials')
            # A Stop before rotation is honored without touching the record.
            _check_stop(deadline, cancelled)
            # Persist intent before the network request. If the process crashes,
            # the server times out, or saving the replacement fails, never replay
            # the potentially rotated old refresh token.
            _bounded_local(lambda: _save_record(store, {**record, 'refresh_pending': True}),
                           deadline=deadline, cancelled=cancelled)
            started = time.time()
            # Once intent is durable the request runs to a definite outcome
            # bounded by the caller's deadline. Abandoning an in-flight rotation
            # would strand refresh_pending and force reauthorization even when
            # the server did rotate the token; a Stop still prevents the token
            # from being used for a model request after this point.
            response = _request(TOKEN_ENDPOINT, {'grant_type': 'refresh_token',
                'client_id': client, 'refresh_token': refresh, 'resource': RESOURCE},
                deadline=deadline)
            replacement = _credential_response(response, started, prior_scope=scope)
            updated = {**record, **replacement}
            # The rotation already happened: persist it even if a Stop arrived,
            # otherwise the new refresh token would be lost.
            _bounded_local(lambda: _save_record(store, updated),
                           deadline=deadline, cancelled=None)
            _check_stop(deadline, cancelled, 'refresh')
            return updated['access_token']
    except RunCancelled:
        raise
    except GuideError:
        raise
    except Exception:
        raise GuideError('OpenAI OAuth credentials are unavailable or refresh could not be confirmed; please log in again.') from None


# ---------------------------------------------------------------------------
# Hosted deployments: file-backed credentials and a paste-back callback.
#
# A container has no OS keyring and its loopback is not the visitor's machine,
# so the interactive flow cannot receive the redirect directly. The two-phase
# flow below returns the authorization URL, then accepts the redirect URL the
# browser failed to open and exchanges its code with the PKCE verifier that is
# still held server-side.
# ---------------------------------------------------------------------------

REMOTE_CALLBACK_PORT = 1455
PENDING_TTL = 900
_PENDING_FILE = 'openai-oauth-pending.json'


class _FileStore:
    """Minimal keyring-compatible store: {service: {username: value}}."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def get_password(self, service, username):
        return (self._read().get(str(service)) or {}).get(str(username))

    def set_password(self, service, username, value):
        data = self._read()
        data.setdefault(str(service), {})[str(username)] = value
        self._write(data)

    def delete_password(self, service, username):
        data = self._read()
        bucket = data.get(str(service)) or {}
        bucket.pop(str(username), None)
        if bucket:
            data[str(service)] = bucket
        else:
            data.pop(str(service), None)
        self._write(data)


def file_store_enabled() -> bool:
    value = os.environ.get('SG_OAUTH_FILE_STORE', '').strip().lower()
    return value in ('1', 'true', 'yes', 'on')


def _store():
    if not file_store_enabled():
        return _keyring()
    root = Path(os.environ.get('SG_DATA_DIR') or (Path.home() / '.scholarguide'))
    return _FileStore(root / 'oauth' / 'openai-credentials.json')


def _pending_path() -> Path:
    root = Path(os.environ.get('SG_DATA_DIR') or (Path.home() / '.scholarguide'))
    return root / 'oauth' / _PENDING_FILE


def _read_pending():
    try:
        data = json.loads(_pending_path().read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_pending(data):
    path = _pending_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def begin_login():
    """Start a sign-in and return (authorization_url, state) without blocking."""
    store = _store()
    try:
        host = store.get_password(_service(), HOST_ACCOUNT)
        if not host:
            host = 'urn:uuid:' + str(uuid.uuid4())
            store.set_password(_service(), HOST_ACCOUNT, host)
        previous = _load_record(store)
        pending_client = store.get_password(_service(), PENDING_CLIENT_ACCOUNT)
        saved_client = previous['client_id'] if previous else pending_client
    except Exception:
        raise GuideError('Could not securely load OpenAI registration; no login was started.') from None

    d = _discovery()
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    redirect = 'http://127.0.0.1:%d/auth/callback' % REMOTE_CALLBACK_PORT
    params = {'client_id': saved_client or DYNAMIC_CLIENT, 'ext_agent_host_id': host,
              'redirect_uri': redirect, 'response_type': 'code', 'scope': SCOPES,
              'resource': RESOURCE, 'state': state, 'nonce': nonce,
              'code_challenge_method': 'S256',
              'code_challenge': _b64(hashlib.sha256(verifier.encode('ascii')).digest())}
    if not saved_client:
        params['agent_name_hint'] = 'ScholarGuide'
    url = d['authorization_endpoint'] + '?' + urlencode(params)

    now = time.time()
    pending = {k: v for k, v in _read_pending().items()
               if isinstance(v, dict) and now - float(v.get('created_at') or 0) < PENDING_TTL}
    pending[state] = {'nonce': nonce, 'verifier': verifier, 'client': saved_client,
                      'redirect': redirect, 'created_at': now,
                      'token_endpoint': d['token_endpoint'], 'jwks_uri': d['jwks_uri']}
    _write_pending(pending)
    return url, state


def _parse_pasted_callback(text):
    """Accept a full redirect URL, a bare code, or a 'code=...&state=...' fragment."""
    if not isinstance(text, str) or not text.strip():
        raise GuideError('Paste the address the browser stopped at, or the code itself.')
    raw = text.strip()
    if len(raw) > 8192:
        raise GuideError('The pasted value is too long.')
    try:
        if raw.startswith('http://') or raw.startswith('https://'):
            values = parse_qs(urlsplit(raw).query)
        elif 'code=' in raw:
            values = parse_qs(raw.split('?', 1)[-1])
        else:
            return {'code': raw, 'state': None, 'error': None}
    except Exception:
        raise GuideError('The pasted value is not a valid callback URL.') from None
    code = (values.get('code') or [''])[0]
    state = (values.get('state') or [''])[0]
    error = (values.get('error') or [''])[0]
    client = (values.get('client_id') or [None])[0]
    if client is not None and (not client or len(client) > 4096 or client == DYNAMIC_CLIENT
                               or any(ord(c) < 33 or ord(c) > 126 for c in client)):
        raise GuideError('The pasted URL carries an invalid client registration.')
    if not code and not error:
        raise GuideError('The pasted URL has no authorization code.')
    return {'code': code, 'state': state or None, 'error': error or None, 'client': client}


def complete_login(pasted):
    """Finish a sign-in started by begin_login(); returns a small summary."""
    parsed = _parse_pasted_callback(pasted)
    if parsed['error']:
        raise GuideError('OpenAI authorization was declined.')
    pending = _read_pending()
    state = parsed['state']
    if state is None:
        if len(pending) != 1:
            raise GuideError('Paste the full callback address so the request can be matched.')
        state = next(iter(pending))
    entry = pending.get(state)
    if not isinstance(entry, dict):
        raise GuideError('This sign-in is unknown or expired; start again.')
    if time.time() - float(entry.get('created_at') or 0) > PENDING_TTL:
        pending.pop(state, None)
        _write_pending(pending)
        raise GuideError('This sign-in expired; start again.')

    client = parsed.get('client') or entry.get('client')
    if not client or client == DYNAMIC_CLIENT:
        raise GuideError('OpenAI returned no usable client registration; start again.')
    if entry.get('client') and parsed.get('client') and parsed['client'] != entry['client']:
        raise GuideError('OpenAI returned an inconsistent client registration; start again.')
    response = _request(entry['token_endpoint'], {
        'grant_type': 'authorization_code', 'client_id': client,
        'redirect_uri': entry['redirect'], 'code': parsed['code'],
        'code_verifier': entry['verifier'], 'resource': RESOURCE})
    started = time.time()
    credentials = _credential_response(response, started)
    claims = _verify_id_token(response.get('id_token'), client, entry['nonce'], entry['jwks_uri'])
    record = {**credentials, 'client_id': client, 'subject': claims['sub']}
    store = _store()
    _save_record(store, record)
    if entry.get('client'):
        try:
            store.set_password(_service(), PENDING_CLIENT_ACCOUNT, entry['client'])
        except Exception:
            pass
    pending.pop(state, None)
    _write_pending(pending)
    return {'subject': record.get('subject', ''), 'client_id': record.get('client_id', '')}


def list_models():
    """Return the model ids the signed-in account can use."""
    token = access_token()
    request = Request('https://api.openai.com/v1/models',
                      headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    proxy = _validated_proxy(proxy_url())
    opener = build_opener(ProxyHandler({'https': proxy} if proxy else {}))
    try:
        with opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        code = ''
        try:
            body = json.loads(exc.read(4096).decode('utf-8', 'replace'))
            error = body.get('error')
            code = (error.get('code') if isinstance(error, dict) else error) or ''
            if not isinstance(code, str) or len(code) > 80:
                code = ''
            code = ''.join(ch for ch in code if 32 <= ord(ch) < 127)
        except Exception:
            code = ''
        raise GuideError('Model list was rejected (HTTP %d%s)' % (
            exc.code, ', ' + code if code else '')) from None
    except Exception:
        raise GuideError('Could not list models for this account; try again.') from None
    items = payload.get('data') if isinstance(payload, dict) else None
    return sorted({item.get('id') for item in (items or []) if isinstance(item, dict)
                   and isinstance(item.get('id'), str) and item['id']})
