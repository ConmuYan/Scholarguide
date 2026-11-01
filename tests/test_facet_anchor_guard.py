"""Conservative route evidence, using failures observed in bounded live QA.

These tests establish lexical rejection behavior, not expert research-coverage
labels. Unknown aliases remain unknown; relevance and recommendation are retained.
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import Paper
import research_landscape as landscape


PLAN={'scope':'landscape','facets':[
    {'id':'diffusion','label':'Generative reconstruction',
     'query':'3D scene reconstruction diffusion generative'},
    {'id':'feedforward','label':'Feed-forward reconstruction',
     'query':'feed-forward transformer 3D scene reconstruction'},
    {'id':'radiance','label':'Radiance fields',
     'query':'NeRF radiance field scene reconstruction'},
    {'id':'slam','label':'Geometry and mapping',
     'query':'SLAM geometry-aware 3D scene reconstruction'}]}


class FacetAnchorGuardTests(unittest.TestCase):
    def ground(self,quote,ids,planned=PLAN):
        paper=Paper('P','An academic paper',quote,'https://example.test/P')
        row={'paper_id':'P','relevance':2,'fit_score':94,'paper_type':'research',
             'category_status':'unknown','evidence_quote':quote,
             'reason':'The paper is relevant.','facet_ids':list(ids)}
        landscape.ground_facet_rows([row],[paper],planned)
        return paper,row

    def test_generic_exact_quote_cannot_establish_feedforward_method(self):
        quote=('we aim to leverage deep learning (DL) based algorithms to design '
               'a scene reconstruction approach with significantly improved accuracy')
        _,row=self.ground(quote,['feedforward'])
        self.assertEqual(row['facet_ids'],[])
        self.assertEqual(row['facet_status'],'insufficient_facet_anchor')
        self.assertEqual(row['evidence_quote'],quote)
        self.assertEqual((row['relevance'],row['fit_score']),(2,94))

    def test_generic_review_or_goal_does_not_establish_technical_routes(self):
        quotes=[('This comprehensive review systematically examines the evolution '
                 'of 3D reconstruction technologies, analyzing fundamental '
                 'methodologies, benchmark datasets, evaluation metrics'),
                ('a novel, training-free system for reconstructing, understanding, '
                 'and rendering 3D indoor scenes from a sparse set of unposed RGB images')]
        for quote in quotes:
            with self.subTest(quote=quote):
                _,row=self.ground(quote,['diffusion','radiance'])
                self.assertEqual(row['facet_ids'],[])

    def test_multiple_assignments_are_filtered_individually(self):
        _,row=self.ground('We distill video diffusion models for 3D scene reconstruction.',
                          ['diffusion','feedforward'])
        self.assertEqual(row['facet_ids'],['diffusion'])
        self.assertTrue(row['facet_quote'])
        self.assertEqual(row['facet_status'],'insufficient_facet_anchor')

    def test_explicit_method_terms_support_two_routes_in_same_quote(self):
        planned={'scope':'landscape','facets':[
            {'id':'gs','label':'Gaussian splatting',
             'query':'3D Gaussian Splatting scene reconstruction'},
            {'id':'diffusion','label':'Diffusion',
             'query':'diffusion model 3D scene reconstruction'}]}
        quote=('distill the implicit 3D knowledge in the video diffusion models '
               'into an explicit 3D Gaussian Splatting representation')
        _,row=self.ground(quote,['gs','diffusion'],planned)
        self.assertEqual(row['facet_ids'],['gs','diffusion'])
        self.assertEqual(row['facet_status'],'classified')

    def test_shared_topic_terms_cannot_establish_an_individual_route(self):
        _,row=self.ground('Our approach improves 3D scene reconstruction in experiments.',
                          ['diffusion','feedforward','radiance','slam'])
        self.assertEqual(row['facet_ids'],[])

    def test_geometry_and_geometric_use_conservative_morphological_forms(self):
        planned={'scope':'landscape','facets':[
            {'id':'geometry','label':'Geometry','query':'3D geometric reconstruction'},
            {'id':'dynamic','label':'Dynamics','query':'dynamic scene reconstruction'}]}
        _,row=self.ground('We recover static geometry from multi-view images.',
                          ['geometry'],planned)
        self.assertEqual(row['facet_ids'],['geometry'])

    def test_hyphen_spelling_is_normalized_without_substring_matching(self):
        _,row=self.ground('We introduce feedforward prediction from multiple images.',
                          ['feedforward'])
        self.assertEqual(row['facet_ids'],['feedforward'])
        _,row=self.ground('Our transformation improves forward navigation accuracy.',
                          ['feedforward'])
        self.assertEqual(row['facet_ids'],[])

    def test_no_distinguishing_query_terms_remain_unknown(self):
        planned={'scope':'landscape','facets':[
            {'id':'a','label':'Route A','query':'scene reconstruction method'},
            {'id':'b','label':'Route B','query':'scene reconstruction framework'}]}
        _,row=self.ground('Our scene reconstruction framework recovers geometry.',
                          ['a','b'],planned)
        self.assertEqual(row['facet_ids'],[])

    def test_rejected_assignment_produces_gap_instead_of_ready_coverage(self):
        paper,row=self.ground('Our new approach reconstructs 3D scenes accurately.',
                              ['diffusion','feedforward','radiance','slam'])
        coverage=landscape.assessment_coverage({'topic':'3D scenes'},[paper],
                                             {paper.id:row},PLAN)
        state=landscape.summary(coverage,[paper])
        self.assertEqual(state['status'],'partial')
        self.assertEqual(state['covered'],[])
        self.assertEqual(len(state['missing']),4)

    def test_compatibility_review_uses_the_same_gate(self):
        quote='Our new approach reconstructs 3D scenes accurately.'
        paper=Paper('P','An academic paper',quote,'https://example.test/P')
        class Model:
            def generate(self,stage,prompt,data,schema,validate):
                value={'assignments':[{'paper_id':'P','facet_ids':['feedforward'],
                                       'evidence_quote':quote}]}
                validate(value)
                return deepcopy(value)
        result=landscape.review(Model(),{'topic':'3D scenes'},[paper],PLAN)
        self.assertEqual(result['assignments'][0]['facet_ids'],[])
        self.assertEqual(result['assignments'][0]['evidence_quote'],'')


if __name__=='__main__':
    unittest.main()
