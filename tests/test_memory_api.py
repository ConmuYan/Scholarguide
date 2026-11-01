import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from unittest.mock import patch

from scripts.verify_delivery import isolated_environment
from providers import ProviderStore
from webapp import ChatService, Jobs, create_server
from memory_store import MemoryStore

BASE = '/api/v1/memory/preferences'


class MemoryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'memory' / 'preferences.sqlite3'
        self.store_id = None
        env = isolated_environment(self.root)
        env.update(HOME=str(self.root), USERPROFILE=str(self.root), SG_MEMORY_STORE=str(self.db))
        envpatch = patch.dict(os.environ, env, clear=True)
        envpatch.start()
        self.addCleanup(envpatch.stop)
        original = socket.socket.connect
        def guarded(sock, address):
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', 'localhost', '::1'):
                raise AssertionError('network forbidden')
            return original(sock, address)
        guard = patch.object(socket.socket, 'connect', guarded)
        guard.start()
        self.addCleanup(guard.stop)
        providers = ProviderStore(self.root / 'providers.json')
        self.chat = ChatService(self.root / 'chat', providers=providers)
        self.server = create_server(0, jobs=Jobs(self.root / 'runs', providers=providers),
                                    chat=self.chat, providers=providers)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.chat.close()

    def request(self, method, path=BASE, body=None, headers=None):
        con = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        con.request(method, path, None if body is None else json.dumps(body),
                    {'Content-Type': 'application/json', **(headers or {})})
        response = con.getresponse()
        result = response.status, json.loads(response.read())
        con.close()
        if method == 'GET' and path == BASE and result[0] == 200:
            self.store_id = result[1]['store_id']
        return result

    def grant(self, key='reading_goal', value='frontier', revision=0, **kw):
        if self.store_id is None:
            self.request('GET')
        body = dict(value=value, consent=True, expires_at=time.time()+300, expected_revision=revision,
                    store_id=self.store_id or 'a' * 64)
        body.update(kw)
        return self.request('POST', BASE + '/' + key, body)

    def test_empty_and_validation(self):
        status, snapshot = self.request('GET')
        self.assertEqual(status, 200)
        self.assertEqual(snapshot['preferences'], {})
        self.assertEqual(snapshot['active'], {})
        self.assertTrue(self.db.exists())
        for kw in ({'consent': 1}, {'consent': False}, {'extra': 'SECRET'},
                   {'expires_at': 0}, {'value': 'SECRET'}, {'expected_revision': True}):
            status, body = self.grant(**kw)
            self.assertEqual(status, 400, body)
            self.assertNotIn('SECRET', json.dumps(body))
        self.assertEqual(self.grant(key='SECRET')[0], 400)
        self.assertTrue(self.db.exists())
        self.assertNotEqual(self.request('GET', BASE + '/reading_goal')[0], 200)
        self.assertEqual(self.request('POST', BASE, {})[0], 404)
        self.assertEqual(self.request('GET', headers={'Origin': 'https://evil.example'})[0], 403)

    def test_lifecycle_and_conflict(self):
        self.assertEqual(self.grant()[0], 200)
        status, snapshot = self.request('GET')
        self.assertEqual(snapshot['active'], {'reading_goal': 'frontier'})
        self.assertEqual(snapshot['preferences']['reading_goal']['revision'], 1)
        self.assertEqual(self.grant(value='general', revision=0)[0], 409)
        self.assertEqual(self.request('GET')[1]['active']['reading_goal'], 'frontier')
        self.assertEqual(self.request('POST', BASE + '/reading_goal/revoke', {'expected_revision': 1, 'store_id': self.store_id})[0], 200)
        row = self.request('GET')[1]['preferences']['reading_goal']
        self.assertEqual((row['revision'], row['state'], row['value'], row['consent']), (2, 'revoked', None, False))
        self.assertEqual(self.grant(revision=2)[0], 200)
        self.assertEqual(self.request('POST', BASE + '/reading_goal/delete', {'expected_revision': 3, 'store_id': self.store_id})[0], 200)
        self.assertEqual(self.request('GET')[1]['preferences']['reading_goal']['state'], 'deleted')
        self.assertEqual(self.request('POST', BASE + '/reading_goal/delete', {'expected_revision': 4, 'store_id': self.store_id, 'secret': 'SECRET'})[0], 400)
        self.assertEqual(self.grant('prefer_recent', True, 0, expires_at=time.time()+.15)[0], 200)
        time.sleep(.2)
        snapshot = self.request('GET')[1]
        self.assertNotIn('prefer_recent', snapshot['active'])
        self.assertEqual(snapshot['preferences']['prefer_recent']['state'], 'expired')
        self.assertEqual(snapshot['preferences']['prefer_recent']['revision'], 2)

    def test_concurrent_clients_and_process_persistence(self):
        self.assertEqual(self.grant()[0], 200)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda v: self.grant(value=v, revision=1)[0], ('general', 'comparison')))
        self.assertEqual(sorted(results), [200, 409])
        code = ("import sys; from memory_store import MemoryStore, RevisionConflict; "
                "s=MemoryStore(sys.argv[1]); "
                "\ntry: s.grant('reading_goal','orientation',consent=True,expires_at=9999999999,expected_revision=1)"
                "\nexcept RevisionConflict: print('conflict')"
                "\nfinally: s.close()")
        child = subprocess.run([sys.executable, '-c', code, str(self.db)], capture_output=True, text=True, timeout=10)
        self.assertEqual((child.returncode, child.stdout.strip()), (0, 'conflict'), child.stderr)
        self.assertEqual(self.request('GET')[1]['preferences']['reading_goal']['revision'], 2)

    def test_concurrent_processes_and_server_restart(self):
        self.assertEqual(self.grant()[0], 200)
        code = ("import sys; from memory_store import MemoryStore, RevisionConflict; "
                "s=MemoryStore(sys.argv[1]); print('ready',flush=True); input(); "
                "\ntry: s.grant('reading_goal',sys.argv[2],consent=True,expires_at=9999999999,expected_revision=1); print('committed')"
                "\nexcept RevisionConflict: print('conflict')"
                "\nfinally: s.close()")
        children = [subprocess.Popen([sys.executable, '-c', code, str(self.db), value],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for value in ('comparison', 'general')]
        try:
            for child in children:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
            for child in children:
                child.stdin.write('\n')
                child.stdin.flush()
            results = [child.communicate(timeout=10) for child in children]
            self.assertEqual(sorted(out.strip() for out, err in results), ['committed', 'conflict'], results)
            self.assertTrue(all(child.returncode == 0 for child in children), results)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        before = self.request('GET')[1]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        providers = ProviderStore(self.root / 'providers.json')
        self.server = create_server(0, jobs=Jobs(self.root / 'runs', providers=providers),
                                    chat=self.chat, providers=providers)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.request('GET')[1], before)

    def test_default_path_and_fault_redaction(self):
        with patch.dict(os.environ, {'SG_MEMORY_STORE': ''}), patch('webapp.Path.home', return_value=self.root):
            self.assertEqual(self.request('GET')[1]['preferences'], {})
            self.assertTrue((self.root / '.scholarguide').exists())
            self.assertEqual(self.grant()[0], 200)
            self.assertTrue((self.root / '.scholarguide' / 'memory.sqlite3').exists())
        self.request('GET')
        with patch('webapp.MemoryStore', side_effect=OSError('SECRET private credential path')):
            status, result = self.grant()
        self.assertEqual(status, 500)
        self.assertFalse(result['error']['retryable'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertIn('read preferences again', result['error']['message'])

    def test_duplicate_fields_and_activity(self):
        before = self.request('GET', '/api/activity')[1]
        con = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        payload = '{"value":"frontier","consent":false,"consent":true,"expires_at":9999999999,"expected_revision":0}'
        con.request('POST', BASE + '/reading_goal', payload, {'Content-Type': 'application/json'})
        response = con.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        con.close()
        self.assertFalse(self.db.exists())
        self.assertEqual(self.grant()[0], 200)
        self.assertEqual(self.request('GET', '/api/activity')[1], before)

    def test_empty_existing_file_and_openapi(self):
        self.db.parent.mkdir()
        self.db.touch()
        self.assertEqual(self.request('GET')[0], 500)
        self.assertEqual(self.grant()[0], 500)
        self.assertEqual(self.db.read_bytes(), b'')
        status, schema = self.request('GET', '/openapi.json')
        self.assertEqual(status, 200)
        for path in (BASE, BASE + '/{key}', BASE + '/{key}/revoke', BASE + '/{key}/delete'):
            self.assertIn(path, schema['paths'])

    def test_foreign_schema_and_failure_are_safe(self):
        self.db.parent.mkdir()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE foreign_table (secret TEXT)')
            con.execute("INSERT INTO foreign_table VALUES ('SECRET')")
        before = self.db.read_bytes()
        for method, path, body in [('GET', BASE, None), ('POST', BASE+'/reading_goal',
             {'value': 'general', 'consent': True, 'expires_at': time.time()+100, 'expected_revision': 0, 'store_id': 'a' * 64})]:
            status, result = self.request(method, path, body)
            self.assertEqual(status, 500, result)
            self.assertFalse(result['error']['retryable'])
            self.assertNotIn('SECRET', json.dumps(result))
            self.assertNotIn(str(self.db), json.dumps(result))
        self.assertEqual(self.db.read_bytes(), before)

    def test_existing_store_removed_after_check_is_not_recreated(self):
        for method in ('GET', 'POST'):
            with self.subTest(method=method):
                if not self.db.exists():
                    self.assertEqual(self.grant()[0], 200)
                moved = self.root / (method + '.sqlite3')
                def move_then_open(path, *args, **kwargs):
                    self.db.rename(moved)
                    return MemoryStore(path, *args, **kwargs)
                with patch('webapp.MemoryStore', side_effect=move_then_open):
                    status, result = self.request('GET') if method == 'GET' else self.grant(revision=1)
                self.assertEqual(status, 500, result)
                self.assertFalse(result['error']['retryable'])
                self.assertFalse(self.db.exists())
                self.assertNotIn(str(self.db), json.dumps(result))
                moved.rename(self.db)

    def test_concurrent_first_grant(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda value: self.grant(value=value)[0], ('general', 'frontier')))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(self.request('GET')[1]['preferences']['reading_goal']['revision'], 1)
