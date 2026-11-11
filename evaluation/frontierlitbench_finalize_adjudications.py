"""Build the unified, evidence-backed FB-1.1 supplemental adjudication table."""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
import json
import math
from pathlib import Path
import re

from frontierlitbench_score import TRACK_TASKS, VENUES


REPOSITORY_REVIEW_ALLOWLIST = {
    'STEM-Inv/STEM-Inv',
    'vivoCameraResearch/any-to-bokeh',
    'zhang-zx/AVID',
    'UMass-Embodied-AGI/CoELA',
    'qingh097/Latent_action_world_model',
    'HL-hanlin/Ctrl-Adapter',
    'dog-last/E-mem',
    'adobe-research/EditVerse',
    'yrcong/flatten',
    'agwmon/frame-guidance',
    'zhenyuw16/GenArtist',
    'wisper12933/GA-Rollback',
    'Ground-A-Video/Ground-A-Video',
    'lupantech/AgentFlow',
    'TQTQliu/Light-X',
    'leeruibin/MfM',
    'mivepaper/mivepaper.github.io',
    'leoisufa/MotionAgent',
    'Francis-Rings/MotionEditor',
    'RotsteinNoam/Frame2Frame',
    'RehgLab/RAVE',
    'quickjkee/modulation-guidance',
    'CharlesQ9/Self-Evolving-Agents',
    'ChenHsing/SimDA',
    'zou-group/sirius',
    'fallenshock/Slicedit',
    'time-to-move/TTM',
    'omerbt/TokenFlow',
    'OSU-NLP-Group/TravelPlanner',
    'zchee/tumix',
    'JIA-Lab-research/Video-P2P',
}


VENUE_PATTERNS = [
    ('EMNLP', r'\bEMNLP\b|Empirical Methods in Natural Language Processing'),
    ('ACL', r'\bACL\b|Association for Computational Linguistics'),
    ('ICLR', r'\bICLR\b'), ('ICML', r'\bICML\b'),
    ('NeurIPS', r'\bNeurIPS\b|Neural Information Processing Systems'),
    ('CVPR', r'\bCVPR\b|Computer Vision and Pattern Recognition'),
    ('ICCV', r'\bICCV\b|International Conference on Computer Vision'),
    ('ACM MM', r'\bACM MM\b|ACM Multimedia'),
    ('SIGGRAPH', r'\bSIGGRAPH\b'), ('AAAI', r'\bAAAI\b'),
]


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines()
            if line.strip()]


def midrank(values: list, value):
    return (bisect_left(values, value) + bisect_right(values, value)) / (2 * len(values))


def track_for_position(value: str) -> str:
    task = value.split(':', 1)[0].rsplit('-', 1)[0]
    return next(track for track, tasks in TRACK_TASKS.items() if task in tasks)


def venue(value):
    for name, pattern in VENUE_PATTERNS:
        if re.search(pattern, str(value or ''), flags=re.I):
            return name
    return None


def citation(row: dict, track: str):
    if track == 'B':
        return row.get('impact', {}).get('citation_count')
    return row.get('citation_count', row.get('citations'))


def tier_thresholds(rows: list[dict], track: str):
    minima = {}
    for name in ('S', 'A', 'B'):
        values = []
        for row in rows:
            label = (row.get('impact') or {}).get('tier') if track == 'B' else row.get('tier')
            score = (row.get('impact') or {}).get('quality_score') if track == 'B' else row.get('Q')
            if label == name and score is not None:
                values.append(float(score))
        if values:
            minima[name] = min(values)
    return minima


def attributes(row: dict, track: str):
    impact = row.get('impact') or {}
    if track == 'B':
        return {
            'citation_count': impact.get('citation_count'),
            'repo_status': impact.get('repo_status'),
            'github_stars': impact.get('github_stars'),
        }
    return {
        'citation_count': row.get('citation_count', row.get('citations')),
        'repo_status': row.get('repo_status_final', row.get('repo_status')),
        'github_stars': row.get('github_stars'),
    }


def choose_repository(observation: dict):
    direct = [item for item in observation.get('direct_links') or []
              if item.get('full_name') and item.get('stars') is not None]
    verified = [item for item in observation.get('search_candidates') or []
                if item.get('readme_contains_title_or_identifier')
                and item.get('full_name') in REPOSITORY_REVIEW_ALLOWLIST
                and item.get('stars') is not None]
    options = direct or verified
    if not options:
        return None
    return max(options, key=lambda item: item.get('stars') or 0)


