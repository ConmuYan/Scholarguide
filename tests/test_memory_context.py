import sqlite3
import subprocess
import sys
from contextlib import closing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_context import MemoryContext, MemoryContextError
from memory_store import MemoryStore


class MemoryContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'preferences.sqlite3'
        self.expiry = time.time() + 3600

    def grant(self, path=None):
        with MemoryStore(path or self.path) as store:
            store.grant('reading_goal', 'orientation', consent=True,
                        expires_at=self.expiry, expected_revision=0)
            store.grant('prefer_recent', True, consent=True,
                        expires_at=self.expiry, expected_revision=0)

    def test_default_missing_and_unapproved(self):
        for enabled in (False, True):
            ctx = MemoryContext.capture(self.path, enabled=enabled)
            self.assertEqual(ctx.hints(explicit_fields=set()), {})
            ctx.revalidate()
            self.assertFalse(self.path.exists())
        with MemoryStore(self.path):
            pass
        self.assertEqual(MemoryContext.capture(self.path, enabled=True).hints(explicit_fields=set()), {})
        self.grant()
        self.assertEqual(MemoryContext.capture(self.path).hints(explicit_fields=set()), {})
        self.assertEqual(MemoryContext.capture(self.path, enabled=1).hints(explicit_fields=set()), {})

    def test_grants_explicit_conflicts_and_mutation(self):
        self.grant()
        ctx = MemoryContext.capture(self.path, enabled=True)
        expected = {'reading_goal': 'orientation', 'prefer_recent': True}
        self.assertEqual(ctx.hints(explicit_fields={'topic'}), expected)
        self.assertEqual(ctx.hints(explicit_fields={'reading_goal', 'year_from'}), {})
        self.assertEqual(ctx.hints(explicit_fields={'purpose', 'prefer_recent'}), {})
        self.assertEqual(ctx.hints(explicit_fields=None), {})
        self.assertEqual(ctx.hints(explicit_fields={'unknown'}), {})
        self.assertEqual(ctx.hints(explicit_fields=['topic']), {})
        result = ctx.hints(explicit_fields=set())
        result.clear()
        self.assertEqual(ctx.hints(explicit_fields=set()), expected)
        self.assertNotIn('orientation', repr(ctx))
        self.assertNotIn(str(self.path), repr(ctx))
        ctx.revalidate()

    def test_isolation(self):
        other = Path(self.tmp.name) / 'other.sqlite3'
        self.grant()
        with MemoryStore(other) as store:
            store.grant('reading_goal', 'comparison', consent=True,
                        expires_at=self.expiry, expected_revision=0)
        a = MemoryContext.capture(self.path, enabled=True)
        b = MemoryContext.capture(other, enabled=True)
        with MemoryStore(self.path) as store:
            store.revoke('reading_goal', expected_revision=1)
        with self.assertRaises(MemoryContextError):
            a.revalidate()
        b.revalidate()
        self.assertEqual(b.hints(explicit_fields=set()), {'reading_goal': 'comparison'})

    def test_changes_fail_closed(self):
        for operation in ('revoke', 'delete', 'update', 'tamper_value', 'tamper_consent', 'missing', 'corrupt', 'expired'):
            with self.subTest(operation=operation):
                path = Path(self.tmp.name) / (operation + '.sqlite3')
                self.grant(path)
                ctx = MemoryContext.capture(path, enabled=True)
                if operation in ('revoke', 'delete', 'update'):
                    with MemoryStore(path) as store:
                        if operation == 'update':
                            store.grant('reading_goal', 'frontier', consent=True,
                                        expires_at=self.expiry, expected_revision=1)
                        else:
                            getattr(store, operation)('reading_goal', expected_revision=1)
                elif operation.startswith('tamper'):
                    with closing(sqlite3.connect(path)) as con, con:
                        con.execute("UPDATE preferences SET " +
                                    ("value='frontier'" if operation == 'tamper_value' else 'consent=0') +
                                    " WHERE key='reading_goal'")
                elif operation == 'missing':
                    path.unlink()
                elif operation == 'corrupt':
                    path.write_bytes(b'not sqlite')
                with patch('memory_store.time.time', return_value=self.expiry if operation == 'expired' else time.time()):
                    with self.assertRaises(MemoryContextError) as error:
                        ctx.revalidate()
                self.assertEqual(str(error.exception), 'Memory context is no longer valid.')
                self.assertNotIn(str(path), str(error.exception))
                with self.assertRaises(MemoryContextError):
                    ctx.hints(explicit_fields=set())
                with self.assertRaises(MemoryContextError):
                    ctx.revalidate()
                if operation == 'missing':
                    self.assertFalse(path.exists())

    def test_observed_expiration_does_not_reverse(self):
        self.grant()
        ctx = MemoryContext.capture(self.path, enabled=True)
        with patch('memory_store.time.time', return_value=self.expiry):
            with self.assertRaises(MemoryContextError):
                ctx.revalidate()
        with patch('memory_store.time.time', return_value=self.expiry - 100):
            with self.assertRaises(MemoryContextError):
                ctx.revalidate()
            self.assertEqual(MemoryContext.capture(self.path, enabled=True).hints(explicit_fields=set()), {})

    def test_barrier_concurrent_revoke(self):
        self.grant()
        ctx = MemoryContext.capture(self.path, enabled=True)
        barrier = threading.Barrier(2)
        errors = []
        def revoke():
            try:
                with MemoryStore(self.path) as store:
                    store.revoke('reading_goal', expected_revision=1)
                barrier.wait(timeout=5)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=revoke)
        thread.start()
        barrier.wait(timeout=5)
        with self.assertRaises(MemoryContextError):
            ctx.revalidate()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_process_update_invalidates(self):
        self.grant()
        ctx = MemoryContext.capture(self.path, enabled=True)
        code = ("import sys; from memory_store import MemoryStore; "
                "s=MemoryStore(sys.argv[1]); "
                "s.revoke('reading_goal', expected_revision=1); s.close()")
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.path)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        with self.assertRaises(MemoryContextError):
            ctx.revalidate()

    def test_unavailable_store_and_no_retry(self):
        self.grant()
        ctx = MemoryContext.capture(self.path, enabled=True)
        with patch('memory_context._snapshot', side_effect=OSError('private path')) as read:
            with self.assertRaises(MemoryContextError) as error:
                ctx.revalidate()
            self.assertEqual(str(error.exception), 'Memory context is no longer valid.')
            with self.assertRaises(MemoryContextError):
                ctx.revalidate()
            self.assertEqual(read.call_count, 1)
        with patch('memory_context._snapshot', side_effect=OSError('private path')):
            self.assertEqual(MemoryContext.capture(self.path, enabled=True).hints(explicit_fields=set()), {})

    def test_close(self):
        self.grant()
        ctx = MemoryContext.capture(self.path, enabled=True)
        ctx.close()
        ctx.close()
        with self.assertRaises(MemoryContextError):
            ctx.hints(explicit_fields=set())
        with self.assertRaises(MemoryContextError):
            ctx.revalidate()


if __name__ == '__main__':
    unittest.main()
