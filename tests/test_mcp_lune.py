"""Independent Lune adapter tests; evaluation suites are intentionally separate."""
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from unittest.mock import patch
from pathlib import Path

import mcp_client
import mcp_lune
import mcp_plugin
import source as source_mod
from common import GuideError
from source import classify_source_error
from tests.test_mcp_client import MockMcp, _result
from tests.lune_quota_fixture import confirmed_quota


PAYLOAD = {'papers': [
    {'paper_id': 'p-1', 'title': 'RAG', 'abstract': 'A real abstract.',
     'authors': [{'name': 'A. Author'}], 'venue': 'NeurIPS', 'year': 2024,
     'url': 'https://example.invalid/p-1', 'doi': 'https://doi.org/10.1234/RAG.'},
    {'id': 'p-2', 'title': 'Dense Retrieval', 'summary': 'A snippet.',
     'conference': 'ICML', 'published_year': 2023, 'link': 'https://example.invalid/p-2'},
    {'title': 'Title Only'},
    {'abstract': 'No usable identity'},
]}


def _refresh_worker(path, barrier, results, force):
    # Spawned processes use only synthetic data and an in-memory transport.
    os.environ['SG_MCP_TOKEN_FILE'] = path
    original = mcp_lune._read_token_data
    first = True
    def synchronized_read():
        nonlocal first
        data = original()
        if first:
            first = False
            barrier.wait(timeout=5)
        return data
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size=-1):
            time.sleep(.3)
            return json.dumps({'access_token': 'synthetic-new', 'refresh_token': 'synthetic-rotated',
                               'expires_in': 3600}).encode()
    def transport(request, timeout):
        with open(path + '.calls', 'a', encoding='utf-8') as f:
            f.write('1\n')
        return Response()
    try:
        with patch('mcp_lune._read_token_data', side_effect=synchronized_read), \
             patch('mcp_lune.urlopen', side_effect=transport), \
             patch('mcp_lune._oauth_quota', return_value=nullcontext()):
            result = mcp_lune.access_token(force_refresh=force)
        results.put(result == 'synthetic-new')
    except BaseException:
        results.put(False)


def _hold_token_lock(path, ready, release):
    os.environ['SG_MCP_TOKEN_FILE'] = path
    with mcp_lune._token_process_lock(None):
        ready.set()
        release.wait(5)


