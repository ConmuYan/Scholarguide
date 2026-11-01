#!/usr/bin/env python3
"""One explicitly requested Lune discovery or search using the shared quota ledger.

Writes only aggregate metadata, never credentials, request payloads or remote
error text. A current user-confirmed quota snapshot must already exist.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Budget
from lune_quota import LuneQuotaStore, default_quota_path
from mcp_lune import install, papers_from, structured


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', help='allow one logical request')
    parser.add_argument('--operation', choices=('discover', 'search', 'discover-search'), default='discover')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.execute:
        parser.error('explicit --execute is required')
    if args.output.exists():
        parser.error('output already exists; choose a new evidence file')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with LuneQuotaStore(default_quota_path()) as ledger:
        before = ledger.status()
        if before['local_reserved'] or before['local_available'] < 1:
            raise SystemExit('quota snapshot unavailable or pending; no request was attempted')
        report = {'kind': 'lune_' + args.operation + '_governance_probe', 'logical_requests': 0,
                  'metered_http_attempts_including_oauth': None, 'actual_cost': 'unknown',
                  'provider_account_balance': 'unknown', 'status': 'failed'}
        try:
            budget = Budget(30)
            client = install().client(budget=budget)
            client.budget = budget
            if args.operation in ('discover', 'discover-search'):
                report['logical_requests'] += 1
                result = client.list_tools()
                report['allowed_tool_count'] = len(result)
                if args.operation == 'discover':
                    report['status'] = 'completed'
            if args.operation in ('search', 'discover-search'):
                report['logical_requests'] += 1
                result = client.call_tool('search_papers',
                                          {'query': 'retrieval augmented generation', 'limit': 1})
                papers = papers_from(structured(result))
                report.update(status='completed' if len(papers) == 1 else 'partial',
                              returned_papers=len(papers))
        except Exception as exc:
            kind = str(getattr(exc, 'kind', '') or getattr(exc, 'code', '') or type(exc).__name__)
            report['error_kind'] = kind if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', kind) else 'unknown'
            status = getattr(exc, 'status', None)
            if isinstance(status, int):
                report['http_status'] = status
        finally:
            after = ledger.status()
            if before['window_id'] == after['window_id']:
                report['metered_http_attempts_including_oauth'] = max(
                    0, before['local_available'] - after['local_available'])
            report['local_reserved_after'] = after['local_reserved']
            report['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            with args.output.open('x', encoding='utf-8') as stream:
                json.dump(report, stream, indent=2)
                stream.write('\n')
        print(json.dumps(report))
        return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
