"""Ollama or OpenAI-compatible JSON calls, with one bounded repair attempt."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from copy import deepcopy
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request
from network import urlopen

from common import Budget, Config, GuideError

# Measured on the current flagship profile: synthesis latencies straddled the
# generic per-request deadline (86.8 s returned, 95.2 s and 95.9 s were cut off),
# and a cut-off synthesis wastes the whole stage. Synthesis is one bounded
# request that cannot be split, so it gets a larger deadline; the remaining task
# budget and task_timeout still bound it.
STAGE_DEADLINE_FACTOR = {'synthesis': 2.0}

# OpenAI-style structured-output strict mode is a stricter dialect than our own
# schemas: it rejects partial `required` lists and these keywords outright. The
# prompt still states every constraint, so a non-strict `json_schema` keeps the
# contract while remaining acceptable to strict gateways.
STRICT_UNSUPPORTED_KEYS = frozenset({
    'minLength', 'maxLength', 'pattern', 'format',
    'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf',
    'minItems', 'maxItems', 'uniqueItems', 'contains', 'minContains', 'maxContains',
    'unevaluatedItems', 'minProperties', 'maxProperties', 'patternProperties',
    'unevaluatedProperties', 'propertyNames',
})

# A bounded, mechanical reaction to a provider that names a field it will not
# accept. Anything else is reported as-is: no silent guessing about capabilities.


def strict_schema_compatible(schema) -> bool:
    """True only when OpenAI strict mode would actually accept this schema."""
    if not isinstance(schema, dict):
        return False
    if any(key in schema for key in STRICT_UNSUPPORTED_KEYS):
        return False
    if 'properties' in schema:
        if schema.get('type') != 'object':
            return False
        properties = schema.get('properties')
        required = schema.get('required')
        if not isinstance(properties, dict) or not isinstance(required, list):
            return False
        if set(required) != set(properties):
            return False
        if schema.get('additionalProperties') is not False:
            return False
        if not all(strict_schema_compatible(child) for child in properties.values()):
            return False
    elif schema.get('type') == 'object':
        return False  # strict objects must declare their properties explicitly
    items = schema.get('items')
    if items is not None and not strict_schema_compatible(items):
        return False
    for key in ('anyOf', 'oneOf', 'allOf'):
        branch = schema.get(key)
        if branch is not None:
            if not isinstance(branch, list):
                return False
            if not all(strict_schema_compatible(child) for child in branch):
                return False
    return True


def strict_structured_output(config, schema) -> bool:
    """Strict mode is requested only where it provably applies."""
    if strict_schema_compatible(schema):
        return True
    from model_policy import tolerates_partial_strict
    return tolerates_partial_strict(config)


def _error_detail(exc: HTTPError, api_key: str) -> tuple[str, str]:
    """Return (readable detail, raw message) from a provider error body.

    The gateway usually explains the exact rejected field; discarding it turns a
    five-second fix into a credential hunt. Keys are redacted before display.
    """
    try:
        raw = exc.read(4000)
    except (OSError, ValueError, AttributeError):
        raw = b''
    try:
        exc.close()
    except OSError:
        pass
    if not raw:
        return '', ''
    text = raw.decode('utf-8', 'replace')
    message = ''
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get('error')
        if isinstance(error, dict):
            message = str(error.get('message') or error.get('code') or '')
            param = error.get('param')
            if param:
                message += f" (param: {param})"
        elif isinstance(error, str):
            message = error
        if not message:
            message = str(data.get('message') or '')
    else:
        message = text
    message = ' '.join(message.split())
    if api_key:
        message = message.replace(api_key, '***')
    return (' — ' + message[:300] if message else ''), message


def compat_downgrade(payload: dict, message: str) -> dict | None:
    """Drop one parameter the provider explicitly refused, or return None.

    Only called after a 400/422 that quotes the offending field, so the second
    request removes something the server named rather than something guessed.
    """
    lowered = (message or '').casefold()
    if not lowered:
        return None
    reduced = dict(payload)
    changed = False

    def drop(*names):
        nonlocal changed
        for name in names:
            if name in reduced:
                reduced.pop(name, None)
                changed = True

    schema_words = ('json_schema', 'text.format', 'response_format', 'strict',
                    'structured output', 'schema')
    if any(word in lowered for word in schema_words):
        drop('text', 'response_format', 'format')
    if 'reasoning' in lowered:
        drop('reasoning', 'reasoning_effort')
    if 'temperature' in lowered:
        drop('temperature')
    return reduced if changed else None


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith('```') and text.endswith('```'):
        text = '\n'.join(text.splitlines()[1:-1]).strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise GuideError('Model output was not valid JSON.') from None
    if not isinstance(value, dict):
        raise GuideError('Model output must be a JSON object.')
    return value


class NativeUnsupported(GuideError):
    """The initial provider request explicitly rejected native tool support."""


@dataclass(frozen=True, repr=False)
class NativeProposal:
    input_items: list
    response: dict
    call_id: str
    name: str
    arguments: dict
    _snapshot: str = field(repr=False, compare=False)


def _native_arguments(raw, schema):
    from tool_governance import MAX_ARGUMENT_BYTES, validate_arguments
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError
    try:
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_ARGUMENT_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
        return validate_arguments(value, schema)
    except (ValueError, TypeError, RecursionError, UnicodeError, GuideError):
        raise GuideError('Model API returned invalid native tool arguments.') from None


class _Rejected(Exception):
    """A provider answered with an HTTP error status and an explanatory body."""

    def __init__(self, code: int, detail: str, message: str):
        super().__init__(message or detail)
        self.code, self.detail, self.message = code, detail, message


class JsonLLM:
    def __init__(self, config: Config, budget: Budget):
        self.config, self.budget = config, budget
        self.mode = 'live'
        self.events: list[dict] = []
        self.last_http_status: int | None = None
        self.last_model = config.model
        self._stage = ''
        self.last_usage: dict[str, int | None] = {'input_tokens': None, 'output_tokens': None}
        self.last_native_request_sent = False

    def _payload(self, protocol: str, messages: list[dict], schema: dict) -> dict:
        c = self.config
        if c.provider == 'ollama':
            return {'model': c.model, 'messages': messages, 'stream': False,
                    'format': schema,
                    'options': {'temperature': 0, 'num_ctx': 8192, 'num_predict': 3000},
                    'keep_alive': '10m'}
        if protocol == 'responses':
            payload = {'model': c.model, 'input': messages, 'temperature': 0,
                       'max_output_tokens': getattr(c, 'max_output_tokens', 3000)}
            if strict_structured_output(c, schema):
                payload['text'] = {'format': {'type': 'json_schema', 'name': 'scholarguide_output',
                                              'schema': schema, 'strict': True}}
            else:
                # Keep the schema contract, but in the non-strict dialect every
                # OpenAI-compatible gateway accepts.
                payload['text'] = {'format': {'type': 'json_schema', 'name': 'scholarguide_output',
                                              'schema': schema}}
            return payload
        payload = {'model': c.model, 'messages': messages, 'stream': False,
                   'temperature': 0,
                   'max_tokens': getattr(c, 'max_output_tokens', 3000),
                   'response_format': {'type': 'json_object'}}
        if urlsplit(c.base_url).hostname == 'api.deepseek.com':
            payload['thinking'] = {'type': 'disabled'}
        return payload

    @staticmethod
    def _url(protocol: str, base: str) -> str:
        if protocol == 'responses':
            return base if base.endswith('/responses') else base + '/responses'
        return base if base.endswith('/chat/completions') else base + '/chat/completions'

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        from execution_events import operation
        with operation('model.' + (self._stage or 'generate'), category='model',
                       model=self.config.model, protocol=self.config.protocol,
                       attempt=self.budget.llm_calls + 1) as measured:
            value = self._post_raw(url, payload, headers)
            measured['http_status'] = self.last_http_status
            usage = value.get('usage') if isinstance(value, dict) else None
            if isinstance(usage, dict):
                measured['usage'] = {key: usage.get(key) for key in (
                    'input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens') if key in usage}
            return value

    def _post_raw(self, url: str, payload: dict, headers: dict) -> dict:
        c = self.config
        req = Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        self.budget.llm_calls += 1
        self.last_http_status = None
        self.last_model = c.model
        try:
            deadline = c.request_timeout * STAGE_DEADLINE_FACTOR.get(self._stage, 1.0)
            with urlopen(req, timeout=min(deadline, self.budget.remaining())) as response:
                self.last_http_status = getattr(response, 'status', 200)
                body = response.read(2_000_001)
                self.last_model = response.headers.get('x-model') or c.model
            if len(body) > 2_000_000:
                raise GuideError('Model response exceeded 2 MB.')
            return json.loads(body)
        except HTTPError as exc:
            self.last_http_status = exc.code
            detail, message = _error_detail(exc, c.api_key)
            rejected = _Rejected(exc.code, detail, message)
            raise rejected from None
        except TimeoutError:
            raise GuideError('Model API request timed out.') from None
        except (URLError, OSError) as exc:
            reason = getattr(exc, 'reason', exc)
            text = str(reason).lower()
            if isinstance(reason, TimeoutError) or 'timed out' in text:
                raise GuideError('Model API request timed out.') from None
            if isinstance(reason, ConnectionRefusedError) or 'refused' in text or 'errno 111' in text:
                raise GuideError('Model API connection refused.') from None
            raise GuideError('Model API unavailable.') from None
        except ValueError:
            raise GuideError('Model API returned invalid JSON.') from None

    def _call(self, messages: list[dict], schema: dict) -> str:
        self.budget.check_cancelled()
        self.last_usage = {'input_tokens': None, 'output_tokens': None}
        self.last_http_status = None
        guard = getattr(self.budget, 'request_guard', None)
        if guard is not None:
            guard()
        c = self.config
        protocol = getattr(c, 'protocol', 'chat_completions')
        base = c.base_url.rstrip('/')
        if c.provider == 'ollama':
            url = base if base.endswith('/api/chat') else base + '/api/chat'
        else:
            url = self._url(protocol, base)
        payload = self._payload(protocol, messages, schema)
        headers = {'Content-Type': 'application/json'}
        from model_policy import inference_policy
        policy = inference_policy(c, self._stage)
        if policy['reasoning_effort']:
            if protocol == 'responses':
                payload['reasoning'] = {'effort': policy['reasoning_effort']}
            else:
                payload['reasoning_effort'] = policy['reasoning_effort']
        if c.api_key:
            headers['Authorization'] = 'Bearer ' + c.api_key
        for attempt in range(2):
            try:
                data = self._post(url, payload, headers)
                break
            except _Rejected as exc:
                # One bounded retry, only for a field the provider itself named.
                reduced = (compat_downgrade(payload, exc.message)
                           if attempt == 0 and exc.code in (400, 422) else None)
                if reduced is None:
                    raise GuideError(
                        f'Model API HTTP {exc.code}{exc.detail}; '
                        'check model, endpoint and credentials.') from None
                payload = reduced
                if guard is not None:
                    guard()
                continue
        else:
            raise GuideError('Model API unavailable.')
        try:
            if not isinstance(data, dict):
                raise TypeError
            # A paid response can fail content validation or hit its output cap.
            # Account for reported usage before rejecting the response.
            if c.provider == 'ollama':
                tokens = (data.get('prompt_eval_count'), data.get('eval_count'))
            else:
                usage = data.get('usage') or {}
                usage = usage if isinstance(usage, dict) else {}
                keys = ('input_tokens', 'output_tokens') if protocol == 'responses' else ('prompt_tokens', 'completion_tokens')
                tokens = tuple(usage.get(key) for key in keys)
            valid_tokens = tuple(n if type(n) is int and n >= 0 else None for n in tokens)
            self.last_usage = dict(zip(('input_tokens', 'output_tokens'), valid_tokens))
            if all(n is not None for n in valid_tokens):
                self.budget.usage_observed = True
                self.budget.usage_calls += 1
                self.budget.input_tokens += valid_tokens[0]
                self.budget.output_tokens += valid_tokens[1]
            self.budget.remaining()
            if c.provider == 'ollama':
                content = data['message']['content']
            elif protocol == 'responses':
                if data.get('status') in ('failed', 'cancelled', 'incomplete'):
                    raise GuideError('Model API returned an incomplete response.')
                chunks = []
                for item in data.get('output') or []:
                    if not isinstance(item, dict):
                        continue
                    for part in item.get('content') or []:
                        if isinstance(part, dict) and part.get('type') in ('output_text', 'text'):
                            value = part.get('text') or part.get('content')
                            if isinstance(value, str):
                                chunks.append(value)
                if not chunks and isinstance(data.get('output_text'), str):
                    chunks.append(data['output_text'])
                content = ''.join(chunks)
                if isinstance(data.get('model'), str):
                    self.last_model = data['model']
            else:
                choice = data['choices'][0]
                if choice.get('finish_reason') == 'length':
                    raise GuideError('Model response hit its output token limit.')
                content = choice['message']['content']
                if isinstance(data.get('model'), str):
                    self.last_model = data['model']
            if not isinstance(content, str) or not content.strip():
                raise GuideError('Model API returned an empty response.')
            return content
        except (KeyError, IndexError, TypeError):
            raise GuideError('Model API returned an unexpected response format.') from None

    def _native_request(self, payload: dict, *, initial: bool) -> dict:
        self.last_native_request_sent = False
        self.budget.check_cancelled()
        self.budget.remaining()
        self.last_http_status = None
        guard = getattr(self.budget, 'request_guard', None)
        if guard is not None:
            guard()
        self.budget.check_cancelled()
        self.budget.remaining()
        headers = {'Content-Type': 'application/json'}
        if self.config.api_key:
            headers['Authorization'] = 'Bearer ' + self.config.api_key
        self.last_native_request_sent = True
        try:
            value = self._post(self._url('responses', self.config.base_url.rstrip('/')), payload, headers)
        except _Rejected as exc:
            # Only a direct capability declaration on the first request is safe
            # for the host to consider as an optional fallback.
            message = exc.message.casefold()
            if (initial and exc.code in (400, 422)
                    and re.search(r'\b(?:tools?|function calling|function calls?)\s+(?:is |are )?(?:not supported|unsupported)\b|\b(?:does not support|do not support)\s+(?:native\s+)?(?:tools?|function calling|function calls?)\b', message)):
                raise NativeUnsupported('Provider does not support native tools.') from None
            raise GuideError(f'Model API HTTP {exc.code}; native request rejected.') from None
        if not isinstance(value, dict):
            raise GuideError('Model API returned an invalid native response.')
        usage = value.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        tokens = tuple(usage.get(key) for key in ('input_tokens', 'output_tokens'))
        tokens = tuple(n if type(n) is int and n >= 0 else None for n in tokens)
        self.last_usage = dict(zip(('input_tokens', 'output_tokens'), tokens))
        if all(n is not None for n in tokens):
            self.budget.usage_observed = True
            self.budget.usage_calls += 1
            self.budget.input_tokens += tokens[0]
            self.budget.output_tokens += tokens[1]
        if isinstance(value.get('model'), str):
            self.last_model = value['model']
        self.budget.remaining()
        self.budget.check_cancelled()
        if value.get('status') != 'completed' or not isinstance(value.get('output'), list):
            raise GuideError('Model API returned an invalid native response.')
        return value

    def propose_action(self, system: str, data: dict, tools: list[dict]) -> NativeProposal:
        from native_tool_protocol import responses_tool_payload
        from tool_governance import MAX_SCHEMA_BYTES, _schema_safety
        from jsonschema import Draft202012Validator
        if self.config.provider != 'openai' or self.config.protocol != 'responses':
            raise GuideError('Native actions require OpenAI Responses protocol.')
        try:
            if not isinstance(system, str) or not isinstance(data, dict) or not isinstance(tools, list) or not tools:
                raise ValueError
            definitions = deepcopy(tools)
            schemas = {}
            for tool in definitions:
                if not isinstance(tool, dict):
                    raise ValueError
                name, schema = tool.get('name'), tool.get('parameters')
                if (not isinstance(name, str) or not name.strip() or name in schemas
                        or not isinstance(schema, dict) or not schema
                        or schema.get('type') != 'object'
                        or not isinstance(schema.get('properties'), dict)
                        or schema.get('additionalProperties') is not False):
                    raise ValueError
                if len(json.dumps(schema, allow_nan=False).encode('utf-8')) > MAX_SCHEMA_BYTES:
                    raise ValueError
                _schema_safety(schema)
                Draft202012Validator.check_schema(schema)
                schemas[name] = schema
            input_items = [{'role': 'system', 'content': system},
                           {'role': 'user', 'content': json.dumps(data, ensure_ascii=False, allow_nan=False)}]
            payload = responses_tool_payload(self.config.model, input_items, definitions,
                                             tool_choice='required', max_output_tokens=self.config.max_output_tokens)
        except Exception:
            raise GuideError('Invalid native tool request.') from None
        response = self._native_request(payload, initial=True)
        output = response['output']
        if any(not isinstance(item, dict) or item.get('type') not in ('reasoning', 'message', 'function_call')
               or item.get('status', 'completed') != 'completed' for item in output):
            raise GuideError('Model API returned an invalid native response.')
        calls = [item for item in output if item['type'] == 'function_call']
        if len(calls) != 1:
            raise GuideError('Model API returned an invalid native response.')
        call = calls[0]
        call_id, name = call.get('call_id'), call.get('name')
        if (not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 1024
                or call.get('status', 'completed') != 'completed'
                or not isinstance(name, str) or name not in schemas):
            raise GuideError('Model API returned an invalid native response.')
        arguments = _native_arguments(call.get('arguments'), schemas[name])
        try:
            snapshot = json.dumps({'input': input_items, 'response': response, 'call_id': call_id,
                                   'tools': definitions}, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise GuideError('Model API returned an invalid native response.') from None
        return NativeProposal(deepcopy(input_items), deepcopy(response), call_id, name, arguments, snapshot)

    def continue_action(self, proposal: NativeProposal, tool_output: str) -> dict:
        from native_tool_protocol import responses_followup_input, responses_tool_payload
        if self.config.provider != 'openai' or self.config.protocol != 'responses':
            raise GuideError('Native actions require OpenAI Responses protocol.')
        try:
            if not isinstance(proposal, NativeProposal) or not isinstance(tool_output, str):
                raise ValueError
            if len(tool_output.encode('utf-8')) > 256000:
                raise ValueError
            snapshot = json.loads(proposal._snapshot)
            input_items = responses_followup_input(snapshot['input'], snapshot['response'],
                                                   snapshot['call_id'], tool_output)
            payload = responses_tool_payload(self.config.model, input_items, snapshot['tools'],
                                             tool_choice='none', max_output_tokens=self.config.max_output_tokens)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise GuideError('Invalid native tool result.') from None
        response = self._native_request(payload, initial=False)
        chunks = []
        for item in response['output']:
            if (not isinstance(item, dict) or item.get('type') not in ('reasoning', 'message')
                    or item.get('status', 'completed') != 'completed'):
                raise GuideError('Model API returned an invalid native response.')
            if item['type'] == 'message':
                if item.get('status', 'completed') != 'completed' or not isinstance(item.get('content'), list):
                    raise GuideError('Model API returned an invalid native response.')
                for part in item['content']:
                    if not isinstance(part, dict) or part.get('type') != 'output_text' or not isinstance(part.get('text'), str):
                        raise GuideError('Model API returned an invalid native response.')
                    chunks.append(part['text'])
        if not chunks or not ''.join(chunks).strip():
            raise GuideError('Model API returned an invalid native response.')
        return {'status': 'completed', 'content': ''.join(chunks)}

    def generate(self, stage: str, system: str, data: dict, schema: dict, validate) -> dict:
        self._stage = stage
        user_content = json.dumps(data, ensure_ascii=False)
        messages = [{'role': 'system', 'content': system + '\nRequired JSON schema:\n' + json.dumps(schema)},
                    {'role': 'user', 'content': user_content}]
        # The model-facing payload size is the quantity the efficiency work
        # actually reduces, so it is measured rather than estimated.
        payload_bytes = len(user_content.encode('utf-8'))
        for attempt in range(2):
            self.budget.check_cancelled()
            started = time.monotonic()
            text = self._call(messages, schema)
            elapsed = round(time.monotonic() - started, 3)
            try:
                parsed = parse_json(text)
                validate(parsed)
                from model_policy import inference_policy
                self.events.append({'stage': stage, 'attempt': attempt + 1, 'status': 'ok',
                                    'payload_bytes': payload_bytes,
                                    'elapsed_seconds': elapsed,
                                    'usage': dict(self.last_usage),
                                    'inference_policy':inference_policy(self.config,stage)})
                return parsed
            except GuideError as exc:
                self.events.append({'stage': stage, 'attempt': attempt + 1,
                                    'status': 'invalid_output', 'error': str(exc),
                                    'payload_bytes': payload_bytes,
                                    'elapsed_seconds': elapsed,
                                    'usage': dict(self.last_usage)})
                if attempt == 1:
                    raise GuideError(f'{stage}: structured output invalid after one repair ({exc}).') from None
                messages.extend([
                    {'role': 'assistant', 'content': text[:15000]},
                    {'role': 'user', 'content': 'Correct the JSON using the original input. Error: ' + str(exc)},
                ])
        raise GuideError('No model response.')

    def stage_metrics(self) -> dict:
        """Per-stage call counts, model-facing bytes, elapsed time and usage.

        Diagnostics only: they make an actual regression visible instead of
        inferred. Byte counts are what the model received, not a token guess.
        """
        out: dict[str, dict] = {}
        for event in self.events:
            stage = str(event.get('stage') or 'unknown')
            row = out.setdefault(stage, {'calls': 0, 'payload_bytes_total': 0,
                                         'payload_bytes_max': 0, 'elapsed_seconds': 0.0,
                                         'input_tokens': 0, 'output_tokens': 0})
            row['calls'] += 1
            size = int(event.get('payload_bytes') or 0)
            row['payload_bytes_total'] += size
            row['payload_bytes_max'] = max(row['payload_bytes_max'], size)
            row['elapsed_seconds'] = round(row['elapsed_seconds']
                                           + float(event.get('elapsed_seconds') or 0.0), 3)
            usage = event.get('usage') or {}
            row['input_tokens'] += int(usage.get('input_tokens') or 0)
            row['output_tokens'] += int(usage.get('output_tokens') or 0)
        return out
