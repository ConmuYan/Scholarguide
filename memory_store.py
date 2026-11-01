"""Explicitly consented local preferences, independent of session state.

Only a trusted host selects the database and authenticates the user. SQLite
updates cannot erase WAL files, backups, or data already sent elsewhere; this
module provides neither multi-user authentication nor OS isolation. A commit
failure has unknown outcome: reopen the store and inspect revisions before a
user decides what to do. No operation automatically retries a failed commit.
Observed expiry is durable, but a clock rollback before expiry is ever observed
cannot be detected without a trusted time source. Store identity detects
accidental target changes; it does not authenticate a database against tampering.
"""
from __future__ import annotations

import math
import os
import re
import secrets
from pathlib import Path
import sqlite3
import tempfile
import threading
import time


class MemoryError(Exception):
    """A sanitized validation, lifecycle, or storage error."""


class RevisionConflict(MemoryError):
    """The supplied per-key revision is no longer current."""


_KEYS = ('reading_goal', 'prefer_recent')
_GOALS = ('orientation', 'comparison', 'frontier', 'general')
_MAX_REVISION = 9223372036854775807
_SCHEMA = [
    ('key', 'TEXT', 0, None, 1),
    ('revision', 'INTEGER', 1, None, 0),
    ('state', 'TEXT', 1, None, 0),
    ('value', 'TEXT', 0, None, 0),
    ('consent', 'INTEGER', 1, None, 0),
    ('expires_at', 'REAL', 0, None, 0),
]


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _key(key):
    if type(key) is not str or key not in _KEYS:
        raise MemoryError('Invalid preference key.')


def _revision(revision):
    if type(revision) is not int or not 0 <= revision < _MAX_REVISION:
        raise MemoryError('Invalid expected revision.')


def _encode(key, value):
    if key == 'reading_goal' and type(value) is str and value in _GOALS:
        return value
    if key == 'prefer_recent' and type(value) is bool:
        return 'true' if value else 'false'
    raise MemoryError('Invalid preference value.')


def _decode(key, value):
    if key == 'reading_goal' and type(value) is str and value in _GOALS:
        return value
    if key == 'prefer_recent' and value in ('true', 'false') and type(value) is str:
        return value == 'true'
    raise ValueError


