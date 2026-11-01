#!/usr/bin/env python3
"""Fill the public repository slug into the docs after the GitHub repo exists.

Run once from the project root after creating the repository:

    python scripts/set_repository.py --repository owner/repo

It rewrites the documented install commands in place. It does not create or push
a repository and does not touch credentials or run data.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ('README.md', 'PUBLIC_README.md', 'docs/INSTALL.md', 'docs/RELEASE_PACKAGING.md')
PLACEHOLDER = 'OWNER/REPOSITORY'
REPO_RE = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, help='GitHub slug, e.g. alice/scholarguide')
    parser.add_argument('--check', action='store_true', help='Report whether placeholders remain')
    args = parser.parse_args(argv)
    repository = args.repository.strip()
    if not REPO_RE.fullmatch(repository) or any(part in ('.', '..') for part in repository.split('/')):
        print('Error: repository must be OWNER/REPO', file=sys.stderr)
        return 2
    pending = {name: (ROOT / name).read_text(encoding='utf-8') for name in TARGETS
               if (ROOT / name).is_file()}
    remaining = {name: text.count(PLACEHOLDER) for name, text in pending.items() if PLACEHOLDER in text}
    if args.check:
        print(f'repository: {repository}')
        print('placeholders remaining: ' + (', '.join(f'{k} x{v}' for k, v in remaining.items()) or 'none'))
        return 0
    changed = []
    for name, text in pending.items():
        updated = text.replace(PLACEHOLDER, repository)
        if updated != text:
            (ROOT / name).write_text(updated, encoding='utf-8', newline='\n')
            changed.append(name)
    print('updated: ' + (', '.join(changed) if changed else 'nothing (already set)'))
    leftovers = [name for name in changed if PLACEHOLDER in (ROOT / name).read_text(encoding='utf-8')]
    if leftovers:
        print('Error: placeholders remain in ' + ', '.join(leftovers), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
