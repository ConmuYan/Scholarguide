"""Local-only ledger for the shared Lune daily request allowance.

Why this module exists
----------------------
Lune's daily allowance (300 requests) is an **account-wide** limit shared by
every client of that account.  This module keeps a *local* ledger of the
requests this machine has made, so a local run cannot knowingly overshoot the
allowance it was told about.

Hard rules implemented here
---------------------------
* **Fail closed.**  Any missing / corrupt / expired / exhausted state makes
  :meth:`LuneQuotaStore.reserve` raise a :class:`common.GuideError` carrying a
  stable ``code``.  A failure never consumes quota and never reaches the
  network.
* **The local ledger is not the account truth.**  Other clients consume the
  same allowance and that is not observable from here, so the store only ever
  reports what it knows locally and marks ``external_usage_unknown = True``.
  It never claims to know the real account balance.
* **Snapshots are host supplied.**  :meth:`LuneQuotaStore.confirm` is called by
  the host after the *user* explicitly authorised a fresh reading.  Nothing in
  this module reads configuration files, environment variables, credentials or
  the network.
* **No reset guessing.**  The reset moment is never inferred from the system
  clock; ``expires_at`` comes from the host.  Once the window is over the host
  must confirm again.
* **Conservative accounting.**  A reservation is spent at *reserve* time.  A
  failed or unknown outcome is **not** refunded, and a reservation left behind
  by a crash counts as unknown/consumed.
* **No secrets in the database.**  Only an opaque id, a timestamp, a
  whitelisted method name and a status are stored.  Parameters, queries,
  credentials and tokens are rejected outright and never persisted.

The store is a single SQLite file.  Several processes may share the same path:
every mutation runs inside a ``BEGIN IMMEDIATE`` transaction, so the ledger
stays consistent across threads and processes and never overshoots the
allowance it was given.
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from common import GuideError

#: The documented daily allowance of a Lune account (shared by all clients).
ACCOUNT_DAILY_LIMIT = 300

# --- stable error codes -----------------------------------------------------
CODE_INVALID_INPUT = 'quota_invalid_input'
CODE_INVALID_METHOD = 'quota_invalid_method'
CODE_INVALID_OUTCOME = 'quota_invalid_outcome'
CODE_NO_SNAPSHOT = 'quota_no_snapshot'
CODE_EXPIRED = 'quota_snapshot_expired'
CODE_EXHAUSTED = 'quota_exhausted'
CODE_PENDING_RESERVED = 'quota_pending_reserved'
CODE_STALE_SNAPSHOT = 'quota_stale_snapshot'
CODE_NOT_FOUND = 'quota_reservation_not_found'
CODE_CORRUPT = 'quota_snapshot_corrupt'
CODE_UNAVAILABLE = 'quota_ledger_unavailable'
CODE_CLOSED = 'quota_store_closed'

#: Only these method names may be written into the ledger.  A method that is
#: not on the list is rejected before anything is persisted, which is what
#: keeps free-form parameters (queries, tokens) out of the database.
ALLOWED_METHODS = (
    'oauth/register',
    'oauth/token',
    'search_papers',
    'search_related_papers',
    'get_paper_fulltext',
    'get_paper_citations',
    'list_conferences',
    'search_papers_many',
    'initialize',
    'notifications/initialized',
    'session/delete',
    'ping',
    'tools/list',
    'tools/call',
    'prompts/list',
    'prompts/get',
    'resources/list',
    'resources/read',
)

#: Outcomes the host may report.  Everything that is not a clear success is
#: accounted as unknown/consumed.
_OUTCOME_MAP = {
    'ok': 'ok',
    'success': 'ok',
    'response_received': 'response_received',
    'error': 'error',
    'failed': 'error',
    'unknown': 'unknown',
    'timeout': 'unknown',
    'cancelled': 'unknown',
}
ALLOWED_OUTCOMES = tuple(_OUTCOME_MAP)

STATUS_RESERVED = 'reserved'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    window_id INTEGER NOT NULL,
    remaining INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    confirmed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_events (
    id TEXT PRIMARY KEY,
    window_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quota_events_window ON quota_events (window_id);
CREATE INDEX IF NOT EXISTS quota_events_status ON quota_events (status);
"""


def default_quota_path() -> Path:
    """Shared local path; never contains account credentials or query text."""
    configured = os.environ.get('SG_LUNE_QUOTA_DB', '').strip()
    return Path(configured).expanduser() if configured else (
        Path.home() / '.scholarguide' / 'lune-quota.sqlite3')


