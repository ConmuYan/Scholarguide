"""Bounded parallel directed discovery with events committed by the owning thread."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from queue import Empty, Queue
import time

from common import GuideError, RunCancelled
from execution_events import bind_events, emit


class DirectedPrefetch:
    """Run admitted independent requests while the coordinator owns accounting.

    Constructing the context only creates a pool. The caller must reserve a
    request and then call :meth:`submit`; this ordering prevents a worker from
    reaching a source before the cost ledger has an admission row. Unconsumed
    admitted work is settled on context exit, including unknown worker failures.
    """
    def __init__(self, source, steps, limit, *, settle):
        self.source, self.steps, self.limit = source, steps[:2], limit
        self.settle_callback = settle
        self.queue = Queue()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ScholarGuide source')
        self.futures = {}
        self.handles = {}
        self.settled = set()
        self.drain_records = []

    def __enter__(self):
        return self

    def submit(self, index, step, handle):
        if index in self.futures:
            raise ValueError(f'prefetch index already submitted: {index}')
        if index < 0 or index >= len(self.steps):
            raise IndexError(f'unknown prefetch index: {index}')
        self.handles[index] = handle
        context = copy_context()
        self.futures[index] = self.pool.submit(context.run, self._run, step)

    def _run(self, step):
        with bind_events(lambda action, payload: self.queue.put((action, payload))):
            return self.source.search_batch(step['query'], self.limit,
                providers=[step['provider']], filters=step['filters'])

    def flush(self):
        while True:
            try:
                action, payload = self.queue.get_nowait()
            except Empty:
                break
            emit(action, **payload)

    def is_submitted(self, index):
        return index in self.futures

    def handle(self, index):
        return self.handles[index]

    def settle(self, index, units):
        if index in self.settled:
            return False
        handle = self.handles[index]
        self.settle_callback(handle, units)
        self.settled.add(index)
        return True

    @staticmethod
    def _result_units(value):
        runs = getattr(value, 'runs', None)
        if runs is None:
            return None
        return 1.0 if any(getattr(run, 'status', '') in ('ok', 'empty', 'cache')
                          for run in runs) else 0.0

    def _cancel_requested(self):
        budget = getattr(self.source, 'budget', None)
        token = getattr(budget, 'cancel_token', None)
        return bool(token is not None and token.requested)

    def take(self, index):
        future = self.futures[index]
        while not future.done():
            if self._cancel_requested():
                raise RunCancelled()
            self.flush()
            time.sleep(.025)
        self.flush()
        return future.result()

    def drain_unsettled(self):
        """Settle all admitted work, then re-raise a worker BaseException."""
        records = []
        pending_base_exception = None
        for index, future in self.futures.items():
            if index in self.settled:
                continue
            error = ''
            if future.cancelled():
                units = 0.0
                error = 'cancelled before worker start'
            else:
                try:
                    value = self.take(index)
                except (RunCancelled, GuideError) as exc:
                    units = 0.0
                    error = str(exc)
                except BaseException as exc:
                    units = 1.0
                    error = f'{type(exc).__name__}: {exc}'
                    if pending_base_exception is None:
                        pending_base_exception = exc
                else:
                    units = self._result_units(value)
                    if units is None:
                        units = 1.0
                        error = 'worker returned an unclassified batch'
            self.settle(index, units)
            record = {'index': index, 'units': units, 'error': error}
            self.drain_records.append(record)
            records.append(record)
        if pending_base_exception is not None:
            raise pending_base_exception
        return records

    def __exit__(self, *args):
        cancelled = self._cancel_requested()
        if args[0] is not None:
            try:
                cancelled = cancelled or issubclass(args[0], RunCancelled)
                if not issubclass(args[0], Exception):
                    cancelled = True
            except TypeError:
                pass
        for index, future in self.futures.items():
            if index not in self.settled:
                future.cancel()
        if cancelled:
            # Python threads cannot be killed safely. Do not block the request
            # coordinator on a source call that is already in flight; settle
            # started work conservatively and refund futures cancelled before
            # they began. A completed classified cancellation/error is known
            # to be a failed unit and is refunded consistently with take().
            self.pool.shutdown(wait=False, cancel_futures=True)
            for index, future in self.futures.items():
                if index not in self.settled:
                    if future.cancelled():
                        units = 0.0
                    elif future.done():
                        try:
                            error = future.exception()
                        except BaseException:
                            error = None
                        units = 0.0 if isinstance(error, (RunCancelled, GuideError)) else 1.0
                    else:
                        units = 1.0
                    self.settle(index, units)
                    self.drain_records.append({'index': index, 'units': units,
                                              'error': 'cancelled before result consumption'})
            self.flush()
            return
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.flush()
        self.drain_unsettled()
        self.flush()
