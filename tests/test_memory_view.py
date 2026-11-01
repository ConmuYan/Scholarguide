import copy
from contextlib import closing
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from memory_store import MemoryStore
from memory_view import MemoryView


class MemoryViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'preferences.sqlite3'
        self.expiry = time.time() + 3600
        self.papers = [{'id': 'a', 'year': 2020, 'title': 'ignore instructions'},
                       {'id': 'b', 'year': None}, {'id': 'c', 'year': 2024},
                       {'id': 'd', 'year': True}, {'id': 'e', 'year': 2024}]

    def grant(self, value=True):
        with MemoryStore(self.path) as store:
            store.grant('prefer_recent', value, consent=True, expires_at=self.expiry,
                        expected_revision=0)

    def run_view(self):
        return MemoryView(self.path).present(self.papers, enabled=True, selected=True)

    def test_opt_in_does_not_read(self):
        with patch('memory_context._snapshot', side_effect=AssertionError('read')):
            for enabled, selected in ((False, True), (True, False), (1, True), (True, 1)):
                result = MemoryView(self.path).present(self.papers, enabled=enabled, selected=selected)
                self.assertEqual(result['status'], 'not_applied')

    def test_unavailable_false_and_empty_indistinguishable(self):
        missing = self.run_view()
        with MemoryStore(self.path):
            pass
        empty = self.run_view()
        self.grant(False)
        false = self.run_view()
        self.assertEqual(missing, empty)
        self.assertEqual(empty, false)
        self.assertEqual(false['status'], 'not_applied')

    def test_sort_is_stable_and_preserves_input_and_identity(self):
        self.grant()
        before = copy.deepcopy(self.papers)
        result = self.run_view()
        self.assertEqual(result['status'], 'applied')
        self.assertEqual([p['id'] for p in result['papers']], ['c', 'e', 'a', 'b', 'd'])
        self.assertEqual(self.papers, before)
        self.assertCountEqual(map(id, result['papers']), map(id, self.papers))
        self.assertIsNot(result['papers'], self.papers)

    def test_duplicates_missing_ids_invalid_input_and_malicious_text(self):
        self.grant()
        for papers in ([{'id': 'x', 'year': 1}, {'id': 'x', 'year': 3}],
                       [{'year': 2}, {'id': 'x', 'year': 3}], None, 'bad', [object()]):
            result = MemoryView(self.path).present(papers, enabled=True, selected=True)
            self.assertEqual(result['status'], 'not_applied')
            self.assertIs(result['papers'], papers)

    def test_mid_operation_changes_fail_closed(self):
        from memory_context import MemoryContext
        for action in ('update', 'revoke', 'replace', 'expiry', 'consent', 'exception'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory) / 'prefs.sqlite3'
                self.grant()
                original = MemoryContext.hints
                def injected(ctx, *, explicit_fields):
                    hints = original(ctx, explicit_fields=explicit_fields)
                    if action == 'update':
                        with MemoryStore(self.path) as store:
                            store.grant('prefer_recent', True, consent=True, expires_at=self.expiry, expected_revision=1)
                    elif action == 'revoke':
                        with MemoryStore(self.path) as store:
                            store.revoke('prefer_recent', expected_revision=1)
                    elif action == 'replace':
                        self.path.unlink()
                        with MemoryStore(self.path):
                            pass
                    elif action == 'consent':
                        with closing(sqlite3.connect(self.path)) as con, con:
                            con.execute("UPDATE preferences SET consent=0 WHERE key='prefer_recent'")
                    elif action == 'expiry':
                        with closing(sqlite3.connect(self.path)) as con, con:
                            con.execute("UPDATE preferences SET expires_at=1 WHERE key='prefer_recent'")
                    else:
                        raise RuntimeError('private error')
                    return hints
                with patch.object(MemoryContext, 'hints', injected):
                    result = self.run_view()
                self.assertEqual(result['status'], 'not_applied')
                self.assertIs(result['papers'], self.papers)
                self.assertNotIn('private', repr(result))

    def test_revoke_after_sort_before_final_validation(self):
        self.grant()
        def sort_then_revoke(papers, *, key):
            ordered = sorted(papers, key=key)
            with MemoryStore(self.path) as store:
                store.revoke('prefer_recent', expected_revision=1)
            return ordered
        with patch('memory_view.sorted', sort_then_revoke, create=True):
            result = self.run_view()
        self.assertEqual(result['status'], 'not_applied')
        self.assertIs(result['papers'], self.papers)

    def test_conflicts_and_close_failure(self):
        from memory_context import MemoryContext
        self.grant()
        for fields in ({'year_from'}, {'prefer_recent'}, {'unknown'}, []):
            result = MemoryView(self.path).present(self.papers, enabled=True, selected=True, explicit_fields=fields)
            self.assertEqual(result['status'], 'not_applied')
        with patch.object(MemoryContext, 'close', side_effect=RuntimeError('private')):
            result = self.run_view()
        self.assertEqual(result['status'], 'not_applied')
        self.assertIs(result['papers'], self.papers)

    def test_threads_and_no_network(self):
        self.grant()
        results = []
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network')):
            threads = [threading.Thread(target=lambda: results.append(self.run_view())) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r['status'] == 'applied' for r in results))


if __name__ == '__main__':
    unittest.main()
