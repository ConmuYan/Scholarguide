"""Offline unit contracts for the bounded MiniMax native-tool probe."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import probe_minimax_native_tool as probe


class ProbeHelperTests(unittest.TestCase):
    def test_usage_accepts_responses_and_chat_aliases_but_rejects_bool(self):
        value = probe._usage({'usage': {
            'prompt_tokens': 4, 'completion_tokens': 3,
            'input_tokens_details': {'cached_tokens': 2},
            'output_tokens_details': {'reasoning_tokens': 1}}})
        self.assertEqual(value, {'input_tokens': 4, 'output_tokens': 3,
                                 'cached_tokens': 2, 'reasoning_tokens': 1})
        self.assertIsNone(probe._usage({'usage': {'input_tokens': True}})['input_tokens'])

    def test_estimate_is_snapshot_price_estimate_not_zero(self):
        self.assertEqual(probe._estimate({'input_tokens': 100, 'output_tokens': 20},
                                         0.15, 0.6), 0.000027)
        self.assertIsNone(probe._estimate({'input_tokens': None, 'output_tokens': 20},
                                          0.15, 0.6))

    def test_response_meta_never_persists_tool_name_or_arguments(self):
        meta = probe._response_meta({'id': 'r', 'status': 'completed',
            'output': [{'type': 'function_call', 'call_id': 'c',
                        'name': 'secret-looking-tool', 'arguments': '{}'}],
            'usage': {'input_tokens': 1, 'output_tokens': 2}})
        self.assertNotIn('name', meta['tool_calls'][0])
        self.assertNotIn('arguments', json.dumps(meta))

    def test_probe_returns_nonzero_for_noncompleted_or_no_tool_response(self):
        profile = SimpleNamespace(id='fixture', name='fixture', protocol='responses',
                                  model='fixture', base_url='https://example.invalid',
                                  input_price=None, output_price=None, currency='USD',
                                  api_key='fake-test-key')
        profile.config = lambda: SimpleNamespace(request_timeout=10, model='fixture')
        for response, expected in [({'status': 'incomplete', 'output': []}, 'incomplete_response'),
                                   ({'status': 'completed', 'output': []}, 'completed_without_tool_call')]:
            with self.subTest(status=expected), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'result.json'
                with patch.object(probe, 'ProviderStore') as store, \
                     patch.object(probe, '_post', return_value=(response, 200, 0.1)) as post, \
                     patch('builtins.print'):
                    store.return_value.active.return_value = profile
                    self.assertEqual(probe.run(path), 1)
                report = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(report['status'], expected)
                self.assertEqual(report['calls_made'], 1)
                self.assertEqual(post.call_count, 1)
                self.assertIsNone(report['estimated_model_cost_usd_at_snapshot_price'])


if __name__ == '__main__':
    unittest.main()
