"""Durable Search and bounded native I/O, using only local deterministic fakes."""
from __future__ import annotations

from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config, GuideError, RunCancelled
from demo import DemoLLM, DemoSource
from network import execution_context, urlopen
from recovery import (JobLease, JournalLLM, JournalSource, RecoveryUnsafe,
                      SearchJournal, atomic_json)
from webapp import Jobs, create_server


def _fixture_worker(directory, action, gate):
    """Executed in a genuinely separate process; the parent can hard-kill it."""
    import webapp
    directory = Path(directory)
    config = Config(provider='openai', base_url='http://fixture.invalid/v1',
                    model='recovery-local-fixture', batch_size=2, research_mode=False,
                    task_timeout=60, cache=False)

    def charge(kind):
        with (directory / 'charges.txt').open('a', encoding='utf-8') as stream:
            stream.write(kind + '\n')
            stream.flush()

    class Providers:
        def config(self, _base):
            return config

    class Source(DemoSource):
        mode = 'live'
        def __init__(self, _config, budget):
            super().__init__('satisfied')
            self.budget = budget
        def search(self, query, limit=10):
            charge('source')
            self.budget.search_http_calls += 1
            return super().search(query, limit)

    class LLM(DemoLLM):
        mode = 'live'
        def __init__(self, _config, budget):
            self.budget = budget
            self.events = []
        def generate(self, stage, *args):
            charge(stage)
            self.budget.llm_calls += 1
            self.budget.usage_calls += 1
            self.budget.usage_observed = True
            self.budget.input_tokens += 7
            self.budget.output_tokens += 3
            if action == 'start' and gate == 'pending' and stage == 'assess':
                (directory / 'gate').write_text('pending', encoding='utf-8')
                while True:
                    time.sleep(.05)
            return super().generate(stage, *args)

    actual_run = webapp.run
    def gated_run(*args, progress=None, **kwargs):
        def observe(event, checkpoint=None):
            progress(event, checkpoint=checkpoint)
            expected = 'search_result' if gate == 'search' else 'assess'
            if action == 'start' and gate != 'pending' and event['action'] == expected:
                (directory / 'gate').write_text(expected, encoding='utf-8')
                while True:
                    time.sleep(.05)
        observe.accepts_checkpoint = True
        return actual_run(*args, progress=observe, **kwargs)

    with patch.object(webapp, 'AcademicSource', Source), patch.object(webapp, 'JsonLLM', LLM), \
            patch.object(webapp, 'run', gated_run):
        providers = Providers()
        jobs = Jobs(output=directory / 'runs', providers=providers)
        if action == 'start':
            job_id = jobs.submit({'mode': 'live', 'method': 'A', 'source': 'arxiv',
                                 'request': 'Find five RAG papers.', 'research_mode': False,
                                 'cache': False})
            (directory / 'job-id').write_text(job_id, encoding='utf-8')
        else:
            job_id = (directory / 'job-id').read_text(encoding='utf-8')
            server = create_server(0, jobs=jobs, providers=providers)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('GET', '/api/jobs/' + job_id)
                response = connection.getresponse()
                before = json.loads(response.read())
                atomic_json(directory / 'restored.json', before)
                connection.request('POST', '/api/jobs/' + job_id + '/resume', body=b'{}',
                                   headers={'Content-Type': 'application/json'})
                response = connection.getresponse()
                resumed = json.loads(response.read())
                status = response.status
                connection.close()
                if status != 202:
                    atomic_json(directory / 'resume-error.json', {'status': status, **resumed})
                    return
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = jobs.snapshot(job_id)
            if value and value['state'] in ('done', 'error'):
                atomic_json(directory / 'observed.json', value)
                return
            time.sleep(.02)
        raise RuntimeError('fixture timed out')


