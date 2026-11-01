"""Real process restart: saved ChatService sessions must rehydrate and resume."""
from http.client import HTTPConnection
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(port, method, path, body=None, timeout=20):
    con = HTTPConnection('127.0.0.1', port, timeout=timeout)
    headers = {'Host': f'127.0.0.1:{port}'}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    con.request(method, path, body=payload, headers=headers)
    response = con.getresponse()
    raw = response.read()
    con.close()
    return response.status, json.loads(raw.decode('utf-8')) if raw else {}


class ProcessRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self, port):
        proc = subprocess.Popen([PYTHON, 'app.py', 'web', '--port', str(port),
                                 '--runs', str(self.runs)],
                                cwd=str(ROOT), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                status, _ = request(port, 'GET', '/api/chat/sessions', timeout=1)
                if status == 200:
                    return proc
            except OSError:
                pass
            time.sleep(.1)
        proc.terminate()
        self.fail('server did not start')

    def test_saved_chat_survives_real_process_restart(self):
        port = free_port()
        first = self._start(port)
        try:
            status, created = request(port, 'POST', '/api/chat/sessions', {
                'mode': 'demo', 'scenario': 'followup', 'reuse': True,
                'message': 'Recommend five RAG papers including one evaluation paper.'})
            self.assertEqual(status, 201)
            session_id = created['session_id']
            self.assertTrue(created['result']['papers'])
        finally:
            first.terminate()
            first.wait(10)
        second = self._start(port)
        try:
            status, snapshot = request(port, 'GET', '/api/chat/sessions/' + session_id)
            self.assertEqual(status, 200)
            self.assertTrue(snapshot['history'])
            self.assertTrue(snapshot['history'][0]['papers'])
            status, listed = request(port, 'GET', '/api/chat/sessions')
            self.assertEqual(status, 200)
            self.assertIn(session_id, [item['session_id'] for item in listed['sessions']])
            status, resumed = request(port, 'POST', f'/api/chat/sessions/{session_id}/messages',
                                      {'message': 'Add one survey paper.'})
            self.assertEqual(status, 200)
            self.assertIn(resumed['result']['status'], ('complete', 'partial', 'unsupported'))
        finally:
            second.terminate()
            second.wait(10)


if __name__ == '__main__':
    unittest.main()
