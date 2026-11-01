"""Synthetic end-to-end outcome categories must survive each trusted boundary."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_tools import ToolObservation
from api_v1 import error_payload, execution_error
from common import GuideError
from mcp_client import McpError
from mcp_lune import LuneProvider
from source import SourceRun


class OutcomeInteroperabilityTests(unittest.TestCase):
    def test_explicit_remote_error_and_received_response_are_not_unknown(self):
        for outcome in ('error', 'response_received'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                failure = McpError('protocol', 'fixed synthetic error')
                failure.remote_outcome, failure.retry_safe = outcome, False
                class Client:
                    def call_tool(self, name, args): raise failure
                path = Path(tmp) / 'ledger.json'
                provider = LuneProvider(client=Client(), ledger_path=path)
                with self.assertRaises(GuideError) as caught:
                    provider.search('fixture', 1)
                error = caught.exception
                self.assertEqual(error.remote_outcome, outcome)
                self.assertIs(error.retry_safe, False)
                self.assertEqual(json.loads(path.read_text())['entries'][-1]['status'], outcome)
                run = SourceRun('lune', 'error', remote_outcome=outcome, retry_safe=False)
                self.assertEqual(run.as_dict()['remote_outcome'], outcome)
                observation = ToolObservation('search', False, 'error', remote_outcome=outcome, retry_safe=False)
                self.assertEqual(observation.as_dict()['remote_outcome'], outcome)
                self.assertEqual(error_payload(error)['error']['remote_outcome'], outcome)
                self.assertEqual(execution_error(error)['remote_outcome'], outcome)

    def test_mcp_error_dict_does_not_reflect_untrusted_outcome(self):
        error = McpError('protocol', 'fixed synthetic error')
        error.remote_outcome, error.retry_safe = 'synthetic-private', True
        self.assertNotIn('synthetic-private', repr(error.as_dict()))
        self.assertNotIn('retry_safe', error.as_dict())


if __name__ == '__main__':
    unittest.main()