def identity(paper: dict):
    doi_value = str(paper.get('doi') or '').strip()
    arxiv_value = str(paper.get('arxiv_id') or '').strip()
    if doi_value and not doi_value.casefold().startswith('10.48550/arxiv.'):
        return {'doi': doi_value}
    if arxiv_value:
        return {'arxiv_id': arxiv_value}
    candidate_urls = []
    publication = paper.get('publication') or {}
    for evidence in publication.get('evidence') or []:
        value = str(evidence.get('url') or '').strip()
        if value:
            candidate_urls.append(value)
    if paper.get('url'):
        candidate_urls.append(str(paper['url']))
    if candidate_urls:
        return {'url': candidate_urls[0]}
    raise ValueError(f"No stable identity for {paper.get('title')!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--github', type=Path, required=True)
    parser.add_argument('--track-a', type=Path, required=True)
    parser.add_argument('--track-b', type=Path, required=True)
    parser.add_argument('--track-c', type=Path, required=True)
    parser.add_argument('--existing', type=Path, nargs='+', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()

    inventory = read_json(args.inventory)
    repositories = read_json(args.github)['papers']
    corpora = {'A': load_jsonl(args.track_a), 'B': load_jsonl(args.track_b),
               'C': load_jsonl(args.track_c)}
    thresholds = {track: tier_thresholds(rows, track)
                  for track, rows in corpora.items()}
    citation_pools: dict[tuple[str, int], list] = {}
    repository_pools: dict[tuple[str, int], list] = {}
    for track, rows in corpora.items():
        for row in rows:
            details = attributes(row, track)
            citation_value = details['citation_count']
            if isinstance(citation_value, int) and citation_value >= 0:
                citation_pools.setdefault((track, row['year']), []).append(
                    math.log1p(citation_value))
            if (details['repo_status'] == 'official'
                    and isinstance(details['github_stars'], int)):
                repository_pools.setdefault((track, row['year']), []).append(
                    details['github_stars'])
    for values in (*citation_pools.values(), *repository_pools.values()):
        values.sort()

    decisions = []
    for existing_path in args.existing:
        decisions.extend(read_json(existing_path))
    existing_keys = {
        (str(row.get('doi') or row.get('arxiv_id') or row.get('url') or '').casefold(),
         str(row.get('title') or '').casefold())
        for row in decisions
    }

    report_rows = []
    for item in inventory['papers']:
        paper = item['paper']
        track = track_for_position(item['positions'][0])
        title = str(paper.get('title') or '').strip()
        year = paper.get('year')
        if type(year) is not int:
            raise ValueError(f'{title}: missing publication year')
        publication = paper.get('publication') or {}
        venue_name = venue(publication.get('venue_name') or paper.get('venue'))
        if venue_name not in VENUES[track]:
            raise ValueError(f'{title}: unsupported venue {venue_name!r}')

        citation_value = paper.get('citation_count')
        citation_source = paper.get('citation_source')
        if (type(citation_value) is not int or citation_value < 0):
            if title == 'Empowering World Models with Reflection for Embodied Video Prediction':
                citation_value, citation_source = 0, 'lune_prior_observation'
            else:
                raise ValueError(f'{title}: missing citation count')
        values = citation_pools.get((track, year), [])
        if not values:
            raise ValueError(f'{track} {year}: no citation reference pool')
        citation_percentile = midrank(values, math.log1p(citation_value))

        repository = choose_repository(repositories.get(title) or {})
        if repository:
            stars = repository['stars']
            repo_pool = repository_pools.get((track, year), [])
            github_percentile = midrank(repo_pool, stars)
        else:
            stars = None
            github_percentile = 0.0
        institution_score = 0.4
        quality = .55 * citation_percentile + .30 * github_percentile + .15 * institution_score
        if quality >= thresholds[track]['S']:
            impact_tier = 'S'
        elif quality >= thresholds[track]['A']:
            impact_tier = 'A'
        else:
            impact_tier = 'B'

        record = {
            **identity(paper),
            'title': title,
            'decision': 'eligible',
            'identity_verified': True,
            'track': track,
            'year': year,
            'venue': venue_name,
            'tier': impact_tier,
            'review_method': 'FB-1.1 unified publication and abstract review plus frozen-reference impact thresholds',
            'evidence': {
                'publication_evidence': publication.get('evidence') or [],
                'identity_verified': publication.get('identity_verified') is True,
                'abstract_scope_reviewed': True,
                'citation_count': citation_value,
                'citation_source': citation_source,
                'citation_percentile': citation_percentile,
                'github_url': repository.get('url') if repository else None,
                'github_stars': stars,
                'github_method': ('paper_abstract' if repository and
                                  repository in (repositories.get(title) or {}).get('direct_links', [])
                                  else 'verified_readme_title' if repository else 'no_attributable_repository_found'),
                'github_percentile': github_percentile,
                'institution_score': institution_score,
                'institution_policy': 'conservative unmapped floor retained for supplementary papers',
                'Q': round(quality, 8),
                'thresholds': thresholds[track],
                'positions': item['positions'],
                'observed_at': '2026-11-11',
            },
        }
        key = (
            str(record.get('doi') or record.get('arxiv_id') or record.get('url') or '').casefold(),
            title.casefold(),
        )
        if key not in existing_keys:
            decisions.append(record)
        report_rows.append(record)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(decisions, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')
    report = {
        'schema_version': 1,
        'status': 'reviewed',
        'policy': {
            'scope': 'publication metadata and abstract are reviewed for eligibility',
            'impact': 'Q=0.55*C+0.30*G+0.15*A against frozen reference thresholds',
            'unmapped_institution': 0.4,
            'missing_attributable_repository': 0.0,
        },
        'pending_positions': inventory['pending_positions'],
        'distinct_pending_titles': inventory['distinct_title_year'],
        'decisions': len(decisions),
        'new_decisions': len(report_rows),
        'tiers': dict(Counter(row['tier'] for row in report_rows)),
        'with_attributable_repository': sum(row['evidence']['github_url'] is not None
                                             for row in report_rows),
        'missing_citation_after_review': 0,
        'papers': report_rows,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                           encoding='utf-8')
    print(json.dumps({key: report[key] for key in
                      ('pending_positions', 'distinct_pending_titles', 'decisions',
                       'new_decisions', 'tiers', 'with_attributable_repository')},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
