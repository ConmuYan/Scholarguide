"""Build a separate Track A review candidate from confirmed scope decisions."""
from argparse import ArgumentParser
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from pathlib import Path
import json

from frontierlitbench_cli import ROOT, load_gold, read_json, save_new
from frontierlitbench_score import TASKS, TRACK_TASKS, VENUES


def midrank(values, value):
    return (bisect_left(values, value) + bisect_right(values, value)) / (2 * len(values))


def main():
    p = ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    decisions = read_json(ROOT / 'frontierlitbench/review/tracka_scope_decisions.json')
    excluded = {d['lune_id'] for d in decisions if d['decision'] == 'out_of_scope'}
    original = load_gold('FB-01')
    if not excluded <= {r['lune_id'] for r in original}:
        p.error('Scope decisions reference unknown papers.')
    rows = [r for r in original if r.get('tier') in ('S', 'A', 'B') and r['lune_id'] not in excluded]
    citations, stars = defaultdict(list), defaultdict(list)
    for r in rows:
        citations[r['year']].append(r['citations'])
        if r['repo_status_final'] == 'official':
            stars[r['year']].append(r['github_stars'])
        elif r['repo_status_final'] != 'none_available':
            p.error(f"Unresolved repository label: {r['lune_id']}")
    for values in (*citations.values(), *stars.values()):
        values.sort()
    for r in rows:
        c = midrank(citations[r['year']], r['citations'])
        official = r['repo_status_final'] == 'official'
        g = midrank(stars[r['year']], r['github_stars']) if official else 0.0
        r.update(C=c, G=g, citation_percentile=c, github_percentile=g if official else None,
                 Q=round(.55 * c + .30 * g + .15 * r['A'], 6),
                 corpus_version='trackA-review-candidate-20261111')
    rows.sort(key=lambda r: (-r['Q'], r['doi'] or r['lune_id']))
    n_s, n_a = (len(rows) + 2) // 5, (2 * len(rows) + 2) // 5
    for i, r in enumerate(rows):
        r['tier'] = 'S' if i < n_s else 'A' if i < n_s + n_a else 'B'
        r['rank_in_track'] = i + 1
    tasks = {}
    for task in sorted(TRACK_TASKS['A']):
        years, venues = TASKS[task]
        papers = [r for r in rows if r['year'] in years and r['venue'] in (venues or VENUES['A'])]
        tasks[task] = {'count': len(papers), 'eligible': len(papers) >= 5,
                       'tiers': dict(Counter(r['tier'] for r in papers))}
    args.out.mkdir(parents=True, exist_ok=False)
    with (args.out / 'gold_video.jsonl').open('x', encoding='utf-8') as out:
        for r in rows:
            out.write(json.dumps(r, ensure_ascii=False) + '\n')
    save_new(args.out / 'review.json', {
        'status': 'review_candidate', 'unresolved': ['remaining topic flags', 'publication-time affiliations'],
        'count': len(rows), 'excluded_for_scope': len(excluded),
        'tiers': dict(Counter(r['tier'] for r in rows)), 'tasks': tasks})
    print(f'{len(rows)} provisional labels; {len(excluded)} reviewed scope exclusions')


if __name__ == '__main__':
    main()
