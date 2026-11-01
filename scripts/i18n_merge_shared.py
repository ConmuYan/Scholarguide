"""Merge the shared front-end dictionary into the pages that load research/trace UI.

Page-specific entries win over shared entries for the same key.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'
PAGES = ('index.html', 'chat.html')
BLOCK = re.compile(
    r'(<script type="application/json" id="sg-page-i18n">)(.*?)(</script>)', re.S)


def main() -> int:
    shared = json.loads((WEB / 'i18n-shared.json').read_text(encoding='utf-8'))
    for page in PAGES:
        path = WEB / page
        text = path.read_text(encoding='utf-8')
        match = BLOCK.search(text)
        if not match:
            print(f'{page}: no page dictionary')
            continue
        current = json.loads(match.group(2))
        merged = dict(shared)
        merged.update(current)
        body = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True)
        updated = text[:match.start()] + match.group(1) + '\n' + body + '\n  ' + match.group(3) + text[match.end():]
        path.write_text(updated, encoding='utf-8')
        print(f'{page}: merged {len(merged)} keys (shared {len(shared)})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
