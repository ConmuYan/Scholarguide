"""Offline pagination admission and logical-call outcome regression tests."""
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Budget, BudgetExceeded, Config, RunCancelled
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, ServerConfig
from mcp_lune import LuneProvider
from source import AcademicSource


class PageOutcomeTests(unittest.TestCase):
    def run_interrupted(self, cancel, router=False):
        with tempfile.TemporaryDirectory() as directory, \
             LuneQuotaStore(Path(directory) / 'quota.sqlite') as quota:
            root = Path(directory)
            quota.confirm(4, time.time() + 60)
            sent = []
            def transport(request, timeout):
                message = json.loads(request.data)
                sent.append(message['params']['arguments']['offset'])
                payload = {'papers': [{'id': 'first', 'title': 'First paper'}], 'has_more': True}
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                              'result': {'structuredContent': payload}}).encode())
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid/mcp',
                                            allow=('search_papers',)), transport=transport,
                               quota_store=quota)
            provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
            budget = Budget(3 if cancel else .5)
            source = AcademicSource(Config(source='lune', cache=False), budget)
            reserved = threading.Event()
            waiting = threading.Event()
            original_reserve = quota.reserve
            original_wait = budget.wait
            count = 0
            def reserve(method, **kwargs):
                nonlocal count
                result = original_reserve(method, **kwargs)
                count += 1
                if count == 2:
                    source.cooldown['lune'] = time.monotonic() + 1
                    reserved.set()
                return result
            def wait(seconds):
                if reserved.is_set():
                    waiting.set()
                return original_wait(seconds)
            errors = []
            results = []
            def run():
                try:
                    if router:
                        from retrieval import RetrievalRouter
                        from agent_tools import ToolRegistry
                        tools = ToolRegistry.from_source(source) if router == 'registry' else None
                        results.append(RetrievalRouter(source.config, source, tools=tools).retrieve(
                            {'topic': 'fixture', 'target_count': 10}, 'fixture'))
                    else:
                        source.search_batch('fixture', limit=10)
                except BaseException as exc:
                    errors.append(exc)
            with patch('mcp_lune.install', return_value=provider), \
                 patch.object(source, 'available', return_value=True), \
                 patch.object(source, '_wait_for'), \
                 patch.object(quota, 'reserve', side_effect=reserve), \
                 patch.object(budget, 'wait', side_effect=wait):
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(waiting.wait(2))
                    if cancel:
                        budget.cancel_token.request()
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                finally:
                    budget.cancel_token.request()
                    thread.join(3)
            remaining = quota.status()['remaining_from_snapshot']
            self.assertEqual(sent, [0])
            self.assertEqual(client.http_attempts, 1)
            self.assertEqual(provider.requests, 2)
            self.assertEqual(remaining, 2)
            if router:
                self.assertEqual(errors, [])
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].observation()['tool_cost_unknown'])
                self.assertEqual([p.id for p in results[0].papers], ['lune:first'])
                self.assertEqual(results[0].batches[0].stop_reason, 'user_cancelled')
                self.assertEqual([p.id for p in source.last_batch.papers], ['lune:first'])
                self.assertEqual(source.last_batch.stop_reason, 'user_cancelled')
                return
            self.assertEqual(len(errors), 1)
            error = errors[0]
            self.assertIsInstance(error, RunCancelled if cancel else BudgetExceeded)
            self.assertEqual(error.remote_outcome, 'unknown')
            self.assertFalse(error.retry_safe)
            self.assertEqual([p.id for p in error.partial_papers], ['lune:first'])
            self.assertIs(error.partial_batch, source.last_batch)
            self.assertEqual([p.id for p in source.last_batch.papers], ['lune:first'])
            self.assertEqual(source.last_batch.stop_reason, 'user_cancelled' if cancel else 'budget_exceeded')
            self.assertIsNone(source.last_batch.runs[-1].cost_cny)
            self.assertEqual(source.last_batch.runs[-1].status, 'partial')

    def test_cancel_after_second_reservation(self):
        self.run_interrupted(True)

    def test_router_preserves_unknown_cost_and_partial_candidates(self):
        self.run_interrupted(True, router=True)

    def test_registry_router_preserves_partial_candidates_after_cancel(self):
        self.run_interrupted(True, router='registry')

    def test_second_page_sent_unknown_is_not_masked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with LuneQuotaStore(root / 'quota.sqlite') as quota:
                quota.confirm(3, time.time() + 60)
                offsets = []
                def transport(request, timeout):
                    message = json.loads(request.data)
                    offsets.append(message['params']['arguments']['offset'])
                    if len(offsets) == 2:
                        raise TimeoutError('synthetic timeout')
                    result = {'structuredContent': {'papers': [{'id': 'first', 'title': 'First'}],
                                                    'has_more': True}}
                    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                                  'result': result}).encode())
                client = McpClient(ServerConfig('lune', 'https://fixture.invalid/mcp',
                                                allow=('search_papers',)), transport=transport,
                                   quota_store=quota)
                provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
                source = AcademicSource(Config(source='lune', cache=False), Budget(3))
                with patch('mcp_lune.install', return_value=provider), \
                     patch.object(source, 'available', return_value=True), \
                     patch.object(source, '_wait_for'):
                    from common import GuideError
                    with self.assertRaises(GuideError) as caught:
                        source.search_batch('fixture', limit=10)
                self.assertEqual(offsets, [0, 1])
                self.assertEqual(client.http_attempts, 2)
                self.assertEqual(quota.status()['remaining_from_snapshot'], 1)
                self.assertEqual(caught.exception.remote_outcome, 'unknown')
                self.assertFalse(caught.exception.retry_safe)
                self.assertEqual([p.id for p in source.last_batch.papers], ['lune:first'])
                self.assertEqual(source.last_batch.runs[-1].status, 'partial')
                self.assertEqual(source.last_batch.stop_reason, 'source_error')

    def test_budget_after_second_reservation(self):
        self.run_interrupted(False)


if __name__ == '__main__':
    unittest.main()
