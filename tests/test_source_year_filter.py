"""Offline regressions for source filtering with year-only discovery requests."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config, Paper
from source import AcademicSource


S2_CONFERENCE = {
    'data': [{
        'paperId': 'year-filter-fixture',
        'title': 'A retrieval augmented generation benchmark',
        'abstract': 'We introduce a benchmark for retrieval augmented generation.',
        'url': 'https://www.semanticscholar.org/paper/year-filter-fixture',
        'year': 2024,
        'externalIds': {'DOI': '10.1234/year-filter-fixture'},
        'publicationTypes': ['Conference'],
        'publicationVenue': {'id': 'acl-fixture', 'type': 'conference', 'name': 'ACL'},
    }],
}


def source():
    return AcademicSource(Config(source='semantic_scholar', cache=False), Budget(60))


def publication(venue, year):
    return {'type': 'conference', 'status': 'accepted', 'presentation': 'poster',
            'venue_id': f'{venue}.cc/{year}/Conference', 'venue_name': venue,
            'date': str(year), 'verified': True, 'identity_verified': True,
            'evidence': [{'source': 'openreview',
                          'url': 'https://openreview.net/forum?id=year-filter-fixture',
                          'field': 'content.venueid',
                          'value': f'{venue}.cc/{year}/Conference'}]}


class SourceYearFilterTests(unittest.TestCase):
    def test_s2_year_only_discovery_keeps_matching_unverified_publication(self):
        academic = source()
        with patch.object(academic, '_json', return_value=deepcopy(S2_CONFERENCE)):
            batch = academic.search_batch('retrieval generation', 3,
                                          filters={'year_from': 2024, 'year_to': 2024})
        self.assertEqual(batch.runs[0].returned, 1)
        self.assertEqual([paper.id for paper in batch.papers], ['s2:year-filter-fixture'])
        self.assertFalse(batch.papers[0].publication['verified'])
        self.assertEqual(batch.papers[0].publication['status'], 'unknown')

    def test_s2_year_only_discovery_still_rejects_out_of_range_work(self):
        academic = source()
        with patch.object(academic, '_json', return_value=deepcopy(S2_CONFERENCE)):
            batch = academic.search_batch('retrieval generation', 3,
                                          filters={'year_from': 2025, 'year_to': 2026})
        self.assertEqual(batch.runs[0].returned, 1)
        self.assertEqual(batch.papers, [])

    def test_explicit_venue_and_year_must_match_one_verified_record(self):
        academic = source()
        paper = Paper('same-work-fixture', 'A retrieval method', 'A useful retrieval method.',
                      'https://openreview.net/forum?id=year-filter-fixture', year=2026,
                      venue='ICLR', publication={'records': [publication('ICLR', 2024),
                                                             publication('NeurIPS', 2026)]})
        with patch.object(academic, '_search_one', return_value=[paper]):
            mismatched = academic.search_batch('retrieval generation', 3,
                filters={'venue': 'ICLR', 'year_from': 2026, 'year_to': 2026})
            matched = academic.search_batch('retrieval generation', 3,
                filters={'venue': 'NeurIPS', 'year_from': 2026, 'year_to': 2026})
        self.assertEqual(mismatched.papers, [])
        self.assertEqual([item.id for item in matched.papers], [paper.id])


if __name__ == '__main__':
    unittest.main()
