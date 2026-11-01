from http.client import HTTPConnection
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webapp import ChatService, DeletionUnavailable, Jobs, create_server, validate_input
from recovery import JobLease


def _hold_lease(directory, ready, release):
    lease = JobLease(Path(directory))
    try:
        ready.set()
        release.wait(10)
        (Path(directory) / 'worker-finished').write_text('ok', encoding='utf-8')
    finally:
        lease.close()


class DisabledDeletionTests(unittest.TestCase):
    def test_chat_reference_survives_rejection_and_remains_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = ChatService(output=Path(tmp))
            try:
                sid = chat.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})['session_id']
                record = chat.sessions[sid]
                with patch.object(chat, '_load_record', side_effect=AssertionError('loaded')), \
                        patch('webapp.shutil.rmtree', side_effect=AssertionError('deleted')):
                    with self.assertRaises(DeletionUnavailable):
                        chat.delete(sid)
                self.assertIs(chat.sessions[sid], record)
                chat.send(sid, {'message': 'Add one survey paper.'})
                self.assertEqual(len(chat.snapshot(sid)['history']), 2)
            finally:
                chat.close()

    def test_delete_never_inspects_paths_or_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(Path, 'resolve', side_effect=AssertionError('resolved')), \
                    patch('webapp.shutil.rmtree', side_effect=AssertionError('deleted')):
                for service in (Jobs(output=root), ChatService(output=root)):
                    for sid in ('a' * 32, '../outside', ''):
                        with self.assertRaises(DeletionUnavailable):
                            service.delete(sid)
            self.assertEqual(list(root.iterdir()), [])

    def test_cross_process_owner_and_failed_deletion_leave_data_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sid = 'a' * 32
            directory = root / sid
            directory.mkdir()
            marker = directory / 'data'
            marker.write_text('keep', encoding='utf-8')
            ctx = multiprocessing.get_context('spawn')
            ready, release = ctx.Event(), ctx.Event()
            worker = ctx.Process(target=_hold_lease, args=(str(directory), ready, release))
            worker.start()
            try:
                self.assertTrue(ready.wait(10))
                with patch('webapp.shutil.rmtree', side_effect=AssertionError('unsafe delete')):
                    for service in (Jobs(output=root), ChatService(output=root)):
                        with self.assertRaises(DeletionUnavailable):
                            service.delete(sid)
                self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')
            finally:
                release.set()
                worker.join(15)
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
            self.assertEqual(worker.exitcode, 0)
            self.assertEqual((directory / 'worker-finished').read_text(encoding='utf-8'), 'ok')

    def test_links_to_root_and_outside_are_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, outside = base / 'root', base / 'outside'
            root.mkdir(); outside.mkdir()
            (root / 'keep').write_text('root', encoding='utf-8')
            (outside / 'keep').write_text('outside', encoding='utf-8')
            for sid, target in (('a' * 32, root), ('b' * 32, outside)):
                try:
                    (root / sid).symlink_to(target, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f'directory symlinks unavailable: {exc}')
                for service in (Jobs(output=root), ChatService(output=root)):
                    with self.assertRaises(DeletionUnavailable):
                        service.delete(sid)
                self.assertTrue((root / sid).is_symlink())
            self.assertEqual((root / 'keep').read_text(encoding='utf-8'), 'root')
            self.assertEqual((outside / 'keep').read_text(encoding='utf-8'), 'outside')