def _process_alive(pid: int) -> bool | None:
    """Return None when OS access cannot safely establish process liveness."""
    if type(pid) is not int or pid <= 0:
        return None
    if pid == os.getpid():
        return True
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            return False if error == 87 else None
        try:
            status = kernel.WaitForSingleObject(handle, 0)
            return False if status == 0 else True if status == 258 else None
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None


def _error(code: str, message: str) -> GuideError:
    exc = GuideError(message)
    exc.code = code
    return exc


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> Optional[float]:
    if not _is_number(value):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


class LuneQuotaStore:
    """A durable, cross-process ledger of the local share of a Lune allowance.

    Parameters
    ----------
    path:
        Path of the SQLite file backing the ledger.  Parent directories are
        created when missing.  Several stores (in this process or in other
        processes) may point at the same file; they coordinate through SQLite
        locking, not through any in-process cache.
    """

    def __init__(self, path: Any) -> None:
        if not isinstance(path, (str, bytes, os.PathLike)) or isinstance(path, bool):
            raise _error(CODE_INVALID_INPUT, 'ledger path must be a filesystem path.')
        self._path = os.fspath(path)
        if isinstance(self._path, bytes):
            self._path = self._path.decode('utf-8')
        if not self._path.strip():
            raise _error(CODE_INVALID_INPUT, 'ledger path must not be empty.')
        self._lock = threading.RLock()
        self._closed = False
        directory = os.path.dirname(os.path.abspath(self._path))
        if directory and not os.path.isdir(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                raise _error(CODE_UNAVAILABLE, 'cannot create ledger directory: %s' % exc) from None
        try:
            self._conn = sqlite3.connect(
                self._path,
                # Fail closed promptly under contention instead of blocking a
                # cancelled/deadline-limited request for SQLite's default 30s.
                timeout=0.25,
                isolation_level=None,      # explicit transaction control
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise _error(CODE_UNAVAILABLE, 'cannot open quota ledger: %s' % exc) from None
        self._conn.execute('PRAGMA busy_timeout = 250')
        try:
            self._conn.execute('PRAGMA journal_mode = WAL')
        except sqlite3.Error:
            pass  # a rollback journal is still correct, just slower
        try:
            self._conn.execute('PRAGMA synchronous = FULL')
        except sqlite3.Error as exc:
            self._discard()
            raise _error(CODE_CORRUPT, 'quota ledger file is not a usable database: %s' % exc) from None
        try:
            self._ensure_schema()
        except GuideError:
            self._discard()
            raise

    # -- infrastructure ------------------------------------------------------
    def _discard(self) -> None:
        """Close a half-open connection (constructor failure path)."""
        self._closed = True
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def _ensure_schema(self) -> None:
        with self._lock:
            try:
                self._conn.executescript(_SCHEMA)
                columns = {row[1] for row in self._conn.execute('PRAGMA table_info(quota_events)')}
                if 'owner_pid' not in columns:
                    try:
                        self._conn.execute('ALTER TABLE quota_events ADD COLUMN owner_pid INTEGER')
                    except sqlite3.OperationalError:
                        columns = {row[1] for row in self._conn.execute('PRAGMA table_info(quota_events)')}
                        if 'owner_pid' not in columns:
                            raise
                self._conn.execute(
                    'INSERT OR IGNORE INTO quota_meta (key, value) VALUES (?, ?)',
                    ('schema_version', '1'),
                )
                row = self._conn.execute(
                    'SELECT value FROM quota_meta WHERE key = ?', ('schema_version',)).fetchone()
                if row is None or row[0] != '1':
                    raise _error(CODE_CORRUPT, 'unsupported quota ledger schema version.')
            except sqlite3.DatabaseError as exc:
                raise _error(CODE_CORRUPT, 'quota ledger schema is unusable: %s' % exc) from None

    @staticmethod
    def _budget_remaining(budget: Any) -> float:
        budget.check_cancelled()
        return budget.remaining()

    @contextmanager
    def _write_tx(self, budget: Any = None) -> Iterator[sqlite3.Connection]:
        """Take the local lock before BEGIN; never retry a transaction."""
        if budget is None:
            self._lock.acquire()
        else:
            while True:
                remaining = self._budget_remaining(budget)
                if self._lock.acquire(timeout=min(.05, max(0, remaining))):
                    break
        try:
            self._check_open()
            original_timeout = None
            try:
                if budget is not None:
                    remaining = self._budget_remaining(budget)
                    original_timeout = self._conn.execute('PRAGMA busy_timeout').fetchone()[0]
                    timeout = min(original_timeout, max(0, int(remaining * 1000)))
                    self._conn.execute('PRAGMA busy_timeout = %d' % timeout)
                try:
                    self._conn.execute('BEGIN IMMEDIATE')
                except sqlite3.OperationalError as exc:
                    if budget is not None:
                        self._budget_remaining(budget)
                    raise _error(CODE_UNAVAILABLE, 'quota ledger is busy: %s' % exc) from None
                except sqlite3.DatabaseError as exc:
                    raise _error(CODE_CORRUPT, 'quota ledger is unusable: %s' % exc) from None
                try:
                    if budget is not None:
                        self._budget_remaining(budget)
                    yield self._conn
                    if budget is not None:
                        self._budget_remaining(budget)
                except BaseException:
                    try:
                        self._conn.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise
                try:
                    self._conn.execute('COMMIT')
                except sqlite3.Error as exc:
                    try:
                        self._conn.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise _error(CODE_UNAVAILABLE, 'cannot commit quota transaction; commit outcome may be unknown: %s' % exc) from None
            finally:
                if original_timeout is not None:
                    active_error = sys.exc_info()[0] is not None
                    try:
                        self._conn.execute('PRAGMA busy_timeout = %d' % original_timeout)
                    except sqlite3.Error:
                        if not active_error:
                            raise
        finally:
            self._lock.release()

    def _read(self, sql: str, args: Tuple[Any, ...] = ()) -> list:
        with self._lock:
            self._check_open()
            try:
                return list(self._conn.execute(sql, args))
            except sqlite3.OperationalError as exc:
                raise _error(CODE_UNAVAILABLE, 'quota ledger is busy: %s' % exc) from None
            except sqlite3.DatabaseError as exc:
                raise _error(CODE_CORRUPT, 'quota ledger is unusable: %s' % exc) from None

    def _check_open(self) -> None:
        if self._closed:
            raise _error(CODE_CLOSED, 'quota ledger is closed.')

    @staticmethod
    def _now(now: Optional[float]) -> float:
        if now is None:
            return time.time()
        value = _finite(now)
        if value is None:
            raise _error(CODE_INVALID_INPUT, 'now must be a finite number.')
        return value

    # -- snapshot ------------------------------------------------------------
    def confirm(self, remaining: int, expires_at: float, *, now: Optional[float] = None) -> None:
        """Record a host-supplied snapshot of the allowance.

        The host calls this only after the user explicitly authorised a fresh
        reading of the account.  ``remaining`` must be an integer in
        ``0..ACCOUNT_DAILY_LIMIT`` and ``expires_at`` a finite epoch in the
        future.  A refresh is refused while the previous window still has
        unfinished reservations, so a pending call can never be lost.
        """
        moment = self._now(now)
        if isinstance(remaining, bool) or not isinstance(remaining, int):
            raise _error(CODE_INVALID_INPUT, 'remaining must be an integer.')
        if remaining < 0 or remaining > ACCOUNT_DAILY_LIMIT:
            raise _error(
                CODE_INVALID_INPUT,
                'remaining must be between 0 and %d.' % ACCOUNT_DAILY_LIMIT,
            )
        expiry = _finite(expires_at)
        if expiry is None:
            raise _error(CODE_INVALID_INPUT, 'expires_at must be a finite epoch.')
        if expiry <= moment:
            raise _error(CODE_INVALID_INPUT, 'expires_at must be in the future.')

        with self._write_tx() as conn:
            pending = conn.execute(
                'SELECT COUNT(*) FROM quota_events WHERE status = ?', (STATUS_RESERVED,)
            ).fetchone()[0]
            if pending:
                raise _error(
                    CODE_PENDING_RESERVED,
                    'cannot refresh the quota snapshot while %d reservation(s) are unfinished.' % pending,
                )
            row = conn.execute(
                'SELECT window_id, remaining, expires_at, confirmed_at '
                'FROM quota_snapshot WHERE id = 1').fetchone()
            if row:
                try:
                    previous_id, previous_remaining = int(row[0]), int(row[1])
                    previous_expiry, previous_confirmed = float(row[2]), float(row[3])
                except (TypeError, ValueError, OverflowError):
                    raise _error(CODE_CORRUPT, 'the stored quota snapshot is corrupt.') from None
                if (not 0 <= previous_remaining <= ACCOUNT_DAILY_LIMIT
                        or not math.isfinite(previous_expiry)
                        or not math.isfinite(previous_confirmed)):
                    raise _error(CODE_CORRUPT, 'the stored quota snapshot is corrupt.')
                if moment < previous_confirmed:
                    raise _error(CODE_STALE_SNAPSHOT, 'quota confirmation is older than the stored snapshot.')
                if moment < previous_expiry:
                    used = conn.execute(
                        'SELECT COUNT(*) FROM quota_events WHERE window_id = ?', (previous_id,)
                    ).fetchone()[0]
                    if remaining > max(0, previous_remaining - used) or expiry > previous_expiry:
                        raise _error(CODE_STALE_SNAPSHOT,
                                     'confirmation would increase the active local allowance or extend its expiry.')
                window_id = previous_id + 1
            else:
                window_id = 1
            conn.execute(
                'INSERT INTO quota_snapshot (id, window_id, remaining, expires_at, confirmed_at) '
                'VALUES (1, ?, ?, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET window_id = excluded.window_id, '
                'remaining = excluded.remaining, expires_at = excluded.expires_at, '
                'confirmed_at = excluded.confirmed_at',
                (window_id, remaining, expiry, moment),
            )

    # -- reservations --------------------------------------------------------
    def reserve(self, method: str, *, now: Optional[float] = None, budget: Any = None) -> str:
        """Atomically take one unit of the local allowance.

        Returns an opaque reservation id.  Raises a :class:`GuideError` with a
        stable code (and without consuming anything) when the snapshot is
        missing, corrupt, expired or exhausted.
        """
        moment = self._now(now)
        if not isinstance(method, str) or method not in ALLOWED_METHODS:
            raise _error(CODE_INVALID_METHOD, 'method is not on the allow list.')

        with self._write_tx(budget) as conn:
            # Lock contention can last beyond the snapshot expiry. Re-read the
            # real clock after acquiring the cross-process write lock.
            if now is None:
                moment = self._now(None)
            row = conn.execute(
                'SELECT window_id, remaining, expires_at FROM quota_snapshot WHERE id = 1'
            ).fetchone()
            if row is None:
                raise _error(
                    CODE_NO_SNAPSHOT,
                    'no confirmed quota snapshot; call confirm() after the user authorised a reading.',
                )
            try:
                window_id, remaining, expires_at = int(row[0]), int(row[1]), float(row[2])
            except (TypeError, ValueError, OverflowError):
                raise _error(CODE_CORRUPT, 'the stored quota snapshot is corrupt.') from None
            if remaining < 0 or remaining > ACCOUNT_DAILY_LIMIT:
                raise _error(CODE_CORRUPT, 'the stored quota snapshot is out of range.')
            if not math.isfinite(expires_at):
                raise _error(CODE_CORRUPT, 'the stored quota snapshot is corrupt.')
            if expires_at <= moment:
                raise _error(
                    CODE_EXPIRED,
                    'the confirmed quota snapshot expired; a new confirmation is required.',
                )
            used = conn.execute(
                'SELECT COUNT(*) FROM quota_events WHERE window_id = ?', (window_id,)
            ).fetchone()[0]
            if remaining - used <= 0:
                raise _error(CODE_EXHAUSTED, 'the local share of the quota is exhausted.')
            reservation_id = uuid.uuid4().hex
            conn.execute(
                'INSERT INTO quota_events (id, window_id, created_at, method, status, owner_pid) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (reservation_id, window_id, moment, method, STATUS_RESERVED, os.getpid()),
            )
            return reservation_id

    def finish(self, id: str, outcome: str, *, budget: Any = None) -> None:
        """Close a reservation without refunding it.

        After a send, an error leaves finalization uncertain: retain any received
        response, do not resend or refund, and resolve the ledger separately.
        """
        if not isinstance(id, str) or not id.strip():
            raise _error(CODE_INVALID_INPUT, 'reservation id must be a non-empty string.')
        if not isinstance(outcome, str) or outcome not in _OUTCOME_MAP:
            raise _error(CODE_INVALID_OUTCOME, 'outcome is not on the allow list.')
        canonical = _OUTCOME_MAP[outcome]
        with self._write_tx(budget) as conn:
            row = conn.execute('SELECT status FROM quota_events WHERE id = ?', (id,)).fetchone()
            if row is None:
                raise _error(CODE_NOT_FOUND, 'unknown reservation id.')
            if row[0] != STATUS_RESERVED:
                return  # already finished: idempotent no-op, consumption unchanged
            conn.execute('UPDATE quota_events SET status = ? WHERE id = ?', (canonical, id))

    def reconcile_unknown(self, reservation_id: str) -> None:
        """Reconcile only when the reservation's owner process is confirmed dead."""
        if not isinstance(reservation_id, str) or not reservation_id:
            raise _error(CODE_INVALID_INPUT, 'reservation id must be a non-empty string.')
        with self._write_tx() as conn:
            row = conn.execute('SELECT status, owner_pid FROM quota_events WHERE id = ?',
                               (reservation_id,)).fetchone()
            if row is None:
                raise _error(CODE_NOT_FOUND, 'unknown reservation id.')
            if row[0] != STATUS_RESERVED:
                return
            if _process_alive(row[1]) is not False:
                raise _error(CODE_PENDING_RESERVED,
                             'owner process is active or cannot be verified; reconciliation refused.')
            conn.execute('UPDATE quota_events SET status = ? WHERE id = ?',
                         ('unknown', reservation_id))

    def pending(self) -> list[dict[str, Any]]:
        """Return opaque unfinished reservations for explicit recovery.

        Hosts may mark a reservation unknown only after establishing that the
        original operation is no longer running. This never refunds allowance.
        """
        rows = self._read('SELECT id, created_at, method FROM quota_events '
                          'WHERE status = ? ORDER BY created_at, id', (STATUS_RESERVED,))
        return [{'id': row[0], 'created_at': row[1], 'method': row[2]} for row in rows]

    # -- reporting -----------------------------------------------------------
    def status(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Report what is known *locally*.

        ``external_usage_unknown`` is always ``True``: other clients consume
        the same account allowance and that is not observable from here, so the
        returned numbers never claim to be the account's real balance.
        """
        moment = self._now(now)
        rows = self._read(
            'SELECT q.window_id, q.remaining, q.expires_at, '
            '(SELECT COUNT(*) FROM quota_events WHERE status = ?), '
            '(SELECT COUNT(*) FROM quota_events AS e WHERE e.window_id = q.window_id) '
            'FROM (SELECT 1) AS singleton LEFT JOIN quota_snapshot AS q ON q.id = 1',
            (STATUS_RESERVED,))
        row = rows[0]
        local_reserved = int(row[3])
        if row[0] is None:
            return {
                'snapshot_present': False,
                'window_id': None,
                'local_reserved': int(local_reserved),
                'remaining_from_snapshot': None,
                'expires_at': None,
                'expired': None,
                'local_available': 0,
                'external_usage_unknown': True,
                'account_remaining_known': False,
                'account_daily_limit': ACCOUNT_DAILY_LIMIT,
                'note': (
                    'Local ledger only. No confirmed snapshot; the account allowance '
                    'must be confirmed by the host before use.'
                ),
            }
        try:
            window_id, remaining, expires_at = int(row[0]), int(row[1]), float(row[2])
            used = int(row[4])
        except (TypeError, ValueError, OverflowError):
            raise _error(CODE_CORRUPT, 'the stored quota snapshot is corrupt.') from None
        if remaining < 0 or remaining > ACCOUNT_DAILY_LIMIT or not math.isfinite(expires_at):
            raise _error(CODE_CORRUPT, 'the stored quota snapshot is corrupt.')
        remaining_from_snapshot = max(0, remaining - int(used))
        return {
            'snapshot_present': True,
            'window_id': window_id,
            'local_reserved': int(local_reserved),
            'remaining_from_snapshot': remaining_from_snapshot,
            'expires_at': expires_at,
            'expired': expires_at <= moment,
            'local_available': 0 if expires_at <= moment else remaining_from_snapshot,
            'external_usage_unknown': True,
            'account_remaining_known': False,
            'account_daily_limit': ACCOUNT_DAILY_LIMIT,
            'note': (
                'Local ledger only. Other clients may already have consumed part of '
                'the shared account allowance; the real account balance is unknown.'
            ),
        }

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        """Close the ledger.  Safe to call more than once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> 'LuneQuotaStore':
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


__all__ = [
    'ACCOUNT_DAILY_LIMIT',
    'ALLOWED_METHODS',
    'ALLOWED_OUTCOMES',
    'CODE_CLOSED',
    'CODE_CORRUPT',
    'CODE_EXHAUSTED',
    'CODE_EXPIRED',
    'CODE_INVALID_INPUT',
    'CODE_INVALID_METHOD',
    'CODE_INVALID_OUTCOME',
    'CODE_NOT_FOUND',
    'CODE_NO_SNAPSHOT',
    'CODE_PENDING_RESERVED',
    'CODE_UNAVAILABLE',
    'LuneQuotaStore',
]
