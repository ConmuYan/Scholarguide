"""Confirm the real Lune payload shape before trusting the adapter.

Runs three explicit steps: discovery, list_conferences and search_papers.
Every transport attempt, including discovery and OAuth refresh, is conservatively
metered in the shared quota ledger. Real provider cost and balance are unknown.
Writes only new evidence files and never prints the key.

Run: C:/Python314/python.exe scripts/lune_probe.py [--max-requests 5]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcp_client                                   # noqa: E402
import mcp_lune                                     # noqa: E402

OUT_DIR = ROOT / 'runs' / 'verification' / 'lune-probes'
SAFE_SHAPE_KEYS = frozenset({'papers', 'results', 'items', 'data', 'hits', 'documents',
                             'paper_id', 'id', 'title', 'authors', 'year', 'doi', 'url',
                             'abstract', 'summary', 'conference', 'venue', 'score',
                             'citation_count', 'has_more', 'best_score', 'low_confidence'})


def _shape(value, depth=0):
    """Keys and types only — enough to validate the mapping, no corpus content."""
    if depth > 2:
        return type(value).__name__
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:64]
                if k in SAFE_SHAPE_KEYS}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='probe the Lune MCP endpoint')
    parser.add_argument('--max-requests', type=int, default=5)
    parser.add_argument('--query', default='retrieval augmented generation')
    parser.add_argument('--output', type=Path, help='new evidence file; existing files are never replaced')
    args = parser.parse_args(argv)

    output = args.output or OUT_DIR / f'lune-probe-{time.time_ns()}-{os.getpid()}.json'
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        evidence = output.open('x', encoding='utf-8')
    except OSError:
        print(json.dumps({'ok': False, 'error': 'evidence_file_unavailable'}))
        return 2

    report = {'endpoint': 'https://mcp.luneresearch.com', 'steps': {}, 'ok': False,
              'provider_balance': 'unknown', 'actual_cost': 'unknown',
              'oauth_usage': 'see shared local quota ledger'}
    client = None
    status = 1
    with evidence:
        try:
            token = mcp_lune.access_token()
            report['credential'] = ('oauth-token-file' if token else
                                    ('env-access-key' if os.getenv('LUNE_API_KEY') else 'none'))
            if report['credential'] == 'none':
                report['blocked'] = 'No Lune credential configured.'
                status = 2
            else:
                provider = mcp_lune.LuneProvider(max_requests=args.max_requests)
                client = provider.client()
                tools = client.list_tools()
                report['steps']['tools_list'] = {'count': len(tools)}
                venues = mcp_client.structured(client.call_tool('list_conferences', {}))
                report['steps']['list_conferences'] = {'shape': _shape(venues)}
                result = client.call_tool('search_papers', {'query': args.query, 'limit': 3})
                payload = mcp_client.structured(result)
                items = mcp_lune.papers_from(payload)
                report['steps']['search_papers'] = {
                    'payload_shape': _shape(payload), 'items': len(items),
                    'first_item_shape': _shape(items[0]) if items else None}
                report['ok'] = bool(items)
                status = 0 if items else 1
        except mcp_client.McpError as exc:
            report['error'] = {'kind': exc.kind, 'status': exc.status}
        except Exception:
            # No provider-controlled error text or credential is persisted.
            report['error'] = {'kind': 'probe_failed'}
        finally:
            if client is not None:
                report['usage'] = client.usage()
            json.dump(report, evidence, ensure_ascii=False, indent=1)
            evidence.write('\n')
    print(json.dumps({'ok': report['ok'], 'steps': list(report['steps'])}, ensure_ascii=False))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
