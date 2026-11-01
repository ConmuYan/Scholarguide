"""Offline outcome and audit regressions using synthetic clients and temporary files."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_tools import ToolRegistry
from common import GuideError, RunCancelled
from execution_events import bind_events
from mcp_client import McpError
from mcp_lune import LuneProvider
from source import SearchBatch, SourceRun


class Client:
    def __init__(self, result):
        self.result, self.calls = result, 0
    def call_tool(self, name, arguments):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.json'

    def provider(self, result):
        client = Client(result)
        return LuneProvider(client=client, ledger_path=self.path), client

    def test_mcp_error_preserves_safe_outcome_and_never_retries(self):
        for outcome in ('unknown', 'not_started', 'completed', 'secret'):
            with self.subTest(outcome=outcome):
                self.path.unlink(missing_ok=True)
                error = McpError('transport', 'synthetic')
                error.remote_outcome, error.retry_safe = outcome, True
                provider, client = self.provider(error)
                with self.assertRaises(GuideError) as caught:
                    provider.search('q', 1)
                self.assertEqual(caught.exception.remote_outcome, outcome if outcome != 'secret' else 'unknown')
                self.assertIs(caught.exception.retry_safe, False)
                self.assertEqual(client.calls, 1)
                self.assertNotEqual(json.loads(self.path.read_text())['entries'][-1]['status'], 'completed')

    def test_mcp_timeout_does_not_trust_completed_hint(self):
        error = McpError('timeout', 'synthetic')
        error.remote_outcome = 'completed'
        provider, _ = self.provider(error)
        with self.assertRaises(GuideError) as caught:
            provider.search('q', 1)
        self.assertEqual(caught.exception.remote_outcome, 'unknown')
        self.assertIs(caught.exception.retry_safe, False)

    def test_real_mcp_client_synthetic_timeout_transport(self):
        from mcp_client import McpClient, ServerConfig
        calls = []
        def transport(request, *, timeout):
            calls.append(1)
            raise TimeoutError('synthetic private')
        config = ServerConfig('fixture', 'https://example.invalid/mcp', allow=('search_papers',),
                              local_tool_schemas={'search_papers': {'type': 'object'}})
        client = McpClient(config, transport=transport)
        provider = LuneProvider(client=client, ledger_path=self.path)
        with self.assertRaises(GuideError) as caught:
            provider.search('q', 1)
        self.assertEqual(caught.exception.remote_outcome, 'unknown')
        self.assertIs(caught.exception.retry_safe, False)
        self.assertEqual(calls, [1])
        self.assertEqual(json.loads(self.path.read_text())['entries'][-1]['status'], 'unknown')

    def test_timeout_and_invalid_response_are_unknown(self):
        for result in (TimeoutError('private'), {'content': [{'type': 'text', 'text': 'invalid'}]}, None):
            with self.subTest(result=type(result).__name__):
                self.path.unlink(missing_ok=True)
                provider, client = self.provider(result)
                with self.assertRaises(GuideError) as caught:
                    provider.search('q', 1)
                self.assertEqual(caught.exception.remote_outcome, 'unknown')
                self.assertIs(caught.exception.retry_safe, False)
                self.assertEqual(client.calls, 1)
                self.assertEqual(json.loads(self.path.read_text())['entries'][-1]['status'], 'unknown')

    def test_finish_failure_keeps_original_exception_and_emits_fixed_event(self):
        for original in (RunCancelled(), RuntimeError('private')):
            with self.subTest(original=type(original).__name__):
                self.path.unlink(missing_ok=True)
                provider, client = self.provider(original)
                events = []
                with patch.object(provider, '_finish', side_effect=OSError('secret')), bind_events(lambda a, p: events.append((a, p))):
                    with self.assertRaises(type(original)) as caught:
                        provider.search('q', 1)
                self.assertIs(caught.exception, original)
                self.assertEqual(client.calls, 1)
                self.assertEqual(events[0][0], 'source_audit_failed')
                self.assertNotIn('secret', str(events))

    def test_cancelled_during_success_audit_is_not_swallowed(self):
        provider, client = self.provider({'structuredContent': {'papers': [{'id': '1', 'title': 'one'}]}})
        with patch.object(provider, '_finish', side_effect=RunCancelled()):
            with self.assertRaises(RunCancelled) as caught:
                provider.search('q', 1)
        self.assertEqual(caught.exception.remote_outcome, 'completed')
        self.assertIs(caught.exception.retry_safe, False)
        self.assertEqual(client.calls, 1)

    def test_interrupt_during_success_audit_is_not_swallowed(self):
        provider, client = self.provider({'structuredContent': {'papers': []}})
        with patch.object(provider, '_finish', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                provider.search('q', 1)
        self.assertEqual(client.calls, 1)

    def test_audit_cancellation_does_not_replace_original_error(self):
        original = RunCancelled()
        provider, client = self.provider(original)
        with patch.object(provider, '_finish', side_effect=RunCancelled()):
            with self.assertRaises(RunCancelled) as caught:
                provider.search('q', 1)
        self.assertIs(caught.exception, original)
        self.assertEqual(client.calls, 1)

    def test_finish_failure_does_not_erase_success(self):
        provider, client = self.provider({'structuredContent': {'papers': [{'id': '1', 'title': 'one'}]}})
        events = []
        with patch.object(provider, '_finish', side_effect=OSError('secret')), bind_events(lambda a, p: events.append((a, p))):
            papers = provider.search('q', 1)
        self.assertEqual(papers[0].id, 'lune:1')
        self.assertEqual(client.calls, 1)
        self.assertEqual(events[0][0], 'source_audit_failed')
        self.assertNotIn('secret', str(events))
        self.assertEqual(json.loads(self.path.read_text())['entries'][-1]['status'], 'reserved')

    def test_actual_ledger_write_failure_rolls_back_memory_and_stops_pagination(self):
        provider, client = self.provider({'structuredContent': {'papers': [{'id': '1'}], 'has_more': True}})
        save = provider._save_ledger
        calls = []
        def fail_second_save():
            calls.append(1)
            if len(calls) == 2:
                raise OSError('private ledger error')
            save()
        events = []
        with patch.object(provider, '_save_ledger', side_effect=fail_second_save), bind_events(lambda a, p: events.append((a, p))):
            papers = provider.search('q', 10)
        self.assertEqual(len(papers), 1)
        self.assertEqual(client.calls, 1)
        self.assertEqual(provider.entries[-1]['status'], 'reserved')
        self.assertNotIn('ok', provider.entries[-1])
        self.assertEqual(json.loads(self.path.read_text())['entries'][-1]['status'], 'reserved')
        self.assertEqual(events[0][0], 'source_audit_failed')
        self.assertNotIn('private', str(events))

    def test_explicit_success_completed(self):
        provider, _ = self.provider({'structuredContent': {'papers': []}})
        self.assertEqual(provider.search('q', 1), [])
        self.assertEqual(json.loads(self.path.read_text())['entries'][-1]['status'], 'completed')

    def test_search_observation_safe_outcome_for_exception_and_batch(self):
        class Source:
            providers = ['lune']
            def search_batch(self, *args, **kwargs):
                raise error
        source = Source()
        registry = ToolRegistry.from_source(source)
        error = GuideError('private')
        error.remote_outcome, error.retry_safe = 'unknown', False
        obs = registry.search('q', 1, ['lune'])
        self.assertEqual(obs.as_dict()['remote_outcome'], 'unknown')
        self.assertIs(obs.as_dict()['retry_safe'], False)
        error.remote_outcome = 'secret'
        obs = registry.search('q', 1, ['lune'])
        self.assertNotIn('remote_outcome', obs.as_dict())
        source.search_batch = lambda *a, **k: SearchBatch('q', ['lune'], [], [SourceRun('lune', 'error', remote_outcome='unknown', retry_safe=False)])
        registry = ToolRegistry.from_source(source)
        obs = registry.search('q', 1, ['lune'])
        self.assertEqual(obs.as_dict()['remote_outcome'], 'unknown')
        self.assertIs(obs.as_dict()['retry_safe'], False)

if __name__ == '__main__':
    unittest.main()
