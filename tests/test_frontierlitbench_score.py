import unittest
from copy import deepcopy
from evaluation.frontierlitbench_score import score


def gold():
    return [dict(paper_id=f'doi:10.1234/p{i}', dois=[f'10.1234/p{i}'], arxiv_ids=[f'2401.{i:05d}'],
                 title=f'Title {i}', year=2024, venue='ACL', tier='S' if i<3 else 'A') for i in range(5)] + [
            dict(paper_id='doi:10.1234/out', dois=['10.1234/out'], title='Outside', year=2025, venue='ICLR', tier='S')]

class FrontierScoreTests(unittest.TestCase):
    def test_adjudicated_exact_title_year_and_identifier_conflicts(self):
        addition = dict(doi='10.9999/new1', title='New study', decision='eligible', identity_verified=True,
                        track='C', year=2024, venue='ACL', tier='A')
        report = score({'papers':[{'title':'New study','year':2024}]}, gold(), 'FB-10', [addition])
        self.assertEqual(report['positions'][0]['match_method'], 'adjudicated_title_year')
        self.assertEqual(report['positions'][0]['gain'], 3)
        report = score({'papers':[{'title':'New study','year':2024,'doi':'10.9999/other'}]}, gold(), 'FB-10', [addition])
        self.assertEqual(report['positions'][0]['status'], 'pending_adjudication')

    def test_adjudications_extend_ideal_and_score_by_strong_id(self):
        corpus = gold(); before = deepcopy(corpus)
        additions = [dict(doi='10.9999/new1', decision='eligible', identity_verified=True,
                          track='C', year=2024, venue='ACL', tier='S'),
                     dict(doi='10.9999/new2', decision='eligible', identity_verified=True,
                          track='C', year=2024, venue='EMNLP', tier='S'),
                     dict(doi='10.9999/invalid', decision='invalid')]
        r = score({'papers':[{'doi':'10.9999/new1'}, {'doi':'10.9999/new1'},
                             {'doi':'10.9999/invalid'}]}, corpus, 'FB-10', additions)
        self.assertEqual([p['gain'] for p in r['positions']], [5,0,0,0,0])
        self.assertEqual(r['ideal_gains'], [5,5,5,5,5])
        self.assertEqual(r['eligible_adjudicated_count'], 2)
        self.assertEqual(r['positions'][1]['status'], 'duplicate')
        self.assertEqual(r['positions'][2]['status'], 'invalid_adjudicated')
        self.assertEqual(r['status'], 'scored')
        self.assertEqual(corpus, before)

    def test_adjudication_requires_strong_ids_and_valid_explicit_decisions(self):
        base = dict(doi='10.9999/new', decision='eligible', identity_verified=True,
                    track='C', year=2024, venue='ACL', tier='S')
        for additions in ([dict(title='Only title', decision='invalid')],
                          [{**base, 'identity_verified':False}],
                          [{**base, 'tier':'X'}], [{**base, 'track':'B'}],
                          [base, dict(base)], [dict(base, doi='10.1234/p0')],
                          [{**base, 'decision':'pending'}]):
            with self.subTest(additions=additions), self.assertRaises(ValueError):
                score({'papers':[]}, gold(), 'FB-10', additions)

    def test_adjudications_cannot_override_gold_or_force_conflicting_match(self):
        add = [dict(doi='10.9999/new', arxiv_id='2401.22222', decision='eligible',
                    identity_verified=True, track='C', year=2024, venue='ACL', tier='A')]
        for paper in ({'doi':'10.9999/new','arxiv_id':'2401.00000'},
                      {'doi':'10.9999/other','arxiv_id':'2401.22222'},
                      {'doi':'10.9999/new','title':'Title 0','year':2024}):
            with self.subTest(paper=paper):
                r=score({'papers':[paper]},gold(),'FB-10',add)
                self.assertEqual(r['status'],'pending_adjudication')
                self.assertEqual(r['positions'][0]['reason'],'conflicting_identifiers')

    def test_unmatched_and_task_outside_adjudications(self):
        add=[dict(doi='10.9999/new', decision='eligible', identity_verified=True,
                  track='C', year=2025, venue='ICLR', tier='S')]
        r=score({'papers':[{'doi':'10.9999/new'},{'title':'Only a title','year':2024}]},gold(),'FB-10',add)
        self.assertEqual(r['positions'][0]['status'],'out_of_scope')
        self.assertEqual(r['positions'][1]['status'],'pending_adjudication')
        self.assertEqual(r['eligible_adjudicated_count'],0)
        self.assertIsNone(r['metrics'])

    def test_exact_matching_duplicate_missing_and_metrics(self):
        result={'papers':[{'doi':'https://doi.org/10.1234/p0'}, {'id':'arxiv:2401.00001v2'},
                          {'title':'Title 2!', 'year':2024}, {'doi':'10.1234/p0'}]}
        r=score(result,gold(),'FB-10')
        self.assertEqual([p['gain'] for p in r['positions']],[5,5,5,0,0])
        self.assertEqual(r['positions'][3]['status'],'duplicate')
        self.assertEqual(r['positions'][4]['status'],'missing')
        self.assertEqual(r['metrics']['weighted_hit_at_5_percent'],60)
        self.assertEqual(r['metrics']['s_tier_hit_at_5'],3)
        self.assertGreater(r['metrics']['ndcg_at_5'],0)
        self.assertLess(r['metrics']['ndcg_at_5'],1)

    def test_gold_outside_is_pending_and_cannot_produce_final_metrics(self):
        r=score({'papers':[{'title':'Unknown', 'year':2024}, {'title':'Unknown', 'year':2024}]},gold(),'FB-10')
        self.assertEqual(r['status'],'pending_adjudication')
        self.assertIsNone(r['metrics'])
        self.assertEqual(r['positions'][1]['status'],'duplicate')

    def test_out_of_scope_is_zero_and_extras_ignored(self):
        r=score({'papers':[{'doi':'10.1234/out'}]*6},gold(),'FB-10')
        self.assertEqual(r['positions'][0]['status'],'out_of_scope')
        self.assertEqual(r['metrics']['ndcg_at_5'],0)
        self.assertEqual(r['extra_recommendations_ignored'],1)

    def test_ambiguous_title_requires_adjudication(self):
        corpus=gold(); corpus[1]['title']=corpus[0]['title']
        r=score({'papers':[{'title':'Title 0', 'year':2024}]},corpus,'FB-10')
        self.assertEqual(r['positions'][0]['reason'],'ambiguous_match')
        self.assertIsNone(r['metrics'])

    def test_gold_with_insufficient_records_is_ineligible(self):
        with self.assertRaises(ValueError): score({'papers':[]},gold()[:4],'FB-10')

    def test_conflicting_ids_are_not_forced_into_gold(self):
        for paper in ({'doi':'10.1234/p0', 'arxiv_id':'2401.00001'},
                      {'title':'Title 0', 'year':2024, 'doi':'10.9999/unrelated'}):
            with self.subTest(paper=paper):
                result=score({'papers':[paper]},gold(),'FB-10')
                self.assertIsNone(result['metrics'])
                self.assertEqual(result['positions'][0]['reason'],'conflicting_identifiers')

    def test_arxiv_preprint_year_does_not_override_gold_formal_year(self):
        result=score({'papers':[{'id':'arxiv:2401.00000', 'year':2023}]},gold(),'FB-10')
        self.assertEqual(result['positions'][0]['status'],'matched')

    def test_duplicate_gold_ids_are_rejected(self):
        corpus=gold(); corpus[1]['paper_id']=corpus[0]['paper_id']
        with self.assertRaises(ValueError): score({'papers':[]},corpus,'FB-10')

if __name__=='__main__': unittest.main()
