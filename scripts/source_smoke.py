"""One-shot live connectivity check for every academic source.

This is not an evaluation and not a benchmark: it records whether the configured
network can reach each provider, how many results came back, and the failure
surface (429, bot challenge, missing key). Keys are never written.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config
from source import AcademicSource, PROVIDERS


def main() -> int:
    config = Config.from_env()
    config.source_timeout = min(config.source_timeout, 12)
    rows = []
    for name in ('arxiv', 'openalex', 'semantic_scholar', 'dblp', 'openreview',
                 'acl_anthology', 'deepseek'):
        source = AcademicSource(config, Budget(90))
        source.begin_turn()
        started = time.monotonic()
        try:
            items = source._search_one(name, 'retrieval augmented generation evaluation', 2)
            status = 'ok' if items else 'empty'
            error = ''
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic surface
            items, status, error = [], 'error', f'{type(exc).__name__}: {exc}'
        rows.append({
            'provider': name,
            'label': PROVIDERS[name].label,
            'status': status,
            'returned': len(items),
            'first_title': items[0].title[:120] if items else '',
            'first_has_abstract': bool(items and items[0].abstract),
            'first_doi': items[0].doi if items else '',
            'elapsed_ms': int((time.monotonic() - started) * 1000),
            'http_calls': source.budget.search_http_calls,
            'error': error[:300],
            'key_configured': bool(source._provider_key(name)) if name in ('semantic_scholar', 'deepseek') else None,
        })
    payload = {'checked_at': datetime.now(timezone.utc).isoformat(),
               'model': config.model,
               'not_a_benchmark': 'Live connectivity only; no quality labels and no human annotation.',
               'providers': rows}
    out = ROOT / 'docs' / 'SOURCE_SMOKE.json'
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