class WebTests(unittest.TestCase):
    def setUp(self):
        self.jobs = Jobs(output=Path(tempfile.mkdtemp()), runner=lambda data, progress: __import__('demo').demo_run(data['scenario'], data['method'], progress=progress))
        self.server = create_server(0, self.jobs)
        self.thread = __import__('threading').Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = '127.0.0.1'
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, host=None):
        connection = HTTPConnection(self.host, self.port, timeout=5)
        headers = {'Host': host or f'{self.host}:{self.port}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body).encode()
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, response.getheader('Content-Type'), raw

    def test_assets_and_loopback_host_guard(self):
        status, content_type, body = self.request('GET', '/search')
        self.assertEqual(status, 200)
        self.assertIn('text/html', content_type)
        self.assertIn(b'ScholarGuide', body)
        status, _, _ = self.request('GET', '/search', host='evil.example')
        self.assertEqual(status, 403)

    def test_demo_job_progress_and_download(self):
        status, _, body = self.request('POST', '/api/jobs', {'mode':'demo','method':'A','scenario':'followup'})
        self.assertEqual(status, 202)
        job_id = json.loads(body)['id']
        for _ in range(100):
            status, _, body = self.request('GET', '/api/jobs/' + job_id)
            self.assertEqual(status, 200)
            job = json.loads(body)
            if job['state'] == 'done':
                break
            time.sleep(.02)
        else:
            self.fail('demo job did not finish')
        self.assertEqual(job['result']['status'], 'complete')
        self.assertGreaterEqual(len(job['trace']), 5)
        status, content_type, body = self.request('GET', '/api/jobs/' + job_id + '/report.md')
        self.assertEqual(status, 200)
        self.assertIn('text/markdown', content_type)
        self.assertIn(b'OFFLINE FIXTURE DEMO', body)

    def test_delete_completed_job_and_reject_running_job(self):
        status, _, body = self.request('POST', '/api/jobs', {'mode':'demo','method':'A','scenario':'satisfied'})
        self.assertEqual(status, 202)
        job_id = json.loads(body)['id']
        for _ in range(100):
            snapshot = self.jobs.snapshot(job_id)
            if snapshot and snapshot['state'] == 'done':
                break
            time.sleep(.02)
        else:
            self.fail('demo job did not finish')
        status, _, raw = self.request('DELETE', '/api/jobs/' + job_id)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)['error_code'], 'deletion_unavailable')
        self.assertEqual(self.request('GET', '/api/jobs/' + job_id)[0], 200)
        self.assertEqual(self.request('DELETE', '/api/jobs/' + job_id)[0], 503)

        gate = __import__('threading').Event()
        release = __import__('threading').Event()
        def slow(data, progress):
            gate.set(); release.wait(3)
            return __import__('demo').demo_run('satisfied', 'A', progress=progress)
        self.jobs.runner = slow
        try:
            status, _, raw = self.request('POST', '/api/jobs', {'mode':'demo','method':'A','scenario':'satisfied'})
            running_id = json.loads(raw)['id']
            self.assertTrue(gate.wait(1))
            self.assertEqual(self.request('DELETE', '/api/jobs/' + running_id)[0], 503)
            self.assertTrue((self.jobs.output / running_id).exists())
        finally:
            release.set()

    def test_multi_source_is_accepted_by_job_validation(self):
        data = validate_input({'mode': 'live', 'method': 'A', 'source': 'multi',
                               'cache': True, 'reading_goal': 'orientation',
                               'request': 'Find RAG papers.'})
        self.assertEqual(data['source'], 'multi')
        self.assertEqual(data['reading_goal'], 'orientation')
        with self.assertRaises(Exception):
            validate_input({'mode': 'live', 'method': 'A', 'source': 'made_up',
                            'cache': True, 'request': 'Find RAG papers.'})

    def test_job_snapshot_survives_memory_loss(self):
        job = 'c' * 32
        directory = Path(tempfile.mkdtemp()) / 'runs' / job
        directory.mkdir(parents=True)
        result = {'status': 'complete', 'mode': 'demo', 'method': 'A',
                  'started_at': '2026-10-19T00:00:00Z', 'papers': [], 'trace': [{'step': 1}]}
        (directory / 'result.json').write_text(json.dumps(result), encoding='utf-8')
        jobs = Jobs(output=directory.parent)
        snap = jobs.snapshot(job)
        self.assertEqual(snap['state'], 'done')
        self.assertEqual(snap['result']['status'], 'complete')
        self.assertIsNone(jobs.snapshot('../' + job))

    def test_second_job_is_rejected_while_first_is_running(self):
        gate = __import__('threading').Event()
        release = __import__('threading').Event()
        def slow(data, progress):
            gate.set(); release.wait(2)
            return __import__('demo').demo_run('satisfied', 'A', progress=progress)
        self.jobs.runner = slow
        status, _, body = self.request('POST', '/api/jobs', {'mode':'demo','method':'A','scenario':'satisfied'})
        self.assertEqual(status, 202)
        self.assertTrue(gate.wait(1))
        status, _, body = self.request('POST', '/api/jobs', {'mode':'demo','method':'A','scenario':'satisfied'})
        self.assertEqual(status, 409)
        release.set()


if __name__ == '__main__':
    unittest.main()
