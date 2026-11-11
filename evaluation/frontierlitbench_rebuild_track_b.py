"""Rebuild Track B labels from the documented citation, repository, and affiliation review."""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
import json
import math
from pathlib import Path
import re
import unicodedata


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def midrank(values: list, value):
    return (bisect_left(values, value) + bisect_right(values, value)) / (2 * len(values))


def normalized(value) -> str:
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.sub(r'[^a-z0-9]+', '', value)


def institution_score(name: str, rubric: dict) -> float:
    key = normalized(name)
    industry = rubric.get('industrial_labs') or {}
    for label, score in industry.items():
        if key and (key in normalized(label) or normalized(label) in key):
            return float(score)
    for bucket, score in (('top25', 1.0), ('rank_26_50', 0.8),
                          ('rank_51_100', 0.6)):
        for label in rubric.get(bucket) or []:
            candidate = normalized(label)
            if key and (key in candidate or candidate in key):
                return score
    return float(rubric.get('unmapped_policy', 0.4))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--citations', type=Path, required=True)
    parser.add_argument('--affiliations', type=Path, required=True)
    parser.add_argument('--repositories', type=Path, required=True)
    parser.add_argument('--rubric', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()

    records = [json.loads(line) for line
               in args.gold.read_text(encoding='utf-8-sig').splitlines()
               if line.strip()]
    citations = {row['paper_id']: row
                 for row in read_json(args.citations)['papers']}
    affiliations = {row['paper_id']: row
                    for row in read_json(args.affiliations)['papers']}
    repositories = {row['paper_id']: row
                    for row in read_json(args.repositories)['papers']}
    rubric = read_json(args.rubric)
    old_tiers = {row['paper_id']: row['impact']['tier'] for row in records}

    for row in records:
        impact = row['impact']
        paper_id = row['paper_id']
        if paper_id in citations:
            observation = citations[paper_id]
            impact.update(
                citation_count=observation['lune_citation_count'],
                citation_source='lune',
                citation_observed_at='2026-11-11T19:46:47+08:00',
                citation_status='reviewed_lune_snapshot',
            )
        if paper_id in affiliations:
            observation = affiliations[paper_id]
            scored = [institution_score(name, rubric)
                      for name in observation.get('first_author_institutions') or []]
            impact.update(
                institution_score=max(scored) if scored else 0.4,
                institution_names=observation.get('first_author_institutions') or [],
                institution_status='reviewed_paper_affiliation',
                institution_evidence_url=observation.get('evidence_url'),
            )
        elif impact.get('institution_score') is None:
            impact['institution_score'] = float(rubric.get('unmapped_policy', 0.4))
            impact['institution_reason'] = 'reviewed_unmapped_conservative_floor'
        if impact['institution_score'] < 0.4:
            impact['institution_score'] = float(rubric.get('unmapped_policy', 0.4))
        if paper_id in repositories:
            observation = repositories[paper_id]
            impact.update(
                repo_status='official',
                github_url=observation['repository_url'],
                github_stars=observation['github_stars_observed'],
                stars_as_of='2026-11-11',
                repository_review_status='reviewed_after_historical_snapshot',
            )

    citation_pools: dict[int, list] = {}
    repository_pools: dict[int, list] = {}
    for row in records:
        impact = row['impact']
        citation_pools.setdefault(row['year'], []).append(
            math.log1p(impact.get('citation_count') or 0))
        if impact.get('repo_status') == 'official':
            repository_pools.setdefault(row['year'], []).append(
                impact.get('github_stars') or 0)
    for values in (*citation_pools.values(), *repository_pools.values()):
        values.sort()

    for row in records:
        impact = row['impact']
        citation = midrank(citation_pools[row['year']],
                           math.log1p(impact.get('citation_count') or 0))
        if impact.get('repo_status') == 'official':
            github = midrank(repository_pools[row['year']],
                             impact.get('github_stars') or 0)
        else:
            github = 0.0
        institution = impact['institution_score']
        quality = .55 * citation + .30 * github + .15 * institution
        impact.update(
            citation_percentile=citation,
            github_percentile=github,
            quality_score=round(quality, 8),
            C=round(citation, 8),
            G=round(github, 8),
            A=round(institution, 8),
            Q=round(quality, 8),
        )

    records.sort(key=lambda row: (-row['impact']['quality_score'],
                                  row['paper_id']))
    count = len(records)
    s_count = round(.20 * count)
    a_count = round(.40 * count)
    for index, row in enumerate(records):
        tier = 'S' if index < s_count else 'A' if index < s_count + a_count else 'B'
        row['impact'].update(tier=tier, gain={'S': 5, 'A': 3, 'B': 1}[tier])
        row['gold_status'] = 'reviewed_v1.1'
        row['audit'] = {
            **(row.get('audit') or {}),
            'corpus_version': 'FB-1.1-world-2026-11-11-reviewed',
            'citation_review': 'Lune snapshot for 13 previously unobserved records',
            'repository_review': '13 paper-affiliation checks and 2 official-repository corrections',
            'institution_review': '13 paper-affiliation checks; conservative 0.40 floor retained when unmapped',
        }

    tiers = Counter(row['impact']['tier'] for row in records)
    changed = [(row['paper_id'], old_tiers[row['paper_id']],
                row['impact']['tier'])
               for row in records if old_tiers[row['paper_id']] != row['impact']['tier']]
    boundaries = {}
    for tier in ('S', 'A', 'B'):
        values = [row['impact']['quality_score'] for row in records
                  if row['impact']['tier'] == tier]
        boundaries[tier] = {'min_Q': min(values), 'max_Q': max(values)}

    args.out.mkdir(parents=True, exist_ok=False)
    with (args.out / 'gold_world_models.jsonl').open('x', encoding='utf-8') as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    summary = {
        'corpus_version': 'FB-1.1-world-2026-11-11-reviewed',
        'count': count,
        'tiers': dict(tiers),
        'changed_tiers': [{'paper_id': paper_id, 'from': before, 'to': after}
                          for paper_id, before, after in changed],
        'tier_boundaries': boundaries,
        'quantified_records': {
            'citation_updates': len(citations),
            'affiliation_updates': len(affiliations),
            'repository_updates': len(repositories),
        },
        'label_policy': {
            'formula': 'Q=0.55*C+0.30*G+0.15*A',
            'quota': 'S=20%, A=40%, B=40%',
            'unmapped_institution': float(rubric.get('unmapped_policy', 0.4)),
            'missing_official_repository': 0.0,
        },
    }
    (args.out / 'review.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
