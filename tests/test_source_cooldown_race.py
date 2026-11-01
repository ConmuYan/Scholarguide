"""Offline regressions for stale success racing a newer source cooldown."""
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Budget, Config
from source import AcademicSource
from source_store import SourceStore


def _older_success(path, started, proceed, output):
    store = SourceStore(path)
    try:
        started.set()
        if not proceed.wait(5):
            output.put(False)
            return
        store.record_run({'name': 'openalex', 'status': 'ok', 'returned': 1})
        output.put(True)
    finally:
        store.close()


def _newer_limit(path, proceed, output):
    store = SourceStore(path)
    try:
        store.record_run({'name': 'openalex', 'status': 'error', 'code': 'rate_limited'})
        output.put(True)
    finally:
        proceed.set()
        store.close()


class CooldownRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(Path(self.tmp.name) / 'no-token.json'),
                                      'LUNE_API_KEY': '', 'LUNE_TOKEN': '',
                                      'SG_SKIP_PROJECT_ENV': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.path = str(Path(self.tmp.name) / 'sources.sqlite')
        self.store = SourceStore(self.path)
        self.addCleanup(self.store.close)

    def test_delayed_run_success_preserves_newer_active_cooldown(self):
        self.store.record_run({'name': 'openalex', 'status': 'error', 'code': 'rate_limited'})
        until = self.store.cooldown('openalex')
        self.store.record_run({'name': 'openalex', 'status': 'ok', 'returned': 1})
        self.assertEqual(self.store.cooldown('openalex'), until)
        self.assertEqual(self.store.health_snapshot()['openalex']['last_code'], 'rate_limited')

    def test_delayed_http_success_preserves_local_and_persisted_cooldown(self):
        src = AcademicSource(Config(source='openalex', cache=False), Budget(5))
        src.store = self.store
        entered, release = threading.Event(), threading.Event()
        def request(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test barrier')
            return b'{}'
        errors = []
        def earlier_request():
            try:
                src._fetch('https://fixture.invalid', provider='openalex')
            except BaseException as exc:
                errors.append(exc)
        with patch.object(src, '_request_bytes', side_effect=request):
            thread = threading.Thread(target=earlier_request)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                src._pause('openalex', 60, 'rate_limited')
                until = self.store.cooldown('openalex')
                release.set(); thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertGreater(src.cooldown['openalex'], time.monotonic())
                self.assertEqual(self.store.cooldown('openalex'), until)
            finally:
                release.set(); thread.join(3)

    def test_older_process_success_cannot_clear_newer_process_limit(self):
        ctx = multiprocessing.get_context('spawn')
        started, proceed, output = ctx.Event(), ctx.Event(), ctx.Queue()
        older = ctx.Process(target=_older_success, args=(self.path, started, proceed, output))
        newer = ctx.Process(target=_newer_limit, args=(self.path, proceed, output))
        older.start()
        try:
            self.assertTrue(started.wait(5))
            newer.start()
            newer.join(8); older.join(8)
            self.assertEqual((older.exitcode, newer.exitcode), (0, 0))
            self.assertTrue(output.get(timeout=2)); self.assertTrue(output.get(timeout=2))
            self.assertGreater(self.store.cooldown('openalex'), time.time())
        finally:
            proceed.set()
            for process in (older, newer):
                if process.is_alive(): process.terminate(); process.join()
            output.close()

    def test_shorter_later_failure_does_not_shorten_active_cooldown(self):
        now = time.time()
        self.store.cooldown('openalex', now + 120, errorcode='rate_limited')
        self.store.cooldown('openalex', now + 10, errorcode='timeout')
        health = self.store.health_snapshot()['openalex']
        self.assertEqual(health['cooldown_until'], now + 120)
        self.assertEqual(health['cooldown_code'], 'rate_limited')


if __name__ == '__main__':
    unittest.main()
