import os
import sqlite3
import threading
from contextlib import closing
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from memory_context import MemoryContext, MemoryContextError
from memory_store import MemoryStore, MemoryError as StoreError
from tests.test_memory_api import BASE, MemoryApiTests


class MemoryIdentityTests(MemoryApiTests):
    def test_replaced_store_rejects_stale_api_write(self):
        self.db.parent.mkdir(parents=True)
        with MemoryStore(self.db) as store:
            store.grant('reading_goal', 'general', consent=True,
                        expires_at=time.time() + 3600, expected_revision=0)
        status, snapshot = self.request('GET')
        self.assertEqual(status, 200)
        other = self.root / 'other.sqlite3'
        with MemoryStore(other) as store:
            store.grant('reading_goal', 'frontier', consent=True,
                        expires_at=time.time() + 3600, expected_revision=0)
        os.replace(other, self.db)
        for suffix in ('', '/revoke', '/delete'):
            body = {'expected_revision': 1}
            if not suffix:
                body.update(value='comparison', consent=True, expires_at=time.time() + 3600)
            if 'store_id' in snapshot:
                body['store_id'] = snapshot['store_id']
            status, _ = self.request('POST', BASE + '/reading_goal' + suffix, body)
            self.assertEqual(status, 409)
        with MemoryStore(self.db, create=False) as store:
            self.assertEqual(store.active_snapshot()['reading_goal'], 'frontier')

    def test_old_client_and_missing_store_fail_closed(self):
        status, snapshot = self.request('GET')
        self.assertEqual(status, 200)
        self.assertEqual(snapshot['preferences'], {})
        body = {'value': 'general', 'consent': True, 'expires_at': time.time() + 3600,
                'expected_revision': 0}
        self.assertEqual(self.request('POST', BASE + '/reading_goal', body)[0], 409)
        self.db.unlink()
        with MemoryStore(self.db):
            pass
        body['store_id'] = snapshot['store_id']
        self.assertEqual(self.request('POST', BASE + '/reading_goal', body)[0], 409)
        with MemoryStore(self.db) as store:
            self.assertEqual(store.list(), {})

    def test_legacy_nocase_schema_is_rejected_without_migration_or_mutation(self):
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE preferences (key TEXT PRIMARY KEY COLLATE NOCASE, revision INTEGER NOT NULL, state TEXT NOT NULL, value TEXT, consent INTEGER NOT NULL, expires_at REAL)')
            con.execute("INSERT INTO preferences VALUES ('READING_GOAL', 1, 'granted', 'general', 1, 9999999999)")
        before = self.db.read_bytes()
        with self.assertRaises(StoreError):
            with MemoryStore(self.db, create=False):
                pass
        self.assertEqual(self.db.read_bytes(), before)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT key, value FROM preferences').fetchone(), ('READING_GOAL', 'general'))
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='memory_metadata'").fetchone())

    def test_concurrent_legacy_migration_preserves_records(self):
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE preferences (key TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, value TEXT, consent INTEGER NOT NULL, expires_at REAL)')
            con.execute('INSERT INTO preferences VALUES (?, ?, ?, ?, ?, ?)',
                        ('reading_goal', 1, 'granted', 'general', 1, time.time() + 3600))
        barrier = threading.Barrier(4)
        def open_store(_):
            barrier.wait()
            with MemoryStore(self.db, create=False) as store:
                return store.snapshot()
        with ThreadPoolExecutor(max_workers=4) as pool:
            snapshots = list(pool.map(open_store, range(4)))
        self.assertEqual(len({s['store_id'] for s in snapshots}), 1)
        self.assertTrue(all(s['active'] == {'reading_goal': 'general'} for s in snapshots))

    def test_invalid_metadata_and_locked_legacy_preserve_evidence(self):
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE preferences (key TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, value TEXT, consent INTEGER NOT NULL, expires_at REAL)')
            con.execute("INSERT INTO preferences VALUES ('reading_goal', 1, 'granted', 'general', 1, 9999999999)")
        with closing(sqlite3.connect(self.db, isolation_level=None)) as lock:
            lock.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(StoreError):
                MemoryStore(self.db, create=False)
            lock.execute('ROLLBACK')
        with closing(sqlite3.connect(self.db)) as con, con:
            self.assertEqual(con.execute('SELECT value FROM preferences').fetchone(), ('general',))
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='memory_metadata'").fetchone())
            con.execute('CREATE TABLE memory_metadata (store_id TEXT NOT NULL)')
            con.execute("INSERT INTO memory_metadata VALUES ('broken')")
        before = self.db.read_bytes()
        with self.assertRaises(StoreError):
            MemoryStore(self.db, create=False)
        self.assertEqual(self.db.read_bytes(), before)

    def test_context_rejects_same_inode_different_store(self):
        expiry = time.time() + 3600
        self.db.parent.mkdir(parents=True)
        other = self.root / 'other.sqlite3'
        for path in (self.db, other):
            with MemoryStore(path) as store:
                store.grant('reading_goal', 'general', consent=True,
                            expires_at=expiry, expected_revision=0)
        with patch('memory_context._identity', return_value=(1, 1)):
            context = MemoryContext.capture(self.db, enabled=True)
            self.assertEqual(context.hints(explicit_fields=set()), {'reading_goal': 'general'})
            os.replace(other, self.db)
            with self.assertRaises(MemoryContextError):
                context.revalidate()
