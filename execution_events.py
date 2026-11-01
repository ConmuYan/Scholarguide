"""Public, real execution events shared by sources, models and application services.

Only explicit public fields are passed here. Callers must never include credentials,
HTTP headers, model prompts or hidden reasoning. A heartbeat is a transport concern,
not an execution event. Sinks are scoped to the current execution/thread.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import logging
import time
import uuid
from typing import Any, Callable

from common import utc_now

_sink: ContextVar[Callable | None] = ContextVar('scholarguide_event_sink', default=None)
_parent: ContextVar[str] = ContextVar('scholarguide_event_parent', default='')


@contextmanager
def bind_events(sink: Callable[[str, dict], Any] | None):
    """Bind a persistence/notification sink for an actual execution."""
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def emit(action: str, **payload: Any) -> None:
    sink = _sink.get()
    if sink is not None:
        sink(action, deepcopy({'at': utc_now(), **payload}))


def _terminal_emit(action: str, **payload: Any) -> None:
    try:
        emit(action, **payload)
    except Exception:
        # A terminal audit failure cannot change the already observed remote
        # outcome or replace the original exception; never log sink contents.
        try:
            logging.getLogger(__name__).warning('Terminal audit event unavailable.')
        except Exception:
            pass


@contextmanager
def operation(tool: str, *, source: str = '', category: str = 'tool', **public_fields):
    """Emit the actual start and terminal state of one bounded operation.

    The yielded dictionary can carry measured results, e.g. returned or tokens.
    Exceptions retain their original type and flow after the terminal event.
    """
    started = time.monotonic()
    identity = uuid.uuid4().hex
    parent = _parent.get()
    base = {'span_id': identity, 'parent_span_id': parent,
            'tool': tool, 'source': source, 'category': category, **public_fields}
    measured: dict[str, Any] = {}
    emit('tool_start', **base, status='running')
    token = _parent.set(identity)
    try:
        yield measured
    except BaseException as exc:
        cancelled = getattr(exc, 'code', '') == 'user_cancelled' or type(exc).__name__ == 'RunCancelled'
        _terminal_emit('tool_error', **{**base, **measured,
             'status': 'cancelled' if cancelled else 'error',
             'error_code': getattr(exc, 'code', '') or getattr(exc, 'kind', '') or type(exc).__name__,
             'error': 'Operation cancelled.' if cancelled else 'Operation failed; details withheld.',
             'duration_ms': round((time.monotonic() - started) * 1000, 1)})
        raise
    else:
        values = {**base, 'status': 'ok', **measured,
                  'duration_ms': round((time.monotonic() - started) * 1000, 1)}
        _terminal_emit('tool_end', **values)
    finally:
        _parent.reset(token)
