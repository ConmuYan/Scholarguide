import json
import socket
import threading
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    jwt = None
    rsa = None

import openai_oauth as oauth
from common import GuideError, RunCancelled


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home_patch = patch.object(oauth.Path, 'home', return_value=Path(self.tmp.name))
        home_patch.start()
        self.addCleanup(home_patch.stop)
        service_patch = patch.object(oauth, '_service', return_value=oauth.SERVICE)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.store = Mock()
        self.values = {}
        self.store.set_password.side_effect = lambda s, a, v: self.values.__setitem__((s, a), v)
        self.store.get_password.side_effect = lambda s, a: self.values.get((s, a))
        self.store.delete_password.side_effect = lambda s, a: self.values.pop((s, a), None)
        self.discovery = {'issuer': oauth.ISSUER,
                          'authorization_endpoint': oauth.ISSUER + '/authorize',
                          'token_endpoint': oauth.ISSUER + '/token',
                          'jwks_uri': oauth.ISSUER + '/keys',
                          'code_challenge_methods_supported': ['S256'],
                          'id_token_signing_alg_values_supported': ['RS256']}

    def test_chunked_keyring_commit_is_atomic_on_inactive_write_failure(self):
        original = {'access_token': 'a' * 4000, 'refresh_token': 'b' * 4000, 'refresh_pending': True}
        oauth._save_record(self.store, original)
        def fail_inactive(service, account, value):
            if account.startswith('credentials-b-'):
                raise OSError('simulated failure')
            self.values[(service, account)] = value
        self.store.set_password.side_effect = fail_inactive
        with self.assertRaises(OSError):
            oauth._save_record(self.store, {'access_token': 'c' * 4000, 'refresh_token': 'd' * 4000})
        self.assertEqual(oauth._load_record(self.store), original)

    def test_proxy_requires_explicit_local_uncredentialed_url(self):
        with patch.object(oauth, '_keyring', return_value=self.store):
            self.assertEqual(oauth.proxy_url(), '')
            with patch.dict('os.environ', {'HTTPS_PROXY': 'http://127.0.0.1:7890'}):
                oauth.configure_system_proxy()
            self.assertEqual(oauth.proxy_url(), 'http://127.0.0.1:7890')
        for value in ('https://127.0.0.1:7890', 'http://evil.example:7890',
                      'http://user:secret@127.0.0.1:7890', 'http://127.0.0.1:7890/path'):
            with self.subTest(value=value), self.assertRaises(GuideError):
                oauth._validated_proxy(value)

    def test_callback_rejects_csrf_duplicates_errors(self):
        self.assertEqual(oauth._parse_callback('/auth/callback?state=abc&code=xyz&client_id=issued', 'abc'), ('xyz', 'issued'))
        self.assertEqual(oauth._parse_callback('/auth/callback?state=abc&code=xyz', 'abc', 'issued'), ('xyz', 'issued'))
        for path in ['/auth/callback?state=bad&code=x&client_id=c', '/auth/callback?state=abc&state=abc&code=x',
                     '/auth/callback?state=abc&code=x&error=no', '//evil/auth/callback?state=abc&code=x',
                     '/auth/callback?state=abc&code=x', '/callback?state=abc&code=x&client_id=c',
                     '/auth/callback?state=abc&code=x&client_id=dynamic_agent_client']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                oauth._parse_callback(path, 'abc')
        with self.assertRaises(ValueError):
            oauth._parse_callback('/auth/callback?state=abc&code=x&client_id=other', 'abc', 'issued')
        with self.assertRaises(ValueError):
            oauth._parse_callback('/auth/callback?state=%E4%B8%AD&code=x&client_id=c', 'abc')
        with self.assertRaises(oauth._AccessDenied):
            oauth._parse_callback('/auth/callback?state=abc&error=access_denied', 'abc')
        with self.assertRaises(ValueError):
            oauth._parse_callback('/auth/callback?state=bad&error=access_denied', 'abc')

    @unittest.skipUnless(jwt is not None and rsa is not None, 'PyJWT and cryptography required')
    def test_real_signature_and_claim_verification(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid='kid', use='sig', alg='RS256')
        claims = {'iss': oauth.ISSUER, 'sub': 'person', 'aud': 'client',
                  'iat': int(time.time()), 'exp': int(time.time()) + 300, 'nonce': 'nonce'}
        with patch.object(oauth, '_request', return_value={'keys': [jwk]}):
            def verify(c, nonce='nonce'):
                token = jwt.encode(c, key, algorithm='RS256', headers={'kid': 'kid'})
                return oauth._verify_id_token(token, 'client', nonce, self.discovery['jwks_uri'])
            self.assertEqual(verify(claims)['sub'], 'person')
            self.assertEqual(verify({**claims, 'iat': int(time.time()) + 10})['sub'], 'person')
            for update in [{'iss': 'https://evil.invalid'}, {'aud': 'other'},
                           {'exp': int(time.time()) - 31}, {'iat': int(time.time()) + 90}, {'nonce': 'wrong'},
                           {'aud': ['client', 'other']}]:
                with self.subTest(update=update), self.assertRaises(GuideError):
                    verify({**claims, **update})
            other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            forged = jwt.encode(claims, other, algorithm='RS256', headers={'kid': 'kid'})
            with self.assertRaises(GuideError):
                oauth._verify_id_token(forged, 'client', 'nonce', self.discovery['jwks_uri'])

    def test_callback_slow_client_is_bounded(self):
        import contextlib, io
        captured = io.StringIO()
        server = oauth._callback_server('state')
        server.deadline = time.monotonic() + 0.3
        worker = threading.Thread(target=server.handle_request, daemon=True)
        worker.start()
        try:
            with contextlib.redirect_stderr(captured):
                with socket.create_connection(('127.0.0.1', server.server_port), timeout=2) as client:
                    client.sendall(b'GET /auth/callback HTTP/1.1' + bytes([13, 10]) + b'Host:')
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
        finally:
            server.server_close()
        # The deadline timer shuts the socket down mid-response; that local
        # disconnect is not an authorization failure and must stay quiet.
        self.assertNotIn('Traceback', captured.getvalue())


    def test_discovery_requires_trusted_oidc_not_registration(self):
        with patch.object(oauth, '_request', return_value=self.discovery):
            self.assertEqual(oauth._discovery(), self.discovery)
        for change in [{'jwks_uri': 'https://evil.invalid/keys'}, 
                       {'issuer': 'https://evil.invalid'}, {'code_challenge_methods_supported': ['plain']}]:
            with self.subTest(change=change), patch.object(oauth, '_request', return_value={**self.discovery, **change}):
                with self.assertRaises(GuideError):
                    oauth._discovery()

    def test_login_and_reauthorization(self):
        server = Mock(server_port=12345, code=None, denied=False, deadline=time.monotonic()+180)
        seen = {}
        def open_browser(url):
            seen.clear()
            seen.update({k: v[0] for k, v in parse_qs(urlsplit(url).query).items()})
            server.code = 'authorization-code'
            server.client_id = 'client'
            return True
        def request(url, data=None, json_body=False):
            self.assertEqual(url, self.discovery['token_endpoint'])
            self.assertEqual(data['client_id'], 'client')
            self.assertEqual(data['resource'], oauth.RESOURCE)
            self.assertEqual(data['redirect_uri'], seen['redirect_uri'])
            self.assertEqual(data['code'], 'authorization-code')
            self.assertEqual(oauth._b64(__import__('hashlib').sha256(data['code_verifier'].encode()).digest()), seen['code_challenge'])
            return {'access_token': 'secret-token', 'id_token': 'identity', 'token_type': 'Bearer',
                    'expires_in': 3600, 'refresh_token': 'must-not-store', 'scope': oauth.SCOPES}
        with patch.object(oauth, '_keyring', return_value=self.store), \
             patch.object(oauth, '_discovery', return_value=self.discovery), \
             patch.object(oauth, '_callback_server', return_value=server), \
             patch.object(oauth, '_request', side_effect=request), \
             patch.object(oauth.webbrowser, 'open', side_effect=open_browser), \
             patch.object(oauth, '_verify_id_token', return_value={'sub': 'person'}) as verify:
            oauth.login()
            self.assertEqual(oauth.access_token(), 'secret-token')
            verify.assert_called_with('identity', 'client', seen['nonce'], self.discovery['jwks_uri'])
            record = oauth._load_record(self.store)
            self.assertEqual(set(record), {'access_token', 'refresh_token', 'refresh_pending', 'expires_at', 'client_id', 'subject', 'scope'})
            self.assertEqual(record['refresh_token'], 'must-not-store')
            self.assertFalse(record['refresh_pending'])
            self.assertEqual(seen['client_id'], oauth.DYNAMIC_CLIENT)
            self.assertEqual(seen['agent_name_hint'], 'ScholarGuide')
            self.assertEqual(seen['scope'], oauth.SCOPES)
            self.assertEqual(seen['resource'], oauth.RESOURCE)
            self.assertEqual(seen['redirect_uri'], 'http://127.0.0.1:12345/auth/callback')
            self.assertEqual(seen['code_challenge_method'], 'S256')
            self.assertNotEqual(seen['state'], seen['nonce'])
            host = seen['ext_agent_host_id']
            self.assertTrue(host.startswith('urn:uuid:'))
            server.code = None
            oauth.login()
            self.assertEqual(seen['client_id'], 'client')
            self.assertNotIn('agent_name_hint', seen)
            self.assertEqual(seen['ext_agent_host_id'], host)
        self.assertEqual(server.server_close.call_count, 2)

    def test_failed_first_exchange_keeps_unverified_client_for_retry(self):
        server = Mock(server_port=12345, code='code', client_id='client', denied=False, deadline=time.monotonic()+180)
        with patch.object(oauth, '_keyring', return_value=self.store), \
             patch.object(oauth, '_discovery', return_value=self.discovery), \
             patch.object(oauth, '_callback_server', return_value=server), \
             patch.object(oauth, '_request', side_effect=GuideError('network')), \
             patch.object(oauth.webbrowser, 'open', return_value=True) as browser:
            with self.assertRaises(GuideError):
                oauth.login()
            self.assertEqual(self.values[(oauth.SERVICE, oauth.PENDING_CLIENT_ACCOUNT)], 'client')
            self.assertNotIn((oauth.SERVICE, oauth.ACCOUNT), self.values)
            with self.assertRaises(GuideError):
                oauth.login()
            params = parse_qs(urlsplit(browser.call_args.args[0]).query)
            self.assertEqual(params['client_id'], ['client'])
            self.assertNotIn('agent_name_hint', params)

    def test_failed_reauth_does_not_replace_credentials(self):
        previous = json.dumps({'client_id': 'client', 'subject': 'person',
                               'access_token': 'old', 'expires_at': time.time() + 500})
        self.values[(oauth.SERVICE, oauth.ACCOUNT)] = previous
        server = Mock(server_port=12345, code='code', client_id='client', denied=False, deadline=time.monotonic()+180)
        response = {'access_token': 'new', 'refresh_token': 'new-refresh', 'token_type': 'Bearer', 'expires_in': 3600,
                    'id_token': 'identity', 'scope': oauth.SCOPES}
        with patch.object(oauth, '_keyring', return_value=self.store), \
             patch.object(oauth, '_discovery', return_value=self.discovery), \
             patch.object(oauth, '_callback_server', return_value=server), \
             patch.object(oauth, '_request', return_value=response) as request, \
             patch.object(oauth.webbrowser, 'open', return_value=True), \
             patch.object(oauth, '_verify_id_token', return_value={'sub': 'other'}):
            with self.assertRaises(GuideError):
                oauth.login()
            self.assertEqual(self.values[(oauth.SERVICE, oauth.ACCOUNT)], previous)
            response['scope'] = 'openid profile'
            with self.assertRaises(GuideError):
                oauth.login()
            self.assertEqual(self.values[(oauth.SERVICE, oauth.ACCOUNT)], previous)
            server.client_id = 'conflict'
            request.reset_mock()
            with self.assertRaises(GuideError):
                oauth.login()
            request.assert_not_called()
            self.assertEqual(self.values[(oauth.SERVICE, oauth.ACCOUNT)], previous)

    def test_expired_invalid_and_unavailable_storage_fail_closed(self):
        with patch.object(oauth, '_keyring', return_value=self.store):
            for value in [None, '{}', json.dumps({'access_token': 'secret', 'expires_at': time.time()-1}),
                          json.dumps({'access_token': 'bad\nheader', 'expires_at': time.time()+500})]:
                self.values[(oauth.SERVICE, oauth.ACCOUNT)] = value
                with self.subTest(value=value), self.assertRaises(GuideError):
                    oauth.access_token()
        with patch.object(oauth, '_keyring', side_effect=GuideError('failed')), \
             patch.object(oauth, '_discovery') as discovery, self.assertRaises(GuideError):
            oauth.login()
        discovery.assert_not_called()

    def test_refresh_rotates_once_and_keeps_identity(self):
        previous = {'client_id': 'client', 'subject': 'person', 'access_token': 'old',
                    'refresh_token': 'old-refresh', 'refresh_pending': False,
                    'scope': oauth.SCOPES, 'expires_at': time.time() - 1}
        self.values[(oauth.SERVICE, oauth.ACCOUNT)] = json.dumps(previous)
        response = {'access_token': 'new', 'refresh_token': 'new-refresh',
                    'token_type': 'Bearer', 'expires_in': 3600}
        def refresh(url, data=None, json_body=False, **kwargs):
            self.assertEqual(url, oauth.TOKEN_ENDPOINT)
            self.assertEqual(data['refresh_token'], 'old-refresh')
            self.assertTrue(oauth._load_record(self.store)['refresh_pending'])
            return response
        with patch.object(oauth, '_keyring', return_value=self.store), patch.object(oauth, '_request', side_effect=refresh) as request:
            self.assertEqual(oauth.access_token(), 'new')
            self.assertEqual(oauth.access_token(), 'new')
            request.assert_called_once()
        stored = oauth._load_record(self.store)
        self.assertEqual(stored['refresh_token'], 'new-refresh')
        self.assertFalse(stored['refresh_pending'])
        self.assertEqual(stored['subject'], 'person')

    def test_uncertain_refresh_does_not_replay(self):
        previous = {'client_id': 'client', 'subject': 'person', 'access_token': 'old',
                    'refresh_token': 'old-refresh', 'refresh_pending': False,
                    'scope': oauth.SCOPES, 'expires_at': time.time() - 1}
        self.values[(oauth.SERVICE, oauth.ACCOUNT)] = json.dumps(previous)
        with patch.object(oauth, '_keyring', return_value=self.store), \
             patch.object(oauth, '_request', side_effect=TimeoutError) as request:
            for _ in range(2):
                with self.assertRaises(GuideError):
                    oauth.access_token()
            request.assert_called_once()
            self.assertTrue(oauth._load_record(self.store)['refresh_pending'])

    def test_refresh_persist_failure_never_replays_old_token(self):
        previous = {'client_id': 'client', 'subject': 'person', 'access_token': 'old',
                    'refresh_token': 'old-refresh', 'refresh_pending': False,
                    'scope': oauth.SCOPES, 'expires_at': time.time() - 1}
        self.values[(oauth.SERVICE, oauth.ACCOUNT)] = json.dumps(previous)
        response = {'access_token': 'new', 'refresh_token': 'new-refresh',
                    'token_type': 'Bearer', 'expires_in': 3600}
        save = oauth._save_record
        def save_once(store, record):
            if not record['refresh_pending']:
                raise OSError('store failed')
            save(store, record)
        with patch.object(oauth, '_keyring', return_value=self.store), \
             patch.object(oauth, '_request', return_value=response) as request, \
             patch.object(oauth, '_save_record', side_effect=save_once):
            for _ in range(2):
                with self.assertRaises(GuideError):
                    oauth.access_token()
            request.assert_called_once()
            self.assertTrue(oauth._load_record(self.store)['refresh_pending'])

    def test_concurrent_login_is_rejected_before_authorization(self):
        from filelock import Timeout
        with patch('filelock.FileLock') as lock_class, patch.object(oauth, '_login') as start:
            lock_class.return_value.__enter__.side_effect = Timeout('test')
            with self.assertRaises(GuideError):
                oauth.login()
            start.assert_not_called()

    def test_login_lock_excludes_another_process(self):
        lock = oauth._lock(0)
        script = ('from filelock import FileLock, Timeout; import sys\n'
                  'try:\n with FileLock(sys.argv[1], timeout=0): pass\n'
                  'except Timeout: sys.exit(7)\n')
        with lock:
            child = subprocess.run([sys.executable, '-c', script, lock.lock_file],
                                   capture_output=True, timeout=15, check=False)
        self.assertEqual(child.returncode, 7, child.stderr.decode(errors='replace'))

    def test_save_failure_is_sanitized(self):
        self.store.get_password.side_effect = RuntimeError('secret-token')
        self.values[(oauth.SERVICE, oauth.ACCOUNT)] = '{}'
        with patch.object(oauth, '_keyring', return_value=self.store), self.assertRaises(GuideError) as caught:
            oauth.access_token()
        self.assertNotIn('secret-token', str(caught.exception))

    def test_token_acquisition_honors_cancel_and_deadline(self):
        previous = {'client_id': 'client', 'subject': 'person', 'access_token': 'current',
                    'refresh_token': 'refresh', 'refresh_pending': False,
                    'scope': oauth.SCOPES, 'expires_at': time.time() + 500}
        self.values[(oauth.SERVICE, oauth.ACCOUNT)] = json.dumps(previous)
        with patch.object(oauth, '_keyring', return_value=self.store) as keyring, \
             patch.object(oauth, '_lock') as lock, patch.object(oauth, '_request') as request:
            with self.assertRaises(RunCancelled):
                oauth.access_token(cancelled=lambda: True)
            with self.assertRaises(GuideError):
                oauth.access_token(deadline=time.monotonic() - 0.01)
            lock.assert_not_called()
            keyring.assert_not_called()
            request.assert_not_called()

    def test_lock_wait_is_bounded_by_remaining_deadline(self):
        previous = {'client_id': 'client', 'subject': 'person', 'access_token': 'current',
                    'refresh_token': 'refresh', 'refresh_pending': False,
                    'scope': oauth.SCOPES, 'expires_at': time.time() + 500}
        self.values[(oauth.SERVICE, oauth.ACCOUNT)] = json.dumps(previous)
        lock = Mock()
        lock.__enter__ = Mock(return_value=None)
        lock.__exit__ = Mock(return_value=False)
        with patch.object(oauth, '_keyring', return_value=self.store), \
             patch.object(oauth, '_lock', return_value=lock) as lock_factory:
            self.assertEqual(oauth.access_token(deadline=time.monotonic() + 2), 'current')
        self.assertLessEqual(lock_factory.call_args.args[0], 2.0)

    def test_blocked_keyring_read_stops_at_deadline(self):
        def slow(service, account):
            time.sleep(0.4)
            return None
        self.store.get_password.side_effect = slow
        with patch.object(oauth, '_keyring', return_value=self.store), \
             patch.object(oauth, '_request') as request:
            started = time.monotonic()
            with self.assertRaises(GuideError):
                oauth.access_token(deadline=time.monotonic() + 0.1)
            self.assertLess(time.monotonic() - started, 0.35)
        request.assert_not_called()

    def test_credential_write_is_bounded_before_rotation(self):
        previous = {'client_id': 'client', 'subject': 'person', 'access_token': 'old',
                    'refresh_token': 'old-refresh', 'refresh_pending': False,
                    'scope': oauth.SCOPES, 'expires_at': time.time() - 1}
        self.values[(oauth.SERVICE, oauth.ACCOUNT)] = json.dumps(previous)

        def slow_save(store, record):
            time.sleep(0.3)

        with patch.object(oauth, '_keyring', return_value=self.store),              patch.object(oauth, '_save_record', side_effect=slow_save),              patch.object(oauth, '_request') as request:
            started = time.monotonic()
            with self.assertRaises(GuideError):
                oauth.access_token(deadline=time.monotonic() + 0.05)
            self.assertLess(time.monotonic() - started, 0.25)
        request.assert_not_called()  # no rotation without a durable pending marker

    def test_lock_wait_observes_cancel(self):
        from filelock import Timeout as LockTimeout

        class BusyLock:
            def acquire(self, timeout=None):
                time.sleep(min(timeout or 0.05, 0.05))
                raise LockTimeout('busy')

            def release(self):
                self.released = True

        polls = {'count': 0}

        def cancelled():
            polls['count'] += 1
            return polls['count'] > 2

        with patch.object(oauth, '_lock', return_value=BusyLock()):
            started = time.monotonic()
            with self.assertRaises(RunCancelled):
                oauth.access_token(deadline=time.monotonic() + 5, cancelled=cancelled)
            self.assertLess(time.monotonic() - started, 0.5)

    def test_refresh_http_is_bounded_and_rotation_survives_cancel(self):
        previous = {'client_id': 'client', 'subject': 'person', 'access_token': 'old',
                    'refresh_token': 'old-refresh', 'refresh_pending': False,
                    'scope': oauth.SCOPES, 'expires_at': time.time() - 1}
        self.values[(oauth.SERVICE, oauth.ACCOUNT)] = json.dumps(previous)
        response = {'access_token': 'new', 'refresh_token': 'new-refresh',
                    'token_type': 'Bearer', 'expires_in': 3600}
        cancelled = {'value': False}
        seen = {}
        def refresh(url, data=None, json_body=False, **kwargs):
            seen.update(kwargs)
            cancelled['value'] = True
            return response
        deadline = time.monotonic() + 5
        with patch.object(oauth, '_keyring', return_value=self.store), \
             patch.object(oauth, '_request', side_effect=refresh):
            with self.assertRaises(RunCancelled):
                oauth.access_token(deadline=deadline, cancelled=lambda: cancelled['value'])
        self.assertEqual(seen['deadline'], deadline)
        stored = oauth._load_record(self.store)
        self.assertEqual(stored['refresh_token'], 'new-refresh')
        self.assertFalse(stored['refresh_pending'])

    def test_refresh_http_timeout_never_exceeds_remaining_deadline(self):
        response = Mock()
        response.read.return_value = b'{"ok": 1}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(oauth, 'build_opener', return_value=opener), \
             patch.object(oauth, 'proxy_url', return_value=''):
            self.assertEqual(oauth._request(oauth.DISCOVERY, deadline=time.monotonic() + 0.5),
                             {'ok': 1})
        self.assertLessEqual(opener.open.call_args.kwargs['timeout'], 0.5)
        opener.open.reset_mock()
        with patch.object(oauth, 'build_opener', return_value=opener), \
             patch.object(oauth, 'proxy_url', return_value=''):
            with self.assertRaises(GuideError):
                oauth._request(oauth.DISCOVERY, deadline=time.monotonic() - 1)
        opener.open.assert_not_called()

    def test_request_honors_cancellation_before_dispatch(self):
        with patch.object(oauth, 'build_opener') as opener:
            with self.assertRaises(RunCancelled):
                oauth._request(oauth.DISCOVERY, cancelled=lambda: True)
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
