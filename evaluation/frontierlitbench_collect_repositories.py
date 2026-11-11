"""Collect attributable GitHub repository evidence for pending papers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import unicodedata
from urllib.parse import quote


GH_URL = re.compile(r'https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)', re.I)


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def normalized(value) -> str:
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ''.join(character for character in value if character.isalnum())


def gh_api(endpoint: str):
    for attempt in range(4):
        process = subprocess.run(
            ['gh', 'api', endpoint],
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        if process.returncode == 0:
            return json.loads(process.stdout)
        message = (process.stderr or process.stdout).strip()
        if 'rate limit' not in message.casefold() or attempt == 3:
            raise RuntimeError(message)
        time.sleep(15 * (attempt + 1))
    raise AssertionError('unreachable')


def gh_readme(full_name: str) -> str:
    for attempt in range(4):
        process = subprocess.run(
            ['gh', 'api', '-H', 'Accept: application/vnd.github.raw+json',
             f'repos/{full_name}/readme'],
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        if process.returncode == 0:
            return process.stdout
        message = (process.stderr or process.stdout).strip()
        if 'rate limit' not in message.casefold() or attempt == 3:
            return ''
        time.sleep(15 * (attempt + 1))
    return ''


def repo_record(full_name: str):
    record = gh_api(f'repos/{full_name}')
    return {
        'full_name': record.get('full_name'),
        'url': record.get('html_url'),
        'stars': record.get('stargazers_count'),
        'created_at': record.get('created_at'),
        'description': record.get('description'),
        'homepage': record.get('homepage'),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()

    inventory = read_json(args.inventory)
    observations = read_json(args.out).get('papers', {}) if args.out.is_file() else {}
    for index, row in enumerate(inventory['papers'], 1):
        paper = row['paper']
        title = str(paper.get('title') or '').strip()
        prior = observations.get(title, {})
        prior_candidates = prior.get('search_candidates') or []
        needs_verification = any(
            'readme_contains_title_or_identifier' not in item
            for item in prior_candidates if item.get('full_name'))
        if not title or (prior and not needs_verification):
            continue
        text = ' '.join(str(paper.get(field) or '')
                        for field in ('abstract', 'snippet', 'url'))
        links = list(dict.fromkeys(
            f'{owner}/{repo.rstrip(".")}'
            for owner, repo in GH_URL.findall(text)
        ))
        direct = prior.get('direct_links') or []
        if not direct:
            for full_name in links[:5]:
                try:
                    direct.append(repo_record(full_name))
                except Exception as exc:
                    direct.append({'full_name': full_name, 'error': str(exc)})

        candidates = prior_candidates
        if not any('error' not in item for item in direct):
            if not candidates:
                query = quote(f'"{title}" in:name,description,readme')
                try:
                    search = gh_api(f'search/repositories?q={query}&per_page=5')
                    for item in search.get('items') or []:
                        candidates.append({
                            'full_name': item.get('full_name'),
                            'url': item.get('html_url'),
                            'stars': item.get('stargazers_count'),
                            'created_at': item.get('created_at'),
                            'description': item.get('description'),
                        })
                except Exception as exc:
                    candidates = [{'error': str(exc)}]

        identifiers = [value for field in ('title', 'arxiv_id', 'doi')
                       if (value := normalized(paper.get(field)))]
        for item in candidates:
            full_name = item.get('full_name')
            if not full_name:
                continue
            readme = gh_readme(full_name)
            text = normalized(' '.join((full_name, str(item.get('description') or ''),
                                        readme)))
            item['readme_contains_title_or_identifier'] = any(
                identifier in text for identifier in identifiers)

        observations[title] = {
            'direct_links': direct,
            'search_candidates': candidates,
            'searched_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        payload = {
            'schema_version': 1,
            'source': 'GitHub REST API via authenticated gh CLI',
            'inventory': str(args.inventory.resolve()),
            'processed': len(observations),
            'total': len(inventory['papers']),
            'papers': observations,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                            encoding='utf-8')
        print(f'[{index}/{len(inventory["papers"])}] direct={len(direct)} '
              f'candidates={len(candidates)}: {title}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
