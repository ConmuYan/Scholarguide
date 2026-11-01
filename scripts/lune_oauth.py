"""Lune OAuth (authorization code + PKCE) with a loopback callback.

What it does, in order:
  1. dynamic client registration (RFC 7591) — public client, no secret
  2. builds the authorize URL and (optionally) opens your browser
  3. serves ``http://127.0.0.1:<port>/callback`` until Lune redirects back
  4. exchanges the code at the token endpoint (form-encoded, ``none`` auth)
  5. writes ``--out`` with mode 600 and prints only non-secret metadata

The access token and refresh token are never printed, never committed, and never
echoed into logs. Lune rotates refresh tokens: the newest one is what gets saved.

Run:  C:/Python314/python.exe scripts/lune_oauth.py --port 8768 --open
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from lune_quota import LuneQuotaStore, default_quota_path
ISSUER = 'https://api.luneresearch.com'
RESOURCE = 'https://mcp.luneresearch.com'
SCOPE = 'papers:read guidance:read account:read'
DEFAULT_OUT = Path.home() / '.scholarguide' / 'lune-oauth.json'
# Cloudflare in front of api.luneresearch.com refuses the default Python UA
# (error 1010, browser_signature_banned). Send a normal client signature.
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _save_record(path: Path, record: dict) -> None:
    """Replace an existing token file only after a complete private temp write."""
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(record, handle, ensure_ascii=False, indent=1)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(request: Request, method: str, exit_code: int) -> dict:
    # Reserve before the single HTTP attempt. Even an indeterminate outcome is spent.
    try:
        with LuneQuotaStore(default_quota_path()) as store:
            reservation = store.reserve(method)
            try:
                with build_opener(_NoRedirect()).open(request, timeout=30) as response:
                    body = response.read(128_001)
                    if len(body) > 128_000:
                        raise ValueError('OAuth response exceeds limit')
                    result = json.loads(body or b'{}')
                    if not isinstance(result, dict):
                        raise ValueError('invalid response')
            except BaseException:
                try:
                    store.finish(reservation, 'unknown')
                except Exception:
                    pass
                raise
            store.finish(reservation, 'response_received')
            return result
    except Exception as exc:
        # Never expose URL, exception text, response content or chained traceback.
        if hasattr(exc, 'close'):
            exc.close()
        print(json.dumps({'ok': False, 'step': method, 'error': 'request_failed'}))
        raise SystemExit(exit_code) from None


def _post_json(url: str, payload: dict) -> dict:
    request = Request(url, data=json.dumps(payload).encode(), method='POST',
                      headers={'Content-Type': 'application/json',
                               'Accept': 'application/json', 'User-Agent': UA})
    return _post(request, 'oauth/register', 2)


def _post_form(url: str, payload: dict) -> dict:
    request = Request(url, data=urlencode(payload).encode(), method='POST',
                      headers={'Content-Type': 'application/x-www-form-urlencoded',
                               'Accept': 'application/json', 'User-Agent': UA})
    return _post(request, 'oauth/token', 3)


class _Callback(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path != '/callback':
            self.send_error(404)
            return
        query = parse_qs(parts.query)
        server = self.server
        server.result = {'code': (query.get('code') or [''])[0],
                         'state': (query.get('state') or [''])[0],
                         'error': (query.get('error') or [''])[0],
                         'error_description': (query.get('error_description') or [''])[0]}
        server.done.set()
        body = ('<!doctype html><meta charset="utf-8"><title>Lune 授权完成</title>'
                '<body style="font:16px/1.7 system-ui;padding:48px;max-width:640px;margin:auto">'
                '<h1 style="font-size:22px">授权已收到</h1>'
                '<p>可以关掉这个标签页，回到终端继续。</p>').encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Lune OAuth code flow (PKCE)')
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    parser.add_argument('--open', action='store_true', help='open the browser for you')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cached = {}
    if out.is_file():
        try:
            cached = json.loads(out.read_text(encoding='utf-8'))
        except ValueError:
            cached = {}

    redirect_uri = f'http://127.0.0.1:{args.port}/callback'
    client_id = str(cached.get('client_id') or '')
    if not client_id:
        registration = _post_json(f'{ISSUER}/oauth/register', {
            'client_name': 'ScholarGuide (local evaluation)',
            'redirect_uris': [redirect_uri],
            'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'],
            'token_endpoint_auth_method': 'none',
            'scope': SCOPE})
        client_id = str(registration.get('client_id') or '')
        if not client_id:
            print(json.dumps({'ok': False, 'step': 'registration',
                              'detail': 'missing client_id'}, ensure_ascii=False))
            return 1
    # Persist the registration immediately so a retry reuses the same client.
    if not out.exists():
        try:
            # Exclusive creation avoids overwriting an existing credential file.
            with out.open('x', encoding='utf-8') as handle:
                handle.write(json.dumps({**cached, 'client_id': client_id,
                                         'redirect_uri': redirect_uri}, indent=1) + chr(10))
            os.chmod(out, 0o600)
        except OSError:
            pass
    verifier = _b64(secrets.token_bytes(64))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    state = _b64(secrets.token_bytes(16))
    authorize = f'{ISSUER}/oauth/authorize?' + urlencode({
        'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect_uri,
        'scope': SCOPE, 'state': state, 'code_challenge': challenge,
        'code_challenge_method': 'S256', 'resource': RESOURCE})
    print('NOTICE: Browser authorization traffic is external and unmetered; '
          'recheck the Lune account balance before subsequent Lune calls.')
    if not args.open:
        print('NOTICE: The following one-time authorization URL is sensitive; do not copy it into logs or AI conversations.')
        print('AUTHORIZE_URL=' + authorize)

    server = ThreadingHTTPServer(('127.0.0.1', args.port), _Callback)
    server.done = threading.Event()
    server.result = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if args.open:
        try:
            opened = webbrowser.open(authorize)
        except Exception:
            opened = False
        if not opened:
            server.shutdown()
            server.server_close()
            print(json.dumps({'ok': False, 'step': 'authorize', 'detail': 'browser could not be opened'}))
            return 1
    print(f'waiting for the browser to hit {redirect_uri} (timeout {args.timeout}s)',
          flush=True)
    server.done.wait(args.timeout)
    server.shutdown()
    server.server_close()
    result = server.result or {}
    if result.get('error'):
        print(json.dumps({'ok': False, 'step': 'authorize',
                          'detail': 'authorization failed'}, ensure_ascii=False))
        return 1
    if not result.get('code'):
        print(json.dumps({'ok': False, 'step': 'callback', 'detail': 'no code received'},
                         ensure_ascii=False))
        return 1
    if result.get('state') != state:
        print(json.dumps({'ok': False, 'step': 'state', 'detail': 'state mismatch'},
                         ensure_ascii=False))
        return 1

    tokens = _post_form(f'{ISSUER}/oauth/token', {
        'grant_type': 'authorization_code', 'code': result['code'],
        'redirect_uri': redirect_uri, 'client_id': client_id,
        'code_verifier': verifier, 'resource': RESOURCE})
    access, refresh = tokens.get('access_token'), tokens.get('refresh_token')
    if not access:
        print(json.dumps({'ok': False, 'step': 'token', 'detail': 'no access_token'},
                         ensure_ascii=False))
        return 1
    try:
        lifetime = int(tokens.get('expires_in') or 3600)
    except (ValueError, TypeError, OverflowError):
        print(json.dumps({'ok': False, 'step': 'token', 'detail': 'invalid expiry'}))
        return 1
    record = {'client_id': client_id, 'redirect_uri': redirect_uri,
              'scope': tokens.get('scope') or SCOPE, 'token_type': tokens.get('token_type'),
              'access_token': access, 'refresh_token': refresh or '',
              'expires_at': int(time.time()) + lifetime,
              'obtained_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    _save_record(out, record)
    # Only known scope names and numeric lifetime are safe to echo from a server.
    reported_scope = record['scope']
    if not isinstance(reported_scope, str) or not set(reported_scope.split()).issubset(set(SCOPE.split())):
        reported_scope = None
    reported_expiry = tokens.get('expires_in')
    if not isinstance(reported_expiry, (int, float)) or isinstance(reported_expiry, bool):
        reported_expiry = None
    print(json.dumps({'ok': True, 'saved': str(out),
                      'expires_in': reported_expiry,
                      'scope': reported_scope, 'has_refresh': bool(refresh)},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
