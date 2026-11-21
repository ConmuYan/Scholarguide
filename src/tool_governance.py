"""Host-side argument validation and privacy-preserving audit summaries.

Schemas do not grant permissions. Callers must separately authorize a tool and
supply a locally reviewed or explicitly discovered schema before execution.
"""
from __future__ import annotations

import json
import math
import multiprocessing
import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, exceptions as schema_exceptions
try:
    from referencing import Registry
except ImportError:  # The bounded validator rejects all $ref before construction.
    Registry = None

from common import GuideError

MAX_ARGUMENT_BYTES = 256_000
MAX_SCHEMA_BYTES = 256_000
MAX_SCHEMA_DEPTH = 64
SENSITIVE_KEY = re.compile(r'(?:key|token|auth|cookie|secret|password|credential|session|jwt)', re.I)
_REMOTE_SCHEMA_SLOTS = threading.BoundedSemaphore(2)


class ToolGovernanceError(GuideError):
    code = 'tool_governance_error'


def _schema_safety(value: Any, depth: int = 0, combination_depth: int = 0,
                   combination_product: int = 1, counters: dict | None = None) -> None:
    if counters is None:
        counters = {'nodes': 0, 'enum': 0}
    counters['nodes'] += 1
    if counters['nodes'] > 4096:
        raise ToolGovernanceError('tool schema exceeds maximum structural size')
    if depth > MAX_SCHEMA_DEPTH:
        raise ToolGovernanceError('tool schema exceeds maximum depth')
    if isinstance(value, Mapping):
        if any(key in value for key in ('anyOf', 'oneOf', 'allOf')):
            combination_depth += 1
            if combination_depth > 6:
                raise ToolGovernanceError('tool schema has excessive nested combinations')
        complexity = 1
        for key in ('anyOf', 'oneOf', 'allOf'):
            branches = value.get(key)
            if isinstance(branches, list):
                complexity *= len(branches)
        combination_product *= complexity
        if complexity > 32 or combination_product > 128:
            raise ToolGovernanceError('tool schema has too many combination branches')
        for key, child in value.items():
            if key == 'enum' and isinstance(child, list):
                counters['enum'] += len(child)
                if len(child) > 16 or counters['enum'] > 128:
                    raise ToolGovernanceError('tool schema has too many enum values')
                if any(isinstance(item, (dict, list)) or
                       (isinstance(item, str) and len(item.encode('utf-8')) > 256) for item in child):
                    raise ToolGovernanceError('complex enum values require local review')
            if key == 'const' and (isinstance(child, (dict, list)) or
                                   (isinstance(child, str) and len(child.encode('utf-8')) > 256)):
                raise ToolGovernanceError('complex const values require local review')
            if key in ('$dynamicRef', '$recursiveRef'):
                raise ToolGovernanceError('dynamic schema references are not supported')
            if key == '$ref':
                # Even local refs permit exponential expansion or cycles that
                # bypass textual depth/branch limits. Fail closed until a
                # bounded reference evaluator is implemented.
                raise ToolGovernanceError('schema references are not supported')
            # Patterns are supplied by remote servers and can cause unbounded
            # regular-expression evaluation. Refuse rather than silently ignore.
            if key in ('pattern', 'patternProperties'):
                raise ToolGovernanceError('remote regular-expression schemas require local review')
            if key == 'uniqueItems' and child is True:
                # Deep equality on untrusted arrays can be quadratic.
                raise ToolGovernanceError('uniqueItems schemas require local review')
            if key in ('dependentSchemas', 'unevaluatedProperties', 'unevaluatedItems'):
                # These can repeatedly re-evaluate the same large instance,
                # bypassing the simple combination-product bound.
                raise ToolGovernanceError('re-evaluating schemas require local review')
            _schema_safety(child, depth + 1, combination_depth, combination_product, counters)
    elif isinstance(value, list):
        for child in value:
            _schema_safety(child, depth + 1, combination_depth, combination_product, counters)


