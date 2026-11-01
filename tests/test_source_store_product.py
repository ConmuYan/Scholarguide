"""Source service product contracts. All network-facing clients are mocked."""
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from common import Budget, Config, GuideError
from source import SearchBatch, SourceRun
from source_store import CANONICAL_SOURCES, SourceStore


class SourceStoreProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            'SG_MCP_TOKEN_FILE': str(self.root / 'no-token.json'),
            'OPENALEX_API_KEY': '', 'S2_API_KEY': '', 'SEMANTIC_SCHOLAR_API_KEY': '',
            'DEEPSEEK_SEARCH_KEY': '', 'DEEPSEEK_SEARCH_API_KEY': '',
            'LUNE_API_KEY': '', 'LUNE_TOKEN': '', 'SG_API_KEY': 'model-env-secret',
            'SG_BASE_URL': 'https://model.example/v1',
        })
        self.env.start()
        self.store = SourceStore(self.root / 'private' / 'sources.sqlite3')
        self.config = Config(api_key='model-secret', base_url='https://model.example/v1')

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.temp.cleanup()

    def item(self, name):
        return next(row for row in self.store.public_state()['sources'] if row['name'] == name)

    def test_invoke_setup_cancelled_while_store_lock_held(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from common import RunCancelled
        budget = Budget(3)
        held, release = threading.Event(), threading.Event()
        def hold():
            with self.store._lock:
                held.set()
                release.wait(3)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(held.wait(1))
            with patch('mcp_lune.install') as install, ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.store.invoke, 'lune', 'search', {'query': 'fixture'}, self.config, budget=budget)
                time.sleep(.1)
                budget.cancel_token.request()
                with self.assertRaises(RunCancelled):
                    future.result(timeout=1)
                install.assert_not_called()
        finally:
            release.set(); thread.join(2)

    def test_catalog_cancel_during_readiness_after_slot_before_transport(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from common import RunCancelled
        budget = Budget(3)
        entered, proceed = threading.Event(), threading.Event()
        original = self.store._ensure_ready
        def ready(*args):
            entered.set()
            proceed.wait(2)
            return original(*args)
        blocker = sqlite3.connect(str(self.store.path), isolation_level=None)
        try:
            with patch.object(self.store, '_ensure_ready', side_effect=ready), \
                 patch('mcp_lune.install') as install, ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.store.catalog, 'lune', self.config, budget=budget)
                self.assertTrue(entered.wait(1))
                blocker.execute('BEGIN EXCLUSIVE')
                proceed.set()
                time.sleep(.1)
                budget.cancel_token.request()
                with self.assertRaises(RunCancelled):
                    future.result(timeout=1)
                install.assert_not_called()
        finally:
            proceed.set()
            if blocker.in_transaction:
                blocker.execute('ROLLBACK')
            blocker.close()

    def test_skill_public_filters_and_canonical_ids_map_to_mcp_arguments(self):
        self.assertEqual(SourceStore._arguments('search_papers', {'query':'RAG','year_from':2024,'year_to':2025}),
                         {'query':'RAG','year_min':2024,'year_max':2025})
        self.assertEqual(SourceStore._arguments('get_paper_fulltext', {'paper_id':'lune:actual-uuid'}),
                         {'paper_id':'actual-uuid'})

    def test_default_path_and_environment_override(self):
        with patch.dict(os.environ, {'SG_SOURCE_STORE': str(self.root / 'env.sqlite3')}):
            store = SourceStore()
            try:
                self.assertEqual(store.path, self.root / 'env.sqlite3')
            finally:
                store.close()
        with patch.dict(os.environ, {}, clear=True), patch('source_store.Path.home', return_value=self.root):
            store = SourceStore()
            try:
                self.assertEqual(store.path, self.root / '.scholarguide' / 'sources.sqlite3')
            finally:
                store.close()

    def test_listing_is_offline_and_reports_unknown_observations(self):
        with patch('source.AcademicSource', side_effect=AssertionError('network probe')), \
             patch('mcp_lune.install', side_effect=AssertionError('MCP install')), \
             patch('pathlib.Path.read_text', side_effect=AssertionError('credential read')):
            state = self.store.public_state()
        self.assertEqual(state['default_source'], 'multi')
        self.assertEqual({row['name'] for row in state['sources']}, set(CANONICAL_SOURCES))
        required = {'name', 'label', 'enabled', 'priority', 'configured', 'auth_configured',
                    'capabilities', 'health', 'quota'}
        for row in state['sources']:
            self.assertTrue(required <= row.keys())
            self.assertIsNone(row['health'])
            self.assertIsNone(row['quota'])
        self.assertFalse(self.item('lune')['configured'])
        self.assertFalse(self.item('deepseek')['configured'])
        self.assertTrue(self.item('arxiv')['enabled'])
        self.assertEqual(self.store.enabled_sources(), list(CANONICAL_SOURCES[:6]))

    def test_oauth_file_existence_enables_lune_first_without_reading(self):
        token_path = self.root / 'token.json'
        token_path.write_text('{"access_token":"oauth-secret"}', encoding='utf-8')
        with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(token_path)}), \
             patch('pathlib.Path.read_text', side_effect=AssertionError('read OAuth while listing')):
            state = self.store.public_state()
            first = state['sources'][0]
            self.assertEqual(first['name'], 'lune')
            self.assertTrue(first['configured'])
            self.assertTrue(first['auth_configured'])
            self.assertTrue(first['enabled'])
            self.assertEqual(first['priority'], 0)
            self.assertEqual(self.store.enabled_sources()[0], 'lune')
        public = json.dumps(state)
        self.assertNotIn('oauth-secret', public)
        self.assertNotIn(str(token_path), public)

    def test_saved_keys_are_private_durable_and_blank_preserves(self):
        row = self.store.upsert({'name': 'semantic_scholar', 'api_key': 's2-secret',
                                 'enabled': False, 'priority': 2})
        self.assertTrue(row['auth_configured'])
        self.assertFalse(row['enabled'])
        self.assertNotIn('s2-secret', json.dumps(row))
        self.assertNotIn('api_key', row)
        self.store.upsert({'name': 'semantic_scholar', 'api_key': '  '})
        second = SourceStore(self.store.path)
        try:
            configured = second.configure(self.config)
            self.assertEqual(configured.s2_key, 's2-secret')
            self.assertFalse(next(row for row in second.public_state()['sources']
                                  if row['name'] == 'semantic_scholar')['enabled'])
        finally:
            second.close()
        self.store.upsert({'name': 'semantic_scholar', 'clear_key': True})
        self.assertEqual(self.store.configure(replace(self.config, s2_key='old-key')).s2_key, '')

    def test_invalid_upsert_is_atomic(self):
        cases = [({'name': 'not-a-provider'}, 'unknown_source'),
                 ({'name': 'arxiv', 'enabled': 1}, 'invalid_enabled'),
                 ({'name': 'arxiv', 'priority': True}, 'invalid_priority'),
                 ({'name': 'arxiv', 'priority': 101}, 'invalid_priority'),
                 ({'name': 'arxiv', 'priority': -1}, 'invalid_priority'),
                 ({'name': 'deepseek', 'api_key': None}, 'invalid_api_key'),
                 ({'name': 'deepseek', 'api_key': 'key\nvalue'}, 'invalid_api_key'),
                 ({'name': 'deepseek', 'clear_key': 'yes'}, 'invalid_api_key'),
                 ({'name': 'lune', 'endpoint': 'https://key:secret@example.test'}, 'invalid_endpoint'),
                 ({'name': 'lune', 'endpoint': 'https://example.test?key=secret'}, 'invalid_endpoint'),
                 ({'name': 'lune', 'endpoint': 'file:///tmp/mcp'}, 'invalid_endpoint'),
                 ({'name': 'arxiv', 'endpoint': 'https://example.test'}, 'unsupported_endpoint')]
        original = self.item('deepseek')
        for settings, code in cases:
            with self.subTest(settings={key: value for key, value in settings.items() if key != 'api_key'}):
                with self.assertRaises(GuideError) as caught:
                    self.store.upsert(settings)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(GuideError):
            self.store.upsert({'name': 'deepseek', 'enabled': True, 'priority': 101,
                               'api_key': 'must-not-save'})
        self.assertEqual(self.item('deepseek'), original)

    def test_configure_preserves_model_credentials_and_original(self):
        self.store.upsert({'name': 'deepseek', 'api_key': 'source-secret'})
        self.store.upsert({'name': 'openalex', 'api_key': 'openalex-secret'})
        configured = self.store.configure(self.config)
        self.assertIsNot(configured, self.config)
        self.assertIs(configured.source_store, self.store)
        self.assertEqual(configured.deepseek_search_key, 'source-secret')
        self.assertEqual(configured.openalex_key, 'openalex-secret')
        self.assertEqual(configured.api_key, 'model-secret')
        self.assertEqual(configured.base_url, self.config.base_url)
        self.assertEqual(configured.model, self.config.model)
        self.assertEqual(self.config.deepseek_search_key, '')
        self.assertEqual(configured.source, self.config.source)
        self.assertNotIn('source-secret', json.dumps(self.store.public_state()))

    def test_source_environment_is_independent_and_explicit_clear_blocks_fallback(self):
        with patch.dict(os.environ, {'DEEPSEEK_SEARCH_KEY': 'source-env-secret', 'LUNE_API_KEY': 'lune-env-secret'}):
            self.assertTrue(self.item('deepseek')['configured'])
            self.assertTrue(self.item('lune')['configured'])
            self.assertEqual(self.store.configure(self.config).deepseek_search_key, 'source-env-secret')
            self.store.upsert({'name': 'deepseek', 'clear_key': True})
            self.assertFalse(self.item('deepseek')['configured'])
            self.assertEqual(self.store.configure(self.config).deepseek_search_key, '')
        self.assertFalse(self.item('deepseek')['configured'])
        self.assertNotIn('model-env-secret', json.dumps(self.store.public_state()))

    def test_enabled_sources_respects_priority_registry_and_disabled(self):
        self.store.upsert({'name': 'openalex', 'priority': 1})
        self.store.upsert({'name': 'arxiv', 'enabled': False})
        self.assertEqual(self.store.enabled_sources(['arxiv', 'openalex', 'dblp']), ['openalex', 'dblp'])
        self.assertEqual(self.store.enabled_sources('dblp,openalex'), ['openalex', 'dblp'])

    def test_recorded_health_quota_attempts_and_cooldown_survive_reopen(self):
        self.store.upsert({'name': 'openalex', 'api_key': 'source-secret'})
        self.store.record_run({'name': 'openalex', 'status': 'error', 'code': 'rate_limited',
                               'error': 'HTTP 429 api_key=source-secret', 'elapsed_ms': 21,
                               'quota': {'remaining': 0, 'limit': 100, 'api_key': 'source-secret'}})
        store = SourceStore(self.store.path)
        try:
            state = store.health_snapshot()['openalex']
            self.assertEqual(state['failures'], 1)
            self.assertEqual(state['last_code'], 'rate_limited')
            self.assertGreater(state['cooldown_until'], 0)
            public = next(row for row in store.public_state()['sources'] if row['name'] == 'openalex')
            self.assertEqual(public['quota'], {'remaining': 0, 'limit': 100})
            self.assertNotIn('source-secret', json.dumps(public))
            with closing(sqlite3.connect(store.path)) as db:
                row = db.execute('SELECT status,error,elapsed_ms FROM attempts').fetchone()
            self.assertEqual(row[0], 'error')
            self.assertNotIn('source-secret', row[1])
            self.assertEqual(row[2], 21)
        finally:
            store.close()
        # An unversioned success cannot prove it occurred after the failure.
        # Keep the active cooldown until expiry or an explicit user probe.
        self.store.record_run(SourceRun('openalex', 'ok', returned=3))
        self.assertEqual(self.store.health_snapshot()['openalex']['last_code'], 'rate_limited')
        self.assertGreater(self.store.cooldown('openalex'), time.time())
        self.store.cooldown('openalex', 0)
        self.store.record_run(SourceRun('openalex', 'ok', returned=3))
        self.assertEqual(self.store.health_snapshot()['openalex']['failures'], 0)
        self.assertEqual(self.store.cooldown('openalex'), 0)

    def test_cache_and_skips_do_not_invent_connectivity(self):
        self.store.record_run(SourceRun('arxiv', 'cache', returned=2))
        self.assertIsNone(self.item('arxiv')['health'])
        self.store.record_run(SourceRun('arxiv', 'error', error='timed out', code='timeout'))
        health = self.item('arxiv')['health']
        self.store.record_run(SourceRun('arxiv', 'cache', returned=2))
        self.store.record_run(SourceRun('arxiv', 'skipped'))
        self.assertEqual(self.item('arxiv')['health'], health)

    def test_durable_atomic_rate_reservations_and_cooldown(self):
        self.assertEqual(self.store.reserve('arxiv', now=100), 0)
        second = SourceStore(self.store.path)
        try:
            self.assertEqual(second.reserve('arxiv', now=100), 4)
            second.cooldown('arxiv', 120, errorcode='rate_limited')
            self.assertEqual(self.store.reserve('arxiv', now=100), 20)
            self.assertEqual(second.reserve('arxiv', now=100), 24)
        finally:
            second.close()
        with self.assertRaises(GuideError):
            self.store.cooldown('arxiv', float('nan'))
        with self.assertRaises(GuideError):
            self.store.reserve('arxiv', interval=-1)

    def test_probe_executes_single_provider_bounded_uncached_search(self):
        batch = SearchBatch('rag', ['arxiv'], [], [SourceRun('arxiv', 'empty', elapsed_ms=5)])
        with patch('source.AcademicSource') as academic, patch.object(self.store, 'reserve', return_value=0):
            academic.return_value.search_batch.return_value = batch
            result = self.store.probe('arxiv', self.config, query='rag')
            args = academic.call_args.args
            self.assertEqual(args[0].source, 'arxiv')
            self.assertFalse(args[0].cache)
            self.assertEqual(args[0].per_search, 1)
            self.assertEqual(args[1].seconds, 30)
            academic.return_value.search_batch.assert_called_once_with('rag', 1)
        self.assertTrue(result['ok'])
        self.assertEqual(result['health']['status'], 'empty')
        self.assertGreaterEqual(result['duration_ms'], 0)

    def test_probe_contains_real_failure_without_generic_500_or_key_leak(self):
        config = replace(self.config, s2_key='transient-source-secret')
        with patch('source.AcademicSource', side_effect=TimeoutError('timed out transient-source-secret model-secret')), \
             patch.object(self.store, 'reserve', return_value=0):
            result = self.store.probe('semantic_scholar', config)
        self.assertFalse(result['ok'])
        self.assertEqual(result['health']['last_code'], 'timeout')
        self.assertNotIn('transient-source-secret', json.dumps(result))
        self.assertNotIn('model-secret', json.dumps(result))
        self.assertIn('[redacted]', result['health']['error'])

    def test_deepseek_key_is_independent_from_model_profile(self):
        # A model key/base URL is not source authorization. This avoids a web
        # MiniMax profile silently enabling DeepSeek while CLI disagrees.
        with patch.dict(os.environ, {'SG_API_KEY': 'model-only-key',
                                     'SG_BASE_URL': 'https://api.deepseek.com',
                                     'DEEPSEEK_SEARCH_KEY': ''}):
            self.assertFalse(self.item('deepseek')['configured'])
            self.assertFalse(self.item('deepseek')['auth_configured'])
            self.assertEqual(self.item('deepseek')['setup_hint'], '配置独立来源 key 后启用')
        with patch.dict(os.environ, {'DEEPSEEK_SEARCH_KEY': 'source-only-key'}):
            self.assertTrue(self.item('deepseek')['configured'])

    def test_bot_challenge_gets_a_durable_long_cooldown_and_can_be_retested(self):
        self.store.record_run(SourceRun('dblp', 'error', error='DBLP returned an HTML challenge',
                                        code='bot_challenge'))
        row = self.item('dblp')
        self.assertEqual(row['health']['last_code'], 'bot_challenge')
        self.assertGreater(row['cooldown_until'], time.time() + 5 * 3600)
        state = self.store.public_state()
        self.assertIn('dblp', state['cooling_down'])
        self.assertNotIn('dblp', state['routing'])
        from source import AcademicSource
        source = AcademicSource(Config(source='dblp'), Budget(3))
        source.store = self.store
        self.assertFalse(source.available('dblp'))
        # Explicit Sources test is the recovery path after fixing egress.
        with patch('source.AcademicSource._search_one', return_value=[]):
            self.store.probe('dblp', self.config)
        self.assertEqual(self.item('dblp')['cooldown_until'], 0)

    def test_repeated_rate_limits_back_off_and_explicit_probe_is_allowed(self):
        self.store.record_run(SourceRun('semantic_scholar', 'error', error='HTTP 429',
                                        code='rate_limited'))
        first = self.item('semantic_scholar')['cooldown_until']
        self.store.record_run(SourceRun('semantic_scholar', 'error', error='HTTP 429',
                                        code='rate_limited'))
        second = self.item('semantic_scholar')['cooldown_until']
        self.assertGreater(second, first)
        self.assertTrue(self.item('semantic_scholar')['key_recommended'])

    def test_routing_order_is_exposed_for_the_source_page(self):
        state = self.store.public_state()
        # Only configured, enabled sources can actually route. In this isolated
        # environment Lune has no token; a configured Lune is checked live in
        # the product acceptance run.
        self.assertIn('arxiv', state['routing'])
        self.assertTrue(all(item['configured'] and item['enabled']
                            for item in state['sources'] if item['name'] in state['routing']))
        self.assertEqual(state['routing'], [item['name'] for item in state['sources']
                                            if item['enabled'] and item['configured']])
        self.assertIn('requires_key', self.item('lune'))
        self.assertIn('concurrency', self.item('lune'))

    def test_probe_unconfigured_source_does_not_make_request(self):
        with patch('source.AcademicSource') as academic:
            result = self.store.probe('deepseek', self.config)
        academic.assert_not_called()
        self.assertFalse(result['ok'])
        self.assertEqual(result['health']['code'], 'source_not_configured')
        with self.assertRaises(GuideError) as caught:
            self.store.probe('not-a-provider', self.config)
        self.assertEqual(caught.exception.code, 'unknown_source')

    def test_skill_declarations_are_available_offline_without_auth(self):
        with patch('mcp_lune.install', side_effect=AssertionError('online discovery')):
            skills = self.store.skills('lune', self.config)
            self.assertIn('search_papers', [row['name'] for row in skills['skills']])
            self.assertIn('search_related_papers', [row['name'] for row in skills['skills']])
            self.assertIn('get_paper_citations', [row['name'] for row in skills['skills']])
            self.assertFalse(self.store.skills('arxiv', self.config)['supported'])

    def test_skill_validation_rejects_unknown_tools_and_unbounded_args_before_network(self):
        bad_calls = [('delete_everything', {}), ('search', {}), ('search', {'query': 'rag', 'limit': 11}),
                     ('search', {'query': 'rag', 'limit': True}),
                     ('search', {'query': 'rag', 'api_key': 'injected'}),
                     ('search', {'query': 'rag', 'year_from': 2025, 'year_to': 2024}),
                     ('fulltext', {'paper_id': ''}), ('search', [])]
        with patch('mcp_lune.install') as install:
            for tool, args in bad_calls:
                with self.subTest(tool=tool, args=args), self.assertRaises(GuideError):
                    self.store.invoke('lune', tool, args, self.config)
            with self.assertRaises(GuideError) as caught:
                self.store.invoke('arxiv', 'search', {'query': 'rag'}, self.config)
            self.assertEqual(caught.exception.code, 'unsupported_skill')
        install.assert_not_called()

    def test_invoke_dispatches_actual_tool_catalog_and_redacts_results(self):
        provider = MagicMock()
        provider.client.return_value.call_tool.return_value = {
            'structuredContent': {'papers': [{'id': 'p1'}], 'api_key': 'lune-env-secret',
                                  'note': 'Bearer hidden-token'}}
        provider.usage.return_value = {'requests': 1, 'remaining': 299, 'daily_allowance': 300}
        with patch.dict(os.environ, {'LUNE_API_KEY': 'lune-env-secret'}), \
             patch('mcp_lune.install', return_value=provider), \
             patch.object(self.store, 'reserve', return_value=0):
            result = self.store.invoke('lune', 'search', {'query': 'rag', 'limit': 2}, self.config)
            provider.client.return_value.call_tool.assert_called_once_with('search_papers', {'query': 'rag', 'limit': 2})
            provider._charge.assert_called_once_with('search_papers', {})
            self.assertEqual(result['result']['papers'], [{'id': 'p1'}])
            self.assertNotIn('lune-env-secret', json.dumps(result))
            self.assertNotIn('hidden-token', json.dumps(result))
            for alias, expected in [('related', 'search_related_papers'), ('fulltext', 'get_paper_fulltext'),
                                    ('citations', 'get_paper_citations')]:
                self.store.invoke('lune', alias, {'paper_id': 'p1'}, self.config)
                self.assertEqual(provider.client.return_value.call_tool.call_args.args[0], expected)
            catalog = self.store.catalog('lune', self.config)
            self.assertEqual(catalog['skill'], 'list_conferences')
            provider.client.return_value.call_tool.assert_called_with('list_conferences', {})
        self.assertEqual(self.item('lune')['quota']['remaining'], 299)

    def test_saved_lune_key_uses_local_client_and_endpoint_without_environment_mutation(self):
        self.store.upsert({'name': 'lune', 'api_key': 'saved-lune-secret', 'endpoint': 'https://mcp.example.test'})
        provider = SimpleNamespace(config_path=self.root / 'servers.json')
        provider.config_path.write_text(json.dumps({'servers': {'lune': {
            'url': 'https://original.example.test', 'headers': {'Authorization': 'Bearer ${LUNE_API_KEY}'},
            'allow': ['search_papers'], 'timeout': 60}}}), encoding='utf-8')
        with patch('mcp_lune.install', return_value=provider), \
             patch('mcp_client.McpClient.call_tool', return_value={'structuredContent': {'papers': []}}) as call, \
             patch.object(self.store, 'reserve', return_value=0):
            result = self.store.invoke('lune', 'search', {'query': 'rag'}, self.config)
        call.assert_called_once()
        self.assertEqual(call.call_args.args, ('search_papers', {'query': 'rag'}))
        self.assertTrue(callable(call.call_args.kwargs['_before_send']))
        self.assertEqual(result['result'], {'papers': []})
        self.assertEqual(os.environ['LUNE_API_KEY'], '')

    def test_lune_terminal_outcomes_and_success_audit_failure(self):
        from common import RunCancelled
        from execution_events import bind_events
        from mcp_client import McpToolError
        provider = MagicMock()
        events = []
        with patch.dict(os.environ, {'LUNE_API_KEY': 'synthetic-key'}), \
             patch('mcp_lune.install', return_value=provider), \
             patch.object(self.store, 'reserve', return_value=0), \
             bind_events(lambda action, payload: events.append((action, payload))):
            for outcome, expected in [({'isError': True, 'content': 'remote-private'}, 'error'),
                                      (McpToolError('protocol', 'tool reported an error'), 'error'),
                                      ({}, 'unknown'),
                                      (TimeoutError('remote-private'), 'unknown'),
                                      (RunCancelled(), 'unknown')]:
                provider.reset_mock()
                provider._charge.return_value = 1
                provider.client.return_value.call_tool.side_effect = outcome if isinstance(outcome, BaseException) else None
                provider.client.return_value.call_tool.return_value = outcome
                with self.assertRaises((GuideError, TimeoutError, RunCancelled)):
                    self.store.invoke('lune', 'search', {'query': 'private-query'}, self.config)
                self.assertEqual(provider._finish.call_args.kwargs['status'], expected)
                self.assertFalse(provider._finish.call_args.kwargs['ok'])
            provider.reset_mock()
            provider._charge.return_value = 1
            provider.client.return_value.call_tool.side_effect = None
            provider.client.return_value.call_tool.return_value = {'structuredContent': {'papers': []}}
            with patch.object(self.store, 'record_run', side_effect=sqlite3.OperationalError('private-db-text')):
                result = self.store.invoke('lune', 'search', {'query': 'private-query'}, self.config)
            self.assertEqual(result['result'], {'papers': []})
            self.assertEqual(result['audit_status'], 'failed')
            self.assertTrue(any(action == 'source_audit_failed' for action, _ in events))
            self.assertNotIn('private-db-text', json.dumps(events))

    def test_lune_finish_write_failure_does_not_mask_cancel_or_success(self):
        from common import RunCancelled
        from execution_events import bind_events
        provider = MagicMock()
        provider._charge.return_value = 1
        provider._finish.side_effect = OSError('private-local-text')
        events = []
        with patch.dict(os.environ, {'LUNE_API_KEY': 'synthetic-key'}), \
             patch('mcp_lune.install', return_value=provider), \
             patch.object(self.store, 'reserve', return_value=0), \
             bind_events(lambda action, payload: events.append((action, payload))):
            provider.client.return_value.call_tool.side_effect = RunCancelled()
            with self.assertRaises(RunCancelled):
                self.store.invoke('lune', 'search', {'query': 'private-query'}, self.config)
            provider.client.return_value.call_tool.side_effect = None
            provider.client.return_value.call_tool.return_value = {'structuredContent': {'papers': []}}
            result = self.store.invoke('lune', 'search', {'query': 'private-query'}, self.config)
        self.assertEqual(result['audit_status'], 'failed')
        self.assertEqual(result['result'], {'papers': []})
        self.assertTrue(any(a == 'source_audit_failed' for a, _ in events))
        self.assertNotIn('private-local-text', json.dumps(events))
        self.assertNotIn('private-query', json.dumps(events))

    def test_cancel_during_local_processing_reports_completed_without_content(self):
        from common import RunCancelled
        from execution_events import bind_events
        provider = MagicMock()
        provider._charge.return_value = 1
        provider.client.return_value.call_tool.return_value = {'structuredContent': {'papers': ['private-remote']}}
        events = []
        cancel = RunCancelled()
        with patch.dict(os.environ, {'LUNE_API_KEY': 'synthetic-key'}), \
             patch('mcp_lune.install', return_value=provider), \
             patch.object(self.store, 'reserve', return_value=0), \
             patch('mcp_client.structured', side_effect=cancel), \
             bind_events(lambda action, payload: events.append((action, payload))):
            with self.assertRaises(RunCancelled) as caught:
                self.store.invoke('lune', 'search', {'query': 'private-query'}, self.config)
        self.assertIs(caught.exception, cancel)
        self.assertEqual(cancel.remote_outcome, 'completed')
        self.assertFalse(cancel.retry_safe)
        self.assertTrue(any(a == 'source_remote_completed' and p.get('status') == 'completed' for a, p in events))
        self.assertNotIn('private-query', json.dumps(events))
        self.assertNotIn('private-remote', json.dumps(events))
        self.assertEqual(provider._finish.call_args.kwargs['status'], 'completed')
        self.assertEqual(provider.client.return_value.call_tool.call_count, 1)

    def test_cancel_in_post_success_audit_and_usage_is_not_swallowed(self):
        from common import RunCancelled
        from execution_events import bind_events
        for stage in ('finish', 'usage', 'record'):
            with self.subTest(stage=stage):
                provider = MagicMock()
                provider._charge.return_value = 1
                provider.client.return_value.call_tool.return_value = {'structuredContent': {'papers': []}}
                error = RunCancelled()
                if stage == 'finish':
                    provider._finish.side_effect = error
                if stage == 'usage':
                    provider.usage.side_effect = error
                events = []
                record = patch.object(self.store, 'record_run', side_effect=error) if stage == 'record' else patch.object(self.store, 'record_run', wraps=self.store.record_run)
                with patch.dict(os.environ, {'LUNE_API_KEY': 'synthetic-key'}), \
                     patch('mcp_lune.install', return_value=provider), \
                     patch.object(self.store, 'reserve', return_value=0), record, \
                     bind_events(lambda action, payload: events.append((action, payload))):
                    with self.assertRaises(RunCancelled) as caught:
                        self.store.invoke('lune', 'search', {'query': 'private-query'}, self.config)
                self.assertIs(caught.exception, error)
                self.assertEqual(error.remote_outcome, 'completed')
                self.assertFalse(error.retry_safe)
                self.assertTrue(any(a == 'source_remote_completed' for a, _ in events))
                self.assertNotIn('private-query', json.dumps(events))
                self.assertEqual(provider.client.return_value.call_tool.call_count, 1)

    def test_lune_success_then_locked_database_reports_remote_success(self):
        from execution_events import bind_events
        provider = MagicMock()
        events = []
        blocker = sqlite3.connect(str(self.store.path), isolation_level=None)
        def answer(*args):
            blocker.execute('BEGIN EXCLUSIVE')
            return {'structuredContent': {'papers': []}}
        provider._charge.return_value = 1
        provider.client.return_value.call_tool.side_effect = answer
        started = time.monotonic()
        try:
            with patch.dict(os.environ, {'LUNE_API_KEY': 'synthetic-key'}), \
                 patch('mcp_lune.install', return_value=provider), \
                 patch.object(self.store, 'reserve', return_value=0), \
                 bind_events(lambda action, payload: events.append((action, payload))):
                with self.assertRaises(GuideError) as caught:
                    self.store.invoke('lune', 'search', {'query': 'private-query'}, self.config)
            self.assertEqual(caught.exception.code, 'remote_success_local_failure')
            self.assertEqual(provider.client.return_value.call_tool.call_count, 1)
            self.assertEqual(provider._finish.call_args.kwargs['status'], 'completed')
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(any(action == 'source_audit_failed' for action, _ in events))
            self.assertNotIn('private-query', json.dumps(events))
        finally:
            if blocker.in_transaction:
                blocker.execute('ROLLBACK')
            blocker.close()

    def test_lune_error_audit_database_lock_is_bounded_and_preserves_error(self):
        from mcp_client import McpError
        import sqlite3
        import time
        blocker = sqlite3.connect(str(self.store.path), isolation_level=None)
        provider = MagicMock()
        def fail(*args, **kwargs):
            blocker.execute('BEGIN IMMEDIATE')
            raise McpError('rate_limited', 'fixture', status=429)
        provider.client.return_value.call_tool.side_effect = fail
        started = time.monotonic()
        try:
            with patch.dict(os.environ, {'LUNE_API_KEY': 'synthetic-key'}), \
                 patch('mcp_lune.install', return_value=provider), \
                 patch.object(self.store, 'reserve', return_value=0):
                with self.assertRaises(GuideError) as caught:
                    self.store.invoke('lune', 'search', {'query': 'fixture'}, self.config)
            self.assertEqual(caught.exception.code, 'rate_limited')
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            blocker.execute('ROLLBACK'); blocker.close()

    def test_lune_error_is_not_replaced_by_failed_audit_write(self):
        from mcp_client import McpError
        import sqlite3
        provider = MagicMock()
        provider.client.return_value.call_tool.side_effect = McpError('rate_limited', 'fixture', status=429)
        with patch.dict(os.environ, {'LUNE_API_KEY': 'synthetic-key'}), \
             patch('mcp_lune.install', return_value=provider), \
             patch.object(self.store, 'reserve', return_value=0), \
             patch.object(self.store, 'record_run', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(GuideError) as caught:
                self.store.invoke('lune', 'search', {'query': 'fixture'}, self.config)
        self.assertEqual(caught.exception.code, 'rate_limited')

    def test_lune_errors_are_structured_recorded_and_secret_safe(self):
        from mcp_client import McpError
        provider = MagicMock()
        provider.client.return_value.call_tool.side_effect = McpError('rate_limited', '429 lune-env-secret', status=429)
        with patch.dict(os.environ, {'LUNE_API_KEY': 'lune-env-secret'}), \
             patch('mcp_lune.install', return_value=provider), \
             patch.object(self.store, 'reserve', return_value=0):
            with self.assertRaises(GuideError) as caught:
                self.store.invoke('lune', 'search', {'query': 'rag'}, self.config)
        self.assertEqual(caught.exception.code, 'rate_limited')
        self.assertNotIn('lune-env-secret', str(caught.exception))
        self.assertEqual(self.item('lune')['health']['status'], 'error')
        self.assertGreater(self.store.cooldown('lune'), 0)

    def test_cross_origin_override_does_not_forward_other_configured_headers(self):
        self.store.upsert({'name': 'lune', 'api_key': 'new-synthetic-key',
                           'endpoint': 'https://new.example.test/mcp'})
        path = self.root / 'servers.json'
        path.write_text(json.dumps({'servers': {'lune': {
            'url': 'https://old.example.test/mcp', 'allow': ['search_papers'],
            'headers': {'X-Api-Key': 'old-synthetic-secret', 'Cookie': 'secret=fixture'}}}}),
            encoding='utf-8')
        observed = []
        def call(client, tool, args, *, _before_send):
            self.assertTrue(callable(_before_send))
            observed.append(dict(client.config.headers))
            return {'structuredContent': {'papers': []}}
        with patch('mcp_lune.install', return_value=SimpleNamespace(config_path=path)), \
             patch('mcp_client.McpClient.call_tool', autospec=True, side_effect=call), \
             patch.object(self.store, 'reserve', return_value=0):
            self.store.invoke('lune', 'search', {'query': 'fixture'}, self.config)
        self.assertEqual(observed, [{'Authorization': 'Bearer new-synthetic-key'}])

    def test_changing_lune_endpoint_requires_explicit_key_reentry(self):
        self.store.upsert({'name': 'lune', 'api_key': 'synthetic-key',
                           'endpoint': 'https://first.example.test/mcp'})
        with self.assertRaises(GuideError):
            self.store.upsert({'name': 'lune', 'endpoint': 'https://second.example.test/mcp'})
        self.assertEqual(self.store._row('lune')['endpoint'], 'https://first.example.test/mcp')
        self.store.upsert({'name': 'lune', 'endpoint': 'https://second.example.test/mcp',
                           'api_key': 'new-synthetic-key'})

    def test_custom_lune_host_does_not_receive_environment_key_implicitly(self):
        self.store.upsert({'name': 'lune', 'endpoint': 'https://untrusted.example.test/mcp'})
        path = self.root / 'servers.json'
        path.write_text(json.dumps({'servers': {'lune': {
            'url': 'https://official.example.test/mcp', 'allow': ['search_papers']}}}), encoding='utf-8')
        provider = SimpleNamespace(config_path=path)
        with patch.dict(os.environ, {'LUNE_API_KEY': 'synthetic-env-key'}), \
             patch('mcp_lune.install', return_value=provider), \
             patch('mcp_client.McpClient.call_tool') as call, \
             patch.object(self.store, 'reserve', return_value=0):
            with self.assertRaises(GuideError):
                self.store.invoke('lune', 'search', {'query': 'fixture'}, self.config)
        call.assert_not_called()

    def test_custom_lune_host_does_not_receive_existing_oauth_token(self):
        token_path = self.root / 'oauth.json'
        token_path.write_text('{"access_token":"synthetic-token"}', encoding='utf-8')
        self.store.upsert({'name': 'lune', 'endpoint': 'https://untrusted.example.test/mcp'})
        path = self.root / 'servers.json'
        path.write_text(json.dumps({'servers': {'lune': {
            'url': 'https://official.example.test/mcp', 'allow': ['search_papers']}}}), encoding='utf-8')
        provider = SimpleNamespace(config_path=path)
        with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(token_path)}), \
             patch('mcp_lune.install', return_value=provider), \
             patch('mcp_lune.access_token', return_value='synthetic-token') as token, \
             patch('mcp_client.McpClient.call_tool') as call, \
             patch.object(self.store, 'reserve', return_value=0):
            with self.assertRaises(GuideError):
                self.store.invoke('lune', 'search', {'query': 'fixture'}, self.config)
        token.assert_not_called()
        call.assert_not_called()

    def test_oauth_is_programmatic_and_shared_client_budget_is_restored(self):
        from mcp_lune import LuneProvider
        token_path = self.root / 'oauth.json'
        token_path.write_text('{"access_token":"oauth-test-secret"}', encoding='utf-8')
        config_path = self.root / 'mcp.json'
        config_path.write_text(json.dumps({'servers': {'lune': {
            'url': 'https://mcp.example.test', 'timeout': 60,
            'headers': {'Authorization': 'Bearer ${LUNE_API_KEY}'},
            'allow': ['search_papers']}}}), encoding='utf-8')
        provider = LuneProvider(config_path=config_path, ledger_path=self.root / 'ledger.json', max_requests=3)
        observed = []
        def answer(client, tool, arguments, *, _before_send):
            self.assertTrue(callable(_before_send))
            observed.append((client.config.headers['Authorization'], client.config.timeout, client.budget.seconds))
            return {'structuredContent': {'papers': []}}
        with patch.dict(os.environ, {'SG_MCP_TOKEN_FILE': str(token_path)}), \
             patch('mcp_lune.access_token', return_value='oauth-test-secret') as token, \
             patch('mcp_lune.install', return_value=provider), \
             patch('mcp_client.McpClient.call_tool', autospec=True, side_effect=answer), \
             patch.object(self.store, 'reserve', return_value=0):
            result = self.store.invoke('lune', 'search', {'query': 'rag'}, self.config)
        self.assertTrue(token.called)
        self.assertEqual(observed[0][0], 'Bearer oauth-test-secret')
        self.assertLessEqual(observed[0][1], 30)
        self.assertEqual(observed[0][2], 30)
        self.assertIsNone(provider._client.budget)
        self.assertEqual(provider._client.config.timeout, 60)
        self.assertEqual(provider.requests, 1)
        self.assertNotIn('oauth-test-secret', json.dumps(result))

    def test_cloned_lune_client_tracks_rejected_token_with_request_budget(self):
        from mcp_client import McpClient, ServerConfig
        shared = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)))
        shared.set_auth_provider(lambda force: 'unused')
        provider = MagicMock()
        provider.client.return_value = shared
        budget = Budget(3)
        def call(client, tool, arguments, *, _before_send):
            self.assertTrue(callable(_before_send))
            self.assertTrue(client._auth_provider_budgeted)
            self.assertEqual(client._auth_provider(False, budget=client.budget, rejected_token=None), 'synthetic-old')
            self.assertEqual(client._auth_provider(True, budget=client.budget, rejected_token='synthetic-old'), 'synthetic-new')
            return {'structuredContent': {'papers': []}}
        with patch.dict(os.environ, {'LUNE_API_KEY': 'synthetic-key'}), \
             patch('mcp_lune.install', return_value=provider), \
             patch('mcp_lune.access_token', side_effect=['synthetic-old', 'synthetic-new']) as token, \
             patch('mcp_client.McpClient.call_tool', autospec=True, side_effect=call), \
             patch.object(self.store, 'reserve', return_value=0):
            self.store.invoke('lune', 'search', {'query': 'fixture'}, self.config, budget=budget)
        self.assertEqual(token.call_args_list[0].kwargs,
                         {'force_refresh': False, 'budget': budget, 'rejected_token': None})
        self.assertEqual(token.call_args_list[1].kwargs,
                         {'force_refresh': True, 'budget': budget, 'rejected_token': 'synthetic-old'})
        self.assertIsNone(shared.budget)

    def test_unlisted_fulltext_tool_fails_before_charge_or_request(self):
        from mcp_client import McpClient, ServerConfig
        client = McpClient(ServerConfig('lune', 'https://mcp.example.test', allow=('search_papers',)))
        provider = MagicMock()
        provider.client.return_value = client
        with patch.dict(os.environ, {'LUNE_API_KEY': 'lune-env-secret'}), \
             patch('mcp_lune.install', return_value=provider), \
             patch('mcp_client.McpClient.call_tool') as call, \
             patch.object(self.store, 'reserve', return_value=0):
            with self.assertRaises(GuideError) as caught:
                self.store.invoke('lune', 'fulltext', {'paper_id': 'p1'}, self.config)
        self.assertEqual(caught.exception.code, 'unsupported_skill')
        provider._charge.assert_not_called()
        call.assert_not_called()
        self.assertIsNone(client.budget)

    def test_probe_does_not_duplicate_attempt_already_recorded_by_source(self):
        run = SourceRun('arxiv', 'ok', returned=1, elapsed_ms=7)
        def search(query, limit):
            self.store.record_run(run)
            return SearchBatch(query, ['arxiv'], [], [run])
        with patch('source.AcademicSource') as academic, patch.object(self.store, 'reserve', return_value=0):
            academic.return_value.search_batch.side_effect = search
            self.store.probe('arxiv', self.config)
        with closing(sqlite3.connect(self.store.path)) as db:
            count = db.execute("SELECT COUNT(*) FROM attempts WHERE name='arxiv'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_parallel_store_instances_keep_rate_slots_and_failure_counts_atomic(self):
        from concurrent.futures import ThreadPoolExecutor
        def work(index):
            store = SourceStore(self.store.path)
            try:
                delay = store.reserve('openalex', interval=1, now=100)
                store.record_run(SourceRun('openalex', 'error', error='temporary failure', code='source_error'))
                return delay
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=4) as executor:
            delays = list(executor.map(work, range(8)))
        self.assertEqual(sorted(delays), list(range(8)))
        self.assertEqual(self.store.health_snapshot()['openalex']['failures'], 8)

    def test_adapter_run_redacts_transient_configuration_credentials(self):
        config = replace(self.config, s2_key='transient-source-key')
        self.store.configure(config)
        self.store.record_run(SourceRun('semantic_scholar', 'error',
            error='upstream echoed transient-source-key and model-secret', code='auth'))
        encoded = json.dumps(self.store.public_state())
        self.assertNotIn('transient-source-key', encoded)
        self.assertNotIn('model-secret', encoded)
        self.assertIn('[redacted]', encoded)

    def test_memory_store_is_usable(self):
        store = SourceStore(':memory:')
        try:
            store.upsert({'name': 'arxiv', 'priority': 0})
            self.assertEqual(store.public_state()['sources'][0]['name'], 'arxiv')
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
