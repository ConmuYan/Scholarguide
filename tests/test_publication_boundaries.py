"""Offline regressions for publication and persistence boundary cases."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config, Paper, merge_publication_records
from research_policy import publication_matches
from source import AcademicSource, _publication, merge_papers
from state import StateStore, content_hash, semantic_key


def evidence(source='openreview', url=None):
    urls = {
        'openreview': 'https://openreview.net/forum?id=fixture-record',
        'openalex': 'https://api.openalex.org/works/Wfixture',
        'semantic_scholar': 'https://api.semanticscholar.org/graph/v1/paper/sfixture',
        'acl_anthology': 'https://aclanthology.org/fixture',
        'arxiv': 'https://arxiv.org/abs/fixture',
    }
    return [{'source': source, 'url': url or urls[source], 'field': 'status', 'value': 'accepted'}]


def record(venue='ICLR', *, kind='conference', status='accepted', year=2025,
           presentation='poster', verified=True, venue_id='', source='openreview',
           url=None, reason=''):
    return {'type': kind, 'status': status, 'presentation': presentation,
            'venue_name': venue, 'venue_id': venue_id, 'date': str(year) if year else '',
            'issn': [], 'verified': verified, 'identity_verified': True,
            'verification_reason': reason, 'evidence': evidence(source, url)}


def paper(idx, *, abstract='retrieval augmented generation', venue='', year=None,
          records=None, authors=None, open_access_url='', abstract_source='',
          metadata_only=None, snippet='', authority=0.0, source='arxiv'):
    if metadata_only is None:
        metadata_only = not bool(abstract)
    return Paper(f'fixture:{idx}', f'Fixture paper {idx}', abstract,
                 'https://arxiv.org/abs/fixture-' + str(idx), year=year, venue=venue,
                 authors=list(authors or []), open_access_url=open_access_url,
                 abstract_source=abstract_source, metadata_only=metadata_only,
                 snippet=snippet, authority=authority, sources=[source], source=source,
                 publication={'records': deepcopy(records)} if records is not None else {})


class StateBoundaryTests(unittest.TestCase):
    def test_weak_same_hash_metadata_cannot_erase_strong_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            with StateStore(path) as store:
                strong = paper(1, authors=['Alice'], venue='ICLR', year=2025,
                               open_access_url='https://arxiv.org/pdf/fixture-1',
                               abstract_source='arxiv', snippet='strong snippet',
                               metadata_only=False, authority=0.9)
                weak = paper(1, authors=[], venue='', year=None, open_access_url='',
                             abstract_source='', metadata_only=True, snippet='', authority=0.1)
                store.register_paper(strong.to_dict())
                store.register_paper(weak.to_dict())
                row = dict(store.con.execute('SELECT * FROM papers').fetchone())
                self.assertEqual(json.loads(row['authors_json']), ['Alice'])
                self.assertEqual(row['open_access_url'], strong.open_access_url)
                self.assertEqual(row['abstract_source'], 'arxiv')
                self.assertEqual(row['snippet'], 'strong snippet')
                self.assertEqual(row['metadata_only'], 0)
                self.assertEqual(row['authority'], 0.9)

    def test_reuse_requires_current_exact_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            with StateStore(Path(directory) / 'state.sqlite3') as store:
                session = store.create_session({})
                item = paper(2, abstract='The method retrieves useful evidence.')
                store.add_candidate(session, item.to_dict(), 'fixture')
                ref = store.candidate_refs(session)[0]
                semantic = semantic_key('retrieval', '', 'none', 'model', 'prompt')
                computed = {'content_hash': ref['content_hash'], 'semantic_key': semantic,
                            'model': 'model', 'prompt_version': 'prompt'}
                store.put_assessment(session, paper_id=item.id, content=ref['content_hash'],
                                     semantic=semantic, computed=computed, relevance=2,
                                     category_status='unknown', understanding_sentence='fixture reason',
                                     evidence_quote=item.abstract, evidence_verified=True)
                self.assertIn(item.id, store.valid_assessments(
                    session, [item.id], task={'topic': 'retrieval'}, model='model',
                    prompt_version='prompt'))
                store.con.execute('UPDATE papers SET abstract = "the method retrieves useful evidence."')
                store.commit()
                self.assertNotIn(item.id, store.valid_assessments(
                    session, [item.id], task={'topic': 'retrieval'}, model='model',
                    prompt_version='prompt'))

    def test_uppercase_legacy_hashes_remain_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            with StateStore(Path(directory) / 'state.sqlite3') as store:
                session = store.create_session({})
                item = paper(3, abstract='A stable abstract for migration.')
                store.add_candidate(session, item.to_dict(), 'fixture')
                ref = store.candidate_refs(session)[0]
                semantic = semantic_key('migration', '', 'none', 'model', 'prompt')
                store.put_assessment(session, paper_id=item.id, content=ref['content_hash'],
                                     semantic=semantic,
                                     computed={'content_hash': ref['content_hash'],
                                               'semantic_key': semantic, 'model': 'model',
                                               'prompt_version': 'prompt'},
                                     relevance=0, category_status='unknown',
                                     understanding_sentence='fixture reason')
                old_hash = ref['content_hash'].upper()
                store.con.execute('PRAGMA foreign_keys = OFF')
                for table in ('papers', 'candidates', 'assessments'):
                    store.con.execute(f'UPDATE {table} SET content_hash = ?', (old_hash,))
                store.con.execute('UPDATE assessments SET computed_with = ?', (json.dumps({
                    'content_hash': old_hash, 'semantic_key': semantic, 'model': 'model',
                    'prompt_version': 'prompt'}),))
                store.commit()
                rows = store.valid_assessments(
                    session, [item.id], task={'topic': 'migration'}, model='model',
                    prompt_version='prompt')
                self.assertIn(item.id, rows)


class PublicationBoundaryTests(unittest.TestCase):
    def test_openalex_venue_identity_rejects_acl_neighbors_and_requires_unknown_exact_match(self):
        source = AcademicSource(Config(source='openalex', cache=False), Budget(60))
        rows = {'results': [
            {'display_name': 'NAACL', 'id': 'https://openalex.org/S1', 'type': 'conference', 'works_count': 500},
            {'display_name': 'Findings of ACL', 'id': 'https://openalex.org/S2', 'type': 'conference', 'works_count': 400},
            {'display_name': 'ACL Workshop', 'id': 'https://openalex.org/S3', 'type': 'conference', 'works_count': 300},
            {'display_name': 'Annual Meeting of the Association for Computational Linguistics',
             'id': 'https://openalex.org/S4', 'type': 'conference', 'works_count': 100},
        ]}
        with patch.object(source, '_json', return_value=rows):
            self.assertEqual(source._venue_id('ACL', 'conference'), 'S4')
        source = AcademicSource(Config(source='openalex', cache=False), Budget(60))
        with patch.object(source, '_json', return_value={'results': [
            {'display_name': 'Unknown Workshop', 'id': 'https://openalex.org/S5', 'works_count': 99},
            {'display_name': 'Unknown', 'id': 'https://openalex.org/S6', 'works_count': 1},
        ]}):
            self.assertEqual(source._venue_id('Unknown', 'any'), 'S6')

    def test_search_batch_requires_venue_and_year_from_one_publication_record(self):
        source = AcademicSource(Config(source='arxiv', cache=False), Budget(60))
        item = paper(4, venue='ICLR', year=2026, records=[
            record('ICLR', year=2024, venue_id='ICLR.cc/2024/Conference'),
            record('NeurIPS', year=2026, venue_id='NeurIPS.cc/2026/Conference'),
        ])
        with patch.object(source, '_search_one', return_value=[item]):
            batch = source.search_batch('fixture', filters={'venue': 'ICLR', 'year_from': 2026, 'year_to': 2026})
        self.assertEqual(batch.papers, [])
        with patch.object(source, '_search_one', return_value=[item]):
            matching = source.search_batch('fixture matching', filters={
                'venue': 'NeurIPS', 'year_from': 2026, 'year_to': 2026})
        self.assertEqual([p.id for p in matching.papers], [item.id])

    def test_search_batch_keeps_unknown_legacy_record_with_existing_metadata_semantics(self):
        source = AcademicSource(Config(source='arxiv', cache=False), Budget(60))
        item = paper(5, venue='ICLR', year=2026, records=None)
        with patch.object(source, '_search_one', return_value=[item]):
            batch = source.search_batch('fixture', filters={'venue': 'ICLR', 'year_from': 2026, 'year_to': 2026})
        self.assertEqual([p.id for p in batch.papers], [item.id])

    def test_publication_merge_keeps_verified_strength_observations_and_year_boundaries(self):
        accepted = record('A Venue', year=2025, venue_id='')
        weak = deepcopy(accepted)
        weak['verified'] = False
        weak['verification_reason'] = 'unverified_observation'
        facts = merge_publication_records([accepted, weak])
        self.assertEqual(len(facts['records']), 2)
        same_venue_other_year = deepcopy(accepted)
        same_venue_other_year.update(status='rejected', date='2026')
        facts = merge_publication_records([accepted, same_venue_other_year])
        self.assertFalse(facts['conflicts'])
        same_edition_conflict = deepcopy(accepted)
        same_edition_conflict['status'] = 'rejected'
        facts = merge_publication_records([accepted, same_edition_conflict])
        self.assertTrue(facts['conflicts'])
        self.assertTrue(all(not item['verified'] for item in facts['records']))

    def test_presentation_requires_real_openreview_acceptance_and_published_is_still_accepted_only(self):
        published = paper(6, records=[record('ACL', kind='conference', status='published',
                                             presentation='oral', source='acl_anthology')])
        self.assertTrue(publication_matches(published, {'accepted_only': True}))
        self.assertFalse(publication_matches(published, {'presentation_types': ['oral']}))
        fake_presentation = paper(7, records=[record('ICLR', status='accepted',
                                                      presentation='oral', source='openalex')])
        self.assertFalse(publication_matches(fake_presentation, {'presentation_types': ['oral']}))
        genuine = paper(8, records=[record('ICLR', status='accepted',
                                            presentation='oral', source='openreview')])
        self.assertTrue(publication_matches(genuine, {'presentation_types': ['oral']}))

    def test_evidence_requires_source_specific_official_https_host(self):
        attacks = [
            evidence('openreview', 'https://attacker.example/record'),
            evidence('fake_source', 'https://openreview.net/forum?id=fixture'),
            evidence('openreview', 'https://api.openalex.org/works/Wfixture'),
        ]
        for item in attacks:
            with self.subTest(item=item):
                p = paper(9, records=[record()])
                p.publication['records'][0]['evidence'] = item
                self.assertFalse(publication_matches(p, {'accepted_only': True}))


if __name__ == '__main__':
    unittest.main()
