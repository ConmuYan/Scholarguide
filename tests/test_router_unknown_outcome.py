"""A paid unknown result must not authorize later paid router calls."""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_tools import ToolRegistry
from common import Budget, Config, GuideError, Paper
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, ServerConfig
from retrieval import RetrievalPlan, RetrievalRouter
from source import SearchBatch, SourceRun


class UnknownRouterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        environment = patch.dict(os.environ, {'SG_SKIP_PROJECT_ENV': '1',
            'SG_MCP_TOKEN_FILE': str(root / 'no-token'), 'SG_SOURCE_STORE': str(root / 'source.sqlite'),
            'SG_LUNE_QUOTA_DB': str(root / 'quota.sqlite'), 'SG_PROVIDER_STORE': str(root / 'provider.json'),
            'PI_CODING_AGENT_DIR': str(root / 'no-pi'), 'LUNE_API_KEY': '', 'LUNE_TOKEN': ''})
        environment.start()
        self.addCleanup(environment.stop)
        self.quota = LuneQuotaStore(root / 'quota.sqlite')
        self.addCleanup(self.quota.close)
        self.quota.confirm(10, time.time() + 60)
        self.http = []
        def transport(request, timeout):
            message = json.loads(request.data)
            self.http.append(message['method'])
            if len(self.http) == 1:
                raise TimeoutError('synthetic-private')
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                          'result': {'structuredContent': {'papers': []}}}).encode())
        self.client = McpClient(ServerConfig('lune', 'https://fixture.invalid/mcp',
                                             allow=('search_papers',)),
                                transport=transport, quota_store=self.quota)

    def router(self, *, first_not_started=False):
        owner = self
        class Source:
            providers = ['lune', 'openalex', 'deepseek']
            budget = Budget(6)
            calls = []
            def available(self, name): return True
            def search_batch(self, query, limit, providers=None, filters=None):
                names = list(providers or self.providers)
                self.calls.append((query, names))
                if names == ['lune']:
                    if first_not_started and len(self.calls) == 1:
                        error = GuideError('local admission refused')
                        error.remote_outcome, error.retry_safe = 'not_started', False
                        raise error
                    owner.client.call_tool('search_papers', {'query': query, 'limit': limit}, budget=self.budget)
                    return SearchBatch(query, names, [], [SourceRun('lune', 'empty', cost_cny=None)])
                if names == ['openalex']:
                    paper = Paper('fixture:1', 'Fixture paper', 'Abstract about RAG.',
                                  'https://fixture.invalid/paper', sources=['openalex'])
                    return SearchBatch(query, names, [paper], [SourceRun('openalex', 'ok', 1)])
                raise AssertionError('unexpected paid follow-up')
        source = Source()
        router = RetrievalRouter(Config(source='lune'), source,
                                 tools=ToolRegistry.from_source(source))
        return router, source

    def test_unknown_stops_later_paid_query_and_preserves_outcome(self):
        router, source = self.router()
        plan = RetrievalPlan(['lune'], ['first', 'second'], ['fixture'], single_source=True)
        with patch.object(router, 'plan', return_value=plan):
            result = router.retrieve({'topic': 'RAG', 'target_count': 2}, 'RAG', limit=2)
        self.assertEqual(self.http, ['tools/call'])
        self.assertEqual(source.calls, [('first', ['lune'])])
        observation = result.observation()
        self.assertEqual(observation['remote_outcome'], 'unknown')
        self.assertIs(observation['retry_safe'], False)
        self.assertTrue(observation['tool_cost_unknown'])
        self.assertTrue(observation['paid_followups_blocked'])
        self.assertNotIn('synthetic-private', repr(observation))

    def test_not_started_is_not_mistaken_for_unknown_paid_work(self):
        router, source = self.router(first_not_started=True)
        plan = RetrievalPlan(['lune'], ['first', 'second'], ['fixture'], single_source=True)
        def success(request, timeout):
            message = json.loads(request.data)
            self.http.append(message['method'])
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                          'result': {'structuredContent': {'papers': []}}}).encode())
        with patch.object(self.client, '_transport', side_effect=success), \
             patch.object(router, 'plan', return_value=plan):
            result = router.retrieve({'topic': 'RAG', 'target_count': 2}, 'RAG', limit=2)
        self.assertEqual(source.calls, [('first', ['lune']), ('second', ['lune'])])
        self.assertEqual(self.http, ['tools/call'])
        self.assertNotIn('paid_followups_blocked', result.observation())
        self.assertEqual(result.observation()['remote_outcomes'][0]['remote_outcome'], 'not_started')

    def test_explicit_remote_error_is_preserved_without_paid_followup(self):
        router, source = self.router()
        plan = RetrievalPlan(['lune'], ['first', 'second'], ['fixture'], single_source=True)
        def error_response(request, timeout):
            message = json.loads(request.data)
            self.http.append(message['method'])
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                          'error': {'code': -32602, 'message': 'private'}}).encode())
        with patch.object(self.client, '_transport', side_effect=error_response), \
             patch.object(router, 'plan', return_value=plan):
            result = router.retrieve({'topic': 'RAG', 'target_count': 2}, 'RAG', limit=2)
        self.assertEqual(self.http, ['tools/call'])
        self.assertEqual(source.calls, [('first', ['lune'])])
        self.assertEqual(result.observation()['remote_outcome'], 'error')
        self.assertTrue(result.observation()['paid_followups_blocked'])
        self.assertNotIn('private', repr(result.observation()))

    def test_partial_batch_never_repeats_its_failed_paid_source(self):
        class MixedSource:
            providers = ['lune', 'openalex']
            budget = Budget(5)
            calls = []
            def available(self, name): return True
            def search_batch(self, query, limit, providers=None, filters=None):
                names = list(providers)
                self.calls.append((query, names))
                runs = [SourceRun('openalex', 'empty')]
                if 'lune' in names:
                    runs.insert(0, SourceRun('lune', 'error', cost_cny=None,
                                              remote_outcome='unknown', retry_safe=False))
                return SearchBatch(query, names, [], runs)
        source = MixedSource()
        router = RetrievalRouter(Config(source='multi'), source, tools=ToolRegistry.from_source(source))
        plan = RetrievalPlan(['lune', 'openalex'], ['first', 'second'], ['fixture'])
        with patch.object(router, 'plan', return_value=plan):
            result = router.retrieve({'topic': 'RAG', 'target_count': 2}, 'RAG', limit=2)
        self.assertEqual(source.calls, [('first', ['lune', 'openalex']), ('second', ['openalex'])])
        self.assertEqual(result.observation()['remote_outcome'], 'unknown')
        self.assertTrue(result.observation()['paid_followups_blocked'])

    def test_research_keeps_free_source_but_skips_further_paid_work(self):
        router, source = self.router()
        def step(provider, query):
            return {'provider': provider, 'query': query, 'intent': 'fixture', 'filters': {}}
        plan = RetrievalPlan(['lune', 'openalex', 'deepseek'], ['RAG'], ['fixture'], research=True,
                             steps=[step('lune', 'first'), step('openalex', 'free'),
                                    step('deepseek', 'paid')])
        with patch.object(router, 'plan', return_value=plan):
            result = router.retrieve({'topic': 'RAG', 'target_count': 2}, 'RAG', limit=2)
        self.assertEqual(self.http, ['tools/call'])
        self.assertEqual(source.calls, [('first', ['lune']), ('free', ['openalex'])])
        self.assertEqual([paper.id for paper in result.papers], ['fixture:1'])
        observation = result.observation()
        self.assertTrue(observation['paid_followups_blocked'])
        self.assertEqual(observation['remote_outcome'], 'unknown')
        self.assertTrue(observation['tool_cost_unknown'])


if __name__ == '__main__':
    unittest.main()