def _instance_safety(value: Any) -> None:
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 4096 or depth > 32:
            raise ToolGovernanceError('tool arguments exceed structural limits')
        if isinstance(item, dict):
            if len(item) > 256:
                raise ToolGovernanceError('tool arguments exceed object size limit')
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            if len(item) > 256:
                raise ToolGovernanceError('tool arguments exceed array size limit')
            pending.extend((v, depth + 1) for v in item)


def validate_json_schema(value: Any, schema: Mapping[str, Any] | bool | None,
                         *, path: str = 'input') -> None:
    """Validate JSON Schema without fetching external references.

    An empty registry prevents network resolution; all references and remote
    regexes are refused. Validation errors omit user values and remote text.
    """
    if schema is None:
        return
    if not isinstance(schema, (Mapping, bool)):
        raise ToolGovernanceError('tool schema must be an object or boolean')
    try:
        encoded = json.dumps(schema, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > MAX_SCHEMA_BYTES:
            raise ToolGovernanceError('tool schema exceeds maximum size')
        _schema_safety(schema)
        Draft202012Validator.check_schema(schema)
        validator = (Draft202012Validator(schema, registry=Registry())
                     if Registry is not None else Draft202012Validator(schema))
        validator.validate(value)
    except ToolGovernanceError:
        raise
    except schema_exceptions.ValidationError as exc:
        keyword = str(exc.validator or 'constraint')
        if not re.fullmatch(r'[A-Za-z0-9_]{1,32}', keyword):
            keyword = 'constraint'
        raise ToolGovernanceError(f'{path} does not satisfy schema ({keyword})') from None
    except (schema_exceptions.SchemaError, TypeError, ValueError, RecursionError):
        raise ToolGovernanceError('tool schema is invalid or exceeds safety limits') from None
    except Exception:
        # Includes unresolvable local references; never expose raw input/schema.
        raise ToolGovernanceError('tool schema could not be validated safely') from None


def _remote_schema_worker(sender, value, schema, deadline) -> None:
    """Spawn target; only fixed status bytes leave the child process."""
    finished = threading.Event()
    def watchdog():
        parent = multiprocessing.parent_process()
        while not finished.wait(.05):
            if time.monotonic() >= deadline or (parent is not None and not parent.is_alive()):
                os._exit(124)
    threading.Thread(target=watchdog, name='schema-deadline-watchdog', daemon=True).start()
    try:
        validate_json_schema(value, schema)
        sender.send_bytes(b'ok')
    except BaseException:
        try:
            sender.send_bytes(b'invalid')
        except (OSError, EOFError):
            pass
    finally:
        finished.set()
        sender.close()


def validate_remote_schema(value: Any, schema: Mapping[str, Any] | bool | None,
                           *, budget=None, timeout_s: float = 5.0) -> None:
    """Isolate remote validation work in a short-lived, bounded spawn worker.

    A worker is not an OS sandbox. Input uses anonymous IPC, never argv or
    files. Cancellation/deadline is checked while waiting; OS process startup
    and termination can still overshoot a deadline.
    """
    if not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ToolGovernanceError('invalid remote schema validation timeout')
    deadline = time.monotonic() + timeout_s

    def remaining():
        if budget is not None:
            budget.check_cancelled()
            allowed = budget.remaining()
        else:
            allowed = timeout_s
        left = deadline - time.monotonic()
        if left <= 0:
            raise ToolGovernanceError('remote schema validation timed out')
        return min(allowed, left)

    remaining()
    value = validate_arguments(value)
    try:
        raw_schema = json.dumps(schema, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw_schema) > MAX_SCHEMA_BYTES:
            raise ToolGovernanceError('tool schema exceeds maximum size')
        schema = json.loads(raw_schema)
    except ToolGovernanceError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise ToolGovernanceError('tool schema is invalid or exceeds safety limits') from None
    remaining()
    while not _REMOTE_SCHEMA_SLOTS.acquire(timeout=min(.05, remaining())):
        pass
    process = receiver = sender = None
    started = False
    startup_unknown = False
    try:
        remaining()
        if multiprocessing.current_process().daemon:
            raise ToolGovernanceError('remote schema validation worker unavailable')
        try:
            ctx = multiprocessing.get_context('spawn')
            receiver, sender = ctx.Pipe(duplex=False)
            process = ctx.Process(target=_remote_schema_worker,
                                  args=(sender, value, schema, deadline), daemon=True)
            process.start()
            started = True
        except Exception:
            if process is not None:
                try:
                    started = process.pid is not None
                    startup_unknown = not started
                except Exception:
                    startup_unknown = True
            raise ToolGovernanceError('remote schema validation worker unavailable') from None
        sender.close()
        sender = None
        while True:
            wait = min(.05, remaining())
            if receiver.poll(wait):
                try:
                    outcome = receiver.recv_bytes(32)
                except (EOFError, OSError):
                    raise ToolGovernanceError('remote schema validation worker failed') from None
                remaining()
                if outcome != b'ok':
                    raise ToolGovernanceError('remote schema validation failed')
                return
            if process.exitcode is not None:
                raise ToolGovernanceError('remote schema validation worker failed')
    finally:
        original_error = sys.exc_info()[0] is not None
        cleanup_failed = startup_unknown
        try:
            if sender is not None:
                sender.close()
            if receiver is not None:
                receiver.close()
            if process is not None and started:
                if process.is_alive():
                    process.terminate()
                    process.join(.3)
                    if process.is_alive() and hasattr(process, 'kill'):
                        process.kill()
                process.join(.5)
                if process.is_alive():
                    cleanup_failed = True
                else:
                    process.close()
        except BaseException:
            cleanup_failed = True
        finally:
            if not cleanup_failed:
                _REMOTE_SCHEMA_SLOTS.release()
            else:
                # Never let unconfirmed workers be replaced indefinitely. A
                # stuck permit is intentionally recovered only on restart.
                try:
                    from execution_events import emit
                    emit('remote_schema_cleanup_failed', status='unknown')
                except BaseException:
                    pass
        if cleanup_failed and not original_error:
            raise ToolGovernanceError('remote schema validation worker cleanup failed')


def validate_arguments(arguments: Any, schema: Mapping[str, Any] | bool | None = None) -> dict:
    if not isinstance(arguments, Mapping) or any(not isinstance(key, str) for key in arguments):
        raise ToolGovernanceError('tool arguments must be an object with string keys')
    value = dict(arguments)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, RecursionError):
        raise ToolGovernanceError('tool arguments are not valid JSON') from None
    if len(encoded) > MAX_ARGUMENT_BYTES:
        raise ToolGovernanceError('tool arguments exceed the bounded input size')
    # Validate and execute the same detached JSON snapshot: callers may mutate
    # their original nested objects concurrently after this boundary.
    value = json.loads(encoded)
    _instance_safety(value)
    validate_json_schema(value, schema)
    return value


def audit_arguments(value: Any) -> dict:
    """Return a bounded summary without argument values or untrusted field names."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, RecursionError):
        return {'shape': 'invalid_json'}
    # Unsalted fingerprints of low-entropy queries can be recovered by
    # dictionary guessing; retain structural metadata only.
    result = {'shape': 'object' if isinstance(value, Mapping) else type(value).__name__,
              'bytes': len(encoded)}
    if isinstance(value, Mapping):
        result['field_count'] = len(value)
    return result


def redact_text(value: Any) -> str:
    """Best-effort display redaction. Never use for arbitrary audit error text."""
    text = str(value or '')
    text = re.sub(r'(?i)(bearer\s+)[^\s,;]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|token|secret|password|credential)[=:]\s*)[^\s,;]+',
                  r'\1[redacted]', text)
    return text[:400]
