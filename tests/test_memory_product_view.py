"""Offline tests for opt-in presentation of committed Chat results."""
import json
import os
from http.client import HTTPConnection
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from memory_store import MemoryStore
from scripts.verify_delivery import isolated_environment
from webapp import ChatService, create_server


class ProductViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'memory.sqlite3'
        env = isolated_environment(self.root)
        env['SG_MEMORY_STORE'] = str(self.db)
        env_patch = patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.chat = ChatService(self.root / 'chat')
        self.server = create_server(0, chat=self.chat)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        created = self.chat.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        self.session = created['session_id']
        record = self.chat.sessions[self.session]
        result_path = record['directory'] / 'turn-1' / 'result.json'
        saved = json.loads(result_path.read_text(encoding='utf-8'))
        for index, paper in enumerate(record['results'][0]['papers']):
            paper['year'] = 2000 + index
            saved['papers'][index]['year'] = paper['year']
        result_path.write_text(json.dumps(saved), encoding='utf-8')

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.chat.close()

    def request(self, body, session=None, turn=1):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        conn.request('POST', f'/api/chat/sessions/{session or self.session}/turns/{turn}/presentation',
                     json.dumps(body), {'Content-Type': 'application/json'})
        response = conn.getresponse()
        result = response.status, response.getheader('Cache-Control'), json.loads(response.read())
        conn.close()
        return result

    def test_opt_in_and_revoke_preserve_original(self):
        before = self.chat.snapshot(self.session)
        files = {p.relative_to(self.root): p.read_bytes() for p in (self.root / 'chat').rglob('*') if p.is_file()}
        papers = before['history'][0]['papers']
        self.assertGreater(len(papers), 1)
        with MemoryStore(self.db) as store:
            store.grant('prefer_recent', True, consent=True, expires_at=time.time() + 300,
                        expected_revision=0)
        status, cache, result = self.request({'selected': True})
        self.assertEqual((status, cache), (200, 'no-store'))
        self.assertEqual(result['status'], 'applied')
        self.assertNotEqual(result['paper_ids'], [p['id'] for p in papers])
        self.assertEqual(result['paper_ids'], [p['id'] for p in sorted(
            papers, key=lambda p: (0, -p['year']) if type(p['year']) is int and 1 <= p['year'] <= 9999 else (1, 0))])
        self.assertEqual(self.chat.snapshot(self.session), before)
        self.assertEqual({p.relative_to(self.root): p.read_bytes() for p in (self.root / 'chat').rglob('*') if p.is_file()}, files)
        with MemoryStore(self.db) as store:
            store.revoke('prefer_recent', expected_revision=1)
        self.assertEqual(self.request({'selected': True})[2], {'status': 'not_applied', 'paper_ids': []})

    def test_database_change_during_presentation_fails_closed(self):
        from memory_context import MemoryContext
        with MemoryStore(self.db) as store:
            store.grant('prefer_recent', True, consent=True, expires_at=time.time() + 300,
                        expected_revision=0)
        original = MemoryContext.hints
        def revoke(ctx, *, explicit_fields):
            result = original(ctx, explicit_fields=explicit_fields)
            with MemoryStore(self.db) as store:
                store.revoke('prefer_recent', expected_revision=1)
            return result
        with patch.object(MemoryContext, 'hints', revoke):
            self.assertEqual(self.request({'selected': True})[2], {'status': 'not_applied', 'paper_ids': []})

    def test_default_host_store_matches_settings_without_environment_override(self):
        home = self.root / 'default-home'
        home.mkdir()
        env = isolated_environment(self.root / 'second-host')
        env.pop('SG_MEMORY_STORE', None)
        env.update(HOME=str(home), USERPROFILE=str(home))
        with patch.dict(os.environ, env, clear=True):
            db = Path.home() / '.scholarguide' / 'memory.sqlite3'
            db.parent.mkdir(parents=True, exist_ok=True)
            with MemoryStore(db) as store:
                store.grant('prefer_recent', True, consent=True, expires_at=time.time() + 300,
                            expected_revision=0)
            server = create_server(0, chat=self.chat)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                conn.request('POST', f'/api/chat/sessions/{self.session}/turns/1/presentation',
                             json.dumps({'selected': True}), {'Content-Type': 'application/json'})
                response = conn.getresponse()
                result = json.loads(response.read())
                conn.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(result['status'], 'applied')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_explicit_false_recent_and_year_constraints_override_memory(self):
        with MemoryStore(self.db) as store:
            store.grant('prefer_recent', True, consent=True, expires_at=time.time() + 300,
                        expected_revision=0)
        for data in (
            {'mode': 'demo', 'message': 'Recommend five RAG papers.',
             'request_data': {'topic': 'RAG', 'target_count': 5, 'prefer_recent': False}},
            {'mode': 'demo', 'message': 'Recommend five RAG papers.',
             'constraints': {'prefer_recent': False}},
        ):
            with self.subTest(data=data):
                session = self.chat.create(data)['session_id']
                task = self.chat.sessions[session]['store'].current_task(session)['task']
                self.assertIn('prefer_recent', task['_presentation_user_fields'])
                self.assertEqual(self.request({'selected': True}, session=session)[2],
                                 {'status': 'not_applied', 'paper_ids': []})

    def test_legacy_task_without_explicit_scope_is_not_changed(self):
        with MemoryStore(self.db) as store:
            store.grant('prefer_recent', True, consent=True, expires_at=time.time() + 300,
                        expected_revision=0)
        store = self.chat.sessions[self.session]['store']
        task = store.current_task(self.session)
        task['task'].pop('_presentation_user_fields', None)
        store.save_task(self.session, task['revision'], task['task'])
        self.assertEqual(self.request({'selected': True})[2], {'status': 'not_applied', 'paper_ids': []})
        self.assertNotIn('_presentation_user_fields', store.current_task(self.session)['task'])

    def test_invalid_requests_fail_closed(self):
        for body in ({}, {'selected': False}, {'selected': 1}, {'selected': True, 'papers': []},
                     {'selected': True, 'path': str(self.db)}):
            self.assertNotEqual(self.request(body)[2].get('status'), 'applied')
        self.assertNotEqual(self.request({'selected': True}, session='a' * 32)[2].get('status'), 'applied')
        self.assertNotEqual(self.request({'selected': True}, turn=999)[2].get('status'), 'applied')


if __name__ == '__main__':
    unittest.main()
