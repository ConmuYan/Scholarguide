"""Official retraction observations survive deduplication and durable history."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import Budget, Config, Paper, Request
from research_policy import publication_matches
from source import AcademicSource, merge_papers
from state import StateStore
from workflow import select_papers


WORK = {
    'id': 'https://openalex.org/W1234567890', 'title': 'A research survey',
    'doi': 'https://doi.org/10.1007/example-survey', 'type': 'article',
    'publication_year': 2025, 'publication_date': '2025-04-01',
    'is_retracted': False,
    'abstract_inverted_index': {'We': [0], 'survey': [1], 'retrieval': [2], 'methods.': [3]},
    'primary_location': {'is_published': True, 'version': 'publishedVersion',
        'source': {'id': 'https://openalex.org/S122814990', 'type': 'journal',
                   'display_name': 'Artificial Intelligence Review',
                   'issn': ['0269-2821', '1573-7462']}},
}
TASK = {'accepted_only': True, 'publication_type': 'journal'}


def observation(retracted=False):
    source = AcademicSource(Config(source='openalex', cache=False), Budget(60))
    work = deepcopy(WORK)
    work['is_retracted'] = retracted
    with patch.object(source, '_json', return_value={'results': [work]}):
        return source._openalex('retrieval survey', 1)[0]


class RetractionTests(unittest.TestCase):
    def test_official_retraction_has_explicit_evidence_and_is_not_recommended(self):
        item = observation(True)
        record = item.publication
        self.assertEqual(record['status'], 'retracted')
        self.assertTrue(record['verified'])
        self.assertTrue(any(e['field'] == 'is_retracted' and e['value'] is True
                            for e in record['evidence']))
        self.assertFalse(publication_matches(item, TASK))
        self.assertFalse(publication_matches(item, {}))

    def test_retraction_cannot_resurrect_after_dedup_or_another_positive_observation(self):
        item = merge_papers([observation(), observation(True), observation()])[0]
        self.assertFalse(publication_matches(item, TASK))
        self.assertFalse(publication_matches(item, {}))
        self.assertTrue(any(record['status'] == 'retracted' for record in item.publication['records']))
        assessments = {item.id: {'relevance': 2, 'fit_score': 100, 'category_status': 'no',
                                'evidence_verified': True}}
        self.assertEqual(select_papers([item], assessments, Request('retrieval'), research=True), [])

    def test_retraction_survives_sqlite_restart_and_same_content_metadata_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            store = StateStore(path)
            before, after = observation(), observation(True)
            for item in (before, after, before):
                store.register_paper(item.to_dict())
            store.close()
            store = StateStore(path)
            try:
                row = dict(store.con.execute('SELECT * FROM papers').fetchone())
                row['publication'] = json.loads(row['publication_json'])
                self.assertFalse(publication_matches(row, TASK))
                self.assertFalse(publication_matches(row, {}))
            finally:
                store.close()

    def test_same_doi_positive_from_another_provider_cannot_override_retraction(self):
        old = observation()
        record = deepcopy(old.publication['records'][0])
        record.update(venue_id='s2-journal-identity',
                      evidence=[{'source': 'semantic_scholar',
                          'url': 'https://api.semanticscholar.org/graph/v1/paper/example',
                          'field': 'journal', 'value': 'Artificial Intelligence Review'}])
        old.publication = {**record, 'records': [record]}
        item = merge_papers([old, observation(True)])[0]
        self.assertFalse(publication_matches(item, TASK))

    def test_raw_record_history_and_first_import_cannot_bypass_retraction_policy(self):
        before, after = observation(), observation(True)
        raw = before.to_dict()
        raw['publication'] = {'records': before.publication['records'] + after.publication['records']}
        original = deepcopy(raw)
        self.assertFalse(publication_matches(raw, TASK))
        self.assertFalse(publication_matches(raw, {}))
        self.assertEqual(raw, original, 'Policy reads must not mutate historical records.')
        store = StateStore(':memory:')
        try:
            store.register_paper(raw)
            stored = dict(store.con.execute('SELECT * FROM papers').fetchone())
            stored['publication'] = json.loads(stored['publication_json'])
            self.assertFalse(publication_matches(stored, TASK))
        finally:
            store.close()

    def test_unverified_or_forged_retraction_does_not_invalidate_a_publication(self):
        for mutation in ('unverified', 'wrong-domain', 'missing-field', 'false-field'):
            with self.subTest(mutation=mutation):
                negative = observation(True)
                record = deepcopy(negative.publication['records'][0])
                if mutation == 'unverified':
                    record['verified'] = False
                elif mutation == 'wrong-domain':
                    for evidence in record['evidence']:
                        evidence['url'] = 'https://api.openalex.org.evil.test/works/W1234567890'
                elif mutation == 'missing-field':
                    record['evidence'] = [e for e in record['evidence'] if e['field'] != 'is_retracted']
                else:
                    for evidence in record['evidence']:
                        if evidence['field'] == 'is_retracted':
                            evidence['value'] = False
                negative.publication = {**record, 'records': [record]}
                self.assertTrue(publication_matches(merge_papers([observation(), negative])[0], TASK))

    def test_distinct_publication_with_another_doi_keeps_its_own_status(self):
        other = observation()
        record = deepcopy(other.publication['records'][0])
        record.update(type='conference', venue_name='ICLR', venue_id='ICLR.cc/2024/Conference',
                      date='2024', doi='10.1007/another-version')
        other.publication = {**record, 'records': [record]}
        item = merge_papers([other, observation(True)])[0]
        self.assertTrue(publication_matches(item, {'accepted_only': True, 'venue': 'ICLR'}))
        self.assertFalse(publication_matches(item, TASK))


if __name__ == '__main__':
    unittest.main()
