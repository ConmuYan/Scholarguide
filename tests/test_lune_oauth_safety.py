"""Offline OAuth safety regressions: synthetic data, temporary ledger and loopback only."""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lune_quota import LuneQuotaStore

spec = importlib.util.spec_from_file_location('lune_oauth_safety', Path(__file__).resolve().parents[1] / 'scripts' / 'lune_oauth.py')
oauth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oauth)

SECRET = 'synthetic-secret-do-not-print'

class Handler(BaseHTTPRequestHandler):
    calls = []
    def log_message(self, *args):
        pass
    def do_POST(self):
        type(self).calls.append(self.path)
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.send_response(302 if self.path == '/redirect' else 400)
        self.send_header('Location', '/capture')
        self.end_headers()
        self.wfile.write(SECRET.encode())

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'quota.sqlite')
        env = patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': self.db})
        env.start()
        self.addCleanup(env.stop)
        Handler.calls = []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port

    def confirm(self, count=10):
        with LuneQuotaStore(self.db) as store:
            store.confirm(count, time.time() + 600)

    def events(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            return db.execute('SELECT method, status FROM quota_events').fetchall()

    def invoke(self, func, path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(BaseException):
                func(self.url + path, {'code': SECRET, 'refresh_token': SECRET})
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())

    def test_missing_snapshot_never_sends(self):
        for func in (oauth._post_json, oauth._post_form):
            self.invoke(func, '/error')
        self.assertEqual([], Handler.calls)
        self.assertEqual([], self.events())

    def test_failure_consumes_and_hides_body(self):
        self.confirm()
        self.invoke(oauth._post_json, '/error')
        self.invoke(oauth._post_form, '/error')
        self.assertEqual(['/error', '/error'], Handler.calls)
        self.assertEqual(['oauth/register', 'oauth/token'], [row[0] for row in self.events()])
        with LuneQuotaStore(self.db) as store:
            self.assertEqual(8, store.status()['remaining_from_snapshot'])

    def test_redirect_never_followed(self):
        self.confirm()
        for func in (oauth._post_json, oauth._post_form):
            self.invoke(func, '/redirect')
        self.assertEqual(['/redirect', '/redirect'], Handler.calls)
        self.assertEqual(2, len(self.events()))

    def test_registration_and_token_bodies_are_bounded(self):
        self.confirm()
        sizes = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1):
                sizes.append(size)
                return b'x' * size
        class Opener:
            def open(self, *args, **kwargs): return Response()
        with patch.object(oauth, 'build_opener', return_value=Opener()), \
             contextlib.redirect_stdout(io.StringIO()):
            for func in (oauth._post_json, oauth._post_form):
                with self.assertRaises(SystemExit):
                    func('https://fixture.invalid', {'synthetic': 'input'})
        self.assertEqual(sizes, [128_001, 128_001])
        self.assertEqual(len(self.events()), 2)

    def test_existing_token_file_unchanged_on_registration_failure(self):
        token_path = Path(self.tmp.name) / 'token.json'
        original = json.dumps({'access_token': SECRET})
        token_path.write_text(original, encoding='utf-8')
        with patch.object(oauth, '_post_json', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, oauth.main(['--out', str(token_path)]))
        self.assertEqual(original, token_path.read_text(encoding='utf-8'))

    def test_callback_error_is_not_echoed_and_existing_file_is_unchanged(self):
        token_path = Path(self.tmp.name) / 'token.json'
        original = json.dumps({'client_id': 'public-client', 'access_token': SECRET})
        token_path.write_text(original, encoding='utf-8')
        class FakeServer:
            def __init__(self, *args):
                pass
            def serve_forever(self):
                self.result = {'error': SECRET, 'error_description': SECRET}
                self.done.set()
            def shutdown(self):
                pass
            def server_close(self):
                pass
        out = io.StringIO()
        with patch.object(oauth, 'ThreadingHTTPServer', FakeServer), contextlib.redirect_stdout(out):
            self.assertEqual(1, oauth.main(['--out', str(token_path)]))
        self.assertNotIn(SECRET, out.getvalue())
        self.assertIn('external and unmetered', out.getvalue())
        self.assertIn('recheck', out.getvalue())
        self.assertEqual(original, token_path.read_text(encoding='utf-8'))

    def test_failed_atomic_token_save_preserves_existing_file(self):
        token_path = Path(self.tmp.name) / 'token.json'
        original = json.dumps({'access_token': SECRET})
        token_path.write_text(original, encoding='utf-8')
        with patch.object(oauth.os, 'replace', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                oauth._save_record(token_path, {'access_token': 'new-synthetic'})
        self.assertEqual(original, token_path.read_text(encoding='utf-8'))
        self.assertEqual([token_path], list(token_path.parent.glob('token.json*')))

    def test_open_mode_does_not_print_sensitive_authorize_url(self):
        token_path = Path(self.tmp.name) / 'token.json'
        token_path.write_text(json.dumps({'client_id': SECRET}), encoding='utf-8')
        class FakeServer:
            def __init__(self, *args): pass
            def serve_forever(self):
                self.result = {'error': 'synthetic'}; self.done.set()
            def shutdown(self): pass
            def server_close(self): pass
        out = io.StringIO()
        with patch.object(oauth, 'ThreadingHTTPServer', FakeServer), \
             patch.object(oauth.webbrowser, 'open', return_value=True), \
             contextlib.redirect_stdout(out):
            self.assertEqual(1, oauth.main(['--out', str(token_path), '--open']))
        self.assertNotIn(SECRET, out.getvalue())
        self.assertNotIn('AUTHORIZE_URL=', out.getvalue())

    def test_registration_error_response_not_printed(self):
        out = io.StringIO()
        with patch.object(oauth, '_post_json', return_value={'error_description': SECRET}), contextlib.redirect_stdout(out):
            self.assertEqual(1, oauth.main(['--out', str(Path(self.tmp.name) / 'token.json')]))
        self.assertNotIn(SECRET, out.getvalue())

if __name__ == '__main__':
    unittest.main()
