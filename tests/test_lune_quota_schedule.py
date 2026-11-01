"""Daily allowance windows: automation never claims to read the account balance."""
import importlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lune_quota
import lune_quota_schedule as schedule


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / 'schedule.json'
        self.db = Path(self.tmp.name) / 'quota.sqlite3'
        self.store = lune_quota.LuneQuotaStore(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_missing_config_is_disabled_and_manual(self):
        config = schedule.read_config(self.config_path)
        self.assertFalse(config['enabled'])
        self.assertEqual(config['mode'], schedule.MODE_MANUAL)

    def test_enable_writes_private_config_and_round_trips(self):
        path = schedule.write_config({'enabled': True, 'mode': schedule.MODE_ASSUME,
                                      'reset_utc_hour': 7}, self.config_path)
        self.assertEqual(path, self.config_path)
        config = schedule.read_config(self.config_path)
        self.assertTrue(config['enabled'])
        self.assertEqual(config['reset_utc_hour'], 7)
        if sys.platform != 'win32':
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_invalid_values_fall_back_without_crashing(self):
        self.config_path.write_text('{"enabled":"yes","mode":"nonsense","reset_utc_hour":99}',
                                    encoding='utf-8')
        config = schedule.read_config(self.config_path)
        self.assertTrue(config['enabled'])
        self.assertEqual(config['mode'], schedule.MODE_MANUAL)
        self.assertEqual(config['reset_utc_hour'], 0)

    def test_next_reset_is_strictly_in_the_future(self):
        moment = datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(schedule.next_reset(moment, 0), datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(schedule.next_reset(moment, 6), datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))

    def test_disabled_schedule_and_manual_mode_never_confirm(self):
        disabled = {'enabled': False, 'mode': schedule.MODE_ASSUME, 'reset_utc_hour': 0}
        record = schedule.tick(self.store, disabled)
        self.assertFalse(record['confirmed'])
        self.assertEqual(record['skipped'], 'schedule_disabled')
        manual = {'enabled': True, 'mode': schedule.MODE_MANUAL, 'reset_utc_hour': 0}
        record = schedule.tick(self.store, manual)
        self.assertEqual(record['skipped'], 'manual_mode_requires_review')
        self.assertFalse(self.store.status()['snapshot_present'])

    def test_assume_mode_opens_one_window_and_is_idempotent(self):
        config = {'enabled': True, 'mode': schedule.MODE_ASSUME, 'reset_utc_hour': 0}
        first = schedule.tick(self.store, config)
        self.assertTrue(first['confirmed'], first)
        self.assertEqual(first['assumption'], schedule.MODE_ASSUME)
        self.assertTrue(first['external_usage_unknown'])
        self.assertFalse(first['account_remaining_known'])
        status = self.store.status()
        self.assertEqual(status['remaining_from_snapshot'], lune_quota.ACCOUNT_DAILY_LIMIT)
        second = schedule.tick(self.store, config)
        self.assertFalse(second['confirmed'])
        self.assertEqual(second['skipped'], 'window_active')

    def test_expired_window_reopens_after_reset(self):
        config = {'enabled': True, 'mode': schedule.MODE_ASSUME, 'reset_utc_hour': 0}
        later = datetime.now(timezone.utc) + timedelta(days=2)
        record = schedule.tick(self.store, config, now=later)
        self.assertTrue(record['confirmed'], record)
        self.assertGreater(record['expires_at'], later.timestamp())

    def test_pending_reservation_blocks_refresh(self):
        config = {'enabled': True, 'mode': schedule.MODE_ASSUME, 'reset_utc_hour': 0}
        self.assertTrue(schedule.tick(self.store, config)['confirmed'])
        self.store.reserve('search_papers')
        later = datetime.now(timezone.utc) + timedelta(days=2)
        record = schedule.tick(self.store, config, now=later)
        self.assertFalse(record['confirmed'])
        self.assertEqual(record['skipped'], lune_quota.CODE_PENDING_RESERVED)

    def test_module_never_reads_the_network(self):
        source = (ROOT / 'lune_quota_schedule.py').read_text(encoding='utf-8')
        for banned in ('urlopen', 'requests', 'http.client', 'socket'):
            self.assertNotIn(banned, source)
        self.assertEqual(set(schedule.MODES), {schedule.MODE_ASSUME, schedule.MODE_MANUAL})


if __name__ == '__main__':
    unittest.main()
