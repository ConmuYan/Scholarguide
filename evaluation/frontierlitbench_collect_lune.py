"""Collect exact-title Lune metadata for pending benchmark recommendations."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import time
import unicodedata

from mcp_lune import LuneProvider


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def normalized(value) -> str:
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ''.join(character for character in value if character.isalnum())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--limit-per-title', type=int, default=3)
    parser.add_argument('--max-requests', type=int)
    args = parser.parse_args()

    inventory = read_json(args.inventory)
    existing = read_json(args.out).get('observations', {}) if args.out.is_file() else {}
    for index, row in enumerate(inventory['papers'], 1):
        paper = row['paper']
        title = str(paper.get('title') or '').strip()
        if not title or (title in existing and existing[title].get('status') != 'error'):
            continue
        results = None
        error = None
        for attempt in range(3):
            try:
                provider = LuneProvider(max_requests=1)
                results = provider.search(title, limit=args.limit_per_title)
                error = None
                break
            except Exception as exc:
                error = exc
                time.sleep(2 * (attempt + 1))
        try:
            if results is None:
                raise error or RuntimeError('Lune search did not run')
        except Exception as exc:
            existing[title] = {
                'status': 'error',
                'error': str(exc),
                'error_type': type(exc).__name__,
            }
        else:
            matches = [asdict(item) for item in results
                       if normalized(item.title) == normalized(title)]
            existing[title] = {
                'status': 'matched' if matches else 'not_found',
                'match': matches[:1],
                'returned': [asdict(item) for item in results],
            }
        payload = {
            'schema_version': 1,
            'source': 'Lune MCP search_papers',
            'inventory': str(args.inventory.resolve()),
            'processed': len(existing),
            'total': len(inventory['papers']),
            'observations': existing,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                            encoding='utf-8')
        state = existing[title]['status']
        print(f'[{index}/{len(inventory["papers"])}] {state}: {title}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
