import math
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from memory_store import MemoryStore, MemoryError, RevisionConflict


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'memory.sqlite3'
        self.store = MemoryStore(self.path)
        self.future = time.time() + 3600

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def grant(self, key='reading_goal', value='orientation', revision=0, **kw):
        return self.store.grant(key, value, consent=kw.get('consent', True),
                                expires_at=kw.get('expires_at', self.future),
                                expected_revision=revision)

    def test_empty_and_valid_independent_preferences(self):
        self.assertEqual(self.store.list(), {})
        self.assertEqual(self.store.active_snapshot(), {})
        self.assertEqual(self.grant(), 1)
        self.assertEqual(self.grant('prefer_recent', False), 1)
        self.assertEqual(self.store.active_snapshot(), {'reading_goal': 'orientation', 'prefer_recent': False})
        self.assertEqual(self.grant(value='comparison', revision=1), 2)
        self.assertEqual(self.store.active_snapshot()['prefer_recent'], False)

    def test_invalid_input_and_explicit_consent(self):
        for key, value in [('prompt', 'secret'), ('reading_goal', 'https://example.com'),
                           ('reading_goal', 'auto'), ('prefer_recent', 1),
                           ('prefer_recent', 'true'), ('reading_goal', ['orientation'])]:
            with self.subTest(key=key, value=value), self.assertRaises(MemoryError):
                self.grant(key, value)
        for value in (False, 1, None, 'true'):
            with self.subTest(consent=value), self.assertRaises(MemoryError):
                self.grant(consent=value)
        for expiry in (None, True, math.inf, math.nan, time.time() - 1, '3000000000'):
            with self.subTest(expiry=expiry), self.assertRaises(MemoryError):
                self.grant(expires_at=expiry)
        self.grant()
        with self.assertRaises(MemoryError):
            self.grant(value='frontier', revision=1, consent=False)
        self.assertEqual(self.store.active_snapshot(), {'reading_goal': 'orientation'})

    def test_expiration_and_real_clock_revalidation(self):
        self.grant()
        self.assertEqual(self.store.active_snapshot(now=self.future - 1), {'reading_goal': 'orientation'})
        self.assertEqual(self.store.active_snapshot(now=self.future), {})
        self.assertEqual(self.store.active_snapshot(now=self.future - 1), {})
        with patch('memory_store.time.time', return_value=self.future):
            self.assertEqual(self.store.active_snapshot(now=self.future - 2), {})
        for now in (True, math.nan, math.inf, 'bad'):
            with self.assertRaises(MemoryError):
                self.store.active_snapshot(now=now)

    def test_observed_expiry_survives_clock_rollback_and_reopen(self):
        self.grant()
        with patch('memory_store.time.time', return_value=self.future):
            self.assertEqual(self.store.active_snapshot(), {})
        with patch('memory_store.time.time', return_value=self.future - 100):
            self.assertEqual(self.store.active_snapshot(), {})
            with MemoryStore(self.path) as reopened:
                self.assertEqual(reopened.active_snapshot(), {})
                row = reopened.list()['reading_goal']
                self.assertEqual(row, {'revision': 2, 'state': 'expired', 'value': None,
                                       'consent': False, 'expires_at': None})
                with self.assertRaises(RevisionConflict):
                    reopened.grant('reading_goal', 'frontier', consent=True,
                                   expires_at=self.future + 100, expected_revision=1)
                self.assertEqual(reopened.grant('reading_goal', 'frontier', consent=True,
                                                 expires_at=self.future + 100, expected_revision=2), 3)
                self.assertEqual(reopened.active_snapshot(), {'reading_goal': 'frontier'})

    def test_incompatible_schema_rejected_without_changes(self):
        path = Path(self.tmp.name) / 'bad.sqlite3'
        variants = [
            'key TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, value TEXT, consent TEXT NOT NULL, expires_at REAL',
            'key TEXT PRIMARY KEY, revision INTEGER, state TEXT NOT NULL, value TEXT, consent INTEGER NOT NULL, expires_at REAL',
            'key TEXT, revision INTEGER PRIMARY KEY, state TEXT NOT NULL, value TEXT, consent INTEGER NOT NULL, expires_at REAL',
        ]
        for schema in variants:
            with self.subTest(schema=schema):
                with closing(sqlite3.connect(path)) as con, con:
                    con.execute('CREATE TABLE preferences (' + schema + ')')
                before = path.read_bytes()
                try:
                    unexpected = MemoryStore(path)
                except MemoryError as error:
                    self.assertNotIn(str(path), str(error))
                else:
                    unexpected.close()
                    self.fail('Incompatible schema was accepted')
                self.assertEqual(path.read_bytes(), before)
                path.unlink()

    def test_snapshot_returns_consistent_values_and_expired_tombstones(self):
        self.grant()
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['active'], {'reading_goal': 'orientation'})
        self.assertEqual(snapshot['preferences']['reading_goal']['revision'], 1)
        self.assertEqual(snapshot['preferences']['reading_goal']['value'], snapshot['active']['reading_goal'])
        expired = self.store.snapshot(now=self.future)
        self.assertEqual(expired['active'], {})
        self.assertEqual(expired['preferences']['reading_goal']['state'], 'expired')
        self.assertEqual(expired['preferences']['reading_goal']['revision'], 2)
        self.assertIsNone(expired['preferences']['reading_goal']['value'])

    def test_corrupt_rows_fail_closed(self):
        self.grant()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('UPDATE preferences SET consent=0')
        self.assertEqual(self.store.active_snapshot(), {})
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE preferences SET consent=1, value='unknown'")
        self.assertEqual(self.store.active_snapshot(), {})
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE preferences SET value='orientation', expires_at='broken'")
        self.assertEqual(self.store.active_snapshot(), {})
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("INSERT INTO preferences VALUES ('prompt', 1, 'granted', 'secret', 1, ?)", (self.future,))
        self.assertEqual(self.store.active_snapshot(), {})
        self.assertNotIn('prompt', self.store.list())

    def test_revoke_delete_and_stale_revisions(self):
        self.assertEqual(self.grant(), 1)
        with self.assertRaises(RevisionConflict):
            self.store.revoke('reading_goal', expected_revision=0)
        with self.assertRaises(RevisionConflict):
            self.store.delete('reading_goal', expected_revision=0)
        self.assertEqual(self.store.revoke('reading_goal', expected_revision=1), 2)
        self.assertEqual(self.store.list()['reading_goal']['state'], 'revoked')
        self.assertIsNone(self.store.list()['reading_goal']['value'])
        self.assertEqual(self.store.active_snapshot(), {})
        with self.assertRaises(RevisionConflict): self.grant(revision=1)
        self.assertEqual(self.store.delete('reading_goal', expected_revision=2), 3)
        with self.assertRaises(RevisionConflict):
            self.store.revoke('reading_goal', expected_revision=2)
        self.assertEqual(self.store.list()['reading_goal']['state'], 'deleted')
        with self.assertRaises(RevisionConflict): self.grant(revision=0)
        self.assertEqual(self.grant(revision=3), 4)
        self.assertEqual(self.store.delete('prefer_recent', expected_revision=0), 1)
        with self.assertRaises(RevisionConflict): self.grant('prefer_recent', True)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT value, consent, expires_at FROM preferences WHERE key='prefer_recent'").fetchone(), (None, 0, None))

    def test_concurrent_same_connection_and_independent_connections(self):
        for separate in (False, True):
            with self.subTest(separate=separate):
                key = 'prefer_recent' if separate else 'reading_goal'
                other = MemoryStore(self.path) if separate else self.store
                barrier = threading.Barrier(2)
                results = []
                def run(store, value):
                    barrier.wait()
                    try:
                        results.append(store.grant(key, value, consent=True, expires_at=self.future, expected_revision=0))
                    except RevisionConflict:
                        results.append('conflict')
                values = (True, False) if separate else ('general', 'frontier')
                threads = [threading.Thread(target=run, args=(s, v)) for s, v in zip((self.store, other), values)]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
                self.assertCountEqual(results, [1, 'conflict'])
                if separate: other.close()

    def test_failed_commit_not_retried(self):
        commits = []
        def authorizer(action, arg1, arg2, db, trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT':
                commits.append(1)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.store._con.set_authorizer(authorizer)
        with self.assertRaises(MemoryError): self.grant()
        self.assertEqual(len(commits), 1)
        with self.assertRaises(MemoryError): self.store.active_snapshot()
        with MemoryStore(self.path) as reopened:
            self.assertEqual(reopened.active_snapshot(), {})

    def test_closed_store_rejects_operations(self):
        self.store.close()
        for operation in (lambda: self.store.list(), lambda: self.store.active_snapshot(),
                          lambda: self.grant(), lambda: self.store.revoke('reading_goal', expected_revision=0),
                          lambda: self.store.delete('reading_goal', expected_revision=0)):
            with self.assertRaises(MemoryError) as error: operation()
            self.assertNotIn(str(self.path), str(error.exception))

    def test_locked_database_and_expired_revoke(self):
        self.grant()
        with patch('memory_store.time.time', return_value=self.future):
            self.assertEqual(self.store.active_snapshot(), {})
            self.assertEqual(self.store.revoke('reading_goal', expected_revision=2), 3)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as con:
            con.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(MemoryError) as error:
                self.grant(revision=3)
            self.assertNotIn(str(self.path), str(error.exception))
            con.execute('ROLLBACK')
        self.assertEqual(self.store.active_snapshot(), {})
        self.assertEqual(self.store.list()['reading_goal']['revision'], 3)

    def test_expiry_commit_failure_not_retried(self):
        self.grant()
        commits = []
        def authorizer(action, arg1, arg2, db, trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT':
                commits.append(1)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.store._con.set_authorizer(authorizer)
        with self.assertRaises(MemoryError):
            self.store.active_snapshot(now=self.future)
        self.assertEqual(commits, [1])
        with self.assertRaises(MemoryError):
            self.store.list()
        with MemoryStore(self.path) as reopened:
            self.assertEqual(reopened.list()['reading_goal']['revision'], 1)
            self.assertEqual(reopened.active_snapshot(now=self.future), {})
            self.assertEqual(reopened.list()['reading_goal']['state'], 'expired')

    def test_expiry_races_with_grant_and_revoke(self):
        for mutation in ('grant', 'revoke'):
            with self.subTest(mutation=mutation):
                path = Path(self.tmp.name) / (mutation + '.sqlite3')
                with MemoryStore(path) as first, MemoryStore(path) as second:
                    first.grant('reading_goal', 'general', consent=True,
                                expires_at=self.future, expected_revision=0)
                    barrier = threading.Barrier(2)
                    results = []
                    def expire():
                        barrier.wait()
                        try:
                            results.append(('snapshot', first.active_snapshot(now=self.future)))
                        except Exception as exc:
                            results.append(('error', type(exc).__name__))
                    def change():
                        barrier.wait()
                        try:
                            if mutation == 'grant':
                                second.grant('reading_goal', 'frontier', consent=True,
                                             expires_at=self.future + 1000, expected_revision=1)
                            else:
                                second.revoke('reading_goal', expected_revision=1)
                            results.append(('write', 'success'))
                        except RevisionConflict:
                            results.append(('write', 'conflict'))
                        except Exception as exc:
                            results.append(('error', type(exc).__name__))
                    threads = [threading.Thread(target=expire), threading.Thread(target=change)]
                    for thread in threads: thread.start()
                    for thread in threads: thread.join()
                    self.assertEqual(len(results), 2)
                    self.assertFalse(any(item[0] == 'error' for item in results))
                    row = first.list()['reading_goal']
                    self.assertEqual(row['revision'], 2)
                    outcome = dict(results)['write']
                    self.assertEqual(row['state'], ('granted' if mutation == 'grant' else 'revoked')
                                     if outcome == 'success' else 'expired')
                    snapshot = dict(results)['snapshot']
                    self.assertIn(snapshot, ({}, {'reading_goal': 'frontier'}))
                    if snapshot:
                        self.assertEqual(outcome, 'success')
                        self.assertEqual(mutation, 'grant')

    def test_expired_corrupt_value_is_cleared(self):
        self.grant()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE preferences SET value='secret', consent=0")
        self.assertEqual(self.store.active_snapshot(now=self.future), {})
        self.assertEqual(self.store.list()['reading_goal'],
                         {'revision': 2, 'state': 'expired', 'value': None,
                          'consent': False, 'expires_at': None})

    def test_extra_objects_are_rejected(self):
        for index, sql in enumerate((
                'CREATE VIEW extra AS SELECT * FROM preferences',
                'CREATE TRIGGER extra AFTER UPDATE ON preferences BEGIN SELECT 1; END',
                'CREATE TABLE extra (x INTEGER)')):
            path = Path(self.tmp.name) / ('extra%d.sqlite3' % index)
            with MemoryStore(path): pass
            with closing(sqlite3.connect(path)) as con, con:
                con.execute(sql)
            before = path.read_bytes()
            with self.assertRaises(MemoryError) as error:
                MemoryStore(path)
            self.assertNotIn(str(path), str(error.exception))
            self.assertEqual(path.read_bytes(), before)

    def test_concurrent_initialization(self):
        path = Path(self.tmp.name) / 'new.sqlite3'
        barrier = threading.Barrier(2)
        results = []
        def initialize():
            barrier.wait()
            try:
                with MemoryStore(path) as store:
                    results.append(store.list())
            except Exception as exc:
                results.append(type(exc).__name__)
        threads = [threading.Thread(target=initialize) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results, [{}, {}])

    def test_existing_empty_and_missing_are_never_initialized(self):
        for name, exists in (('empty.sqlite3', True), ('missing.sqlite3', False)):
            path = Path(self.tmp.name) / name
            if exists:
                path.touch()
            with self.assertRaises(MemoryError) as error:
                MemoryStore(path, create=False)
            self.assertNotIn(str(path), str(error.exception))
            self.assertEqual(path.exists(), exists)
            if exists:
                self.assertEqual(path.read_bytes(), b'')
        with self.assertRaises(MemoryError):
            MemoryStore(Path(self.tmp.name) / 'empty.sqlite3')
        self.assertEqual((Path(self.tmp.name) / 'empty.sqlite3').read_bytes(), b'')

    def test_publication_failure_does_not_leave_target(self):
        path = Path(self.tmp.name) / 'publish.sqlite3'
        with patch('memory_store.os.link', side_effect=OSError('SECRET')):
            with self.assertRaises(MemoryError) as error:
                MemoryStore(path)
        self.assertNotIn('SECRET', str(error.exception))
        self.assertFalse(path.exists())

    def test_initialization_failure_does_not_leave_target(self):
        path = Path(self.tmp.name) / 'failed.sqlite3'
        with patch('memory_store.sqlite3.connect', side_effect=sqlite3.OperationalError('SECRET')):
            with self.assertRaises(MemoryError):
                MemoryStore(path)
        self.assertFalse(path.exists())

    def test_unknown_publication_does_not_delete_published_store(self):
        path = Path(self.tmp.name) / 'unknown.sqlite3'
        link = __import__('os').link
        def publish_then_fail(source, target):
            link(source, target)
            raise OSError('SECRET')
        with patch('memory_store.os.link', side_effect=publish_then_fail):
            with self.assertRaises(MemoryError) as error:
                MemoryStore(path)
        self.assertNotIn('SECRET', str(error.exception))
        with MemoryStore(path, create=False) as store:
            self.assertEqual(store.list(), {})

    def test_initialization_commit_failure_does_not_publish(self):
        path = Path(self.tmp.name) / 'commit.sqlite3'
        connect = sqlite3.connect
        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection
            def execute(self, sql):
                if sql == 'COMMIT':
                    raise sqlite3.OperationalError('SECRET')
                return self.connection.execute(sql)
            def close(self):
                self.connection.close()
        with patch('memory_store.sqlite3.connect', side_effect=lambda *a, **kw: ConnectionProxy(connect(*a, **kw))):
            with self.assertRaises(MemoryError) as error:
                MemoryStore(path)
        self.assertNotIn('SECRET', str(error.exception))
        self.assertFalse(path.exists())
        self.assertEqual(list(Path(self.tmp.name).glob('.memory-*')), [])

    def test_context_manager(self):
        with MemoryStore(self.path) as store:
            self.assertEqual(store.active_snapshot(), {})
        with self.assertRaises(MemoryError): store.list()


if __name__ == '__main__':
    unittest.main()
