import io
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_delivery import isolated_environment
from common import Budget, BudgetExceeded, Config, GuideError, RunCancelled
from source import AcademicSource
from source_store import SourceStore


class QueuedRevocationTests(unittest.TestCase):
    def test_recheck_preserves_cancel_and_budget_exceptions(self):
        for kind in (RunCancelled, BudgetExceeded):
            for operation in ('search', 'fetch'):
                with self.subTest(kind=kind, operation=operation), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    with patch.dict(os.environ, isolated_environment(root, inherited=dict(os.environ)), clear=True), \
                         patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
                        source = AcademicSource(Config(source='deepseek', deepseek_search_key='fixture', cache=False), Budget(5))
                        original = source._deepseek_target_key
                        calls = []
                        def validate(target):
                            calls.append(1)
                            if len(calls) == 2:
                                raise kind()
                            return original(target)
                        with patch.object(source, '_deepseek_target_key', side_effect=validate), \
                             patch.object(source, '_wait_for'), patch('source.urlopen') as send:
                            with self.assertRaises(kind):
                                if operation == 'search':
                                    source._deepseek_search('fixture', 1)
                                else:
                                    source.fetch('https://paper.invalid/fixture')
                            send.assert_not_called()

    def test_valid_dispatch_releases_store_transaction(self):
        for operation in ('search', 'fetch'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.dict(os.environ, isolated_environment(root, inherited=dict(os.environ)), clear=True), \
                     patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
                    store = SourceStore(root / 'sources.sqlite')
                    other = SourceStore(root / 'sources.sqlite')
                    try:
                        store.upsert({'name': 'deepseek', 'api_key': 'synthetic-old'})
                        source = AcademicSource(store.configure(Config(source='deepseek', cache=False)), Budget(5))
                        def send(request, timeout):
                            other.upsert({'name': 'deepseek', 'clear_key': True})
                            return io.BytesIO(b'{"results": [], "content": "fixture"}')
                        with patch.object(source, '_wait_for'), patch('source.urlopen', side_effect=send) as mocked:
                            if operation == 'search':
                                self.assertEqual(source._deepseek_search('fixture', 1), [])
                            else:
                                self.assertEqual(source.fetch('https://paper.invalid/fixture')['content'], 'fixture')
                        self.assertEqual(mocked.call_count, 1)
                    finally:
                        store.close()
                        other.close()

    def test_revocation_after_rate_before_dispatch(self):
        for operation in ('search', 'fetch'):
            for change in ('clear', 'rotate', 'rebind', 'same_origin_path', 'disable'):
                with self.subTest(operation=operation, change=change), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    with patch.dict(os.environ, isolated_environment(root, inherited=dict(os.environ)), clear=True), \
                         patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
                        store = SourceStore(root / 'sources.sqlite')
                        other = SourceStore(root / 'sources.sqlite')
                        try:
                            store.upsert({'name': 'deepseek', 'api_key': 'synthetic-old'})
                            source = AcademicSource(store.configure(Config(source='deepseek', cache=False)), Budget(5))
                            reached, release = threading.Event(), threading.Event()
                            errors = []
                            original = source._wait_for
                            def wait(provider):
                                original(provider)
                                reached.set()
                                if not release.wait(3):
                                    raise AssertionError('barrier timeout')
                            def run():
                                try:
                                    if operation == 'search':
                                        source._deepseek_search('fixture', 1)
                                    else:
                                        source.fetch('https://paper.invalid/fixture')
                                except BaseException as exc:
                                    errors.append(exc)
                            with patch.object(source, '_wait_for', side_effect=wait), \
                                 patch('source.urlopen', return_value=io.BytesIO(b'{"results": [], "content": "fixture"}')) as send:
                                thread = threading.Thread(target=run)
                                thread.start()
                                try:
                                    self.assertTrue(reached.wait(3))
                                    update = {'clear': {'clear_key': True}, 'rotate': {'api_key': 'synthetic-new'},
                                              'rebind': {'api_key': 'synthetic-old', 'endpoint': 'https://other.invalid/search'},
                                              'same_origin_path': {'endpoint': 'https://api.deepseek.com/changed'},
                                              'disable': {'enabled': False}}[change]
                                    other.upsert({'name': 'deepseek', **update})
                                finally:
                                    release.set()
                                    thread.join(5)
                                self.assertFalse(thread.is_alive())
                                send.assert_not_called()
                            self.assertEqual(len(errors), 1)
                            self.assertIsInstance(errors[0], GuideError)
                            self.assertEqual(getattr(errors[0], 'remote_outcome', None), 'not_started')
                            self.assertNotIn('synthetic-old', str(errors[0]))
                        finally:
                            store.close()
                            other.close()


if __name__ == '__main__':
    unittest.main()
