"""Pure protocol helpers for compatibility probes and the later governed executor.

This module does not authorize, execute, or log tools. It only preserves the
provider transcript shape needed to compare Responses and Chat Completions.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping, Sequence


PROBE_TOOL = {
    'name': 'native_probe',
    'description': 'Return a fixed local probe result. Never access files or network.',
    'parameters': {
        'type': 'object',
        'properties': {'value': {'type': 'string', 'enum': ['fixed']}},
        'required': ['value'],
        'additionalProperties': False,
    },
}


def responses_tool_payload(model: str, input_items: Sequence[Mapping[str, Any]],
                           tools: Sequence[Mapping[str, Any]], *,
                           tool_choice: Any = 'auto', max_output_tokens: int = 256) -> dict:
    """Build an OpenAI/MiniMax-style Responses request without hidden state."""
    return {
        'model': model,
        'input': deepcopy(list(input_items)),
        'tools': [
            {'type': 'function', 'name': str(tool['name']),
             'description': str(tool.get('description') or ''),
             'parameters': deepcopy(tool.get('parameters') or {})}
            for tool in tools
        ],
        'tool_choice': tool_choice,
        'parallel_tool_calls': False,
        'max_output_tokens': int(max_output_tokens),
    }


def chat_tool_payload(model: str, messages: Sequence[Mapping[str, Any]],
                      tools: Sequence[Mapping[str, Any]], *,
                      tool_choice: Any = 'auto', max_tokens: int = 256) -> dict:
    """Build the Chat Completions tool shape for offline dialect comparison."""
    return {
        'model': model,
        'messages': deepcopy(list(messages)),
        'tools': [
            {'type': 'function', 'function': {
                'name': str(tool['name']),
                'description': str(tool.get('description') or ''),
                'parameters': deepcopy(tool.get('parameters') or {}),
            }} for tool in tools
        ],
        'tool_choice': tool_choice,
        'parallel_tool_calls': False,
        'max_tokens': int(max_tokens),
    }


def extract_responses_tool_calls(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize Responses ``function_call`` output items without executing them."""
    calls = []
    for item in response.get('output') or []:
        if not isinstance(item, Mapping) or item.get('type') != 'function_call':
            continue
        call_id = item.get('call_id')
        name = item.get('name')
        arguments = item.get('arguments')
        if not isinstance(call_id, str) or not call_id:
            raise ValueError('Responses function_call is missing call_id')
        if not isinstance(name, str) or not name:
            raise ValueError('Responses function_call is missing name')
        if not isinstance(arguments, str):
            raise ValueError('Responses function_call arguments must be a JSON string')
        try:
            parsed_arguments = json.loads(arguments)
        except (TypeError, ValueError):
            raise ValueError('Responses function_call arguments are not valid JSON') from None
        if not isinstance(parsed_arguments, dict):
            raise ValueError('Responses function_call arguments must be a JSON object')
        calls.append({'call_id': call_id, 'name': name, 'arguments': arguments,
                      'output_item': deepcopy(dict(item))})
    return calls


def extract_chat_tool_calls(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize Chat ``message.tool_calls`` items without executing them."""
    choices = response.get('choices') or []
    if not choices or not isinstance(choices[0], Mapping):
        return []
    message = choices[0].get('message') or {}
    calls = []
    for item in message.get('tool_calls') or []:
        if not isinstance(item, Mapping):
            continue
        function = item.get('function') or {}
        call_id = item.get('id')
        name = function.get('name')
        arguments = function.get('arguments')
        if not isinstance(call_id, str) or not call_id:
            raise ValueError('Chat tool_call is missing id')
        if not isinstance(name, str) or not name:
            raise ValueError('Chat tool_call is missing function.name')
        if not isinstance(arguments, str):
            raise ValueError('Chat tool_call arguments must be a JSON string')
        try:
            parsed_arguments = json.loads(arguments)
        except (TypeError, ValueError):
            raise ValueError('Chat tool_call arguments are not valid JSON') from None
        if not isinstance(parsed_arguments, dict):
            raise ValueError('Chat tool_call arguments must be a JSON object')
        calls.append({'call_id': call_id, 'name': name, 'arguments': arguments,
                      'assistant_message': deepcopy(dict(message))})
    return calls


def responses_followup_input(original_input: Sequence[Mapping[str, Any]],
                             response: Mapping[str, Any], call_id: str,
                             tool_output: str) -> list[dict[str, Any]]:
    """Preserve the full model output before appending its matching result."""
    output = response.get('output')
    if not isinstance(output, list):
        raise ValueError('Responses response has no output list to preserve')
    function_calls = [item for item in output
                      if isinstance(item, Mapping) and item.get('type') == 'function_call']
    if len(function_calls) != 1:
        raise ValueError('exactly one Responses function_call is required for this follow-up')
    if function_calls[0].get('call_id') != call_id:
        raise ValueError('tool result call_id does not match preserved output')
    return [*deepcopy(list(original_input)), *deepcopy(output),
            {'type': 'function_call_output', 'call_id': call_id,
             'output': str(tool_output)}]


def chat_followup_messages(original_messages: Sequence[Mapping[str, Any]],
                           response: Mapping[str, Any], call_id: str,
                           tool_output: str) -> list[dict[str, Any]]:
    """Preserve the assistant tool_calls message before appending tool output."""
    choices = response.get('choices') or []
    if not choices or not isinstance(choices[0], Mapping):
        raise ValueError('Chat response has no choice to preserve')
    assistant = choices[0].get('message')
    if not isinstance(assistant, Mapping):
        raise ValueError('Chat response has no assistant message to preserve')
    tool_calls = [item for item in assistant.get('tool_calls') or []
                  if isinstance(item, Mapping)]
    if len(tool_calls) != 1:
        raise ValueError('exactly one Chat tool_call is required for this follow-up')
    if not isinstance(call_id, str) or not call_id:
        raise ValueError('Chat follow-up call_id must be a non-empty string')
    ids = {item.get('id') for item in tool_calls}
    if call_id not in ids:
        raise ValueError('tool result call_id does not match assistant tool_calls')
    return [*deepcopy(list(original_messages)), deepcopy(dict(assistant)),
            {'role': 'tool', 'tool_call_id': call_id, 'content': str(tool_output)}]
