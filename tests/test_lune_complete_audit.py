import io
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_delivery import isolated_environment
from common import Budget, Config, GuideError, Paper
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, ServerConfig
from mcp_lune import LuneProvider
from source import AcademicSource, retained_partial_batch


class CompleteAuditTests(unittest.TestCase):
    def test_completed_audit_failures_stop_paid_followups(self):
        for failure in ('quota', 'ledger'):
            for empty in (False, True):
                with self.subTest(failure=failure, empty=empty), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    with patch.dict(os.environ, isolated_environment(root, inherited=dict(os.environ)), clear=True), \
                         patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), \
                         LuneQuotaStore(root / 'quota.sqlite') as quota:
                        quota.confirm(3, time.time() + 60)
                        calls = []
                        def transport(request, timeout):
                            calls.append(1)
                            msg = json.loads(request.data)
                            papers = [] if empty else [{'id': 'one', 'title': 'First'}]
                            result = {'structuredContent': {'papers': papers, 'has_more': False,
                                                            'audit_status': 'recorded'}}
                            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode())
                        client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                                           transport=transport, quota_store=quota)
                        provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
                        source = AcademicSource(Config(source='openalex,lune,deepseek', cache=False), Budget(5))
                        prior = Paper('openalex:W1', 'Prior', '', 'https://fixture.invalid/1')
                        original = source._search_one
                        seen = []
                        def search(name, *args):
                            seen.append(name)
                            return [prior] if name == 'openalex' else original(name, *args)
                        failing = patch.object(quota, 'finish', side_effect=OSError('private')) if failure == 'quota' else patch.object(provider, '_finish', side_effect=OSError('private'))
                        with failing, patch('mcp_lune.install', return_value=provider), \
                             patch.object(source, 'available', return_value=True), \
                             patch.object(source, '_wait_for'), patch.object(source, '_search_one', side_effect=search):
                            with self.assertRaises(GuideError) as caught:
                                source.search_batch('fixture', providers=['openalex', 'lune', 'deepseek'])
                        error = caught.exception
                        batch = retained_partial_batch(error, 'fixture', ['openalex', 'lune', 'deepseek'], 10)
                        self.assertIsNotNone(batch)
                        self.assertEqual([p.id for p in batch.papers], ['openalex:W1'] + ([] if empty else ['lune:one']))
                        run = batch.runs[-1]
                        self.assertEqual(run.status, 'empty' if empty else 'ok')
                        self.assertEqual(run.remote_outcome, 'completed')
                        self.assertIs(run.retry_safe, False)
                        self.assertEqual(run.audit_status, 'failed')
                        self.assertEqual(batch.stop_reason, 'source_error')
                        self.assertEqual(seen, ['openalex', 'lune'])
                        self.assertEqual(calls, [1])
                        self.assertNotIn('private', str(error))

    def test_plain_list_and_empty_success_are_normal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, isolated_environment(root, inherited=dict(os.environ)), clear=True), \
                 patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), \
                 LuneQuotaStore(root / 'quota.sqlite') as quota:
                quota.confirm(2, time.time() + 60)
                def transport(request, timeout):
                    msg = json.loads(request.data)
                    result = {'structuredContent': {'papers': [], 'has_more': False, 'audit_status': 'failed'}}
                    return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}).encode())
                client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)), transport=transport, quota_store=quota)
                provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
                source = AcademicSource(Config(source='lune', cache=False), Budget(5))
                with patch('mcp_lune.install', return_value=provider), patch.object(source, 'available', return_value=True), patch.object(source, '_wait_for'):
                    batch = source.search_batch('fixture')
                self.assertEqual(batch.runs[0].status, 'empty')
                self.assertIsNone(batch.runs[0].audit_status)
                self.assertEqual(batch.stop_reason, '')


if __name__ == '__main__':
    unittest.main()
