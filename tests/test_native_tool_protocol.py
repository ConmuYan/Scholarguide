"""Local protocol-only tests; no model, source or filesystem tool is executed."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_tool_protocol import (
    PROBE_TOOL,
    chat_followup_messages,
    chat_tool_payload,
    extract_chat_tool_calls,
    extract_responses_tool_calls,
    responses_followup_input,
    responses_tool_payload,
)

ARGS = '{"value":"fixed"}'


class NativeToolProtocolTests(unittest.TestCase):
    def test_responses_payload_declares_function_tool_and_serializes_input(self):
        payload = responses_tool_payload('MiniMax-M3.1-Flash',
                                         [{'role': 'user', 'content': 'probe'}],
                                         [PROBE_TOOL])
        self.assertEqual(payload['tools'][0]['type'], 'function')
        self.assertEqual(payload['tools'][0]['name'], 'native_probe')
        self.assertEqual(payload['tools'][0]['parameters'], PROBE_TOOL['parameters'])
        self.assertFalse(payload['parallel_tool_calls'])
        self.assertEqual(payload['input'][0]['role'], 'user')
        self.assertNotIn('Authorization', json.dumps(payload))

    def test_chat_payload_uses_function_wrapper(self):
        payload = chat_tool_payload('MiniMax-M3.1-Flash',
                                    [{'role': 'user', 'content': 'probe'}],
                                    [PROBE_TOOL])
        function = payload['tools'][0]['function']
        self.assertEqual(function['name'], 'native_probe')
        self.assertEqual(function['parameters'], PROBE_TOOL['parameters'])
        self.assertFalse(payload['parallel_tool_calls'])

    def test_responses_extracts_call_id_and_preserves_full_output_on_followup(self):
        response = {
            'id': 'resp_1', 'status': 'completed',
            'output': [
                {'type': 'reasoning', 'id': 'reason_1', 'summary': []},
                {'type': 'function_call', 'id': 'fc_item', 'call_id': 'call_1',
                 'name': 'native_probe', 'arguments': ARGS},
            ],
        }
        calls = extract_responses_tool_calls(response)
        self.assertEqual(calls[0]['call_id'], 'call_1')
        followup = responses_followup_input(
            [{'role': 'user', 'content': 'probe'}], response, 'call_1', '{"ok":true}')
        self.assertEqual(followup[1]['type'], 'reasoning')
        self.assertEqual(followup[2]['type'], 'function_call')
        self.assertEqual(followup[-1], {'type': 'function_call_output',
                                        'call_id': 'call_1', 'output': '{"ok":true}'})

    def test_chat_extracts_tool_calls_and_preserves_assistant_message(self):
        response = {'choices': [{'message': {
            'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'call_1', 'type': 'function',
                            'function': {'name': 'native_probe', 'arguments': ARGS}}]},
            'finish_reason': 'tool_calls'}]}
        calls = extract_chat_tool_calls(response)
        self.assertEqual(calls[0]['name'], 'native_probe')
        followup = chat_followup_messages(
            [{'role': 'user', 'content': 'probe'}], response, 'call_1', '{"ok":true}')
        self.assertEqual(followup[1]['tool_calls'][0]['id'], 'call_1')
        self.assertEqual(followup[-1]['role'], 'tool')
        self.assertEqual(followup[-1]['tool_call_id'], 'call_1')

    def test_chat_missing_id_is_rejected(self):
        response = {'choices': [{'message': {'tool_calls': [
            {'id': None, 'function': {'name': 'native_probe', 'arguments': ARGS}}]}}]}
        with self.assertRaises(ValueError):
            extract_chat_tool_calls(response)

    def test_responses_missing_call_id_is_rejected_even_when_item_id_exists(self):
        response = {'output': [{'type': 'function_call', 'id': 'item_1',
                                'name': 'native_probe', 'arguments': ARGS}]}
        with self.assertRaises(ValueError):
            extract_responses_tool_calls(response)

    def test_responses_invalid_json_arguments_are_rejected(self):
        response = {'output': [{'type': 'function_call', 'call_id': 'call_1',
                                'name': 'native_probe', 'arguments': '{'}]}
        with self.assertRaises(ValueError):
            extract_responses_tool_calls(response)

    def test_responses_arguments_must_be_a_json_object(self):
        response = {'output': [{'type': 'function_call', 'call_id': 'call_1',
                                'name': 'native_probe', 'arguments': '[]'}]}
        with self.assertRaises(ValueError):
            extract_responses_tool_calls(response)

    def test_responses_multiple_calls_are_rejected_for_single_followup(self):
        response = {'output': [
            {'type': 'function_call', 'call_id': 'call_1', 'name': 'native_probe', 'arguments': ARGS},
            {'type': 'function_call', 'call_id': 'call_2', 'name': 'native_probe', 'arguments': ARGS},
        ]}
        with self.assertRaises(ValueError):
            responses_followup_input([], response, 'call_1', '{}')

        response = {'output': [{'type': 'function_call', 'call_id': 'call_1',
                                'name': 'native_probe', 'arguments': ARGS}]}
        with self.assertRaises(ValueError):
            responses_followup_input([], response, 'wrong', '{}')

    def test_malformed_call_arguments_are_rejected(self):
        response = {'output': [{'type': 'function_call', 'call_id': 'call_1',
                                'name': 'native_probe', 'arguments': {}}]}
        with self.assertRaises(ValueError):
            extract_responses_tool_calls(response)


if __name__ == '__main__':
    unittest.main()
