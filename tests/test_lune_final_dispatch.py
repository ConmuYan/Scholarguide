"""Final Lune dispatch checks using temporary stores and a synthetic transport."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Budget, BudgetExceeded, Config, RunCancelled
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, McpError, ServerConfig
from mcp_lune import LuneProvider
from source import AcademicSource
from source_store import SourceStore


class FinalDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        env = {'SG_SKIP_PROJECT_ENV': '1', 'SG_MCP_TOKEN_FILE': str(root / 'missing-token'),
               'SG_PROVIDER_STORE': str(root / 'provider.json'), 'SG_SOURCE_STORE': str(root / 'source.sqlite'),
               'SG_LUNE_QUOTA_DB': str(root / 'quota.sqlite'), 'SG_LUNE_LEDGER': str(root / 'ledger.json'),
               'PI_CODING_AGENT_DIR': str(root / 'missing-pi'), 'LUNE_API_KEY': '', 'LUNE_TOKEN': '',
               'SG_API_KEY': '', 'OPENAI_API_KEY': '', 'MINIMAX_API_KEY': '',
               'DEEPSEEK_SEARCH_KEY': '', 'DEEPSEEK_SEARCH_API_KEY': '', 'SG_SOURCE_PROXY': ''}
        environment = patch.dict(os.environ, env)
        environment.start()
        self.addCleanup(environment.stop)
        self.root = root
        self.store = SourceStore(root / 'source.sqlite')
        self.other = SourceStore(root / 'source.sqlite')
        self.addCleanup(self.store.close)
        self.addCleanup(self.other.close)
        self.quota = LuneQuotaStore(root / 'quota.sqlite')
        self.addCleanup(self.quota.close)
        self.quota.confirm(3, time.time() + 60)
        self.sent = []
        def transport(request, timeout):
            self.sent.append(time.monotonic())
            message = json.loads(request.data)
            result = {'structuredContent': {'papers': []}}
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode())
        config = ServerConfig('lune', 'https://fixture.invalid/mcp', allow=('search_papers',))
        self.client = McpClient(config, transport=transport, quota_store=self.quota)
        self.provider = LuneProvider(client=self.client, ledger_path=root / 'ledger.json')
        self.config = Config(source='lune', source_store=self.store, cache=False)

    def inject_cooldown_after_reservation(self, seconds=10):
        original = LuneQuotaStore.reserve
        def reserve(ledger, method, **kwargs):
            reservation = original(ledger, method, **kwargs)
            self.other.cooldown('lune', time.time() + seconds, errorcode='rate_limited')
            return reservation
        return patch.object(LuneQuotaStore, 'reserve', reserve)

    def test_search_checks_cooldown_after_quota_admission(self):
        source = AcademicSource(self.config, Budget(.8))
        with patch('mcp_lune.install', return_value=self.provider), self.inject_cooldown_after_reservation():
            with self.assertRaises(BudgetExceeded) as caught:
                source._search_one('lune', 'fixture', 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 2)
        self.assertEqual(getattr(caught.exception, 'remote_outcome', None), 'not_started')
        self.assertIs(getattr(caught.exception, 'retry_safe', None), False)

    def test_sources_skill_checks_cooldown_after_quota_admission(self):
        with patch.object(self.store, '_ensure_ready', return_value=self.config), \
             patch('mcp_lune.install', return_value=self.provider), self.inject_cooldown_after_reservation():
            with self.assertRaises(BudgetExceeded) as caught:
                self.store.invoke('lune', 'search', {'query': 'fixture'}, self.config, budget=Budget(.8))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 2)
        self.assertEqual(getattr(caught.exception, 'remote_outcome', None), 'not_started')
        self.assertIs(getattr(caught.exception, 'retry_safe', None), False)

    def test_late_cooldown_is_followed_by_new_rate_admission(self):
        import source_store
        source = AcademicSource(self.config, Budget(3))
        original = self.store.wait_for_rate
        reservations = []
        def reserve_rate(name, budget):
            reservations.append(name)
            return original(name, budget)
        with patch.dict(source_store._RATE, {'lune': .08}), \
             patch.object(self.store, 'wait_for_rate', side_effect=reserve_rate), \
             patch('mcp_lune.install', return_value=self.provider), \
             self.inject_cooldown_after_reservation(.2):
            started = time.monotonic()
            self.assertEqual(source._search_one('lune', 'fixture', 1), [])
        self.assertEqual(reservations, ['lune', 'lune'])
        self.assertEqual(len(self.sent), 1)
        self.assertGreaterEqual(self.sent[0] - started, .17)
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 2)

    def test_cancel_during_late_cooldown_does_not_dispatch(self):
        budget = Budget(4)
        source = AcademicSource(self.config, budget)
        waiting = threading.Event()
        original_wait = budget.wait
        errors = []
        def wait(seconds):
            if seconds > 1:
                waiting.set()
            return original_wait(seconds)
        def run():
            try:
                source._search_one('lune', 'fixture', 1)
            except BaseException as exc:
                errors.append(exc)
        with patch('mcp_lune.install', return_value=self.provider), \
             self.inject_cooldown_after_reservation(2), patch.object(budget, 'wait', side_effect=wait):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(waiting.wait(2))
                budget.cancel_token.request()
                thread.join(2)
                self.assertFalse(thread.is_alive())
            finally:
                budget.cancel_token.request()
                thread.join(2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RunCancelled)
        self.assertEqual(getattr(errors[0], 'remote_outcome', None), 'not_started')
        self.assertIs(getattr(errors[0], 'retry_safe', None), False)
        self.assertEqual(self.sent, [])
        self.assertEqual(budget.search_http_calls, 0)
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 2)

    def test_callback_exception_is_sanitized_and_never_replayed(self):
        def bad_callback():
            raise OSError('synthetic-private')
        with self.assertRaises(McpError) as caught:
            self.client.call_tool('search_papers', {'query': 'fixture'}, budget=Budget(2),
                                  _before_send=bad_callback)
        self.assertEqual(caught.exception.remote_outcome, 'not_started')
        self.assertIs(caught.exception.retry_safe, False)
        self.assertNotIn('synthetic-private', str(caught.exception))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.client.http_attempts, 0)
        self.assertEqual(self.quota.status()['remaining_from_snapshot'], 2)


if __name__ == '__main__':
    unittest.main()
