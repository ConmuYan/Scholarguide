"""Regression tests for audit O1-O3 hardening."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_tools import ToolRegistry
from common import Budget, Config
from source import AcademicSource
from webapp import ChatService


class HardeningTests(unittest.TestCase):
    def test_tool_timeout_does_not_leave_worker_thread(self):
        spec = ToolRegistry.from_source(object()).specs['arxiv']
        spec = replace(spec, timeout_s=0.01, max_retries=1, min_interval_s=0, failure_threshold=10)
        def slow(**values):
            time.sleep(0.08)
        registry = ToolRegistry([spec], handlers={'arxiv': slow})
        before = {t.name for t in threading.enumerate()}
        started = time.monotonic()
        observation = registry.call('arxiv', slow, _input={'query': 'fixture'})
        self.assertEqual(observation.status, 'timeout')
        self.assertGreaterEqual(time.monotonic() - started, 0.07)
        after = {t.name for t in threading.enumerate()}
        self.assertNotIn('ThreadPoolExecutor-', ' '.join(after - before))

    def test_eviction_never_closes_a_running_session(self):
        class Store:
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True
        with tempfile.TemporaryDirectory() as directory:
            service = ChatService(output=Path(directory) / 'web')
            try:
                service.sessions = {f'dummy-{i:02d}': {'running': i == 0,
                                                        'store': Store()}
                                    for i in range(21)}
                service._evict_idle()
                self.assertIn('dummy-00', service.sessions)
                self.assertTrue(service.sessions['dummy-00']['running'])
                self.assertEqual(len(service.sessions), 20)
            finally:
                service.close()

    def test_import_store_construction_failure_removes_directory(self):
        from demo import demo_run
        with tempfile.TemporaryDirectory() as directory:
            service = ChatService(output=Path(directory) / 'web')
            with patch('webapp.StateStore', side_effect=RuntimeError('constructor')):
                with self.assertRaises(RuntimeError):
                    service.import_run(demo_run('followup'), 'b' * 32)
            self.assertEqual(list((Path(directory) / 'web').glob('*')), [])
            service.close()

    def test_import_failure_removes_orphan_directory(self):
        from demo import demo_run
        with tempfile.TemporaryDirectory() as directory:
            service = ChatService(output=Path(directory) / 'web')
            result = demo_run('followup')
            with patch('webapp.StateStore.add_candidate', side_effect=RuntimeError('synthetic')):
                with self.assertRaises(RuntimeError):
                    service.import_run(result, 'a' * 32)
            self.assertEqual(list((Path(directory) / 'web').glob('*/state.sqlite3')), [])
            self.assertEqual(service.sessions, {})
            service.close()

    def test_create_store_construction_failure_removes_orphan_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ChatService(output=Path(directory) / 'web')
            with patch('webapp.StateStore', side_effect=RuntimeError('synthetic-store')):
                with self.assertRaises(RuntimeError):
                    service.create({'mode': 'demo', 'scenario': 'followup',
                                    'message': 'Recommend five RAG papers.'})
            self.assertEqual(list((Path(directory) / 'web').glob('*')), [])
            service.close()

    def test_failed_started_create_preserves_published_session(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ChatService(output=Path(directory) / 'web')
            published = []
            def fail_after_writing(chat, *args, **kwargs):
                chat.store.append_event(chat.session_id, 'test_progress', {'status': 'saved'})
                chat.store.commit()
                raise RuntimeError('synthetic post-admission failure')
            try:
                with patch('webapp.ChatSession.start', fail_after_writing):
                    with self.assertRaises(RuntimeError):
                        service.create({'mode': 'demo', 'message': 'Recommend papers.'},
                                       on_created=published.append)
                self.assertEqual(len(published), 1)
                path = Path(directory) / 'web' / published[0] / 'state.sqlite3'
                self.assertTrue(path.is_file())
                self.assertIn(published[0], service.sessions)
                self.assertTrue(any(e['action'] == 'test_progress'
                                    for e in service.sessions[published[0]]['store'].events(published[0])))
                self.assertFalse(service.sessions[published[0]]['running'])
            finally:
                service.close()

    def test_create_failure_removes_orphan_directory_and_store(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ChatService(output=Path(directory) / 'web')
            before = set((Path(directory) / 'web').glob('*')) if (Path(directory) / 'web').exists() else set()
            with patch('webapp.ChatSession', side_effect=RuntimeError('synthetic')):
                with self.assertRaises(RuntimeError):
                    service.create({'mode': 'demo', 'scenario': 'followup',
                                    'message': 'Recommend five RAG papers.'})
            after = set((Path(directory) / 'web').glob('*')) if (Path(directory) / 'web').exists() else set()
            self.assertEqual(before, after)
            self.assertEqual(service.sessions, {})
            service.close()


if __name__ == '__main__':
    unittest.main()
