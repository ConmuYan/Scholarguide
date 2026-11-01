"""In-memory, revocable preference hints for a trusted host.

The host selects the user store and determines explicit fields without relying
on model or third-party claims. Call revalidate immediately before *each*
outbound dispatch, and discard any previously obtained hints after failure.
Validation and network dispatch cannot be atomic; in-flight requests cannot be
retracted. Do not persist or log hints. No product integration is provided.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time

from memory_store import MemoryStore, MemoryError as StoreError


class MemoryContextError(Exception):
    """The context was closed or can no longer be safely used."""


_ERROR = 'Memory context is no longer valid.'
_FIELDS = frozenset((
    'topic', 'category', 'minimum_category_count', 'target_count', 'unsupported',
    'purpose', 'excluded_paper_ids', 'prefer_recent', 'venue', 'year_from',
    'year_to', 'reading_goal', 'venue_scope', 'publication_type', 'accepted_only',
    'presentation_types', 'minimum_count', 'maximum_count',
))
_CONFLICTS = {
    'reading_goal': frozenset(('reading_goal', 'purpose', 'category', 'minimum_category_count')),
    'prefer_recent': frozenset(('prefer_recent', 'year_from', 'year_to')),
}
_VALUES = {'reading_goal': frozenset(('orientation', 'comparison', 'frontier', 'general')),
           'prefer_recent': frozenset((True, False))}


class _ExistingStore(MemoryStore):
    """Open only an existing database; a valid legacy schema may be migrated."""

    def __init__(self, path):
        super().__init__(path, create=False)


def _identity(path):
    stat = os.stat(path)
    return stat.st_dev, stat.st_ino


def _snapshot(path):
    before = _identity(path)
    with _ExistingStore(path) as store:
        snapshot = store.snapshot()
    if _identity(path) != before:
        raise StoreError('Preference store changed.')
    return (before, snapshot['store_id']), snapshot


class MemoryContext:
    """A single operation's captured preferences; no automatic refresh or retry.

    capture(path, *, enabled=False) returns an empty context on unavailable
    storage. hints(*, explicit_fields) returns only permitted, non-conflicting
    primitive values. revalidate() returns None or permanently invalidates the
    context and raises MemoryContextError. close() permanently invalidates it.
    An empty captured context never gains new permissions.
    """

    def __init__(self, path=None, records=None, identity=None):
        self._lock = threading.RLock()
        self._path = path
        self._identity = identity
        self._records = records or {}
        self._invalid = False

    def __repr__(self):
        return '<MemoryContext>'

    @classmethod
    def capture(cls, path, *, enabled=False):
        if enabled is not True:
            return cls()
        try:
            path = os.fspath(path)
            if type(path) is not str or not path or path == ':memory:' or path.startswith('file:'):
                return cls()
            path = os.path.abspath(path)
            identity, snapshot = _snapshot(path)
            records = {key: dict(snapshot['preferences'][key]) for key in _VALUES
                       if key in snapshot['active']}
            return cls(path if records else None, records, identity if records else None)
        except (StoreError, sqlite3.Error, OSError, TypeError, ValueError, KeyError):
            return cls()

    def _check(self, *, expiry=True):
        if self._invalid:
            raise MemoryContextError(_ERROR) from None
        if expiry and self._records:
            now = time.time()
            if (type(now) not in (int, float) or not math.isfinite(now)
                    or any(row['expires_at'] <= now for row in self._records.values())):
                self.revalidate()
                self._invalidate()
                raise MemoryContextError(_ERROR) from None

    def _invalidate(self):
        self._invalid = True
        self._records.clear()
        self._path = None
        self._identity = None

    def close(self):
        with self._lock:
            self._invalidate()

    def hints(self, *, explicit_fields):
        with self._lock:
            self._check()
            if (type(explicit_fields) is not set
                    or any(type(field) is not str for field in explicit_fields)
                    or not explicit_fields <= _FIELDS):
                return {}
            result = {}
            for key, row in self._records.items():
                value = row['value']
                if (_CONFLICTS[key].isdisjoint(explicit_fields)
                        and ((key == 'reading_goal' and type(value) is str and value in _VALUES[key])
                             or (key == 'prefer_recent' and type(value) is bool))):
                    result[key] = value
            return result

    def revalidate(self):
        with self._lock:
            self._check(expiry=False)
            if not self._records:
                return
            try:
                identity, snapshot = _snapshot(self._path)
                if identity != self._identity:
                    raise StoreError('Preference store changed.')
                for key, row in self._records.items():
                    if (key not in snapshot['active']
                            or snapshot['preferences'].get(key) != row
                            or type(snapshot['active'][key]) is not type(row['value'])
                            or snapshot['active'][key] != row['value']):
                        raise StoreError('Preference changed.')
            except (StoreError, sqlite3.Error, OSError, TypeError, ValueError, KeyError):
                self._invalidate()
                raise MemoryContextError(_ERROR) from None
            now = time.time()
            if (type(now) not in (int, float) or not math.isfinite(now)
                    or any(row['expires_at'] <= now for row in self._records.values())):
                self._invalidate()
                raise MemoryContextError(_ERROR) from None