class TokenPersistenceTests(unittest.TestCase):
    def test_atomic_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            original = b'{"access_token":"synthetic-old"}'
            path.write_bytes(original)
            with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(path)}), \
                 patch('mcp_lune.os.replace', side_effect=OSError):
                with self.assertRaises(OSError):
                    mcp_lune._save_tokens({'access_token': 'synthetic-new'})
            self.assertTrue(path.read_bytes() == original)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def _concurrent_refresh(self, force):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            path.write_text(json.dumps({'access_token': 'synthetic-old',
                                        'refresh_token': 'synthetic-refresh',
                                        'expires_at': int(time.time()) + (3600 if force else 0)}), encoding='utf-8')
            ctx = multiprocessing.get_context('spawn')
            barrier, results = ctx.Barrier(2), ctx.Queue()
            workers = [ctx.Process(target=_refresh_worker, args=(str(path), barrier, results, force))
                       for _ in range(2)]
            for worker in workers: worker.start()
            try:
                for worker in workers: worker.join(8)
                self.assertTrue(all(not worker.is_alive() and worker.exitcode == 0 for worker in workers))
                self.assertTrue(all(results.get(timeout=2) for _ in workers))
                self.assertEqual(len(Path(str(path) + '.calls').read_text().splitlines()), 1)
            finally:
                for worker in workers:
                    if worker.is_alive(): worker.terminate(); worker.join()
                results.close()

    def test_process_lock_wait_observes_cancellation(self):
        import threading
        from common import Budget, RunCancelled
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            path.write_text(json.dumps({'access_token': 'synthetic-old', 'refresh_token': 'synthetic-refresh'}))
            ctx = multiprocessing.get_context('spawn')
            ready, release = ctx.Event(), ctx.Event()
            worker = ctx.Process(target=_hold_token_lock, args=(str(path), ready, release))
            worker.start()
            try:
                self.assertTrue(ready.wait(5))
                budget = Budget(2)
                timer = threading.Timer(.15, budget.cancel_token.request)
                with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(path)}), \
                     patch('mcp_lune.urlopen') as transport:
                    timer.start()
                    try:
                        with self.assertRaises(RunCancelled):
                            mcp_lune.access_token(budget=budget)
                        transport.assert_not_called()
                    finally:
                        timer.cancel(); timer.join()
            finally:
                release.set(); worker.join(5)
                if worker.is_alive(): worker.terminate(); worker.join()

    def test_process_lock_released_on_process_exit(self):
        from common import Budget
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            ctx = multiprocessing.get_context('spawn')
            ready, release = ctx.Event(), ctx.Event()
            worker = ctx.Process(target=_hold_token_lock, args=(str(path), ready, release))
            worker.start()
            try:
                self.assertTrue(ready.wait(5))
                worker.terminate(); worker.join(5)
                self.assertFalse(worker.is_alive())
                with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(path)}):
                    with mcp_lune._token_process_lock(Budget(2)):
                        pass
            finally:
                if worker.is_alive(): worker.terminate(); worker.join()

    def test_oversized_oauth_response_is_bounded_and_unknown(self):
        from contextlib import nullcontext
        from common import Budget, GuideError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            original = json.dumps({'access_token': 'synthetic-old', 'refresh_token': 'synthetic-refresh',
                                   'expires_at': 0})
            path.write_text(original, encoding='utf-8')
            sizes = []
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, size=-1):
                    sizes.append(size)
                    return b'x' * size
            with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(path)}), \
                 patch('mcp_lune.urlopen', return_value=Response()), \
                 patch('mcp_lune._oauth_quota', return_value=nullcontext()):
                with self.assertRaises(GuideError) as caught:
                    mcp_lune.access_token(budget=Budget(2))
            self.assertEqual(caught.exception.code, 'oauth_refresh_unknown')
            self.assertEqual(sizes, [128_001])
            self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_failed_rotated_token_save_stops_before_using_old_token(self):
        from contextlib import nullcontext
        from common import Budget, GuideError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            original = json.dumps({'access_token': 'synthetic-old', 'refresh_token': 'synthetic-refresh',
                                   'expires_at': 0})
            path.write_text(original, encoding='utf-8')
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, size=-1):
                    return b'{"access_token":"synthetic-new","refresh_token":"synthetic-rotated"}'
            with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(path)}), \
                 patch('mcp_lune.urlopen', return_value=Response()), \
                 patch('mcp_lune._oauth_quota', return_value=nullcontext()), \
                 patch('mcp_lune._save_tokens', side_effect=OSError('synthetic-private')):
                with self.assertRaises(GuideError) as caught:
                    mcp_lune.access_token(budget=Budget(2))
            self.assertEqual(caught.exception.code, 'oauth_rotation_persist_failed')
            self.assertNotIn('synthetic-private', str(caught.exception))
            self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_force_refresh_uses_update_after_rejected_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            path.write_text(json.dumps({'access_token': 'synthetic-new', 'refresh_token': 'synthetic-rotated',
                                        'expires_at': int(time.time()) + 3600}))
            with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(path)}), \
                 patch('mcp_lune.urlopen') as transport:
                result = mcp_lune.access_token(force_refresh=True, rejected_token='synthetic-old')
            self.assertTrue(result == 'synthetic-new')
            transport.assert_not_called()

    def test_force_refresh_refreshes_unchanged_rejected_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tokens.json'
            path.write_text(json.dumps({'access_token': 'synthetic-old', 'refresh_token': 'synthetic-refresh',
                                        'expires_at': int(time.time()) + 3600}))
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, size=-1):
                    return b'{"access_token":"synthetic-new","refresh_token":"synthetic-rotated"}'
            with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(path)}), \
                 patch('mcp_lune.urlopen', return_value=Response()) as transport, \
                 patch('mcp_lune._oauth_quota', return_value=nullcontext()):
                result = mcp_lune.access_token(force_refresh=True, rejected_token='synthetic-old')
            self.assertTrue(result == 'synthetic-new')
            self.assertEqual(transport.call_count, 1)

    def test_expired_token_refresh_is_single_across_processes(self):
        self._concurrent_refresh(False)

    def test_forced_token_refresh_is_single_across_processes(self):
        self._concurrent_refresh(True)


