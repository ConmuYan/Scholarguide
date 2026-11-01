"""Daily allowance window automation for the local Lune quota ledger.

Why this exists
---------------
Lune's daily allowance is account-wide and there is no public endpoint that
reports the remaining balance. The ledger in :mod:`lune_quota` therefore refuses
to invent a number. This module automates the *window*, not the truth:

* At the configured reset time it opens a new window with the documented daily
  allowance and an expiry of the next reset.
* Every result it writes is marked ``assumption: assume_full_allowance_at_reset``
  and the ledger still reports ``external_usage_unknown = True``.
* ``manual`` mode is the conservative alternative: it never confirms anything,
  it only reports that the window lapsed.

The mapping from "UTC midnight" to a local clock is not observable here either,
so the schedule stores a UTC hour and converts for display only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from common import GuideError
from lune_quota import ACCOUNT_DAILY_LIMIT, LuneQuotaStore, default_quota_path

MODE_ASSUME = 'assume_full_allowance_at_reset'
MODE_MANUAL = 'manual'
MODES = (MODE_ASSUME, MODE_MANUAL)
DEFAULT_UTC_HOUR = 0
CONFIG_NAME = 'lune-quota-schedule.json'


def default_config_path() -> Path:
    configured = os.environ.get('SG_LUNE_QUOTA_SCHEDULE', '').strip()
    if configured:
        return Path(configured).expanduser()
    return Path(default_quota_path()).parent / CONFIG_NAME


def _finite_int(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def read_config(path: Path | None = None) -> dict:
    """Return a validated schedule; a missing file means "disabled"."""
    path = Path(path or default_config_path())
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'version': 1, 'enabled': False, 'mode': MODE_MANUAL,
                'reset_utc_hour': DEFAULT_UTC_HOUR, 'config_path': str(path)}
    if not isinstance(raw, dict):
        return {'version': 1, 'enabled': False, 'mode': MODE_MANUAL,
                'reset_utc_hour': DEFAULT_UTC_HOUR, 'config_path': str(path)}
    mode = raw.get('mode') if raw.get('mode') in MODES else MODE_MANUAL
    hour = raw.get('reset_utc_hour')
    return {'version': 1, 'enabled': bool(raw.get('enabled')), 'mode': mode,
            'reset_utc_hour': hour if _finite_int(hour, 0, 23) else DEFAULT_UTC_HOUR,
            'config_path': str(path)}


def write_config(config: dict, path: Path | None = None) -> Path:
    path = Path(path or default_config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'version': 1, 'enabled': bool(config.get('enabled')),
               'mode': config.get('mode') if config.get('mode') in MODES else MODE_MANUAL,
               'reset_utc_hour': config.get('reset_utc_hour') if _finite_int(config.get('reset_utc_hour'), 0, 23) else DEFAULT_UTC_HOUR}
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=1)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    return path


def next_reset(now: datetime, reset_utc_hour: int = DEFAULT_UTC_HOUR) -> datetime:
    """The next occurrence of the UTC reset hour, strictly after ``now``."""
    moment = now.astimezone(timezone.utc)
    candidate = moment.replace(hour=reset_utc_hour, minute=0, second=0, microsecond=0)
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate


def window_state(status: dict, *, now: datetime | None = None) -> str:
    if not status.get('snapshot_present'):
        return 'missing'
    expires_at = status.get('expires_at')
    moment = (now or datetime.now(timezone.utc)).timestamp()
    if isinstance(expires_at, (int, float)) and expires_at <= moment:
        return 'expired'
    return 'active'


def tick(store: LuneQuotaStore, config: dict, *, now: datetime | None = None) -> dict:
    """Open the next allowance window when due. Never guesses beyond the config.

    Returns a JSON-friendly record describing what happened. A pending
    reservation makes the refresh fail closed: the older window stays intact so
    an in-flight call cannot be forgotten.
    """
    moment = now or datetime.now(timezone.utc)
    status = store.status(now=moment.timestamp())
    state = window_state(status, now=moment)
    record = {'checked_at': moment.isoformat(), 'window': state,
              'enabled': bool(config.get('enabled')), 'mode': config.get('mode', MODE_MANUAL),
              'reset_utc_hour': config.get('reset_utc_hour', DEFAULT_UTC_HOUR),
              'confirmed': False, 'skipped': '', 'local_available': status.get('local_available', 0),
              'external_usage_unknown': True, 'account_remaining_known': False}
    if not config.get('enabled'):
        record['skipped'] = 'schedule_disabled'
        return record
    if config.get('mode') != MODE_ASSUME:
        record['skipped'] = 'manual_mode_requires_review' if state != 'active' else 'window_active'
        return record
    if state == 'active':
        record['skipped'] = 'window_active'
        return record
    reset = next_reset(moment, config.get('reset_utc_hour', DEFAULT_UTC_HOUR))
    try:
        store.confirm(ACCOUNT_DAILY_LIMIT, reset.timestamp(), now=moment.timestamp())
    except GuideError as exc:
        record['skipped'] = getattr(exc, 'code', 'refused')
        record['detail'] = str(exc)
        return record
    after = store.status(now=moment.timestamp())
    record.update(confirmed=True, remaining_from_snapshot=after.get('remaining_from_snapshot'),
                  expires_at=after.get('expires_at'), window='active',
                  assumption=MODE_ASSUME,
                  note=('Window opened with the documented daily allowance at the configured '
                        'UTC reset. Other clients may already have consumed part of it; '
                        'the account balance was not queried.'))
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Lune daily allowance window automation')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--status', action='store_true')
    action.add_argument('--enable', action='store_true')
    action.add_argument('--disable', action='store_true')
    action.add_argument('--run-now', action='store_true')
    parser.add_argument('--mode', choices=MODES, default=MODE_ASSUME)
    parser.add_argument('--reset-utc-hour', type=int, default=DEFAULT_UTC_HOUR)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args(argv)

    config = read_config(args.config)
    if args.enable:
        if not 0 <= args.reset_utc_hour <= 23:
            print('Error: --reset-utc-hour must be 0-23', file=sys.stderr)
            return 2
        config.update(enabled=True, mode=args.mode, reset_utc_hour=args.reset_utc_hour)
        path = write_config(config, args.config)
        print(json.dumps({'enabled': True, 'mode': config['mode'],
                          'reset_utc_hour': config['reset_utc_hour'],
                          'config': str(path)}, indent=1))
        return 0
    if args.disable:
        config.update(enabled=False)
        path = write_config(config, args.config)
        print(json.dumps({'enabled': False, 'config': str(path)}, indent=1))
        return 0
    if args.status:
        with LuneQuotaStore(default_quota_path()) as store:
            status = store.status()
        print(json.dumps({'schedule': {k: config[k] for k in ('enabled', 'mode', 'reset_utc_hour')},
                          'window': window_state(status), 'next_reset': next_reset(
                              datetime.now(timezone.utc), config['reset_utc_hour']).isoformat(),
                          'ledger': status}, indent=1, ensure_ascii=False))
        return 0
    with LuneQuotaStore(default_quota_path()) as store:
        record = tick(store, config)
    print(json.dumps(record, indent=1, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