class MemoryStore:
    """One trusted-host-selected SQLite database; all revisions are per key.

    grant(key, value, *, consent, expires_at, expected_revision) -> int
    revoke(key, *, expected_revision) -> int
    delete(key, *, expected_revision) -> int
    list() -> dict: recognized records keyed by preference, with revision,
        state, value, consent and expires_at, including expired tombstones.
        Invalid records are omitted.
    active_snapshot(*, now=None) -> dict: only currently active preferences;
        observed expirations are persisted as revisioned tombstones.

    Timestamps are finite Unix seconds. A supplied now can only make expiry
    stricter; actual wall time is always checked as well. The host must obtain
    explicit per-item consent and submit every change deliberately.
    """

    def __init__(self, path: str | os.PathLike, *, create=True):
        self._lock = threading.RLock()
        self._con = None
        self._closed = False
        temporary = None
        try:
            path = os.fspath(path)
            if type(path) is not str or not path or path == ':memory:' or path.startswith('file:'):
                raise MemoryError('An explicit filesystem database path is required.')
            path = Path(path).absolute()
            if create:
                try:
                    path.stat()
                except FileNotFoundError:
                    descriptor, temporary = tempfile.mkstemp(prefix='.memory-', suffix='.sqlite3', dir=path.parent)
                    os.close(descriptor)
                    con = sqlite3.connect(Path(temporary).as_uri() + '?mode=rw', uri=True,
                                          timeout=1, isolation_level=None)
                    try:
                        con.execute('BEGIN IMMEDIATE')
                        con.execute('CREATE TABLE preferences (key TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, value TEXT, consent INTEGER NOT NULL, expires_at REAL)')
                        con.execute('CREATE TABLE memory_metadata (store_id TEXT NOT NULL)')
                        con.execute('INSERT INTO memory_metadata VALUES (?)', (secrets.token_hex(32),))
                        con.execute('COMMIT')
                    finally:
                        con.close()
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        pass
            self._con = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=1,
                                        isolation_level=None, check_same_thread=False)
            self._con.execute('BEGIN IMMEDIATE')
            objects = self._con.execute("SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if objects not in ([('preferences', 'table')], [('memory_metadata', 'table'), ('preferences', 'table')]):
                raise MemoryError('Database is not an isolated preference store.')
            # Column metadata alone does not reveal COLLATE, CHECK or other
            # semantics. Accept only schemas emitted by this implementation;
            # an unfamiliar legacy schema must never be migrated or written.
            definitions = dict(self._con.execute(
                "SELECT name, sql FROM sqlite_master WHERE name IN ('preferences', 'memory_metadata')").fetchall())
            canonical = lambda sql: re.sub(r'\s+', '', sql or '').casefold().rstrip(';')
            expected_preferences = ('CREATE TABLE preferences (key TEXT PRIMARY KEY, revision INTEGER NOT NULL, '
                                    'state TEXT NOT NULL, value TEXT, consent INTEGER NOT NULL, expires_at REAL)')
            if canonical(definitions.get('preferences')) != canonical(expected_preferences):
                raise MemoryError('Invalid preference store schema.')
            if len(objects) == 2 and canonical(definitions.get('memory_metadata')) != canonical(
                    'CREATE TABLE memory_metadata (store_id TEXT NOT NULL)'):
                raise MemoryError('Invalid preference metadata.')
            columns = self._con.execute('PRAGMA table_info(preferences)').fetchall()
            extended = self._con.execute('PRAGMA table_xinfo(preferences)').fetchall()
            if (len(columns) != len(_SCHEMA) or len(extended) != len(_SCHEMA)
                    or any(tuple(c[1:6]) != expected
                           for c, expected in zip(columns, _SCHEMA))
                    or any(c[6] != 0 for c in extended)):
                raise MemoryError('Invalid preference store schema.')
            if len(objects) == 1:
                self._con.execute('CREATE TABLE memory_metadata (store_id TEXT NOT NULL)')
                self._con.execute('INSERT INTO memory_metadata VALUES (?)', (secrets.token_hex(32),))
            metadata_columns = self._con.execute('PRAGMA table_xinfo(memory_metadata)').fetchall()
            if (len(metadata_columns) != 1 or tuple(metadata_columns[0][1:6]) != ('store_id', 'TEXT', 1, None, 0)
                    or metadata_columns[0][6] != 0):
                raise MemoryError('Invalid preference metadata.')
            self._store_id()
            self._con.execute('COMMIT')
        except (sqlite3.Error, OSError, TypeError, ValueError, MemoryError):
            if self._con is not None:
                try:
                    self._con.close()
                except sqlite3.Error:
                    pass
            self._closed = True
            raise MemoryError('Preference store could not be opened.') from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _store_id(self):
        rows = self._con.execute('SELECT store_id FROM memory_metadata').fetchall()
        if (len(rows) != 1 or type(rows[0][0]) is not str or len(rows[0][0]) != 64
                or any(c not in '0123456789abcdef' for c in rows[0][0])):
            raise MemoryError('Invalid preference metadata.')
        return rows[0][0]

    def _check(self):
        if self._closed:
            raise MemoryError('Preference store is closed or unavailable.')

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._con.close()
                except sqlite3.Error:
                    raise MemoryError('Preference store could not be closed.') from None

    def __enter__(self):
        with self._lock:
            self._check()
            return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _mutate(self, key, expected_revision, state, value=None, expires_at=None, expected_store_id=None):
        _key(key)
        _revision(expected_revision)
        with self._lock:
            self._check()
            committing = False
            try:
                self._con.execute('BEGIN IMMEDIATE')
                if expected_store_id is not None and self._store_id() != expected_store_id:
                    raise RevisionConflict('Preference store conflict.')
                row = self._con.execute('SELECT revision FROM preferences WHERE key=?', (key,)).fetchone()
                current = 0 if row is None else row[0]
                if type(current) is not int or current < 1 and row is not None or current != expected_revision:
                    raise RevisionConflict('Preference revision conflict.')
                new_revision = current + 1
                self._con.execute('INSERT INTO preferences VALUES (?, ?, ?, ?, ?, ?) '
                                  'ON CONFLICT(key) DO UPDATE SET revision=excluded.revision, '
                                  'state=excluded.state, value=excluded.value, consent=excluded.consent, expires_at=excluded.expires_at',
                                  (key, new_revision, state, value, int(state == 'granted'), expires_at))
                committing = True
                self._con.execute('COMMIT')
                return new_revision
            except (sqlite3.Error, MemoryError) as exc:
                if committing:
                    self._closed = True
                    try:
                        self._con.close()
                    except sqlite3.Error:
                        pass
                    raise MemoryError('Commit outcome unknown; reopen and inspect before acting.') from None
                try:
                    if self._con.in_transaction:
                        self._con.execute('ROLLBACK')
                except sqlite3.Error:
                    self._closed = True
                    try:
                        self._con.close()
                    except sqlite3.Error:
                        pass
                    raise MemoryError('Preference store is unavailable.') from None
                if isinstance(exc, RevisionConflict):
                    raise
                raise MemoryError('Preference store operation failed.') from None

    def grant(self, key, value, *, consent, expires_at, expected_revision, expected_store_id=None):
        _key(key)
        encoded = _encode(key, value)
        if consent is not True:
            raise MemoryError('Explicit consent is required.')
        if not _number(expires_at) or expires_at <= time.time():
            raise MemoryError('A finite future expiration is required.')
        return self._mutate(key, expected_revision, 'granted', encoded, expires_at, expected_store_id)

    def revoke(self, key, *, expected_revision, expected_store_id=None):
        return self._mutate(key, expected_revision, 'revoked', expected_store_id=expected_store_id)

    def delete(self, key, *, expected_revision, expected_store_id=None):
        return self._mutate(key, expected_revision, 'deleted', expected_store_id=expected_store_id)

    def list(self):
        with self._lock:
            self._check()
            try:
                rows = self._con.execute('SELECT key, revision, state, value, consent, expires_at FROM preferences').fetchall()
            except sqlite3.Error:
                raise MemoryError('Preference store read failed.') from None
        result = {}
        for key, revision, state, value, consent, expires_at in rows:
            if key not in _KEYS or type(revision) is not int or revision < 1:
                continue
            if state == 'granted':
                if type(consent) is not int or consent != 1 or not _number(expires_at):
                    continue
                try:
                    value = _decode(key, value)
                except ValueError:
                    continue
            elif state in ('revoked', 'deleted', 'expired'):
                if value is not None or type(consent) is not int or consent != 0 or expires_at is not None:
                    continue
            else:
                continue
            result[key] = {'revision': revision, 'state': state, 'value': value,
                           'consent': consent == 1, 'expires_at': expires_at}
        return result

    def active_snapshot(self, *, now=None):
        return self.snapshot(now=now)['active']

    def snapshot(self, *, now=None):
        """Return active values and their matching records from one transaction."""
        if now is not None and not _number(now):
            raise MemoryError('Invalid current time.')
        with self._lock:
            self._check()
            committing = False
            try:
                self._con.execute('BEGIN IMMEDIATE')
                store_id = self._store_id()
                rows = self.list()
                raw = self._con.execute(
                    'SELECT key, revision, state, expires_at FROM preferences').fetchall()
                actual = time.time()
                if not _number(actual):
                    raise MemoryError('Current time is unavailable.')
                cutoff = max(actual, now) if now is not None else actual
                for key, revision, state, expiry in raw:
                    if (key not in _KEYS or state != 'granted' or not _number(expiry)
                            or expiry > cutoff or type(revision) is not int or revision < 1):
                        continue
                    if revision >= _MAX_REVISION:
                        raise MemoryError('Preference revision is exhausted.')
                    updated = self._con.execute(
                        "UPDATE preferences SET revision=revision+1, state='expired', "
                        "value=NULL, consent=0, expires_at=NULL "
                        "WHERE key=? AND revision=? AND state='granted' AND expires_at=?",
                        (key, revision, expiry))
                    if updated.rowcount != 1:
                        raise MemoryError('Preference expiry conflict.')
                    rows[key] = {'revision': revision + 1, 'state': 'expired', 'value': None,
                                 'consent': False, 'expires_at': None}
                active = {key: row['value'] for key, row in rows.items()
                          if row['state'] == 'granted' and row['expires_at'] > cutoff}
                committing = True
                self._con.execute('COMMIT')
                return {'preferences': rows, 'active': active, 'store_id': store_id}
            except (sqlite3.Error, MemoryError):
                if committing:
                    self._closed = True
                    try:
                        self._con.close()
                    except sqlite3.Error:
                        pass
                    raise MemoryError('Commit outcome unknown; reopen and inspect before acting.') from None
                try:
                    if self._con.in_transaction:
                        self._con.execute('ROLLBACK')
                except sqlite3.Error:
                    self._closed = True
                    try:
                        self._con.close()
                    except sqlite3.Error:
                        pass
                raise MemoryError('Preference snapshot failed.') from None
