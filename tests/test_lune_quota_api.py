"""Local quota HTTP contract; no source or model request is made."""
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from lune_quota import LuneQuotaStore
from source_store import SourceStore
from webapp import Jobs, create_server


class LuneQuotaApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.quota_path = self.root / 'quota.sqlite3'
        self.patch = patch('lune_quota.default_quota_path', return_value=self.quota_path)
        self.patch.start()
        self.sources = SourceStore(self.root / 'sources.sqlite3')
        self.server = create_server(0, jobs=Jobs(output=self.root / 'runs'), sources=self.sources)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.sources.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, payload=None):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Host': f'127.0.0.1:{self.server.server_port}'}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        body = json.loads(response.read().decode())
        conn.close()
        return response.status, body

    def test_status_is_local_and_confirmation_requires_acknowledgement(self):
        with patch('mcp_client.urlopen', side_effect=AssertionError('network must not run')):
            status, value = self.request('GET', '/api/v1/sources/lune/quota')
            self.assertEqual(status, 200)
            self.assertFalse(value['snapshot_present'])
            self.assertTrue(value['external_usage_unknown'])
            status, _ = self.request('POST', '/api/sources/lune/quota',
                                     {'remaining': 2, 'expires_at': time.time() + 300})
            self.assertEqual(status, 400)
            status, value = self.request('POST', '/api/sources/lune/quota', {
                'remaining': 2, 'expires_at': time.time() + 300, 'acknowledged': True})
            self.assertEqual(status, 200)
            self.assertEqual(value['remaining_from_snapshot'], 2)
            self.assertFalse(value['account_remaining_known'])

    def test_legacy_slot_recovery_is_local_and_requires_confirmation(self):
        with self.sources._db() as db:
            db.execute('INSERT INTO source_inflight(token,name,expires_at) VALUES(?,?,?)',
                       ('legacy-fixture', 'lune', 0))
        with patch('mcp_client.urlopen', side_effect=AssertionError('network must not run')):
            status, value = self.request('GET', '/api/v1/sources/lune/inflight/legacy')
            self.assertEqual(status, 200)
            self.assertEqual(value['legacy'], 1)
            status, _ = self.request('POST', '/api/sources/lune/inflight/legacy', {'acknowledged': True})
            self.assertEqual(status, 400)
            status, value = self.request('POST', '/api/sources/lune/inflight/legacy',
                                         {'acknowledged': True, 'legacy_clients_stopped': True})
            self.assertEqual(status, 200)
            self.assertEqual(value['removed'], 1)
            self.assertTrue(value['remote_outcome_unknown'])

    def test_direct_custom_quota_mismatch_rejected_before_dispatch(self):
        from common import Budget, GuideError
        other = SourceStore(self.root / 'other-sources.sqlite3',
                            quota_path=self.root / 'other-quota.sqlite3')
        try:
            with patch.object(other, '_ensure_ready', side_effect=AssertionError('must not dispatch')):
                with self.assertRaises(GuideError):
                    other._call_lune_reserved('search_papers', {'query': 'fixture'}, None, Budget(2))
        finally:
            other.close()

    def test_pending_recovery_is_explicit_and_never_refunds(self):
        self.request('POST', '/api/sources/lune/quota', {
            'remaining': 2, 'expires_at': time.time() + 300, 'acknowledged': True})
        store = LuneQuotaStore(self.quota_path)
        try:
            reservation = store.reserve('tools/call')
        finally:
            store.close()
        status, value = self.request('GET', '/api/sources/lune/quota')
        self.assertEqual(status, 200)
        self.assertEqual(value['pending'][0]['id'], reservation)
        status, _ = self.request('POST', '/api/sources/lune/quota/reconcile',
                                 {'id': reservation})
        self.assertEqual(status, 400)
        status, _ = self.request('POST', '/api/sources/lune/quota/reconcile',
                                 {'id': reservation, 'acknowledged': True})
        self.assertEqual(status, 400)  # This process still owns the reservation.
        with patch('lune_quota._process_alive', return_value=False):
            status, value = self.request('POST', '/api/sources/lune/quota/reconcile',
                                         {'id': reservation, 'acknowledged': True})
        self.assertEqual(status, 200)
        self.assertEqual(value['pending'], [])
        self.assertEqual(value['remaining_from_snapshot'], 1)


if __name__ == '__main__':
    unittest.main()
