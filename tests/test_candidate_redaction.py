import json
import unittest

from api_v1 import public_candidates, public_result


class CandidateRedactionTests(unittest.TestCase):
    def test_nested_allowlist_preserves_publication_evidence(self):
        record = {'type': 'journal', 'status': 'published', 'verified': True,
                  'identity_verified': True, 'venue_id': 'S123', 'venue_name': 'Journal',
                  'date': '2025', 'date_precision': 'year', 'issn': ['1234-5678'],
                  'doi': '10.1/example', 'document_type': 'article',
                  'evidence': [{'source': 'openalex', 'url': 'https://api.openalex.org/works/W1',
                                'field': 'primary_location.source',
                                'value': {'id': 'S123', 'display_name': 'Journal',
                                          'type': 'journal', 'issn': ['1234-5678'],
                                          'secret': 'SYNTHETIC_NESTED_SECRET',
                                          'extra': [{'token': 'SYNTHETIC_LIST_SECRET'}]},
                                'token': 'SYNTHETIC_EVIDENCE_SECRET'}],
                  'secret': 'SYNTHETIC_RECORD_SECRET'}
        pub = {**record, 'records': [record], 'conflicts': [{'venue_id': 'S123',
                'statuses': ['published', 'rejected'], 'secret': 'SYNTHETIC_CONFLICT_SECRET'}],
               'schema_version': 1, 'unknown': {'secret': 'SYNTHETIC_PUB_SECRET'}}
        candidate = {'id': 'p1', 'title': 'A paper', 'authors': ['Author', {'secret': 'SYNTHETIC_AUTHOR_SECRET'}],
                     'sources': ['openalex', {'token': 'SYNTHETIC_SOURCE_SECRET'}],
                     'publication': pub, 'secret': 'SYNTHETIC_TOP_SECRET',
                     'reason': 'SYNTHETIC_REASON_SECRET', 'action': 'allow'}
        result = public_candidates([candidate])[0]
        self.assertNotIn('SYNTHETIC_', json.dumps(result))
        self.assertEqual(result['authors'], ['Author'])
        self.assertEqual(result['sources'], ['openalex'])
        self.assertEqual(result['publication']['records'][0]['evidence'][0]['value']['display_name'], 'Journal')
        self.assertEqual(result['publication']['conflicts'][0]['statuses'], ['published', 'rejected'])
        self.assertTrue(result['publication']['verified'])
        self.assertNotIn('value', public_candidates([{'id': 'p', 'title': 't', 'publication': {
            'evidence': [{'field': 'action', 'value': 'SYNTHETIC_ACTION_SECRET'}]}}])[0]
            ['publication']['evidence'][0])

    def test_selected_paper_publication_and_url_are_projected(self):
        paper = {'id': 'p', 'title': 'Paper', 'abstract': 'Abstract',
                 'url': 'https://example.org/a?token=SYNTHETIC_SECRET',
                 'publication': {'status': 'unknown', 'secret': 'SYNTHETIC_SECRET',
                                 'records': [{'status': 'unknown', 'secret': 'SYNTHETIC_SECRET'}]}}
        result = public_result({'papers': [paper]})
        self.assertEqual(result['papers'][0]['paper_id'], 'p')
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(result))

    def test_urls_types_and_no_candidate_promotion(self):
        candidate = {'id': 'p1', 'title': 'Paper', 'url': 'https://user:pw@example.org/a',
                     'open_access_url': 'https://example.org/a?access_token=SYNTHETIC_TOKEN',
                     'year': True, 'metadata_only': 'yes', 'abstract': {'secret': 'SYNTHETIC_SECRET'},
                     'publication': {'verified': 'true', 'evidence': [
                         {'source': 'openreview', 'field': 'decision', 'value': 'Accept',
                          'url': 'https://openreview.net/forum?id=paper1'},
                         {'source': 'x', 'url': 'javascript:alert(1)'},
                         {'url': 'https://example.org/?api_key=SYNTHETIC_KEY'}]}}
        result = public_result({'papers': [], 'candidates': [candidate]})
        self.assertEqual(result['papers'], [])
        item = result['candidates'][0]
        self.assertNotIn('SYNTHETIC_', json.dumps(item))
        for key in ('url', 'open_access_url', 'year', 'metadata_only', 'abstract'):
            self.assertNotIn(key, item)
        self.assertNotIn('verified', item['publication'])
        self.assertEqual(item['publication']['evidence'][0]['url'], 'https://openreview.net/forum?id=paper1')
        self.assertTrue(all('url' not in e for e in item['publication']['evidence'][1:]))

    def test_cycles_and_bounds(self):
        recursive = {}
        recursive['display_name'] = recursive
        recursive['secret'] = 'SYNTHETIC_SECRET'
        item = {'id': 'p', 'title': 'x' * 1000000, 'authors': ['a' * 100000] * 1000,
                'publication': {'records': [], 'evidence': [{'value': recursive}]}}
        item['publication']['records'].append(item['publication'])
        result = public_candidates([item] * 1000)
        self.assertLessEqual(len(result), 20)
        self.assertLess(len(json.dumps(result)), 1000000)
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(result))

    def test_truncated_publication_does_not_claim_verified(self):
        record = {'verified': True, 'evidence': [{'field': 'type', 'value': 'article'}] * 17}
        publication = {'verified': True, 'records': [record] * 17, 'conflicts': []}
        result = public_candidates([{'id': 'p', 'title': 't', 'publication': publication}])[0]['publication']
        self.assertFalse(result['verified'])
        self.assertTrue(all(not row['verified'] for row in result['records']))

    def test_legacy_evidence_gate_remains_compatible(self):
        from research_policy import publication_records
        record = {'type': 'conference', 'status': 'accepted', 'verified': True,
                  'venue_id': 'ICLR.cc/2025/Conference', 'venue_name': 'ICLR',
                  'date': '2025', 'presentation': 'poster', 'evidence': [
                      {'source': 'openreview', 'url': 'https://openreview.net/forum?id=paper1',
                       'field': 'decision', 'value': 'Accept (Poster)'}]}
        candidate = public_candidates([{'id': 'p', 'title': 't', 'publication': record}])[0]
        self.assertEqual(publication_records(candidate)[0]['evidence'], record['evidence'])
        self.assertEqual(publication_records(candidate)[0]['presentation'], 'poster')

    def test_null_and_invalid_candidates(self):
        self.assertEqual(public_candidates(None), [])
        self.assertEqual(public_candidates([None, {}, {'id': None, 'title': 'x'},
                                            {'id': 'x', 'title': None}]), [])
        self.assertEqual(public_candidates([{'paper_id': 'p', 'title': 'Title',
                                             'publication': None}])[0]['paper_id'], 'p')


if __name__ == '__main__':
    unittest.main()
