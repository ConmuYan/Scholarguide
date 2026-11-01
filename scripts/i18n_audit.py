"""Audit which JavaScript translation keys are missing from each page dictionary."""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'

PAGES = {
    'index.html': ['app.js', 'research.js', 'trace.js'],
    'chat.html': ['chat.js', 'research.js', 'trace.js'],
    'landing.html': ['landing.js'],
    'activity.html': ['activity.js', 'activity-story.js'],
    'settings.html': ['settings.js', 'memory.js'],
    'sources.html': ['sources.js'],
}

KEY_CALL = re.compile(r"""\bt\(\s*(['"])((?:\\.|(?!\1).)*)\1""")
DATA_MARK = re.compile(r'data-i18n(?:-(?:placeholder|title|aria-label|alt|content))?="([^"]*)"')


def page_keys(html: Path) -> set[str]:
    text = html.read_text(encoding='utf-8')
    match = re.search(r'<script type="application/json" id="sg-page-i18n">(.*?)</script>', text, re.S)
    if not match:
        return set()
    data = json.loads(match.group(1))
    return set(data)


def json_keys(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return set()


def script_keys(path: Path) -> set[str]:
    text = path.read_text(encoding='utf-8')
    return {match.group(2) for match in KEY_CALL.finditer(text)}


def marked_keys(html: Path) -> set[str]:
    text = html.read_text(encoding='utf-8')
    return set(DATA_MARK.findall(text))


def main() -> int:
    shared = json_keys(WEB / 'i18n-shared.json')
    problems = 0
    for page, scripts in PAGES.items():
        html = WEB / page
        keys = page_keys(html)
        marked = marked_keys(html)
        used = set()
        for name in scripts:
            used |= script_keys(WEB / name)
        missing = sorted(used - keys)
        unmarked = sorted(marked - keys)
        print(f'== {page}: dict={len(keys)} marked={len(marked)} used={len(used)}')
        if missing:
            problems += len(missing)
            print('   missing t() keys:')
            for key in missing:
                print('     -', key)
        if unmarked:
            problems += len(unmarked)
            print('   marked but not in dictionary:')
            for key in unmarked:
                print('     -', key)
    if shared:
        print(f'== shared json keys: {len(shared)}')
    print(f'PROBLEMS={problems}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
