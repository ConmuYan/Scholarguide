"""Focused governance regressions: authorization, schema, budget and privacy."""
from pathlib import Path
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_tools import ToolRegistry, ToolSpec
from execution_events import bind_events, operation
from mcp_client import McpClient, McpError, ServerConfig
from mcp_skills import Param, Skill, SkillRegistry, invoke
from common import Budget, GuideError, RunCancelled


class GovernanceTests(unittest.TestCase):
    def test_host_binding_and_detached_execution_input(self):
        spec = ToolSpec('probe', 'test', 'test', {'type': 'object'}, 1, 1, 'unknown', ())
        received = []
        def handler(**values):
            received.append(values)
            return values
        handlers = {'probe': handler}
        registry = ToolRegistry([spec], handlers=handlers)
        handlers['probe'] = lambda **values: self.fail('binding changed')
        unbound = ToolRegistry([spec]).call('probe', handler, _input={'x': 1})
        self.assertFalse(unbound.ok)
        impostor = registry.call('probe', lambda **values: self.fail('impostor'), _input={'x': 1})
        self.assertFalse(impostor.ok)
        replaced = registry.call('probe', handler, x=2, _input={'x': 1})
        self.assertFalse(replaced.ok)
        original = {'nested': {'values': [1]}}
        def sink(action, payload):
            if action == 'tool_start':
                original['nested']['values'].append(2)
        with bind_events(sink):
            result = registry.call('probe', handler, _input=original)
        self.assertTrue(result.ok)
        self.assertEqual(received, [{'nested': {'values': [1]}}])
        original['nested']['values'].append(3)
        self.assertEqual(received[0]['nested']['values'], [1])

    def test_retry_uses_fresh_validated_snapshot_and_obeys_limit(self):
        spec = ToolSpec('probe', 'test', 'test', {'type': 'object'}, 1, 2, 'unknown', ())
        seen = []
        def handler(**values):
            seen.append(list(values['nested']))
            values['nested'].append('handler mutation')
            error = GuideError('temporary')
            error.retry_safe = True
            raise error
        registry = ToolRegistry([spec], handlers={'probe': handler})
        with patch('agent_tools.time.sleep'):
            result = registry.call('probe', handler, _input={'nested': ['original']})
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)
        self.assertEqual(seen, [['original'], ['original']])

    def test_non_boolean_retry_safe_is_not_authorization(self):
        spec = ToolSpec('probe', 'test', 'test', {'type': 'object'}, 1, 3, 'unknown', ())
        calls = []
        def handler(**values):
            calls.append(1)
            error = GuideError('HTTP 429')
            error.retry_safe = 'true'
            raise error
        result = ToolRegistry([spec], handlers={'probe': handler}).call('probe', handler, _input={})
        self.assertFalse(result.retryable)
        self.assertEqual(calls, [1])

    def test_audit_summary_has_no_reversible_argument_fingerprint(self):
        from tool_governance import audit_arguments
        summary = audit_arguments({'query': 'synthetic-rare-topic'})
        self.assertNotIn('sha256', summary)
        self.assertNotIn('synthetic-rare-topic', str(summary))

    def test_registry_rejects_invalid_input_before_fn(self):
        registry = ToolRegistry.from_source(object())
        called = []
        observation = registry.call('search', lambda: called.append(True),
                                    _input={'query': 'q', 'limit': 99, 'filters': {}})
        self.assertFalse(observation.ok)
        self.assertEqual(observation.status, 'invalid_input')
        self.assertEqual(called, [])

    def test_registry_provenance_and_events_do_not_contain_secret(self):
        def probe(**values):
            return {'ok': True}
        registry = ToolRegistry([ToolSpec(
            name='probe', kind='test', description='test',
            input_schema={'type': 'object'}, timeout_s=1, max_retries=1,
            cost_model='free', provenance=())], handlers={'probe': probe})
        events = []
        with bind_events(lambda action, payload: events.append((action, payload))):
            observation = registry.call('probe', probe,
                                        _input={'api_key': 'secret-value', 'note': 'hello'})
        self.assertTrue(observation.ok)
        self.assertNotIn('secret-value', str(observation.provenance))
        self.assertNotIn('secret-value', str(events))
        self.assertTrue(any(action == 'tool_start' for action, _ in events))
        self.assertTrue(any(action == 'tool_end' for action, _ in events))

    def test_mcp_empty_allow_rejects_before_transport(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(True)
            raise AssertionError('transport must not run')
        client = McpClient(ServerConfig('mock', 'http://example.invalid/mcp'),
                           transport=transport)
        with self.assertRaises(McpError) as caught:
            client.call_tool('search_papers', {'query': 'q'})
        self.assertEqual(caught.exception.kind, 'client_error')
        self.assertEqual(calls, [])

    def test_mcp_input_schema_rejects_before_transport(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(True)
            raise AssertionError('transport must not run')
        client = McpClient(ServerConfig('mock', 'http://example.invalid/mcp', allow_all=True),
                           transport=transport)
        client._tools = [{'name': 'search_papers', 'inputSchema': {
            'type': 'object', 'properties': {'query': {'type': 'string'}},
            'required': ['query'], 'additionalProperties': False}}]
        client._tool_schemas = {'search_papers': client._tools[0]['inputSchema']}
        with self.assertRaises(McpError) as caught:
            client.call_tool('search_papers', {'limit': 999})
        self.assertEqual(caught.exception.kind, 'client_error')
        self.assertEqual(calls, [])

    def test_mcp_prompt_requires_allow_and_budget_cancel_is_observed(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(True)
            raise AssertionError('cancelled prompt must not reach transport')
        client = McpClient(ServerConfig('mock', 'http://example.invalid/mcp',
                                        allow_prompts=('verify_draft',)), transport=transport)
        budget = Budget(10)
        budget.cancel_token.request()
        with self.assertRaises(RunCancelled):
            client.get_prompt('verify_draft', {'draft': 'x'}, budget=budget)
        with self.assertRaises(McpError):
            client.get_prompt('other', {}, budget=Budget(10))
        self.assertEqual(calls, [])

    def test_dynamic_mcp_prompt_requires_explicit_approval(self):
        registry = SkillRegistry()
        registry.register(Skill(
            name='server_flow', description='untrusted',
            params=(Param('topic', required=True),),
            steps=({'kind': 'prompt', 'prompt': 'server_flow'},),
            source='mcp-prompt', server='mock'))
        with self.assertRaises(GuideError):
            invoke(registry, 'server_flow', {'topic': 'x'}, client=object(), execute=True)

    def test_operation_error_is_redacted(self):
        events = []
        with self.assertRaises(ValueError):
            with bind_events(lambda action, payload: events.append((action, payload))):
                with operation('probe', category='test'):
                    raise ValueError('token=secret-value')
        self.assertNotIn('secret-value', str(events))
        self.assertTrue(any(action == 'tool_error' for action, _ in events))


if __name__ == '__main__':
    unittest.main()
