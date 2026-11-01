"""Bounded native session probe. Run only after review and explicit authorization."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import BudgetExceeded, Config, Paper, RunCancelled
from llm import JsonLLM, _native_arguments
from network import urlopen
from providers import ProviderStore
from session import ChatSession
from state import StateStore

FILES = ('session.py', 'llm.py', 'providers.py', 'controller.py', 'native_tool_protocol.py', 'network.py', 'scripts/probe_native_session.py')
MAX_REQUESTS = 2


def manifest():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}


def load_config():
    profile = ProviderStore().active()
    # Avoid Config.from_env and its project environment loading entirely.
    return Config(provider='openai', base_url=profile.base_url, model=profile.model,
                  api_key=profile.api_key, protocol=profile.protocol,
                  input_price=profile.input_price, output_price=profile.output_price,
                  currency=profile.currency)


def validated(config):
    url = urlsplit(config.base_url)
    if (url.scheme != 'https' or url.netloc not in ('api.minimaxi.com', 'api.minimaxi.com:443')
            or url.username or url.password or url.query or url.fragment
            or url.path not in ('', '/', '/v1', '/v1/', '/v1/responses')):
        raise ValueError('invalid endpoint')
    if (config.protocol != 'responses' or not isinstance(config.api_key, str) or not config.api_key
            or not isinstance(config.model, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', config.model)):
        raise ValueError('invalid configuration')
    return replace(config, provider='openai', action_protocol='responses_native',
                   native_fallback='disabled', agent_runtime='legacy', research_mode=False,
                   request_timeout=45.0, task_timeout=90.0, max_output_tokens=512,
                   profile_id='', profile_name='')


class MockSource:
    mode = 'demo'

    def __init__(self):
        self.calls = 0

    def search(self, query, limit=10):
        self.calls += 1
        return [Paper(id='synthetic-1', title='Synthetic paper on retrieval evaluation',
                      abstract='Synthetic abstract for an artificial retrieval evaluation paper.',
                      url='', year=2024, source='synthetic')][:limit]


def contains_secret(value, secret, depth=0):
    """Inspect decoded values, including bounded JSON embedded in tool arguments."""
    if depth > 16:
        raise ValueError('context nesting limit')
    if isinstance(value, str):
        if secret in value:
            return True
        # Also inspect common JSON-style escapes embedded in surrounding text.
        # This is a bounded defense, not general decoding or exfiltration proof.
        decoded = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), value)
        if secret in decoded:
            return True
        stripped = value.strip()
        if stripped.startswith(('{', '[', '"')):
            try:
                nested = json.loads(stripped)
            except RecursionError:
                raise ValueError('context nesting limit') from None
            except ValueError:
                return False
            return contains_secret(nested, secret, depth + 1)
        return False
    if isinstance(value, dict):
        return any(contains_secret(key, secret, depth + 1) or contains_secret(item, secret, depth + 1)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(contains_secret(item, secret, depth + 1) for item in value)
    return False


class ProbeLLM(JsonLLM):
    def __init__(self, config, budget, transport, report, maximum):
        super().__init__(config, budget)
        self.transport, self.report, self.maximum = transport, report, maximum
        self.call_id = None
        self.usages = []

    def _post(self, url, payload, headers):
        if self.report['requests'] >= self.maximum:
            raise BudgetExceeded('request limit')
        expected = self._url('responses', self.config.base_url.rstrip('/'))
        if url != expected:
            raise ValueError('unexpected endpoint')
        if contains_secret(payload, self.config.api_key):
            raise ValueError('secret in context')
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('utf-8')
        choice = payload.get('tool_choice')
        if choice != ('required' if self.report['requests'] == 0 else 'none'):
            raise ValueError('unexpected request')
        if choice == 'none':
            items = payload.get('input', [])
            outputs = [item for item in items if isinstance(item, dict) and item.get('type') == 'function_call_output']
            self.report['call_id_matching'] = len(outputs) == 1 and bool(self.call_id) and outputs[0].get('call_id') == self.call_id
            if not self.report['call_id_matching']:
                raise ValueError('invalid continuation')
        request = Request(url, data=encoded, headers=headers, method='POST')
        self.report['requests'] += 1
        self.budget.llm_calls += 1
        with self.transport(request, timeout=min(45.0, self.budget.remaining()), reject_redirects=True) as response:
            if not 200 <= response.status < 300:
                raise ValueError('http failure')
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError('body limit')
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError('invalid response')
        output = value.get('output')
        items = output[:30] if isinstance(output, list) else []
        status = value.get('status')
        shapes = []
        for item in items:
            if not isinstance(item, dict):
                shapes.append({'type': 'invalid', 'status': 'invalid'})
                continue
            kind = item.get('type')
            item_status = item.get('status', 'completed')
            shape = {'type': kind if kind in ('reasoning', 'message', 'function_call') else 'other',
                     'status': item_status if item_status in ('completed', 'incomplete', 'in_progress') else 'other'}
            if kind == 'message':
                parts = item.get('content')
                shape['content_is_list'] = isinstance(parts, list)
                if isinstance(parts, list):
                    shape['parts'] = [{'type': part.get('type') if part.get('type') in ('output_text', 'refusal') else 'other',
                                       'nonempty': bool(part.get('text', '').strip()) if isinstance(part.get('text'), str) else False}
                                      if isinstance(part, dict) else {'type': 'invalid', 'nonempty': False}
                                      for part in parts[:30]]
            shapes.append(shape)
        self.report['responses'].append({'status': status if status in ('completed', 'incomplete', 'failed', 'in_progress') else 'other',
                                         'output_is_list': isinstance(output, list), 'items': shapes})
        usage = value.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        self.usages.append({k: v if type(v := usage.get(k)) is int and v >= 0 else None
                            for k in ('input_tokens', 'output_tokens')})
        if choice == 'required':
            calls = [x for x in value.get('output', []) if isinstance(x, dict) and x.get('type') == 'function_call']
            if len(calls) == 1:
                call = calls[0]
                self.call_id = call.get('call_id')
                self.report['call_id_present'] = isinstance(self.call_id, str) and bool(self.call_id)
                self.report['proposal_shape']['call_id_valid'] = (
                    isinstance(self.call_id, str) and bool(self.call_id.strip()) and len(self.call_id) <= 1024)
                allowed = {tool['name']: tool['parameters'] for tool in payload['tools']}
                name = call.get('name')
                name_allowed = isinstance(name, str) and name in allowed
                self.report['proposal_shape']['name_allowed'] = name_allowed
                self.report['proposal_shape']['name_is_search'] = name == 'sg_search'
                if name_allowed:
                    try:
                        _native_arguments(call.get('arguments'), allowed[name])
                        self.report['proposal_shape']['arguments_valid'] = True
                    except Exception:
                        self.report['proposal_shape']['arguments_valid'] = False
        # Check both proposal and continuation before session code can consume
        # the response. This does not detect arbitrary encodings or split keys.
        if contains_secret(value, self.config.api_key):
            self.report['proposal_shape']['credential_guard_blocked'] = True
            raise ValueError('secret in model response')
        return value


def run(config, *, transport=None, max_requests=MAX_REQUESTS, before_apply=None):
    report = {'status': 'failed', 'requests': 0, 'source_calls': 0, 'candidates': 0,
              'call_id_present': False, 'call_id_matching': False,
              'usage': {'input_tokens': None, 'output_tokens': None},
              'estimated_cost': {'amount': None, 'currency': None, 'input_price_per_million': None,
                                 'output_price_per_million': None, 'basis': 'snapshot_estimate_not_invoice'},
              'source_sha256': {}, 'source_drift': None, 'error': 'probe_failed',
              'failed_phase': None, 'responses': [], 'proposal_shape': {
                  'name_allowed': None, 'name_is_search': None, 'arguments_valid': None, 'call_id_valid': None,
                  'credential_guard_blocked': False}, 'controller_result': {'outcome': None, 'rejection': None},
              'failure_type': None}
    source, llm, store = MockSource(), None, None
    before, phase = None, 'setup'
    try:
        before = manifest()
        report['source_sha256'] = before
        config = validated(config)
        if type(max_requests) is not int or not 0 <= max_requests <= MAX_REQUESTS:
            raise ValueError('invalid limit')
        with tempfile.TemporaryDirectory(prefix='sg-native-probe-') as directory:
            store = StateStore(Path(directory) / 'session.sqlite3')
            try:
                chat = ChatSession(config, store, source=source)
                llm = ProbeLLM(config, chat.budget, transport or urlopen, report, max_requests)
                chat.llm = llm
                original_guard = chat.budget.request_guard
                def guard():
                    original_guard()
                    if report['requests'] >= max_requests:
                        raise BudgetExceeded('request limit')
                chat.budget.request_guard = guard
                store.save_task(chat.session_id, 1, {'topic': 'synthetic retrieval evaluation',
                                'target_count': 5, 'category': 'none', 'minimum_category_count': 0})
                chat._new_phase(first=True)
                phase = 'proposal'
                decision = None
                try:
                    decision, _ = chat.next_decision()
                finally:
                    # Only program-defined categories leave the temporary store;
                    # query, reason, call ID and exception text are never copied.
                    try:
                        rejected = [e for e in store.events(chat.session_id) if e.get('action') == 'invalid_action']
                        if rejected:
                            code = rejected[-1].get('payload', {}).get('error')
                            allowed_codes = {'unknown_action', 'not_allowed', 'empty_query', 'query_too_long',
                                             'repeated_query', 'invalid_search_providers', 'invalid_search_intent',
                                             'no_paper_ids', 'no_candidates', 'unknown_paper_ids',
                                             'empty_question', 'unknown_field', 'field_not_blocking'}
                            report['controller_result']['rejection'] = code if code in allowed_codes else 'other'
                        if getattr(chat, '_native_pending', None) is not None:
                            outcome = 'native_authorized'
                        elif decision is not None:
                            outcome = 'program_selected'
                        else:
                            outcome = 'no_decision'
                        report['controller_result']['outcome'] = outcome
                    except Exception:
                        report['controller_result']['outcome'] = 'unavailable'
                if decision is None or decision.action != 'SEARCH':
                    raise ValueError('search not proposed')
                if before_apply:
                    before_apply(chat)
                phase = 'apply_and_continuation'
                chat.apply_decision(decision)
                report['candidates'] = len(store.candidate_refs(chat.session_id))
                if report['requests'] != 2 or source.calls != 1 or not report['call_id_matching']:
                    raise ValueError('incomplete')
                report.update(status='completed', error=None)
            finally:
                try:
                    report['candidates'] = len(store.candidate_refs(chat.session_id))
                except BaseException:
                    pass
                store.close()
    except RunCancelled:
        report.update(status='cancelled', error='probe_cancelled', failed_phase=phase, failure_type='cancelled')
    except BudgetExceeded:
        report.update(status='failed', error='probe_failed', failed_phase=phase, failure_type='budget')
    except Exception as exc:
        from common import GuideError
        report.update(status='failed', error='probe_failed', failed_phase=phase,
                      failure_type='guide' if isinstance(exc, GuideError) else 'other')
    except BaseException:
        report.update(status='failed', error='probe_failed', failed_phase=phase, failure_type='interrupted')
    finally:
        report['source_calls'] = source.calls
        if llm and len(llm.usages) == report['requests'] and report['requests'] > 0:
            for key in report['usage']:
                values = [entry[key] for entry in llm.usages]
                if all(value is not None for value in values):
                    report['usage'][key] = sum(values)
        try:
            prices = (config.input_price, config.output_price)
            currency = config.currency
            if (isinstance(currency, str) and re.fullmatch(r'[A-Z]{3}', currency)
                    and all(type(p) in (int, float) and math.isfinite(p) and p >= 0 for p in prices)):
                report['estimated_cost']['currency'] = currency
                report['estimated_cost']['input_price_per_million'] = prices[0]
                report['estimated_cost']['output_price_per_million'] = prices[1]
                a, b = report['usage']['input_tokens'], report['usage']['output_tokens']
                if a is not None and b is not None:
                    report['estimated_cost']['amount'] = round((a * prices[0] + b * prices[1]) / 1000000, 8)
        except BaseException:
            pass
        try:
            report['source_drift'] = before != manifest() if before is not None else None
        except BaseException:
            report['source_drift'] = None
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(args.output, flags, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('not regular')
            try:
                report = run(load_config())
            except BaseException:
                report = {'status': 'failed', 'error': 'probe_failed', 'requests': 0}
            json.dump(report, stream, allow_nan=False, indent=2)
            stream.write('\n')
        return 0 if report['status'] == 'completed' else 1
    except BaseException:
        print('probe_failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
