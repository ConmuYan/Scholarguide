"""Regressions for paid failure gating, source cache identity and slot reentry."""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Budget, Config, GuideError, Paper
from demo import ChatDemoLLM, DemoLLM, DemoSource
from session import ChatSession
from source import AcademicSource
from source_store import SourceStore
from state import StateStore
from workflow import run


class FailedPaidSource(DemoSource):
    providers = ['lune']
    def __init__(self, outcome):
        super().__init__()
        self.outcome, self.calls = outcome, 0
    def search(self, query, limit=10):
        self.calls += 1
        error = GuideError('synthetic paid source failure')
        error.remote_outcome, error.retry_safe = self.outcome, False
        raise error


class RemainingGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.dict(os.environ, {'SG_SKIP_PROJECT_ENV': '1',
            'SG_MCP_TOKEN_FILE': str(self.root / 'no-token'),
            'SG_SOURCE_STORE': str(self.root / 'source.sqlite'),
            'SG_LUNE_QUOTA_DB': str(self.root / 'quota.sqlite'),
            'LUNE_API_KEY': '', 'LUNE_TOKEN': ''})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_single_source_failure_does_not_authorize_workflow_or_chat_retry(self):
        for remote in ('unknown', 'error'):
            with self.subTest(remote=remote):
                source = FailedPaidSource(remote)
                result = run('Recommend five RAG papers.', Config(source='lune'), 'A',
                             source=source, llm=DemoLLM())
                self.assertEqual(source.calls, 1)
                self.assertNotEqual(result['status'], 'complete')
                self.assertIsNone(result['cost']['tools'])
                store = StateStore(self.root / (remote + '.sqlite'))
                try:
                    source = FailedPaidSource(remote)
                    chat = ChatSession(Config(source='lune'), store, source=source, llm=ChatDemoLLM())
                    answer = chat.start(message='Recommend five RAG papers.')
                    self.assertEqual(source.calls, 1)
                    self.assertNotEqual(answer['status'], 'complete')
                    self.assertIsNone(answer['cost']['tools'])
                finally:
                    store.close()

    def test_real_mcp_failure_does_not_repeat_single_source_workflow(self):
        from lune_quota import LuneQuotaStore
        from mcp_client import McpClient, ServerConfig
        from mcp_lune import LuneProvider
        for remote in ('unknown', 'error'):
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with LuneQuotaStore(root / 'quota.sqlite') as quota:
                    quota.confirm(3, time.time() + 60)
                    calls = []
                    def transport(request, timeout):
                        message = json.loads(request.data)
                        calls.append(message['method'])
                        if remote == 'unknown':
                            raise TimeoutError('synthetic-private')
                        return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                            'error': {'code': -32602, 'message': 'synthetic-private'}}).encode())
                    client = McpClient(ServerConfig('lune', 'https://fixture.invalid/mcp',
                                                    allow=('search_papers',)),
                                       transport=transport, quota_store=quota)
                    provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
                    budget = Budget(5)
                    config = Config(source='lune', cache=False)
                    source = AcademicSource(config, budget)
                    with patch('mcp_lune.install', return_value=provider), \
                         patch.object(source, 'available', return_value=True), \
                         patch.object(source, '_wait_for'):
                        result = run('Recommend five RAG papers.', config, 'A',
                                     source=source, llm=DemoLLM(), budget=budget)
                    self.assertEqual(calls, ['tools/call'])
                    self.assertNotEqual(result['status'], 'complete')
                    self.assertIsNone(result['cost']['tools'])
                    self.assertEqual(quota.status()['remaining_from_snapshot'], 2)
                    self.assertNotIn('synthetic-private', repr(result['trace']))

    def test_lune_cached_data_is_not_reused_across_identity_or_endpoint(self):
        first = AcademicSource(Config(source='lune', cache=True, cache_dir=self.root,
                                      lune_key='synthetic-a', lune_endpoint='https://first.invalid'), Budget(3))
        second = AcademicSource(Config(source='lune', cache=True, cache_dir=self.root,
                                       lune_key='synthetic-b', lune_endpoint='https://second.invalid'), Budget(3))
        first_paper = Paper('lune:a', 'First', '', 'https://fixture.invalid/a')
        second_paper = Paper('lune:b', 'Second', '', 'https://fixture.invalid/b')
        legacy_path = first._cache_path('fixture', 10, ['lune'], None)
        legacy = json.dumps({'schema': 2, 'stored_at': time.time(),
                             'papers': [Paper('lune:legacy', 'Legacy', '', '').to_dict()],
                             'runs': [dict(name='lune', status='ok')]})
        legacy_path.write_text(legacy, encoding='utf-8')
        with patch.object(first, 'available', return_value=True), \
             patch.object(first, '_search_one', return_value=[first_paper]), \
             patch.object(second, 'available', return_value=True), \
             patch.object(second, '_search_one', return_value=[second_paper]) as dispatch:
            self.assertEqual(first.search_batch('fixture').papers[0].id, 'lune:a')
            self.assertEqual(second.search_batch('fixture').papers[0].id, 'lune:b')
            dispatch.assert_called_once()
        self.assertEqual(legacy_path.read_text(encoding='utf-8'), legacy)

    def test_same_store_slot_reentry_fails_immediately(self):
        first = SourceStore(self.root / 'shared.sqlite')
        second = SourceStore(self.root / 'shared.sqlite')
        try:
            with first.request_slot('lune', Budget(3)):
                started = time.monotonic()
                with self.assertRaises(GuideError) as caught:
                    with second.request_slot('lune', Budget(.3)):
                        pass
                self.assertEqual(getattr(caught.exception, 'code', None), 'source_slot_reentrant')
                self.assertLess(time.monotonic() - started, .2)
        finally:
            second.close(); first.close()

    def test_cooldown_rejection_is_known_not_started(self):
        for name in ('lune', 'deepseek'):
            with self.subTest(name=name):
                source = AcademicSource(Config(source=name, cache=False), Budget(2))
                source.cooldown[name] = time.monotonic() + 30
                with patch.object(source, '_search_one') as dispatch:
                    with self.assertRaises(GuideError) as caught:
                        source.search_batch('fixture')
                self.assertEqual(caught.exception.remote_outcome, 'not_started')
                self.assertIs(caught.exception.retry_safe, False)
                dispatch.assert_not_called()

    def test_standalone_fork_reinitializes_child_slot(self):
        if not hasattr(os, 'fork'):
            import subprocess
            result = subprocess.run(['wsl', '--', 'python3', '-m', 'unittest',
                'tests.test_governance_remaining.RemainingGovernanceTests.test_standalone_fork_reinitializes_child_slot'],
                capture_output=True, timeout=40)
            self.assertEqual(result.returncode, 0, repr(result.stderr[-2000:]))
            return
        source = AcademicSource(Config(source='lune', cache=False), Budget(2))
        with source._source_slot('lune'):
            reader, writer = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(reader)
                try:
                    child = AcademicSource(Config(source='lune', cache=False), Budget(.5))
                    with child._source_slot('lune'):
                        os.write(writer, b'ok')
                except BaseException:
                    os.write(writer, b'failed')
                finally:
                    os.close(writer)
                    os._exit(0)
            os.close(writer)
            try:
                result = os.read(reader, 32)
                _, status = os.waitpid(pid, 0)
                self.assertEqual(status, 0)
                self.assertEqual(result, b'ok')
            finally:
                os.close(reader)

    def test_standalone_slot_reentry_fails_immediately(self):
        source = AcademicSource(Config(source='lune', cache=False), Budget(1))
        with source._source_slot('lune'):
            started = time.monotonic()
            with self.assertRaises(GuideError) as caught:
                with source._source_slot('lune'):
                    pass
            self.assertEqual(getattr(caught.exception, 'code', None), 'source_slot_reentrant')
            self.assertLess(time.monotonic() - started, .2)


if __name__ == '__main__':
    unittest.main()
