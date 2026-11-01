"""Stable research workspaces and honest interruption state; all models are Demo."""
from http.client import HTTPConnection
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import GuideError
from demo import demo_run
from recovery import atomic_json
from webapp import ChatService, Jobs, TurnRunning, create_server


class SessionWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = ChatService(self.root / 'web')
        self.server = create_server(0, jobs=Jobs(self.root / 'runs'), chat=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.service.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        client.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                       headers={'Content-Type': 'application/json'})
        response = client.getresponse()
        status, value = response.status, json.loads(response.read())
        client.close()
        return status, value

    def create(self):
        return self.service.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})['session_id']

    def finish(self, sid):
        for _ in range(200):
            snapshot = self.service.snapshot(sid)
            if snapshot['status'] != 'running':
                return snapshot
            time.sleep(.02)
        self.fail('Demo background turn did not finish')

    def test_title_is_stable_and_rename_survives_restart_without_mutating_evidence(self):
        sid = self.create()
        title = self.service.snapshot(sid)['title']
        self.service.send(sid, {'message': 'Add one survey paper.'})
        self.assertEqual(self.service.list_sessions()[0]['title'], title)
        directory = self.root / 'web' / sid
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in directory.glob('turn-*/*') if path.is_file()}
        status, renamed = self.request('POST', f'/api/chat/sessions/{sid}/rename', {'title': '  My   RAG workspace  '})
        self.assertEqual(status, 200)
        self.assertEqual(renamed['title'], 'My RAG workspace')
        self.assertTrue(renamed['title_is_custom'])
        restored = ChatService(self.root / 'web')
        try:
            self.assertEqual(restored.snapshot(sid)['title'], renamed['title'])
            self.assertEqual(restored.list_sessions()[0]['title'], renamed['title'])
            reset = restored.rename(sid, {'title': ''})
            self.assertEqual(reset['title'], title)
            self.assertFalse(reset['title_is_custom'])
        finally:
            restored.close()
        self.assertEqual(before, {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in directory.glob('turn-*/*') if path.is_file()})

    def test_rename_validation_preserves_metadata_and_legacy_default(self):
        sid = self.create()
        path = self.root / 'web' / sid / 'meta.json'
        before = path.read_bytes()
        for bad in ({}, {'title': 4}, {'title': 'x' * 81}, {'title': 'ok', 'mode': 'live'}, []):
            status, _ = self.request('POST', f'/api/chat/sessions/{sid}/rename', bad)
            self.assertEqual(status, 400)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.request('POST', f"/api/chat/sessions/{'a' * 32}/rename", {'title': 'a'})[0], 404)
        meta = json.loads(before)
        for key in ('display_title', 'default_title', 'title_is_custom'):
            meta.pop(key, None)
        atomic_json(path, meta)
        restored = ChatService(self.root / 'web')
        try:
            first = restored.snapshot(sid)['title']
            restored.send(sid, {'message': 'Add a survey.'})
            self.assertEqual(restored.snapshot(sid)['title'], first)
        finally:
            restored.close()

    def test_background_failure_has_safe_durable_error_and_retains_last_result(self):
        sid = self.create()
        record = self.service.sessions[sid]
        with patch.object(record['chat'], 'send', side_effect=ValueError('secret-token=https://private.invalid')):
            self.service.send(sid, {'message': 'A new explicit request', 'stream': True})
            failed = self.finish(sid)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['error_code'], 'turn_failed')
        self.assertEqual(len(failed['history']), 1)
        restored = ChatService(self.root / 'web')
        try:
            again = restored.snapshot(sid)
            self.assertEqual(again['error'], failed['error'])
            self.assertEqual(restored.list_sessions()[0]['status'], 'failed')
            self.assertNotIn('secret-token', json.dumps(again))
            self.assertNotIn('secret-token', (record['directory'] / 'runtime.json').read_text(encoding='utf-8'))
        finally:
            restored.close()

    def test_interrupted_turn_retains_results_and_never_replays_on_http_read(self):
        sid = self.create()
        path = self.root / 'web' / sid
        before = (path / 'turn-1' / 'result.json').read_bytes()
        atomic_json(path / 'runtime.json', {'state': 'running', 'expected_turn': 2})
        with patch('webapp.ChatSession.send', side_effect=AssertionError('automatic replay')):
            status, snapshot = self.request('GET', f'/api/chat/sessions/{sid}')
            self.assertEqual(status, 200)
            self.assertEqual(snapshot['status'], 'failed')
            self.assertEqual(snapshot['error_code'], 'turn_interrupted')
            self.assertTrue(snapshot['interrupted'])
            self.assertFalse(snapshot['recovery']['can_resume'])
            self.assertFalse(snapshot['capabilities']['can_retry'])
            self.assertEqual(len(snapshot['history']), 1)
        self.assertEqual((path / 'turn-1' / 'result.json').read_bytes(), before)

    def test_result_commit_wins_over_stale_running_marker(self):
        sid = self.create()
        atomic_json(self.root / 'web' / sid / 'runtime.json', {'state': 'running', 'expected_turn': 1})
        snapshot = self.service.snapshot(sid)
        self.assertEqual(snapshot['status'], 'complete')
        self.assertFalse(snapshot['interrupted'])
        self.assertEqual(snapshot['error'], '')

    def test_second_service_cannot_duplicate_running_turn_and_observes_committed_result(self):
        sid = self.create()
        record = self.service.sessions[sid]
        original = record['chat'].send
        started, release = threading.Event(), threading.Event()
        def blocked(message):
            started.set()
            release.wait(5)
            return original(message)
        observer = ChatService(self.root / 'web')
        try:
            with patch.object(record['chat'], 'send', blocked):
                self.service.send(sid, {'message': 'Add one survey.', 'stream': True})
                self.assertTrue(started.wait(2))
                self.assertEqual(observer.snapshot(sid)['status'], 'running')
                with self.assertRaises(TurnRunning):
                    observer.send(sid, {'message': 'Do not duplicate this turn.', 'stream': True})
                self.assertEqual(self.request('POST', f'/api/chat/sessions/{sid}/rename', {'title': 'blocked'})[0], 409)
                release.set()
                self.finish(sid)
            snapshot = observer.snapshot(sid)
            self.assertEqual(len(snapshot['history']), 2)
            self.assertEqual(snapshot['status'], 'complete')
        finally:
            release.set()
            observer.close()

    def test_search_import_keeps_partial_coverage_and_safe_current_task(self):
        result = demo_run('followup', 'A')
        result['status'] = 'partial'
        result['request']['private_config'] = {'api_key': 'must-not-return'}
        result['research_coverage'] = {'status': 'partial', 'facets': [
            {'id': 'dynamic', 'label': 'Dynamic scenes'}], 'missing': ['dynamic']}
        sid = self.service.import_run(result, 'b' * 32)
        for snapshot in (self.service.snapshot(sid), self.service.list_sessions()[0]):
            self.assertEqual(snapshot['coverage_status'], 'partial')
            self.assertEqual(snapshot['missing_facets'], [{'id': 'dynamic', 'label': 'Dynamic scenes'}])
            self.assertEqual(snapshot['paper_count'], len(result['papers']))
            self.assertNotIn('private_config', snapshot['current_task'])
        self.assertEqual(self.service.import_run(result, 'b' * 32), sid)

    def test_hard_kill_exposes_interruption_without_replaying_a_turn(self):
        child_root = self.root / 'killed'
        child_root.mkdir()
        child = subprocess.Popen([sys.executable, '-c', '''
import sys, time
from pathlib import Path
from webapp import ChatService
root=Path(sys.argv[1]); service=ChatService(root/'web')
sid=service.create({'mode':'demo','message':'Recommend five RAG papers.'})['session_id']
(root/'sid').write_text(sid)
def blocked(message):
    (root/'started').write_text('started')
    time.sleep(120)
service.sessions[sid]['chat'].send=blocked
service.send(sid, {'message':'An in-flight local Demo turn','stream':True})
time.sleep(120)
''', str(child_root)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 20
            while not (child_root / 'started').exists() and time.monotonic() < deadline:
                if child.poll() is not None:
                    self.fail('Demo child exited before the claimed turn')
                time.sleep(.05)
            self.assertTrue((child_root / 'started').exists())
            child.kill()
            child.wait(5)
            sid = (child_root / 'sid').read_text()
            restored = ChatService(child_root / 'web')
            try:
                snapshot = restored.snapshot(sid)
                self.assertEqual(snapshot['error_code'], 'turn_interrupted')
                self.assertTrue(snapshot['interrupted'])
                self.assertFalse(snapshot['recovery']['can_resume'])
                self.assertEqual(len(snapshot['history']), 1)
                self.assertEqual(len(list((child_root / 'web' / sid).glob('turn-*/result.json'))), 1)
            finally:
                restored.close()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(5)


if __name__ == '__main__':
    unittest.main()