def search_script(payload=None):
    return {'tools/call': _result({'structuredContent': payload or PAYLOAD})}


class MappingTests(unittest.TestCase):
    def test_payload_shapes(self):
        self.assertEqual(len(mcp_lune.papers_from({'papers': [{'id': 1}]})), 1)
        self.assertEqual(len(mcp_lune.papers_from({'results': [{'id': 1}]})), 1)
        self.assertEqual(len(mcp_lune.papers_from({'results': {'papers': [{'id': 1}]}})), 1)

    def test_only_real_abstract_is_grounding(self):
        paper = mcp_lune.to_paper(PAYLOAD['papers'][1])
        self.assertEqual(paper.abstract, '')
        self.assertEqual(paper.snippet, 'A snippet.')
        self.assertTrue(paper.metadata_only)

    def test_identifiers_are_stable_and_normalized(self):
        paper = mcp_lune.to_paper(PAYLOAD['papers'][0])
        self.assertEqual(paper.id, 'lune:p-1')
        self.assertEqual(paper.doi, '10.1234/rag')
        self.assertTrue(mcp_lune.to_paper(PAYLOAD['papers'][2]).id.startswith('lune:Title Only ['))
        self.assertEqual(mcp_lune.to_paper({'title': 'x', 'arxiv_id': 'https://arxiv.org/abs/2401.12345v2'}).arxiv_id,
                         '2401.12345v2')

    def test_missing_identity_is_rejected(self):
        with self.assertRaises(GuideError):
            mcp_lune.to_paper({'abstract': 'nothing'})


