"""Optional maintainer script: refresh the real arXiv metadata used by OFFLINE demos.

Run explicitly: python examples/refresh_demo.py. This does not create model outputs
or benchmark labels. The demo's decisions remain scripted, not live inference.
"""
import json
from pathlib import Path
import sys
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Budget, Config, Paper, utc_now
from source import AcademicSource
from vendor.gptr_arxiv import API, ArxivSearch

IDS = ['2005.11401', '2004.04906', '2007.01282', '2002.08909', '2309.15217', '2311.09476']

if __name__ == '__main__':
    client = AcademicSource(Config(cache=False), Budget(120))
    rows = ArxivSearch.parse(client._fetch(API + '?' + urlencode({'id_list': ','.join(IDS)})))
    by_id = {r['arxiv_id']: r for r in rows}
    if set(by_id) != set(IDS):
        raise SystemExit('Incomplete metadata response; existing fixture not overwritten.')
    papers = []
    for identifier in IDS:
        r = by_id[identifier]
        papers.append(Paper(id='arxiv:' + identifier, title=r['title'], abstract=r['body'], url=r['href'],
                            year=r['year'], authors=r['authors'], arxiv_id=identifier, sources=['arxiv'],
                            from_query=['fixture: exact ID fetch'], fetched_at=utc_now()).to_dict())
    path = Path(__file__).with_name('papers.json')
    path.write_text(json.dumps({'notice': 'Real metadata; fixture ordering and demo decisions are scripted.',
                                'papers': papers}, ensure_ascii=False, indent=2), encoding='utf-8')
    for p in papers:
        print(p['id'], p['title'])