def _http_parent_worker(directory, url):
    """Expose a genuine HTTP worker PID so parent-death cleanup is testable."""
    def observe():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            children = [child for child in multiprocessing.active_children()
                        if child.name == 'ScholarGuide HTTP']
            if children:
                (Path(directory) / 'http-child-pid').write_text(str(children[0].pid), encoding='utf-8')
                return
            time.sleep(.005)
    threading.Thread(target=observe, daemon=True).start()
    urlopen(url, timeout=10)


class DurableSearchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def worker(self, action, gate):
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker',
                                 str(self.directory), action, gate], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def interrupt(self, gate):
        child = self.worker('start', gate)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (self.directory / 'gate').exists() and (self.directory / 'job-id').exists():
                    break
                if child.poll() is not None:
                    self.fail(str(child.communicate()))
                time.sleep(.02)
            else:
                self.fail('child did not reach recorded crash boundary')
        finally:
            child.kill()
            child.communicate(timeout=5)
        self.assertIsNotNone(child.returncode)

    def resume(self, gate):
        child = self.worker('resume', gate)
        stdout, stderr = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, (stdout, stderr))

    def charges(self):
        return (self.directory / 'charges.txt').read_text(encoding='utf-8').splitlines()

    def test_real_process_kill_reuses_completed_plan_source_and_assessment(self):
        self.interrupt('assess')
        before = self.charges()
        self.assertEqual(before.count('plan'), 1)
        self.assertEqual(before.count('source'), 1)
        self.assertEqual(before.count('assess'), 1)
        self.resume('assess')
        restored = json.loads((self.directory / 'restored.json').read_text(encoding='utf-8'))
        self.assertEqual(restored['state'], 'needs_retry')
        self.assertTrue(restored['recovery']['can_resume'])
        self.assertEqual(len(restored['result']['papers']), 2)
        self.assertGreater(len(restored['result']['candidates']), 2)
        final = json.loads((self.directory / 'observed.json').read_text(encoding='utf-8'))
        self.assertEqual(final['state'], 'done')
        self.assertEqual(final['result']['status'], 'complete')
        self.assertEqual(len(final['result']['papers']), 5)
        self.assertTrue(all(p['evidence_verified'] for p in final['result']['papers']))
        after = self.charges()
        self.assertEqual(after.count('plan'), 1, 'recorded LLM plan was billed again')
        self.assertEqual(after.count('source'), 1, 'recorded search was called again')
        self.assertEqual(after.count('assess'), 3, 'recorded batch was assessed again')
        metrics = final['result']['metrics']
        self.assertEqual(metrics['llm_calls'], 4)
        self.assertEqual(metrics['input_tokens'], 28)
        self.assertEqual(metrics['recovery']['replayed_calls'], 3)
        self.assertEqual(metrics['recovery']['new_calls'], 2)

    def test_real_process_kill_exposes_candidates_before_assessment(self):
        self.interrupt('search')
        self.resume('search')
        restored = json.loads((self.directory / 'restored.json').read_text(encoding='utf-8'))
        self.assertEqual(restored['state'], 'needs_retry')
        self.assertTrue(restored['recovery']['can_resume'])
        self.assertGreater(len(restored['result']['candidates']), 0)
        self.assertEqual(restored['result']['papers'], [])
        self.assertEqual(self.charges().count('source'), 1)

    def test_real_process_kill_inside_external_call_never_reissues_unknown_charge(self):
        self.interrupt('pending')
        charges = self.charges()
        self.resume('pending')
        restored = json.loads((self.directory / 'restored.json').read_text(encoding='utf-8'))
        self.assertEqual(restored['state'], 'needs_retry')
        self.assertFalse(restored['recovery']['can_resume'])
        self.assertEqual(restored['recovery']['reason'], 'unknown_external_outcome')
        self.assertEqual(restored['recovery']['uncertain_calls'], 1)
        self.assertIn('费用', restored['recovery']['message'])
        self.assertTrue((self.directory / 'resume-error.json').exists())
        self.assertEqual(self.charges(), charges)

    def test_runtime_identity_input_signature_and_os_lease_block_unsafe_replay(self):
        config = Config(model='journal-fixture', api_key='NEVER_PERSIST_THIS_SECRET')
        path = self.directory / 'calls.json'
        journal = SearchJournal(path, config)
        budget = Budget(60)
        value = journal.call('llm.plan', {'input': 'a'}, lambda: {'answer': 1}, budget)
        self.assertEqual(value, {'answer': 1})
        for changed in (replace(config, model='other'), replace(config, base_url='https://other.invalid'),
                        replace(config, source='multi'), replace(config, batch_size=1)):
            with self.subTest(changed=changed.public()), self.assertRaises(RecoveryUnsafe):
                SearchJournal(path, changed, resume=True)
        resumed = SearchJournal(path, config, resume=True)
        with self.assertRaises(RecoveryUnsafe):
            resumed.call('llm.plan', {'input': 'b'}, lambda: self.fail('external work ran'), Budget(60))
        self.assertNotIn(config.api_key, path.read_text(encoding='utf-8'))
        self.assertNotIn(config.base_url, path.read_text(encoding='utf-8'))
        lease = JobLease(self.directory / 'lease')
        try:
            with self.assertRaises(RecoveryUnsafe):
                JobLease(self.directory / 'lease')
        finally:
            lease.close()
        JobLease(self.directory / 'lease').close()

    def test_completed_error_and_budget_are_replayed_without_new_request(self):
        path = self.directory / 'calls.json'
        config, budget = Config(), Budget(60)
        journal = SearchJournal(path, config)
        def failed():
            budget.llm_calls += 1
            budget.started -= 3
            raise GuideError('response was invalid after paid call')
        with self.assertRaises(GuideError):
            journal.call('llm.plan', {}, failed, budget)
        budget = Budget(60)
        resumed = SearchJournal(path, config, resume=True)
        with self.assertRaisesRegex(GuideError, 'paid call'):
            resumed.call('llm.plan', {}, lambda: self.fail('paid call repeated'), budget)
        self.assertEqual(budget.llm_calls, 1)
        self.assertGreaterEqual(time.monotonic() - budget.started, 3)

    def test_builtin_offline_demo_never_uses_research_assessment_schema(self):
        jobs = Jobs(output=self.directory / 'demo-runs')
        job_id = jobs.submit({'mode': 'demo', 'scenario': 'followup', 'method': 'A'})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            snapshot = jobs.snapshot(job_id)
            if snapshot['state'] != 'running':
                break
            time.sleep(.01)
        self.assertEqual(snapshot['state'], 'done')
        self.assertEqual(snapshot['result']['status'], 'complete')
        self.assertEqual(len(snapshot['result']['papers']), 5)
        self.assertFalse(snapshot['result']['config']['research_mode'])

    def test_export_preserves_research_coverage_and_escapes_model_fields(self):
        from report import markdown
        text = markdown({'mode': 'live', 'method': 'A', 'status': 'partial',
                         'stop_reason': 'budget', 'papers': [],
                         'research_coverage': {'scope': 'landscape', 'status': 'partial',
                             'facets': [{'id': 'A', 'label': '<script>route</script>', 'query': 'scene geometry'},
                                        {'id': 'B', 'label': 'Dynamic', 'query': 'dynamic scene'}],
                             'covered': ['A'], 'missing': ['B']}})
        self.assertIn('Research coverage', text)
        self.assertIn('scene geometry', text)
        self.assertIn('Missing: B', text)
        self.assertNotIn('<script>', text)
        self.assertIn('不代表穷尽研究方向', text)


class BoundedTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entered = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                cls.entered.set()
                if self.path == '/limited':
                    self.send_response(429)
                    self.send_header('Retry-After', '3')
                    self.end_headers()
                    self.wfile.write(b'{"error":"rate limit"}')
                    return
                if self.path == '/slow':
                    self.send_response(200)
                    self.send_header('Content-Length', '10000')
                    self.end_headers()
                    try:
                        for _ in range(1000):
                            self.wfile.write(b'.')
                            self.wfile.flush()
                            time.sleep(.02)
                    except (ConnectionError, OSError):
                        return
                else:
                    self.send_response(200)
                    self.send_header('x-model', 'local-fixture')
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def children(self):
        return [child for child in multiprocessing.active_children() if child.name == 'ScholarGuide HTTP']

    def test_response_headers_context_and_http_error_retry_after_are_preserved(self):
        elapsed = []
        for _ in range(3):
            start = time.monotonic()
            with urlopen(self.base + '/ok', timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get('X-Model'), 'local-fixture')
                self.assertEqual(json.loads(response.read()), {'ok': True})
            self.assertTrue(response.closed)
            elapsed.append(round(time.monotonic() - start, 4))
        self.assertFalse(self.children())
        with self.assertRaises(HTTPError) as context:
            urlopen(self.base + '/limited', timeout=3)
        error = context.exception
        self.assertEqual(error.code, 429)
        self.assertEqual(error.headers['Retry-After'], '3')
        self.assertIn(b'rate limit', error.read())
        error.close()
        print('LOCAL_HTTP_SPAWN_LATENCY_SECONDS=' + json.dumps(elapsed), flush=True)

    def test_slow_drip_absolute_deadline_reaps_worker(self):
        self.entered.clear()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            # Windows process startup competes with Chrome in the full suite.
            # The slow body lasts >20s; 2s still proves an absolute deadline.
            urlopen(self.base + '/slow', timeout=2.0)
        elapsed = time.monotonic() - start
        self.assertTrue(self.entered.is_set(), 'fixture did not reach the local server')
        self.assertLess(elapsed, 3.2)
        self.assertGreater(elapsed, 1.8)
        self.assertFalse(self.children(), 'timed-out process left behind')

    def test_cancel_during_body_read_terminates_and_reaps_worker(self):
        self.entered.clear()
        cancelled = threading.Event()
        errors = []
        def call():
            try:
                with execution_context(cancelled=cancelled.is_set):
                    urlopen(self.base + '/slow', timeout=10)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=call)
        thread.start()
        self.assertTrue(self.entered.wait(3))
        start = time.monotonic()
        cancelled.set()
        thread.join(1.5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RunCancelled)
        self.assertIn('remote usage', str(errors[0]))
        self.assertFalse(self.children(), 'cancelled process left behind')

    def test_http_worker_exits_after_its_parent_is_hard_killed(self):
        self.entered.clear()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            parent = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--http-parent',
                                       str(directory), self.base + '/slow'], cwd=ROOT,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            child_handle = None
            try:
                self.assertTrue(self.entered.wait(4))
                pid = int((directory / 'http-child-pid').read_text(encoding='utf-8'))
                if os.name == 'nt':
                    import ctypes
                    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                    kernel.OpenProcess.restype = ctypes.c_void_p
                    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                    child_handle = kernel.OpenProcess(0x100000, False, pid)
                    self.assertTrue(child_handle)
                parent.kill()
                parent.communicate(timeout=3)
                start = time.monotonic()
                if os.name == 'nt':
                    self.assertEqual(kernel.WaitForSingleObject(child_handle, 1500), 0)
                else:
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        stat = Path(f'/proc/{pid}/stat')
                        if not stat.exists() or stat.read_text().split()[2] == 'Z':
                            break
                        time.sleep(.02)
                    else:
                        self.fail('orphan HTTP worker remained alive')
                self.assertLess(time.monotonic() - start, 1.5)
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.communicate(timeout=3)
                if child_handle:
                    kernel.CloseHandle(child_handle)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        _fixture_worker(*sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == '--http-parent':
        _http_parent_worker(*sys.argv[2:])
    else:
        unittest.main()
