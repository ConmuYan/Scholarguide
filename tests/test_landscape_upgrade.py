"""Research landscape contracts: offline model outputs, real selection and gates."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GuideError, Paper, Request
import workflow

PLAN = {'scope':'landscape', 'facets':[
    {'id':'geometry','label':'Geometric reconstruction','query':'3D geometric reconstruction'},
    {'id':'dynamic','label':'Dynamic reconstruction','query':'dynamic scene reconstruction'}]}

def candidate(identity, route='geometry', fit=90, category='no'):
    quote = ('We recover static geometry from multi-view images.' if route == 'geometry'
             else 'We recover dynamic surfaces with temporal consistency.')
    p = Paper(identity, 'Scene reconstruction approach ' + identity,
              quote + ' The study evaluates the proposed reconstruction setting.',
              'https://example.test/' + identity, year=2026, sources=['arxiv'])
    row = {'paper_id':identity,'relevance':2,'fit_score':fit,'paper_type':'research',
           'category_status':category,'evidence_quote':quote,'reason':'Directly studies the requested reconstruction setting.',
           'evidence_verified':True}
    match = {'paper_id':identity,'facet_ids':[route],'evidence_quote':quote,
             'verification':'quote_match_only'}
    return p, row, match

def inputs():
    rows = [candidate('P1',fit=95), candidate('P2',fit=94), candidate('P3',fit=93),
            candidate('P4','dynamic',fit=90)]
    return [r[0] for r in rows], {r[0].id:r[1] for r in rows}, [r[2] for r in rows]

class LandscapeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.request = Request('3D scene reconstruction', target_count=3, reading_goal='frontier')
        self.papers, self.assessments, matches = inputs()
        self.landscape = {**deepcopy(PLAN),'status':'ready','assignments':matches}

    def test_set_selection_covers_distinct_routes_instead_of_three_nearby_papers(self):
        selected = workflow.select_papers(self.papers,self.assessments,self.request,
                                          research=True,landscape=self.landscape)
        self.assertEqual(len(selected),3)
        self.assertIn('P4',[p['id'] for p in selected])
        self.assertEqual(workflow.coverage(selected,self.request,research=True,landscape=self.landscape),[])

    def test_count_satisfied_same_route_has_a_research_gap(self):
        selected = [{**p.to_dict(), **self.assessments[p.id]} for p in self.papers[:3]]
        gaps = workflow.coverage(selected,self.request,research=True,landscape=self.landscape)
        self.assertEqual(gaps[0]['requirement'],'research_coverage')
        self.assertIn('dynamic',gaps[0]['missing_facets'])

    def test_focused_scope_does_not_force_artificial_breadth(self):
        selected = workflow.select_papers(self.papers,self.assessments,self.request,
            research=True,landscape={'scope':'focused','status':'ready','facets':[],'assignments':[]})
        self.assertEqual([p['id'] for p in selected],['P1','P2','P3'])

    def test_explicit_category_quota_is_preserved(self):
        self.request.category,self.request.minimum_category_count='evaluation',2
        for identity in ('P1','P2'):
            self.assessments[identity]['category_status']='yes'
        selected = workflow.select_papers(self.papers,self.assessments,self.request,
                                          research=True,landscape=self.landscape)
        self.assertGreaterEqual(sum(p['category_status']=='yes' for p in selected),2)
        self.assertIn('P4',[p['id'] for p in selected])

    def test_low_fit_paper_does_not_displace_direct_evidence_for_breadth(self):
        self.assessments['P4']['fit_score']=15
        selected = workflow.select_papers(self.papers,self.assessments,self.request,
                                          research=True,landscape=self.landscape)
        self.assertNotIn('P4',[p['id'] for p in selected])
        self.assertTrue(workflow.coverage(selected,self.request,research=True,landscape=self.landscape))

    def test_legacy_selection_is_unchanged(self):
        selected = workflow.select_papers(self.papers,self.assessments,self.request,
                                          research=False,landscape=self.landscape)
        plain = workflow.select_papers(self.papers,self.assessments,self.request,research=False)
        self.assertEqual(selected,plain)

    def test_equal_route_coverage_preserves_existing_publication_preference_order(self):
        from research_landscape import diversify
        preferred,preferred_row,preferred_route=candidate('preferred',fit=90)
        other,other_row,other_route=candidate('other',fit=99)
        # The caller has already ranked publication preference within comparable
        # relevance/fit bands. Coverage must not undo that with exact fit scores.
        result=diversify([preferred,other],{'preferred':preferred_row,'other':other_row},
            Request('reconstruction',target_count=1),
            {**PLAN,'status':'ready','assignments':[preferred_route,other_route]})
        self.assertEqual([p.id for p in result],['preferred'])

class LandscapeValidationTests(unittest.TestCase):
    def module(self):
        import research_landscape
        return research_landscape

    def test_plan_rejects_duplicate_facets_and_focused_with_routes(self):
        m=self.module()
        for plan in ({'scope':'landscape','facets':[PLAN['facets'][0]]*2},
                     {'scope':'focused','facets':PLAN['facets']},
                     {'scope':'landscape','facets':[]}):
            with self.subTest(plan=plan), self.assertRaises(GuideError):
                m.validate_plan(plan)

    def test_assignments_require_real_ids_known_facets_and_exact_quotes(self):
        m=self.module()
        papers,_,matches=inputs()
        raw={'assignments':deepcopy(matches)}
        m.validate_review(raw,papers,PLAN)
        for field,value in (('paper_id','invented'),('facet_ids',['fake']),('evidence_quote','An invented method claim that is absent.')):
            bad=deepcopy(raw);bad['assignments'][0][field]=value
            with self.subTest(field=field),self.assertRaises(GuideError):
                m.validate_review(bad,papers,PLAN)

    def test_missing_assignments_are_not_coverage(self):
        m=self.module()
        papers,_,matches=inputs()
        with self.assertRaises(GuideError):
            m.validate_review({'assignments':matches[:-1]},papers,PLAN)

    def test_malformed_ids_are_controlled_validation_errors(self):
        m=self.module();papers,_,matches=inputs()
        for field,value in (('paper_id',{}),('facet_ids',[{}]),('facet_ids',[None])):
            bad={'assignments':deepcopy(matches)};bad['assignments'][0][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(GuideError):
                m.validate_review(bad,papers,PLAN)

    def test_coverage_review_can_recover_after_an_invalid_first_output(self):
        m=self.module();papers,_,matches=inputs()
        class Model:
            def generate(self,stage,prompt,data,schema,validate):
                result={'assignments':[{k:v for k,v in row.items() if k!='verification'} for row in matches]}
                validate(result);return result
        failed={**deepcopy(PLAN),'status':'unavailable','reason':'previous invalid review'}
        recovered=m.review(Model(),{'topic':'3D scene reconstruction'},papers,failed)
        self.assertEqual(recovered['status'],'ready')

    def test_task_and_abstract_changes_invalidate_review_identity(self):
        m=self.module()
        papers,_,_=inputs()
        task=asdict(Request('3D scene reconstruction',reading_goal='frontier'))
        first=m.review_key(task,papers,PLAN,'model-A')
        self.assertNotEqual(first,m.review_key({**task,'purpose':'dynamic only'},papers,PLAN,'model-A'))
        self.assertNotEqual(first,m.review_key(task,papers,PLAN,'model-B'))
        papers[0].abstract += ' Additional content.'
        self.assertNotEqual(first,m.review_key(task,papers,PLAN,'model-A'))

    def test_soft_venue_hints_come_from_catalog_and_never_override_user_constraints(self):
        m=self.module()
        planned={**deepcopy(PLAN),'status':'ready','preferred_venues':['CVPR','TPAMI']}
        original={'topic':'3D reconstruction','venue_scope':'preferred'}
        routed=m.routing_task(original,planned)
        self.assertEqual(routed['preferred_venues'],['CVPR','TPAMI'])
        self.assertNotIn('preferred_venues',original)
        for task in ({**original,'venue':'ICLR'}, {'topic':'reconstruction','venue_scope':'all'}):
            self.assertNotIn('preferred_venues',m.routing_task(task,planned))
        for venues in (['Invented Conference'],['CVPR','CVPR'],[{}]):
            with self.subTest(venues=venues),self.assertRaises(GuideError):
                m.validate_plan({**PLAN,'preferred_venues':venues})

    def test_planner_can_select_curated_sources_for_a_focused_task_without_forcing_breadth(self):
        m=self.module()
        class Model:
            def generate(self,stage,prompt,data,schema,validate):
                assert 'curated_venue_catalog' in data
                assert 'CVPR' in schema['properties']['preferred_venues']['items']['enum']
                result={'scope':'focused','facets':[],'preferred_venues':['CVPR']}
                validate(result);return result
        result=m.plan(Model(),{'topic':'sparse reconstruction','venue_scope':'preferred'})
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['scope'],'focused')
        self.assertEqual(m.routing_task({'venue_scope':'preferred'},result)['preferred_venues'],['CVPR'])

class LandscapeRuntimeTests(unittest.TestCase):
    def model(self, *, invalid=False, focused=False, cancelled=False):
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from test_research_runtime import OfflineLLM
        class Model(OfflineLLM):
            def generate(self,stage,prompt,data,schema,validate):
                if stage not in ('research_plan','research_coverage'):
                    if stage=='assess' and data.get('research_facets'):
                        def annotated(value):
                            for row in value['assessments']:
                                paper=next(p for p in data['papers'] if p['paper_id']==row['paper_id'])
                                row.update(facet_ids=['dynamic' if 'dynamic surfaces' in paper['abstract'] else 'geometry'],
                                    facet_quote='Invented evidence not in the paper.' if invalid else paper['abstract'])
                            validate(value)
                        value=super().generate(stage,prompt,data,schema,annotated)
                        if cancelled:self.budget.cancel_token.request()
                        return value
                    if stage=='action':
                        observation=data['observation']
                        route_gaps=[g for g in observation.get('gaps',[]) if g.get('requirement')=='research_coverage']
                        if 'ASSESS' not in data['allowed'] and 'SEARCH' in data['allowed'] and route_gaps:
                            queries=route_gaps[0].get('queries') or []
                            if queries:self.actions.insert(0,{'action':'SEARCH','query':queries[0],'reason':'Fill the missing route.'})
                    return super().generate(stage,prompt,data,schema,validate)
                if self.budget:
                    self.budget.check_cancelled()
                    guard=getattr(self.budget,'request_guard',None)
                    if guard:guard()
                    self.budget.llm_calls+=1
                self.calls.append({'stage':stage,'data':deepcopy(data)})
                if stage=='research_plan':
                    value={'scope':'focused','facets':[]} if focused else deepcopy(PLAN)
                else:
                    value={'assignments':[{'paper_id':p['paper_id'],
                        'facet_ids':['dynamic' if 'dynamic surfaces' in p['abstract'] else 'geometry'],
                        'evidence_quote':'Invented evidence not in the paper.' if invalid else p['abstract']}
                        for p in data['papers']]}
                validate(value)
                if stage=='research_coverage' and cancelled:self.budget.cancel_token.request()
                return value
        return Model()

    def workflow(self, *, invalid=False, focused=False, cancelled=False, method='A'):
        from common import Budget,Config
        from test_research_runtime import OfflineSource
        papers,_,_=inputs()
        source=OfflineSource([papers[:3],[papers[3]]])
        llm=self.model(invalid=invalid,focused=focused,cancelled=cancelled)
        budget=Budget(60);source.budget=llm.budget=budget
        task={'topic':'3D scene reconstruction','reading_goal':'frontier','target_count':3,'query':'3D scene reconstruction'}
        result=workflow.run('Understand recent reconstruction methods and research directions',
            Config(source='arxiv',research_mode=True,cache=False),method,
            request_data=task,source=source,llm=llm,budget=budget)
        return result,llm

    def test_search_fills_a_missing_route_after_count_is_already_met(self):
        result,llm=self.workflow()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['queries'],['3D scene reconstruction','dynamic scene reconstruction'])
        self.assertIn('P4',[p['id'] for p in result['papers']])
        self.assertEqual(result['research_coverage']['missing'],[])
        self.assertEqual(llm.count('research_plan'),1)
        self.assertEqual(llm.count('research_coverage'),0)
        self.assertEqual(llm.count('refine'),0)

    def test_single_pass_reports_partial_for_missing_route_without_padding(self):
        result,_=self.workflow(method='S')
        self.assertEqual(len(result['papers']),3)
        self.assertEqual(result['status'],'partial')
        self.assertIn('dynamic',result['research_coverage']['missing'])

    def test_focused_task_uses_one_search_without_coverage_calls(self):
        result,llm=self.workflow(focused=True)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(result['queries']),1)
        self.assertEqual(llm.count('research_coverage'),0)

    def test_invalid_route_evidence_preserves_papers_and_cannot_claim_completion(self):
        result,_=self.workflow(invalid=True,method='S')
        self.assertEqual(len(result['papers']),3)
        self.assertEqual(result['status'],'partial')
        self.assertIn(result['research_coverage']['status'],('partial','unavailable'))
        self.assertTrue(result['research_coverage']['missing'])

    def test_stop_during_review_retains_verified_papers(self):
        result,_=self.workflow(cancelled=True)
        self.assertEqual(result['stop_reason'],'user_cancelled')
        self.assertEqual(len(result['papers']),3)
        self.assertEqual(len(result['queries']),1)

    def test_chat_legacy_and_graph_recover_same_persisted_coverage_without_model_call(self):
        from common import Config
        from session import ChatSession
        from state import StateStore
        from research_runtime import current_landscape
        from test_research_runtime import OfflineSource
        for runtime in ('legacy','langgraph'):
            with self.subTest(runtime=runtime),tempfile.TemporaryDirectory() as directory:
                store=StateStore(Path(directory)/'state.db')
                papers,_,_=inputs()
                source=OfflineSource([papers[:3],[papers[3]]])
                llm=self.model()
                config=Config(source='arxiv',research_mode=True,agent_runtime=runtime,cache=False)
                chat=ChatSession(config,store,source=source,llm=llm)
                source.budget=llm.budget=chat.budget
                result=chat.start(request_data={'topic':'3D scene reconstruction',
                    'reading_goal':'frontier','target_count':3})
                self.assertEqual(result['status'],'complete')
                self.assertIn('P4',[p['id'] for p in result['papers']])
                session_id=chat.session_id
                store.close()
                store=StateStore(Path(directory)/'state.db')
                resumed=ChatSession(config,store,session_id=session_id,source=OfflineSource(),llm=self.model())
                restored=current_landscape(resumed)
                self.assertEqual(restored['status'],'ready')
                self.assertEqual(resumed.llm.calls,[])
                self.assertEqual(resumed.gaps(),[])
                store.close()

if __name__=='__main__':
    unittest.main()
