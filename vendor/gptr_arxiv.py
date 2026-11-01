"""Adapted from GPT Researcher's ArxivSearch (Apache-2.0).

Upstream: assafelovic/gpt-researcher, commit
5cdad9cb434754188b78bd998df18dd8d502cf7e,
gpt_researcher/retrievers/arxiv/arxiv.py.

Changes: replace arxiv SDK with injected HTTP + stdlib XML (following the local pi
connector), retain search(max_results) interface and title/href/body fields, add
stable IDs/year/authors, and use id_list for exact ID lookup.
See LICENSE-gpt-researcher and ../UPSTREAM.md.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

NS = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
API = 'https://export.arxiv.org/api/query'


def arxiv_id(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(
        r'(?:https?://(?:www\.)?(?:export\.)?arxiv\.org/(?:abs|pdf)/|arxiv:)?'
        r'(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?/?',
        value, re.I,
    )
    return match.group(1).lower() if match else ''


class ArxivSearch:
    def __init__(self, query: str, fetch, sort: str = 'Relevance'):
        self.query = query
        self.fetch = fetch
        if sort not in ('Relevance', 'SubmittedDate'):
            raise ValueError('Invalid sort criterion')
        self.sort = 'relevance' if sort == 'Relevance' else 'submittedDate'

    @staticmethod
    def parse(body: bytes) -> list[dict]:
        root = ET.fromstring(body)
        if root.tag != '{http://www.w3.org/2005/Atom}feed':
            raise ValueError('arXiv returned a non-Atom response')
        records = []
        for entry in root.findall('a:entry', NS):
            text = lambda name: ' '.join((entry.findtext(name, '', NS) or '').split())
            identifier = arxiv_id(text('a:id'))
            if not identifier:
                raise ValueError('arXiv returned an error entry or an invalid ID')
            published = text('a:published')
            records.append({
                'title': text('a:title'), 'body': text('a:summary'),
                'href': f'https://arxiv.org/abs/{identifier}',
                'arxiv_id': identifier, 'doi': text('x:doi'),
                'year': int(published[:4]) if published[:4].isdigit() else None,
                'authors': [' '.join((x.text or '').split()) for x in entry.findall('a:author/a:name', NS)],
            })
        return records

    def search(self, max_results: int = 5) -> list[dict]:
        # The planner emits plain English keywords, not executable query syntax.
        # Match words individually instead of quoting the entire user request.
        words = re.findall(r'[\w-]+', self.query, re.UNICODE)[:16]
        if not words:
            raise ValueError('Empty search query')
        query = ' AND '.join(f'all:"{word}"' for word in words)
        params = {'search_query': query, 'start': 0, 'max_results': max_results,
                  'sortBy': self.sort, 'sortOrder': 'descending'}
        return self.parse(self.fetch(f'{API}?{urlencode(params)}'))[:max_results]

    def lookup(self, identifier: str) -> list[dict]:
        canonical = arxiv_id(identifier)
        if not canonical:
            raise ValueError('Expected an arXiv ID or arxiv.org URL')
        rows = self.parse(self.fetch(f'{API}?{urlencode({"id_list": canonical})}'))
        return [row for row in rows if row['arxiv_id'] == canonical]
