"""Collect pending recommendations from completed benchmark run directories."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from frontierlitbench_score import TRACK_TASKS


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, required=True,
                        help='Directory containing TASK-REPEAT run folders.')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()

    grouped: dict[tuple[str, str], dict] = {}
    positions_by_track = Counter()
    positions = 0
    for score_path in sorted(args.runs.glob('FB-*/score.json')):
        result_path = score_path.parent / 'run' / 'result.json'
        if not result_path.is_file():
            continue
        report = read_json(score_path)
        result = read_json(result_path)
        papers = result.get('papers') or []
        for row in report.get('positions') or []:
            if row.get('status') != 'pending_adjudication':
                continue
            rank = int(row['rank'])
            if rank < 1 or rank > len(papers):
                raise ValueError(f'{score_path}: rank {rank} has no result record')
            paper = papers[rank - 1]
            key = (str(paper.get('title') or row.get('title')),
                   str(paper.get('year') or ''))
            grouped.setdefault(key, {'paper': paper, 'positions': []})
            grouped[key]['positions'].append(
                f'{score_path.parent.name}:{rank}')
            positions += 1
            task = report['task']
            track = next(name for name, tasks in TRACK_TASKS.items()
                         if task in tasks)
            positions_by_track[track] += 1

    rows = sorted(grouped.values(),
                  key=lambda row: (str(row['paper'].get('title') or '').casefold(),
                                   str(row['paper'].get('year') or '')))
    payload = {
        'schema_version': 1,
        'runs': str(args.runs.resolve()),
        'pending_positions': positions,
        'distinct_title_year': len(rows),
        'positions_by_track': dict(positions_by_track),
        'papers': rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')
    print(f'{positions} pending positions; {len(rows)} title/year/rank records')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