class ProviderTests(unittest.TestCase):
    def setUp(self):
        mcp_plugin.reset_for_tests()
        mcp_lune.reset_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(Path(self.tmp.name) / 'tokens.json')})
        env.start()
        self.addCleanup(env.stop)
        self.ledger = Path(self.tmp.name) / 'ledger.json'
        self.quota = confirmed_quota(self)

    def tearDown(self):
        mcp_plugin.reset_for_tests()
        mcp_lune.reset_for_tests()

    def provider(self, script, **kwargs):
        mock = MockMcp(script)
        self.addCleanup(mock.__exit__)
        client = mcp_client.McpClient(mcp_client.ServerConfig(
            'lune', mock.url, timeout=10, allow=('search_papers',)), quota_store=self.quota)
        return mcp_lune.LuneProvider(client=client, ledger_path=self.ledger, **kwargs), mock

    def test_oauth_refresh_uses_budget_and_saves_rotated_token_before_cancel(self):
        from unittest.mock import patch
        from contextlib import nullcontext
        from common import Budget, RunCancelled
        budget = Budget(2)
        observed = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1):
                budget.cancel_token.request()
                return b'{"access_token":"new-synthetic","refresh_token":"rotated-synthetic"}'
        def transport(request, timeout):
            observed['timeout'] = timeout
            return Response()
        old = {'access_token': 'old-synthetic', 'refresh_token': 'original-synthetic', 'expires_at': 0}
        with patch('mcp_lune._read_token_data', return_value=old), \
             patch('mcp_lune.urlopen', side_effect=transport), \
             patch('mcp_lune._oauth_quota', return_value=nullcontext(), create=True), \
             patch('mcp_lune._save_tokens') as save:
            with self.assertRaises(RunCancelled):
                mcp_lune.access_token(budget=budget)
        self.assertLessEqual(observed['timeout'], 2)
        self.assertEqual(save.call_args.args[0]['refresh_token'], 'rotated-synthetic')

    def test_oauth_refresh_does_not_follow_even_same_origin_redirect(self):
        import threading
        from contextlib import nullcontext
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.error import HTTPError
        from unittest.mock import patch
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/redirected')
                self.end_headers()
            def do_GET(self):
                paths.append(self.path)
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"access_token":"synthetic"}')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch('mcp_lune.ISSUER', f'http://127.0.0.1:{server.server_port}'), \
                 patch('mcp_lune._oauth_quota', return_value=nullcontext()), \
                 patch('mcp_lune._save_tokens'):
                with self.assertRaises(HTTPError) as caught:
                    mcp_lune._refresh({'refresh_token': 'synthetic'})
                caught.exception.close()
            self.assertEqual(paths, ['/oauth/token'])
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

    def test_oauth_refresh_without_snapshot_fails_before_transport(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from common import Budget
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quota.sqlite3'
            old = {'access_token': 'old-synthetic', 'refresh_token': 'refresh-synthetic', 'expires_at': 0}
            with patch('lune_quota.default_quota_path', return_value=path), \
                 patch('mcp_lune._read_token_data', return_value=old), \
                 patch('mcp_lune.urlopen') as transport:
                with self.assertRaises(GuideError) as caught:
                    mcp_lune.access_token(budget=Budget(2))
            self.assertEqual(caught.exception.code, 'quota_no_snapshot')
            transport.assert_not_called()

    def test_oauth_refresh_attempt_consumes_shared_quota_even_on_failure(self):
        import tempfile
        import time
        from pathlib import Path
        from unittest.mock import patch
        from common import Budget
        from lune_quota import LuneQuotaStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quota.sqlite3'
            store = LuneQuotaStore(path)
            store.confirm(2, time.time() + 60)
            try:
                old = {'access_token': 'old-synthetic', 'refresh_token': 'refresh-synthetic', 'expires_at': 0}
                with patch('lune_quota.default_quota_path', return_value=path), \
                     patch('mcp_lune._read_token_data', return_value=old), \
                     patch('mcp_lune.urlopen', side_effect=TimeoutError('synthetic')):
                    with self.assertRaises(GuideError) as caught:
                        mcp_lune.access_token(budget=Budget(2))
                    self.assertEqual(caught.exception.code, 'oauth_refresh_unknown')
                self.assertEqual(store.status()['remaining_from_snapshot'], 1)
                self.assertEqual(store.status()['local_reserved'], 0)
            finally:
                store.close()

    def test_oauth_lock_wait_observes_cancellation(self):
        import threading
        import time
        from common import Budget, RunCancelled
        budget, entered, errors = Budget(3), threading.Event(), []
        def worker():
            entered.set()
            try:
                mcp_lune.access_token(budget=budget)
            except Exception as exc:
                errors.append(exc)
        mcp_lune._TOKEN_LOCK.acquire()
        thread = threading.Thread(target=worker)
        try:
            thread.start(); self.assertTrue(entered.wait(1))
            time.sleep(.05); budget.cancel_token.request()
            thread.join(.8)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(errors[0], RunCancelled)
        finally:
            mcp_lune._TOKEN_LOCK.release(); thread.join(4)

    def test_each_search_page_passes_rate_admission(self):
        class Client:
            budget = None
            def set_budget(self, value): self.budget = value
            def call_tool(self, name, args):
                offset = args['offset']
                return {'structuredContent': {'papers': [
                    {'id': f'p{offset + i}', 'title': f'paper {offset + i}'} for i in range(5)],
                    'has_more': offset == 0}}
        provider = mcp_lune.LuneProvider(client=Client(), ledger_path=self.ledger, max_requests=5)
        calls = []
        papers = provider.search('fixture', limit=10, before_page=lambda: calls.append('admitted'))
        self.assertEqual(len(papers), 10)
        self.assertEqual(calls, ['admitted', 'admitted'])

    def test_waiting_for_shared_provider_lock_observes_cancellation(self):
        import threading
        import time
        from common import Budget, RunCancelled
        budget = Budget(3)
        provider, _ = self.provider(search_script(), max_requests=5)
        entered, errors = threading.Event(), []
        def worker():
            entered.set()
            try:
                provider.search('fixture', budget=budget)
            except Exception as exc:
                errors.append(exc)
        provider._lock.acquire()
        thread = threading.Thread(target=worker)
        try:
            thread.start(); self.assertTrue(entered.wait(1))
            time.sleep(.05); budget.cancel_token.request()
            thread.join(.8)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(errors[0], RunCancelled)
        finally:
            provider._lock.release(); thread.join(4)

    def test_search_and_reservation_ledger(self):
        provider, mock = self.provider(search_script(), max_requests=5)
        papers = provider.search('rag', limit=10)
        self.assertEqual([paper.id for paper in papers[:2]], ['lune:p-1', 'lune:p-2'])
        self.assertEqual(provider.usage()['requests'], 1)
        self.assertEqual(mock.requests[-1]['params']['arguments']['limit'], 5)
        ledger = json.loads(self.ledger.read_text(encoding='utf-8'))
        self.assertEqual(ledger['entries'][-1]['status'], 'completed')
        self.assertTrue(ledger['entries'][-1]['ok'])

    def test_exact_native_filters(self):
        provider, mock = self.provider(search_script(), max_requests=5)
        provider.search('q', limit=2, filters={'venues': ['NeurIPS', 'ICML'],
                                                 'year_min': 2020, 'year_max': 2024,
                                                 'offset': 10})
        args = mock.requests[-1]['params']['arguments']
        self.assertEqual(args, {'query': 'q', 'limit': 2, 'offset': 10,
                                'venues': ['NeurIPS', 'ICML'], 'year_min': 2020,
                                'year_max': 2024})

    def test_natural_language_venue_list_is_split_before_paid_call(self):
        for filters in ({'venue':'ACL or EMNLP'}, {'venues':['ACL and EMNLP']}):
            provider, mock = self.provider(search_script(), max_requests=5)
            provider.search('q', limit=2, filters=filters)
            self.assertEqual(mock.requests[-1]['params']['arguments']['venues'], ['ACL', 'EMNLP'])

    def test_budget_and_rate_limit(self):
        provider, mock = self.provider(search_script(), max_requests=1)
        provider.search('one', limit=1)
        with self.assertRaises(GuideError):
            provider.search('two', limit=1)
        self.assertEqual(len(mock.requests), 1)
        provider, _ = self.provider({'tools/call': {'status': 429, 'body': b'<html>busy</html>'}}, max_requests=2)
        with self.assertRaises(GuideError) as caught:
            provider.search('q', limit=1)
        self.assertIn('rate_limited', str(caught.exception))
        self.assertEqual(classify_source_error(str(caught.exception)), 'rate_limited')

    def test_install_and_ready_are_local(self):
        cfg = Path(self.tmp.name) / 'mcp.json'
        cfg.write_text(json.dumps({'servers': {'lune': {'url': 'https://example.invalid/mcp',
                                                          'headers': {'Authorization': 'Bearer local'}}}}),
                       encoding='utf-8')
        provider = mcp_lune.LuneProvider(config_path=cfg, ledger_path=self.ledger, max_requests=1)
        self.assertTrue(provider.ready())
        client = provider.client()
        self.assertIs(client, provider.client())
        self.assertIsInstance(client, mcp_client.McpClient)

    def test_client_refuses_insecure_credential_target_before_oauth(self):
        from unittest.mock import patch
        cfg = Path(self.tmp.name) / 'insecure-mcp.json'
        cfg.write_text(json.dumps({'servers': {'lune': {
            'url': 'http://example.invalid/mcp', 'headers': {'Authorization': 'Bearer local'}}}}),
            encoding='utf-8')
        provider = mcp_lune.LuneProvider(config_path=cfg, ledger_path=self.ledger, max_requests=1)
        with patch('mcp_lune.access_token') as token:
            with self.assertRaises(GuideError):
                provider.client()
        token.assert_not_called()

    def test_install_registers_without_evaluation_imports(self):
        mock = MockMcp(search_script())
        self.addCleanup(mock.__exit__)
        client = mcp_client.McpClient(mcp_client.ServerConfig(
            'lune', mock.url, timeout=10, allow=('search_papers',)), quota_store=self.quota)
        provider = mcp_lune.install(client=client, ledger_path=self.ledger, max_requests=2)
        self.assertIs(mcp_lune.installed(), provider)
        self.assertIn('lune', source_mod.PROVIDERS)


if __name__ == '__main__':
    unittest.main()
