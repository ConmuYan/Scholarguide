"""Publication facts are authoritative, scoped and durable; no network calls."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import Budget, Config, GuideError, Paper
from source import AcademicSource, _publication, merge_papers
from state import StateStore


VENUE = 'ICLR.cc/2025/Conference'
GROUP = {'id': VENUE, 'content': {
    'submission_venue_id': {'value': VENUE + '/Submission'},
    'rejected_venue_id': {'value': VENUE + '/Rejected_Submission'},
    'withdrawn_venue_id': {'value': VENUE + '/Withdrawn_Submission'},
    'desk_rejected_venue_id': {'value': VENUE + '/Desk_Rejected_Submission'},
    'decision_field_name': {'value': 'decision'},
    'decision_heading_map': {'value': {
        'ICLR 2025 Poster': 'Accept (Poster)',
        'ICLR 2025 Oral': 'Accept (Oral)',
        'ICLR 2025 Spotlight': 'Accept (Spotlight)',
    }},
}}


def src(provider='openreview', **kwargs):
    return AcademicSource(Config(source=provider, cache=False, **kwargs), Budget(60))


def note(venue_id=VENUE, venue='ICLR 2025 Poster', **kwargs):
    return {'id': 'paper1', 'forum': 'paper1', 'domain': VENUE,
            'invitations': [VENUE + '/-/Submission'], 'pdate': 1737562377552,
            'content': {'title': {'value': 'A useful learning method'},
                        'abstract': {'value': 'We introduce a new learning method and evaluate it.'},
                        'venueid': {'value': venue_id}, 'venue': {'value': venue}}, **kwargs}


def publication(venue_id=VENUE, status='accepted', venue_name='ICLR 2025 Poster'):
    return _publication([{'type': 'conference', 'status': status, 'presentation': 'poster',
        'venue_id': venue_id, 'venue_name': venue_name, 'date': '2025', 'issn': [],
        'verified': True, 'identity_verified': True,
        'evidence': [{'source': 'openreview', 'url': 'https://openreview.net/forum?id=paper1',
                      'field': 'content.venueid', 'value': venue_id}]}])


def openalex_row(**kwargs):
    return {'id': 'https://openalex.org/W12345', 'title': 'A systematic review',
            'doi': 'https://doi.org/10.1145/3708499', 'type': 'review',
            'publication_year': 2024, 'publication_date': '2024-12-18',
            'abstract_inverted_index': {'We': [0], 'review': [1], 'retrieval.': [2]},
            'primary_location': {'version': 'publishedVersion', 'is_published': True,
                'source': {'id': 'https://openalex.org/S157921468', 'type': 'journal',
                           'display_name': 'ACM Computing Surveys',
                           'issn_l': '0360-0300', 'issn': ['0360-0300', '1557-7341']}}, **kwargs}


class OpenReviewPublicationTests(unittest.TestCase):
    def facts(self, value, group=GROUP):
        source = src()
        with patch.object(source, '_json', return_value={'groups': [deepcopy(group)]}):
            return source._openreview_publication(value)

    def test_official_accepted_venue_and_presentation_variants(self):
        for presentation in ('Poster', 'Oral', 'Spotlight'):
            with self.subTest(presentation=presentation):
                facts = self.facts(note(venue='ICLR 2025 ' + presentation))
                self.assertTrue(facts['verified'])
                self.assertEqual(facts['status'], 'accepted')
                self.assertEqual(facts['presentation'], presentation.lower())
                self.assertEqual(facts['venue_id'], VENUE)
                self.assertEqual(facts['date'], '2025')
                self.assertTrue(facts['accepted_at'].startswith('2025-01-22'))
                self.assertTrue(all(e['url'].startswith('https://') for e in facts['evidence']))

    def test_submitted_rejected_withdrawn_are_not_accepted_despite_poster_label(self):
        statuses = {'Submission': 'submitted', 'Rejected_Submission': 'rejected',
                    'Withdrawn_Submission': 'withdrawn', 'Desk_Rejected_Submission': 'desk_rejected'}
        for suffix, expected in statuses.items():
            with self.subTest(suffix=suffix):
                facts = self.facts(note(venue_id=VENUE + '/' + suffix))
                self.assertEqual(facts['status'], expected)
                self.assertTrue(facts['verified'])
                self.assertEqual(facts['presentation'], 'unknown')

    def test_blank_or_generic_venue_is_unknown(self):
        for venue_id, venue in (('', ''), ('', 'ICLR 2025 Poster'), ('ICLR 2025', 'ICLR 2025')):
            facts = self.facts(note(venue_id=venue_id, venue=venue))
            self.assertEqual(facts['status'], 'unknown')
            self.assertFalse(facts['verified'])

    def test_group_failure_remains_unknown_and_does_not_infer_from_source(self):
        source = src()
        with patch.object(source, '_json', side_effect=GuideError('HTTP 403')):
            facts = source._openreview_publication(note())
        self.assertFalse(facts['verified'])
        self.assertEqual(facts['status'], 'unknown')
        self.assertEqual(facts['verification_reason'], 'official_group_unavailable')

    def test_temporary_group_failure_is_retried_after_short_cache_expiry(self):
        source = src()
        with patch.object(source, '_json', side_effect=[GuideError('HTTP 403'), {'groups': [GROUP]}]) as fetch, \
             patch('source.time.monotonic', side_effect=[0, 61, 61]):
            self.assertEqual(source._openreview_group(VENUE), {})
            self.assertEqual(source._openreview_group(VENUE)['id'], VENUE)
        self.assertEqual(fetch.call_count, 2)

    def test_conditional_or_deleted_acceptance_is_not_final_verified_acceptance(self):
        decision = {'forum': 'paper1', 'invitations': [VENUE + '/Submission1/-/Decision'],
                    'signatures': [VENUE + '/Program_Chairs'],
                    'content': {'decision': {'value': 'Accept (conditional poster)'}}}
        facts = self.facts(note(details={'replies': [decision]}))
        self.assertEqual(facts['status'], 'unknown')
        self.assertFalse(facts['verified'])
        self.assertFalse(self.facts(note(ddate=1737562377552))['verified'])

    def test_author_reply_cannot_forge_acceptance_decision(self):
        decision = {'forum': 'paper1', 'invitations': [VENUE + '/Submission1/-/Decision'],
                    'signatures': [VENUE + '/Submission1/Authors'],
                    'content': {'decision': {'value': 'Accept (Oral)'}}}
        facts = self.facts(note(venue_id=VENUE + '/Submission', venue='Submitted to ICLR 2025',
                               details={'replies': [decision]}))
        self.assertEqual(facts['status'], 'submitted')

    def test_trusted_decision_can_prove_acceptance_but_contradictions_remain_unknown(self):
        decision = {'id': 'decision1', 'forum': 'paper1',
                    'invitations': [VENUE + '/Submission1/-/Decision'],
                    'signatures': [VENUE + '/Program_Chairs'],
                    'content': {'decision': {'value': 'Accept (Oral)'}}, 'tmdate': 1737562377552}
        facts = self.facts(note(venue_id=VENUE + '/Submission', venue='Submitted to ICLR 2025',
                               details={'replies': [decision]}))
        self.assertEqual(facts['status'], 'accepted')
        self.assertEqual(facts['presentation'], 'oral')
        self.assertTrue(facts['verified'])
        facts = self.facts(note(venue_id=VENUE + '/Withdrawn_Submission',
                               details={'replies': [decision]}))
        self.assertEqual(facts['status'], 'unknown')
        self.assertFalse(facts['verified'])
        self.assertEqual(facts['verification_reason'], 'conflicting_official_decisions')
        rejection = {**deepcopy(decision), 'id': 'decision2', 'tmdate': 1737562370000,
                     'content': {'decision': {'value': 'Reject'}}}
        facts = self.facts(note(details={'replies': [rejection, decision]}))
        self.assertEqual(facts['status'], 'unknown')
        self.assertEqual(facts['decisions'][0]['id'], 'decision1')
        self.assertEqual(len(facts['decisions']), 2)

    def test_top_level_timestamp_is_used_and_not_content_timestamp(self):
        value = note()
        value['content']['pdate'] = {'value': '1999'}
        facts = self.facts(value)
        self.assertEqual(facts['date'], '2025')
        self.assertTrue(facts['accepted_at'].startswith('2025-'))

    def test_topic_search_is_targeted_bounded_and_rechecks_exact_venueid(self):
        source = src()
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            if '/groups?' in url:
                return {'groups': [GROUP]}
            return {'notes': [note(), note(venue_id=VENUE + '/Submission', id='forged', forum='forged')]}
        with patch.object(source, '_json', side_effect=get):
            papers = source._openreview('in-context learning', 4, {'openreview_venue_ids': [VENUE]})
        self.assertEqual(len(papers), 1)
        search = parse_qs(urlsplit(calls[0]).query)
        self.assertEqual(search['term'], ['in-context learning'])
        self.assertEqual(search['venueid'], [VENUE])
        self.assertEqual(search['limit'], ['4'])
        self.assertEqual(sum('/groups?' in u for u in calls), 1)

    def test_stop_after_successful_search_preserves_notes_without_new_verification_request(self):
        source = src()
        def get(url, **kwargs):
            source.budget.cancel_token.request()
            return {'notes': [note()]}
        with patch.object(source, '_json', side_effect=get) as fetch:
            papers = source._openreview('learning', 3, {'openreview_venue_ids': [VENUE]})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(papers), 1)
        self.assertIn('new learning method', papers[0].abstract)
        self.assertFalse(papers[0].publication['verified'])


class JournalPublicationTests(unittest.TestCase):
    def test_known_journal_alias_uses_exact_issn_without_fuzzy_source_lookup(self):
        source = src('openalex')
        row = openalex_row()
        row['primary_location']['source'].update(display_name='Artificial Intelligence Review',
            issn=['0269-2821','1573-7462'],issn_l='0269-2821')
        with patch.object(source,'_venue_id',side_effect=AssertionError('No fuzzy name lookup')), \
             patch.object(source,'_json',return_value={'results':[row]}) as fetch:
            papers = source._openalex('large language model survey',3,venue='AI Review',
                                      constraints={'publication_type':'journal'})
        self.assertEqual(len(papers),1)
        self.assertEqual(papers[0].venue,'Artificial Intelligence Review')
        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)
        self.assertIn('primary_location.source.issn:0269-2821|1573-7462',query['filter'][0])

    def test_openalex_preserves_verified_journal_identity_type_and_exact_date(self):
        source = src('openalex')
        with patch.object(source, '_json', return_value={'results': [openalex_row()]}) as fetch:
            papers = source._openalex('retrieval', 4, constraints={
                'journal_issns': ['03600300'], 'publication_type': 'journal'})
        query = parse_qs(urlsplit(fetch.call_args[0][0]).query)
        self.assertIn('primary_location.source.issn:0360-0300', query['filter'][0])
        self.assertIn('primary_location.source.type:journal', query['filter'][0])
        facts = papers[0].publication
        self.assertEqual(papers[0].abstract, 'We review retrieval.')
        self.assertEqual(facts['status'], 'published')
        self.assertEqual(facts['type'], 'journal')
        self.assertEqual(facts['document_type'], 'review')
        self.assertEqual(facts['date'], '2024-12-18')
        self.assertEqual(facts['issn'], ['0360-0300', '1557-7341'])
        self.assertTrue(facts['verified'])

    def test_repository_name_editorial_retraction_or_unpublished_version_is_not_journal_publication(self):
        base = openalex_row()
        variations = []
        repository = deepcopy(base)
        repository['primary_location']['source']['type'] = 'repository'
        variations.append(repository)
        for changes in ({'type': 'editorial'}, {'is_retracted': True}, {'doi': None}):
            variations.append({**deepcopy(base), **changes})
        unpublished = deepcopy(base)
        unpublished['primary_location']['is_published'] = False
        variations.append(unpublished)
        for row in variations:
            with self.subTest(row=row):
                source = src('openalex')
                with patch.object(source, '_json', return_value={'results': [row]}):
                    facts = source._openalex('retrieval', 1)[0].publication
                self.assertNotEqual(facts['status'], 'published')

    def test_openalex_rechecks_requested_issn_even_if_api_returns_wrong_journal(self):
        source = src('openalex')
        with patch.object(source, '_json', return_value={'results': [openalex_row()]}):
            self.assertEqual(source._openalex('retrieval', 1, constraints={'journal_issns': ['0000-0000']}), [])

    def test_s2_filters_survive_bulk_fallback_and_missing_issn_stays_unverified(self):
        source = src('semantic_scholar')
        value = {'paperId': 's21', 'title': 'A review', 'abstract': 'A useful review.',
                 'year': 2025, 'publicationDate': '2025-04-02',
                 'externalIds': {'DOI': '10.1234/review'},
                 'publicationTypes': ['Journal Article', 'Review'],
                 'publicationVenue': {'id': 'journal1', 'type': 'journal', 'name': 'A Journal'},
                 'journal': {'name': 'A Journal'}}
        with patch.object(source, '_json', side_effect=[GuideError('HTTP 429'), {'data': [value]}]) as fetch:
            papers = source._semantic_scholar('retrieval', 2, {
                'publication_type': 'journal', 'venue': 'A Journal', 'year_from': 2024, 'year_to': 2025})
        queries = [parse_qs(urlsplit(call.args[0]).query) for call in fetch.call_args_list]
        for query in queries:
            self.assertEqual(query['publicationTypes'], ['JournalArticle'])
            self.assertEqual(query['venue'], ['A Journal'])
            self.assertEqual(query['year'], ['2024-2025'])
            self.assertIn('publicationDate', query['fields'][0])
        self.assertEqual(papers[0].publication['document_type'], 'review')
        self.assertEqual(papers[0].publication['date'], '2025-04-02')
        self.assertFalse(papers[0].publication['verified'])
        self.assertEqual(papers[0].publication['status'], 'unknown')

    def test_s2_review_tag_alone_is_not_journal_article_or_accepted_conference(self):
        source = src('semantic_scholar')
        for publication_types, kind in ((['Review'], 'journal'), (['Conference'], 'conference')):
            value = {'paperId': 's21', 'title': 'A review', 'year': 2025,
                     'externalIds': {'DOI': '10.1234/review'},
                     'publicationTypes': publication_types,
                     'publicationVenue': {'type': kind, 'name': 'A Venue', 'issn': '0360-0300'}}
            with patch.object(source, '_json', return_value={'data': [value]}):
                facts = source._semantic_scholar('retrieval', 1)[0].publication
            self.assertEqual(facts['status'], 'unknown')
            self.assertFalse(facts['verified'])

    def test_s2_verified_journal_requires_issn_doi_and_journal_article_type(self):
        source = src('semantic_scholar')
        value = {'paperId': 's21', 'title': 'A review', 'year': 2025,
                 'externalIds': {'DOI': '10.1234/review'},
                 'publicationTypes': ['JournalArticle', 'Review'],
                 'publicationVenue': {'id': 'csur', 'type': 'journal',
                    'name': 'ACM Computing Surveys', 'issn': '0360-0300',
                    'alternate_issns': ['1557-7341']}}
        with patch.object(source, '_json', return_value={'data': [value]}):
            facts = source._semantic_scholar('retrieval', 1, {'journal_issns': ['15577341']})[0].publication
        self.assertEqual(facts['status'], 'published')
        self.assertTrue(facts['verified'])
        self.assertEqual(facts['date'], '2025')
        self.assertEqual(facts['date_precision'], 'year')


class PublicationMergeAndPersistenceTests(unittest.TestCase):
    def test_title_only_cannot_transfer_verified_publication(self):
        a = Paper('arxiv:1', 'Same title', 'An abstract', 'https://arxiv.org/abs/1')
        b = Paper('openreview:paper1', 'Same title', 'Another abstract',
                  'https://openreview.net/forum?id=paper1', publication=publication())
        self.assertEqual(len(merge_papers([a, b])), 2)
        self.assertEqual(a.publication, {})

    def test_strong_identity_keeps_different_venue_statuses_separate(self):
        a = Paper('openreview:1', 'A title', 'An abstract', 'https://openreview.net/forum?id=1',
                  doi='10.1234/paper', publication=publication(status='rejected'))
        b = Paper('openreview:2', 'A title', 'An abstract', 'https://openreview.net/forum?id=2',
                  doi='10.1234/paper', publication=publication('NeurIPS.cc/2025/Conference', venue_name='NeurIPS 2025'))
        merged = merge_papers([a, b])[0]
        facts = {(r['venue_id'], r['status'], r['verified']) for r in merged.publication['records']}
        self.assertIn((VENUE, 'rejected', True), facts)
        self.assertIn(('NeurIPS.cc/2025/Conference', 'accepted', True), facts)
        self.assertEqual(len(a.publication['records']), 1)

    def test_same_venue_contradictory_status_is_not_washed_into_acceptance(self):
        a = Paper('openreview:1', 'Title', 'Abstract', 'https://openreview.net/forum?id=1',
                  doi='10.1234/paper', publication=publication())
        b = Paper('openreview:2', 'Title', 'Abstract', 'https://openreview.net/forum?id=2',
                  doi='10.1234/paper', publication=publication(status='withdrawn'))
        merged = merge_papers([a, b])[0]
        self.assertTrue(merged.publication['conflicts'])
        self.assertTrue(merged.provenance_conflict)
        self.assertTrue(all(not r['verified'] for r in merged.publication['records']))

    def test_unverified_text_does_not_invalidate_official_acceptance(self):
        official = publication()
        rumor = deepcopy(official['records'][0])
        rumor.update(status='rejected', verified=False)
        facts = _publication(official['records'] + [rumor])
        self.assertFalse(facts['conflicts'])
        self.assertEqual(facts['status'], 'accepted')
        self.assertTrue(facts['verified'])

    def test_reobserving_identical_facts_does_not_grow_records_without_bound(self):
        facts = publication()
        for minute in range(50):
            record = deepcopy(facts['records'][0])
            record['observed_at'] = f'2026-10-20T00:{minute:02}:00+00:00'
            facts = _publication(facts['records'] + [record], facts['conflicts'])
        self.assertEqual(len(facts['records']), 1)
        self.assertTrue(facts['verified'])

    def test_cached_publication_facts_expire_after_one_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = AcademicSource(Config(source='openreview', cache=True, cache_dir=Path(tmp)), Budget(60))
            paper = Paper('openreview:1', 'Title', 'Abstract', 'https://openreview.net/forum?id=1',
                          publication=publication())
            path = source._cache_path('learning', 2, ['openreview'], {})
            path.write_text(json.dumps({'stored_at': time.time() - 4000, 'papers': [paper.to_dict()]}), encoding='utf-8')
            with patch.object(source, '_search_one', return_value=[]) as fetch:
                batch = source.search_batch('learning', 2, ['openreview'], {})
            self.assertEqual(batch.papers, [])
            self.assertEqual(fetch.call_count, 1)

    def test_paper_round_trip_defaults_old_data_to_unknown(self):
        old = Paper('1', 'T', 'A', 'https://example.org/1')
        self.assertEqual(old.publication, {})
        new = Paper(**{**old.to_dict(), 'publication': publication()})
        self.assertEqual(Paper(**new.to_dict()).publication, publication())

    def test_same_content_metadata_update_persists_and_keeps_content_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            with StateStore(path) as store:
                a = Paper('1', 'Title', 'Abstract', 'https://arxiv.org/abs/1',
                          venue='arXiv', year=2024, sources=['arxiv'])
                before = store.register_paper(a.to_dict())
                b = Paper(**{**a.to_dict(), 'venue': 'ICLR 2025 Poster', 'year': 2025,
                    'url': 'https://openreview.net/forum?id=paper1',
                    'sources': ['arxiv', 'openreview'], 'publication': publication()})
                after = store.register_paper(b.to_dict())
                self.assertEqual(before, after)
            with StateStore(path) as store:
                row = dict(store.con.execute('SELECT * FROM papers').fetchone())
                self.assertEqual(row['venue'], 'ICLR 2025 Poster')
                self.assertEqual(row['year'], 2025)
                self.assertEqual(row['url'], b.url)
                self.assertEqual(row['source'], 'arxiv,openreview')
                self.assertEqual(json.loads(row['publication_json']), publication())
                store.register_paper(a.to_dict())
                row = dict(store.con.execute('SELECT * FROM papers').fetchone())
                self.assertEqual(json.loads(row['publication_json']), publication())
                self.assertEqual(row['venue'], 'ICLR 2025 Poster')
                self.assertEqual(row['year'], 2025)

    def test_state_updates_keep_other_publication_versions_and_conflicts_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with StateStore(Path(tmp) / 'state.sqlite3') as store:
                paper = Paper('1', 'Title', 'Abstract', 'https://openreview.net/forum?id=1',
                              publication=publication())
                store.register_paper(paper.to_dict())
                other = publication('NeurIPS.cc/2025/Conference', venue_name='NeurIPS 2025')
                store.register_paper({**paper.to_dict(), 'publication': other})
                row = dict(store.con.execute('SELECT * FROM papers').fetchone())
                facts = json.loads(row['publication_json'])
                self.assertEqual(len(facts['records']), 2)
                store.register_paper({**paper.to_dict(), 'publication': publication(status='withdrawn')})
                # A later one-sided re-observation cannot silently resolve contradiction.
                store.register_paper(paper.to_dict())
                row = dict(store.con.execute('SELECT * FROM papers').fetchone())
                facts = json.loads(row['publication_json'])
                iclr = [r for r in facts['records'] if r['venue_id'] == VENUE]
                self.assertTrue(all(not r['verified'] for r in iclr))
                self.assertEqual({r['status'] for r in iclr}, {'accepted', 'withdrawn'})
                self.assertTrue(facts['conflicts'])

    def test_old_database_migration_preserves_rows_and_defaults_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.sqlite3'
            con = sqlite3.connect(path)
            con.execute('CREATE TABLE papers(paper_id TEXT NOT NULL, content_hash TEXT NOT NULL, '
                'title TEXT NOT NULL, abstract TEXT NOT NULL, url TEXT, year INTEGER, venue TEXT, '
                'source TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(paper_id,content_hash))')
            con.execute("INSERT INTO papers VALUES ('1','hash','Title','Abstract','','2024','arXiv','arxiv','old')")
            con.commit()
            con.close()
            with StateStore(path) as store:
                row = dict(store.con.execute('SELECT * FROM papers').fetchone())
                self.assertEqual(row['title'], 'Title')
                self.assertEqual(json.loads(row['publication_json']), {})
                store.init_schema()
                self.assertEqual(store.con.execute('SELECT COUNT(*) FROM papers').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
