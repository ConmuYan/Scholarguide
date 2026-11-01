"""One bounded, real MiniMax Responses native-tool compatibility probe.

The probe never executes a remote or filesystem tool. If the model asks for the
fixed `native_probe`, the local harness validates its fixed arguments, returns a
constant string, and sends one follow-up preserving the complete output items.
The report contains metadata, usage and snapshot-price estimates only; no key,
raw response, prompt, or model reasoning is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, GuideError
from native_tool_protocol import (
    PROBE_TOOL,
    extract_responses_tool_calls,
    responses_followup_input,
    responses_tool_payload,
)
from providers import ProviderStore

MAX_REQUESTS = 2
SCRIPT_VERSION = 'minimax-responses-native-tool-v5'


def _usage(response: dict[str, Any]) -> dict[str, int | None]:
    usage = response.get('usage')
    if not isinstance(usage, dict):
        return {'input_tokens': None, 'output_tokens': None,
                'cached_tokens': None, 'reasoning_tokens': None}
    input_value = usage.get('input_tokens', usage.get('prompt_tokens'))
    output_value = usage.get('output_tokens', usage.get('completion_tokens'))
    details = usage.get('input_tokens_details')
    cached_value = usage.get('cached_tokens')
    if isinstance(details, dict):
        cached_value = details.get('cached_tokens', cached_value)
    output_details = usage.get('output_tokens_details')
    reasoning_value = usage.get('reasoning_tokens')
    if isinstance(output_details, dict):
        reasoning_value = output_details.get('reasoning_tokens', reasoning_value)
    values = {}
    for key, value in (('input_tokens', input_value), ('output_tokens', output_value),
                       ('cached_tokens', cached_value), ('reasoning_tokens', reasoning_value)):
        values[key] = value if type(value) is int and value >= 0 else None
    return values


def _estimate(usage: dict[str, int | None], input_price: float | None,
              output_price: float | None) -> float | None:
    if (usage['input_tokens'] is None or usage['output_tokens'] is None
            or input_price is None or output_price is None):
        return None
    return round((usage['input_tokens'] * input_price
                  + usage['output_tokens'] * output_price) / 1_000_000, 8)


def _response_meta(response: dict[str, Any]) -> dict[str, Any]:
    output = response.get('output')
    output_items = output if isinstance(output, list) else []
    return {
        'response_id_present': isinstance(response.get('id'), str) and bool(response['id']),
        'status': response.get('status') if isinstance(response.get('status'), str) else None,
        'output_types': [item.get('type') for item in output_items
                         if isinstance(item, dict) and isinstance(item.get('type'), str)],
        'tool_calls': [
            {'call_id_present': isinstance(item.get('call_id'), str) and bool(item.get('call_id')),
             'name_matches_probe': item.get('name') == 'native_probe',
             'argument_bytes': len(item.get('arguments', '').encode('utf-8'))
             if isinstance(item.get('arguments'), str) else None}
            for item in output_items
            if isinstance(item, dict) and item.get('type') == 'function_call'
        ],
        'usage': _usage(response),
    }


def _post(config, payload: dict[str, Any]) -> tuple[dict[str, Any], int, float]:
    url = config.base_url.rstrip('/')
    if not url.endswith('/responses'):
        url += '/responses'
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                      headers={'Content-Type': 'application/json',
                               'Authorization': 'Bearer ' + config.api_key},
                      method='POST')
    started = time.monotonic()
    with __import__('network').urlopen(request, timeout=min(float(config.request_timeout), 45.0)) as response:
        body = response.read(2_000_001)
        status = int(getattr(response, 'status', 200))
    if len(body) > 2_000_000:
        raise GuideError('probe response exceeded 2 MB')
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        raise GuideError('probe response was not JSON') from None
    if not isinstance(value, dict):
        raise GuideError('probe response was not an object')
    return value, status, round(time.monotonic() - started, 3)


def _safe_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, HTTPError):
        try:
            exc.close()
        except OSError:
            pass
        return {'kind': 'http_error', 'http_status': int(exc.code)}
    if isinstance(exc, URLError):
        return {'kind': 'transport_error', 'type': type(getattr(exc, 'reason', exc)).__name__}
    return {'kind': type(exc).__name__}


def _script_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _protocol_sha256() -> str:
    return hashlib.sha256((ROOT / 'native_tool_protocol.py').read_bytes()).hexdigest()


def _http_success(status: int) -> bool:
    return 200 <= int(status) < 300


def run(output: Path) -> int:
    profile = ProviderStore().active()
    config = profile.config()
    report: dict[str, Any] = {
        'probe': SCRIPT_VERSION,
        'script_sha256': _script_sha256(),
        'protocol_sha256': _protocol_sha256(),
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'profile_id': profile.id,
        'profile_name': profile.name,
        'protocol': profile.protocol,
        'model': profile.model,
        'base_host': urlsplit(profile.base_url).hostname,
        'tool_definition_intended': False,
        'first_request_completed': False,
        'tool_call_observed': False,
        'tool_arguments_valid': False,
        'first': None,
        'followup': None,
        'followup_input_item_count': None,
        'followup_input_types': None,
        'calls_made': 0,
        'usage_total': {'input_tokens': None, 'output_tokens': None,
                        'cached_tokens': None, 'reasoning_tokens': None,
                        'complete': False, 'partial': False,
                        'details_complete': False},
        'pricing_snapshot': {'input_price': profile.input_price,
                             'output_price': profile.output_price,
                             'currency': profile.currency,
                             'unit': 'per_1m_tokens',
                             'source': 'active local ProviderStore profile at probe start'},
        'estimated_model_cost_usd_at_snapshot_price': None,
        'status': 'not_started',
        'error': None,
        'limits': {'max_requests': MAX_REQUESTS, 'max_output_tokens': 256},
        'note': 'Fixed local native_probe only; no external tool execution.',
    }
    request_count = 0
    budget = Budget(min(float(config.request_timeout), 90.0))

    def limited_post(payload):
        nonlocal request_count
        if request_count >= MAX_REQUESTS:
            raise GuideError('native probe request limit exceeded')
        budget.remaining()
        request_count += 1
        return _post(config, payload)

    if profile.protocol != 'responses':
        report.update(status='unsupported_profile_protocol', error={'protocol': profile.protocol})
    elif not profile.api_key:
        report.update(status='missing_api_key', error={'kind': 'missing_api_key'})
    else:
        first_input = [
            {'role': 'system', 'content': 'Use the declared native_probe tool exactly once. Do not answer with prose.'},
            {'role': 'user', 'content': 'Call native_probe now.'},
        ]
        payload = responses_tool_payload(config.model, first_input, [PROBE_TOOL],
                                         tool_choice='required', max_output_tokens=256)
        payload['temperature'] = 0
        payload['reasoning'] = {'effort': 'low'}
        report['tool_definition_intended'] = True
        try:
            first, http_status, elapsed = limited_post(payload)
            report['calls_made'] = request_count
            report['first'] = {'http_status': http_status, 'elapsed_seconds': elapsed,
                               **_response_meta(first)}
            if not _http_success(http_status):
                report['status'] = 'http_error'
                report['error'] = {'kind': 'http_error', 'which': 'first',
                                   'http_status': http_status}
            elif first.get('status') != 'completed':
                report['status'] = 'incomplete_response'
                report['error'] = {'kind': 'incomplete_response',
                                   'which': 'first', 'status': first.get('status')}
            else:
                report['first_request_completed'] = True
                calls = extract_responses_tool_calls(first)
                report['tool_call_observed'] = bool(calls)
                if len(calls) > 1:
                    report['status'] = 'multi_call_rejected'
                    report['error'] = {'kind': 'multi_call_rejected', 'count': len(calls)}
                elif not calls:
                    report['status'] = 'completed_without_tool_call'
                else:
                    call = calls[0]
                    parsed = json.loads(call['arguments'])
                    report['tool_arguments_valid'] = parsed == {'value': 'fixed'}
                    if not report['tool_arguments_valid']:
                        report['status'] = 'tool_arguments_rejected'
                        report['error'] = {'kind': 'tool_arguments_rejected'}
                    else:
                        followup_input = responses_followup_input(
                            first_input, first, call['call_id'],
                            '{"ok":true,"source":"local_probe"}')
                        follow_payload = responses_tool_payload(
                            config.model, followup_input, [PROBE_TOOL],
                            tool_choice='auto', max_output_tokens=256)
                        follow_payload['temperature'] = 0
                        follow_payload['reasoning'] = {'effort': 'low'}
                        report['followup_input_item_count'] = len(followup_input)
                        report['followup_input_types'] = [
                            item.get('type') if isinstance(item, dict) and item.get('type')
                            else item.get('role') if isinstance(item, dict) else None
                            for item in followup_input]
                        second, second_status, second_elapsed = limited_post(follow_payload)
                        report['calls_made'] = request_count
                        report['followup'] = {
                            'http_status': second_status, 'elapsed_seconds': second_elapsed,
                            **_response_meta(second),
                            'preserved_output_items': len(first.get('output') or []),
                        }
                        if not _http_success(second_status):
                            report['status'] = 'http_error'
                            report['error'] = {'kind': 'http_error', 'which': 'followup',
                                               'http_status': second_status}
                        elif second.get('status') != 'completed':
                            report['status'] = 'incomplete_response'
                            report['error'] = {'kind': 'incomplete_response',
                                               'which': 'followup', 'status': second.get('status')}
                        else:
                            report['status'] = 'tool_call_and_followup_observed'
        except ValueError:
            report['calls_made'] = request_count
            report['status'] = 'protocol_rejected'
            report['error'] = {'kind': 'protocol_rejected'}
        except BaseException as exc:
            report['calls_made'] = request_count
            report['status'] = 'request_failed_or_unknown'
            report['error'] = _safe_error(exc)

    actual_responses = [report.get('first'), report.get('followup')]
    actual_responses = [response for response in actual_responses if response is not None]
    required_usage = ('input_tokens', 'output_tokens')
    required_values = [
        [(response.get('usage') or {}).get(key) for response in actual_responses]
        for key in required_usage
    ]
    required_complete = (len(actual_responses) == 2 and all(
        values and all(type(value) is int and value >= 0 for value in values)
        for values in required_values))
    report['usage_total']['complete'] = required_complete
    report['usage_total']['partial'] = any(
        any(value is not None for value in values) and
        not all(type(value) is int and value >= 0 for value in values)
        for values in required_values)
    for key, values in zip(required_usage, required_values):
        report['usage_total'][key] = sum(values) if required_complete else None
    optional_usage = ('cached_tokens', 'reasoning_tokens')
    report['usage_total']['details_complete'] = bool(actual_responses)
    for key in optional_usage:
        values = [(response.get('usage') or {}).get(key) for response in actual_responses]
        complete_values = bool(values) and all(type(value) is int and value >= 0
                                               for value in values)
        if not complete_values:
            report['usage_total']['details_complete'] = False
        report['usage_total'][key] = sum(values) if complete_values else None
    if report['usage_total']['complete'] and report['status'] == 'tool_call_and_followup_observed':
        report['estimated_model_cost_usd_at_snapshot_price'] = _estimate(
            {'input_tokens': report['usage_total']['input_tokens'],
             'output_tokens': report['usage_total']['output_tokens']},
            profile.input_price, profile.output_price)
    report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'tool_call_and_followup_observed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    raise SystemExit(run(parser.parse_args().output))
