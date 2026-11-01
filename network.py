"""Direct, bounded HTTP transport with a reapable process boundary.

Each request includes DNS, connect, headers and body in one wall-clock deadline.
Credentials are passed over a private pipe, never a command line or disk file.
Stopping the local process cannot undo work or charges at the remote server.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from email.message import Message
from functools import lru_cache
from io import BytesIO
import math
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

from common import GuideError, RunCancelled

MAX_BODY_BYTES = 4_000_001
_scope = ContextVar('scholarguide_http_scope', default=(None, None))


@lru_cache(maxsize=1)
def configure_background_workers():
    """Use the matching GUI interpreter for Windows spawn children.

    A detached python.exe parent has no console to inherit, so Windows would
    allocate a new one for each console-subsystem child. pythonw.exe keeps the
    same Python environment and private multiprocessing pipe without a window.
    Frozen executables and non-Windows interpreters keep their own spawn setup.
    """
    if sys.platform != 'win32' or getattr(sys, 'frozen', False):
        return None
    executable = Path(sys.executable).with_name('pythonw.exe')
    if not executable.is_file():
        return None
    multiprocessing.set_executable(str(executable))
    return str(executable)


class TransportDeadlineExceeded(GuideError):
    """The enclosing native tool exhausted its whole-operation deadline."""


@contextmanager
def execution_context(*, timeout_s=None, cancelled=None):
    end, previous_cancelled = _scope.get()
    if timeout_s is not None:
        proposed = time.monotonic() + max(0, float(timeout_s))
        end = min(end, proposed) if end is not None else proposed
    if previous_cancelled and cancelled:
        current = cancelled
        cancelled = lambda: previous_cancelled() or current()
    token = _scope.set((end, cancelled or previous_cancelled))
    try:
        yield
    finally:
        _scope.reset(token)


class BufferedResponse(BytesIO):
    def __init__(self, body, *, status=200, headers=(), url=''):
        super().__init__(body)
        self.status = self.code = status
        self.headers = Message()
        for key, value in headers:
            self.headers[key] = value
        self.url = url

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def info(self):
        return self.headers


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, reject_all=False):
        super().__init__()
        self.reject_all = reject_all

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        old_origin = (old.scheme.lower(), old.hostname, old.port or (443 if old.scheme == 'https' else 80))
        new_origin = (new.scheme.lower(), new.hostname, new.port or (443 if new.scheme == 'https' else 80))
        if self.reject_all or old_origin != new_origin:
            raise HTTPError(req.full_url, code, 'redirect refused', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_worker(connection, payload, deadline):
    """A top-level spawn target; only the child owns live sockets."""
    finished = threading.Event()
    def watchdog():
        parent = multiprocessing.parent_process()
        while not finished.wait(.05):
            if time.monotonic() >= deadline or (parent is not None and not parent.is_alive()):
                # The parent might itself have been hard-killed. A child must
                # still stop its sockets rather than surviving as orphan I/O.
                os._exit(124)
    threading.Thread(target=watchdog, name='HTTP deadline/parent watchdog', daemon=True).start()
    try:
        request = Request(payload['url'], data=payload['data'], headers=payload['headers'],
                          method=payload['method'])
        proxy = payload.get('proxy') or ''
        opener = build_opener(ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}),
                              _SafeRedirectHandler(payload.get('reject_redirects', False)))
        try:
            with opener.open(request, timeout=max(.001, deadline - time.monotonic())) as response:
                result = {'kind': 'response', 'status': response.status,
                          'headers': list(response.headers.items()), 'url': response.geturl(),
                          'body': response.read(MAX_BODY_BYTES)}
        except HTTPError as exc:
            try:
                result = {'kind': 'http_error', 'status': exc.code, 'reason': str(exc.reason),
                          'headers': list(exc.headers.items()) if exc.headers else [],
                          'body': exc.read(MAX_BODY_BYTES)}
            finally:
                exc.close()
        connection.send(result)
    except (OSError, URLError, ValueError) as exc:
        reason = getattr(exc, 'reason', exc)
        connection.send({'kind': 'timeout' if isinstance(reason, TimeoutError) else 'network_error',
                         'reason': type(reason).__name__})
    except BaseException as exc:
        connection.send({'kind': 'network_error', 'reason': type(exc).__name__})
    finally:
        finished.set()
        connection.close()


def _reap(process):
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
    process.join(.5)
    if process.is_alive():
        process.kill()
        process.join(.5)
    if process.is_alive():
        raise GuideError('HTTP worker could not be reaped; no further request was started.')
    process.close()


def urlopen(url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *, source_request=False,
            reject_redirects=False, **kwargs):
    """A buffered urllib response with DNS/connect/read in an absolute cap.

    Local buffers retain with/read/close and HTTPError headers/body semantics.
    TLS verification uses urllib defaults; system proxies remain disabled.
    """
    if kwargs:
        raise TypeError('Unsupported HTTP transport option: ' + next(iter(kwargs)))
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT or timeout is None:
        timeout = 30.0
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError('HTTP request deadline exceeded.')
    outer_end, cancelled = _scope.get()
    if cancelled and cancelled():
        raise RunCancelled()
    now = time.monotonic()
    if outer_end is not None and now >= outer_end:
        raise TransportDeadlineExceeded('Native tool wall-clock deadline exceeded.')
    request_end = min(now + timeout, outer_end) if outer_end is not None else now + timeout
    # Source HTTP stays direct by default. A user running a local academic
    # proxy can opt in explicitly; this is separate from model traffic.
    proxy = os.environ.get('SG_SOURCE_PROXY', '').strip() if source_request else ''
    if isinstance(url, Request):
        payload = {'url': url.full_url, 'data': data if data is not None else url.data,
                   'headers': dict(url.header_items()), 'method': url.get_method(),
                   'proxy': proxy}
    else:
        payload = {'url': str(url), 'data': data, 'headers': {},
                   'method': 'POST' if data is not None else 'GET', 'proxy': proxy}
    payload['reject_redirects'] = bool(reject_redirects)
    configure_background_workers()
    context = multiprocessing.get_context('spawn')
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_http_worker,
                              args=(writer, payload, request_end),
                              name='ScholarGuide HTTP', daemon=True)
    try:
        process.start()
        writer.close()
        while True:
            # Keep an already completed response when Stop arrives concurrently.
            if reader.poll(0):
                try:
                    result = reader.recv()
                except EOFError:
                    raise URLError('HTTP worker ended without a response.') from None
                break
            if cancelled and cancelled():
                error = RunCancelled()
                error.args = (str(error) + ' Local HTTP worker stopped; remote usage may have occurred.',)
                raise error
            remaining = request_end - time.monotonic()
            if remaining <= 0:
                if outer_end is not None and outer_end <= request_end:
                    raise TransportDeadlineExceeded('Native tool wall-clock deadline exceeded; remote usage may have occurred.')
                raise TimeoutError('HTTP request timed out; remote usage may have occurred.')
            if not process.is_alive():
                if reader.poll(.01):
                    continue
                raise URLError('HTTP worker ended without a response.')
            reader.poll(min(.05, remaining))
    finally:
        reader.close()
        writer.close()
        _reap(process)
    headers = Message()
    for key, value in result.get('headers', []):
        headers[key] = value
    if result['kind'] == 'http_error':
        raise HTTPError(payload['url'], result['status'], result['reason'], headers,
                        BytesIO(result['body']))
    if result['kind'] == 'timeout':
        raise TimeoutError('HTTP request timed out.')
    if result['kind'] != 'response':
        reason = result.get('reason') or 'HTTP request failed.'
        # Reconstruct safe exception types across the process boundary so a
        # DNS failure, a refused/reset socket and a timeout remain distinct.
        failures = {'gaierror': (socket.gaierror, 'DNS resolution failed.'),
                    'ConnectionRefusedError': (ConnectionRefusedError, 'Connection refused.'),
                    'ConnectionResetError': (ConnectionResetError, 'Connection reset.'),
                    'ConnectionAbortedError': (ConnectionAbortedError, 'Connection aborted.')}
        if reason in failures:
            kind, detail = failures[reason]
            raise URLError(kind(detail))
        raise URLError(reason)
    return BufferedResponse(result['body'], status=result['status'],
                            headers=result['headers'], url=result.get('url') or payload['url'])


def source_urlopen(url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
    """Open an academic/MCP source request using its optional dedicated proxy."""
    return urlopen(url, data=data, timeout=timeout, source_request=True, **kwargs)
