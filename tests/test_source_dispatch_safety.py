"""Offline dispatch safety regressions using only synthetic endpoints and stores."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError, HTTPError

from common import Budget, BudgetExceeded, Config, GuideError, RunCancelled
from source import AcademicSource
from source_store import SourceStore


class DispatchSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        env = patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(root / 'no-token.json'),
                                      'SG_SOURCE_STORE': str(root / 'sources.sqlite'),
                                      'SG_LUNE_QUOTA_DB': str(root / 'quota.sqlite'),
                                      'LUNE_API_KEY': '', 'LUNE_TOKEN': '',
                                      'SG_SKIP_PROJECT_ENV': '1'})
        env.start()
        self.addCleanup(env.stop)

    def test_post_transport_not_retried_and_batch_preserves_outcome(self):
        client = AcademicSource(Config(source='deepseek', deepseek_search_key='fixture', cache=False), Budget(5))
        with patch.object(client, '_wait_for'), patch('source.urlopen', side_effect=URLError(TimeoutError('secret parameters'))) as transport:
            with self.assertRaises(GuideError) as caught:
                client.search_batch('fixture query')
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(caught.exception.remote_outcome, 'unknown')
        self.assertIs(caught.exception.retry_safe, False)
        self.assertNotIn('secret parameters', str(caught.exception))
        self.assertEqual(client.last_batch.runs[0].as_dict()['remote_outcome'], 'unknown')

    def test_probe_clears_cooldown_and_reports_unknown_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 's.sqlite')
            try:
                store.upsert({'name': 'deepseek', 'api_key': 'fixture'})
                store.cooldown('deepseek', time.time() + 30, errorcode='rate_limited')
                with patch('source.urlopen', side_effect=TimeoutError('private')) as transport:
                    result = store.probe('deepseek', Config(source='deepseek', cache=False))
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(result['remote_outcome'], 'unknown')
                self.assertIs(result['retry_safe'], False)
                self.assertFalse(result['ok'])
                self.assertNotIn('private', repr(result))
            finally:
                store.close()

    def test_post_http_failure_not_retried(self):
        client = AcademicSource(Config(source='openalex', cache=False), Budget(5))
        failure = HTTPError('https://fixture.invalid', 503, 'secret', {}, None)
        with patch.object(client, '_wait_for'), patch('source.urlopen', side_effect=failure) as transport:
            with self.assertRaises(GuideError):
                client._fetch('https://fixture.invalid', provider='openalex', data=b'secret')
        self.assertEqual(transport.call_count, 1)

    def test_post_invalid_success_response_preserves_unknown_outcome(self):
        client = AcademicSource(Config(source='deepseek', deepseek_search_key='fixture', cache=False), Budget(5))
        for body in (b'<html>fixture</html>', b'{', b'[]', b'x' * 4_000_001):
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, size): return body[:size]
            with self.subTest(size=len(body)), patch.object(client, '_wait_for'), \
                 patch('source.urlopen', return_value=Response()) as transport:
                with self.assertRaises(GuideError) as caught:
                    client._json('https://fixture.invalid', provider='deepseek', data={'q': 'fixture'})
                self.assertEqual(getattr(caught.exception, 'remote_outcome', None), 'unknown')
                self.assertIs(getattr(caught.exception, 'retry_safe', None), False)
                self.assertEqual(transport.call_count, 1)

    def test_deepseek_semantic_invalid_post_outcome_search_and_fetch(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b'{}'
        for operation in ('search', 'fetch'):
            client = AcademicSource(Config(source='deepseek', deepseek_search_key='fixture', cache=False), Budget(3))
            with self.subTest(operation=operation), patch.object(client, '_wait_for'), \
                 patch('source.urlopen', return_value=Response()) as transport:
                with self.assertRaises(GuideError) as caught:
                    if operation == 'search':
                        client.search_batch('fixture')
                    else:
                        client.fetch('https://fixture.invalid/item')
                self.assertEqual(getattr(caught.exception, 'remote_outcome', None), 'unknown')
                self.assertIs(getattr(caught.exception, 'retry_safe', None), False)
                self.assertEqual(transport.call_count, 1)
                if operation == 'search':
                    self.assertEqual(client.last_batch.runs[0].as_dict()['remote_outcome'], 'unknown')

    def test_cancel_after_dispatch_keeps_unknown(self):
        budget = Budget(5)
        client = AcademicSource(Config(source='deepseek', deepseek_search_key='fixture', cache=False), budget)
        def cancel(*args, **kwargs):
            budget.cancel_token.request()
            raise TimeoutError('private')
        with patch.object(client, '_wait_for'), patch('source.urlopen', side_effect=cancel):
            with self.assertRaises(RunCancelled) as caught:
                client.fetch('https://fixture.invalid/private')
        self.assertEqual(caught.exception.remote_outcome, 'unknown')
        self.assertIs(caught.exception.retry_safe, False)

    def test_new_cooldown_after_rate_wait_prevents_dispatch_and_obeys_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 's.sqlite')
            other = SourceStore(Path(tmp) / 's.sqlite')
            try:
                client = AcademicSource(Config(source='openalex', source_store=store, cache=False), Budget(.4))
                original = client._wait_for
                def wait(name):
                    original(name)
                    other.cooldown(name, time.time() + 10, errorcode='rate_limited')
                with patch.object(client, '_wait_for', side_effect=wait), patch('source.urlopen') as transport:
                    with self.assertRaises(BudgetExceeded):
                        client._fetch('https://fixture.invalid', provider='openalex')
                    transport.assert_not_called()
            finally:
                other.close(); store.close()

    def test_local_cooldown_after_rate_wait_prevents_direct_fetch(self):
        client = AcademicSource(Config(source='deepseek', deepseek_search_key='fixture', cache=False), Budget(.25))
        def wait(name):
            client._pause(name, 10, 'rate_limited')
        with patch.object(client, '_wait_for', side_effect=wait), patch('source.urlopen') as transport:
            with self.assertRaises(BudgetExceeded):
                client.fetch('https://fixture.invalid/private')
            transport.assert_not_called()

    def test_cooldown_extension_is_reobserved_and_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceStore(Path(tmp) / 's.sqlite')
            other = SourceStore(Path(tmp) / 's.sqlite')
            budget = Budget(3)
            client = AcademicSource(Config(source='arxiv', source_store=store, cache=False), budget)
            waiting, proceed = threading.Event(), threading.Event()
            errors = []
            real_wait = budget.wait
            def wait(delay):
                if delay > .1:
                    waiting.set()
                    if not proceed.wait(2):
                        raise AssertionError('barrier timeout')
                return real_wait(delay)
            def run():
                try:
                    client.lookup('2401.12345')
                except BaseException as exc:
                    errors.append(exc)
            try:
                other.cooldown('arxiv', time.time() + .3, errorcode='rate_limited')
                with patch.object(client, '_wait_for'), patch.object(budget, 'wait', side_effect=wait), patch('source.urlopen') as transport:
                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        self.assertTrue(waiting.wait(2))
                        other.cooldown('arxiv', time.time() + 1.5, errorcode='rate_limited')
                        proceed.set()
                        time.sleep(.4)
                        budget.cancel_token.request()
                        thread.join(2)
                        self.assertFalse(thread.is_alive())
                        self.assertIsInstance(errors[0], RunCancelled)
                        transport.assert_not_called()
                    finally:
                        budget.cancel_token.request(); proceed.set(); thread.join(2)
            finally:
                other.close(); store.close()

    def test_late_cooldown_reacquires_rate_before_concurrent_dispatch(self):
        import source_store
        with tempfile.TemporaryDirectory() as tmp:
            first = SourceStore(Path(tmp) / 's.sqlite')
            second = SourceStore(Path(tmp) / 's.sqlite')
            clients = [AcademicSource(Config(source='openalex', source_store=store, cache=False), Budget(4))
                       for store in (first, second)]
            both_ready, resume = threading.Event(), threading.Event()
            ready_count, dispatches, errors = [], [], []
            lock = threading.Lock()
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, size): return b'{}'
            def transport(*args, **kwargs):
                with lock: dispatches.append(time.monotonic())
                return Response()
            def run(client):
                original = client._wait_for
                first_wait = True
                def wait(name):
                    nonlocal first_wait
                    original(name)
                    if first_wait:
                        first_wait = False
                        with lock:
                            ready_count.append(True)
                            if len(ready_count) == 2: both_ready.set()
                        if not resume.wait(2): raise TimeoutError('barrier')
                try:
                    with patch.object(client, '_wait_for', side_effect=wait):
                        client._fetch('https://fixture.invalid', provider='openalex')
                except BaseException as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=run, args=(client,)) for client in clients]
            try:
                with patch.dict(source_store._RATE, {'openalex': .12}), patch('source.urlopen', side_effect=transport):
                    for thread in threads: thread.start()
                    self.assertTrue(both_ready.wait(2))
                    first.cooldown('openalex', time.time() + .3, errorcode='rate_limited')
                    resume.set()
                    for thread in threads: thread.join(3)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(len(dispatches), 2)
                self.assertGreaterEqual(abs(dispatches[1] - dispatches[0]), .08)
            finally:
                resume.set()
                for thread in threads:
                    if thread.is_alive(): thread.join(3)
                second.close(); first.close()

    def test_multisource_whitelists_outcome(self):
        client = AcademicSource(Config(source='dblp', cache=False), Budget(5))
        error = GuideError('safe')
        error.remote_outcome = 'unknown'
        error.retry_safe = False
        def search(name, *args):
            if name == 'dblp':
                raise error
            return []
        with patch.object(client, '_search_one', side_effect=search):
            batch = client.search_batch('fixture', providers=['dblp', 'openalex'])
        self.assertEqual(batch.runs[0].as_dict()['remote_outcome'], 'unknown')
        self.assertIs(batch.runs[0].as_dict()['retry_safe'], False)
        error.remote_outcome = 'private payload'
        error.retry_safe = 'private payload'
        with patch.object(client, '_search_one', side_effect=search):
            batch = client.search_batch('fixture', providers=['dblp', 'openalex'])
        self.assertNotIn('private payload', repr(batch.summary()))


if __name__ == '__main__':
    unittest.main()
