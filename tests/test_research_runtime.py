"""Offline integration contracts for research discovery, state and synthesis.

The fakes use the live source/model interfaces (including model admission), so
these tests exercise production policy and persistence without HTTP or history.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config, GuideError, Paper, Request
from research_runtime import attach_brief
from session import ChatSession
from source import SearchBatch, SourceRun
from state import StateStore
from workflow import run as run_workflow


TASK = {'topic': 'visual retrieval', 'purpose': 'design a controlled experiment',
        'target_count': 3, 'category': 'none', 'minimum_category_count': 0,
        'reading_goal': 'orientation', 'query': 'visual retrieval'}


def paper(identity, *, abstract=None, metadata=False, authority=0, year=2024,
          venue='', publication=None, sources=None):
    return Paper(identity, f'Visual retrieval method {identity}',
                 (f'We introduce visual retrieval method {identity}. '
                  'Our approach is limited to static scenes.') if abstract is None else abstract,
                 f'https://example.test/{identity}', year=year, venue=venue,
                 authority=authority, sources=sources or ['arxiv'],
                 source=(sources or ['arxiv'])[0],
                 abstract_source='' if metadata else (sources or ['arxiv'])[0],
                 metadata_only=metadata, publication=publication or {})


def brief_output(papers):
    evidence, notes, order = [], [], []
    for index, item in enumerate(papers):
        identity = item.get('paper_id') or item.get('id')
        quote = item['abstract'].split('. ', 1)[0] + '.'
        ref = f'E{index}'
        evidence.append({'evidence_id': ref, 'paper_id': identity, 'quote': quote})
        claim = {'text': 'Introduces the retrieval method described in the abstract.',
                 'evidence_ids': [ref]}
        notes.append({'paper_id': identity, 'contribution': claim,
                      'method': deepcopy(claim), 'author_limitation': None,
                      'model_inference': None})
        order.append({'paper_id': identity, 'reason': 'Suggested comparison order.'})
    refs = [item['evidence_id'] for item in evidence[:2]]
    return {'evidence': evidence, 'paper_notes': notes,
        'themes': ([{'title': 'Retrieval approaches',
                    'text': 'The abstracts describe retrieval approaches.', 'evidence_ids': refs}]
                   if len(refs) >= 2 else []),
        'open_questions': ([{'question': 'How would the approaches compare under shared conditions?',
                             'evidence_ids': refs}] if refs else []),
        'research_hypotheses': [{
            'hypothesis': f'Proposed variant {name} may be less sensitive to query changes.',
            'rationale': 'The abstracts motivate comparing retrieval variants.',
            'experiment': 'Propose a paired comparison with fixed queries and compute.',
            'baseline': 'A fixed retrieval input as a proposed control.',
            'measure': 'Proposed query sensitivity and error measurements.',
            'falsifier': 'No consistent improvement would refute this proposal.',
            'evidence_ids': refs,
        } for name in ('A', 'B', 'C')] if refs else [],
        'reading_order': order}


class OfflineSource:
    mode = 'live'

    def __init__(self, batches=(), providers=('arxiv',), available=None):
        self.providers = list(providers)
        self.available_names = set(self.providers if available is None else available)
        self.batches = list(batches)
        self.calls = []
        self.health = {}
        self.budget = None

    def available(self, name):
        return name in self.available_names

    def health_snapshot(self):
        return deepcopy(self.health)

    def search(self, query, limit=10):
        return self.search_batch(query, limit, providers=self.providers).papers

    def search_batch(self, query, limit=10, providers=None, filters=None):
        names = list(self.providers if providers is None else providers)
        if not names or any(name not in self.providers or not self.available(name) for name in names):
            raise AssertionError('Unconfigured/unavailable source was executed.')
        if self.budget is not None:
            self.budget.check_cancelled()
            self.budget.search_http_calls += 1
        self.calls.append({'query': query, 'providers': names, 'filters': deepcopy(filters)})
        value = self.batches.pop(0) if self.batches else []
        if isinstance(value, GuideError):
            for name in names:
                self.health[name] = {'status': 'error', 'last_error': str(value), 'failures': 1}
            return SearchBatch(query, names, [], [SourceRun(name, 'error', error=str(value)) for name in names])
        return SearchBatch(query, names, deepcopy(value[:limit]),
                           [SourceRun(name, 'ok' if value else 'empty', len(value)) for name in names])


class OfflineLLM:
    mode = 'live'

    def __init__(self, ratings=None, actions=(), synthesis_error=None,
                 fake_proposal=False, cancel_after_assess=False,
                 expire_after_assess=False, omit_fit=False):
        self.ratings = ratings or {}
        self.actions = list(actions)
        self.synthesis_error = synthesis_error
        self.fake_proposal = fake_proposal
        self.cancel_after_assess = cancel_after_assess
        self.expire_after_assess = expire_after_assess
        self.omit_fit = omit_fit
        self.budget = None
        self.calls = []
        self.events = []

    def generate(self, stage, prompt, data, schema, validate):
        if self.budget is not None:
            self.budget.check_cancelled()
            guard = getattr(self.budget, 'request_guard', None)
            if guard:
                guard()
            self.budget.remaining()
            self.budget.llm_calls += 1
        self.calls.append({'stage': stage, 'prompt': prompt, 'data': deepcopy(data),
                           'schema': deepcopy(schema)})
        if stage == 'assess':
            rows = []
            for item in data['papers']:
                relevance, fit, kind, category = self.ratings.get(item['paper_id'], (2, 80, 'research', 'no'))
                rows.append({'paper_id': item['paper_id'], 'relevance': relevance, 'fit_score': fit,
                    'paper_type': kind, 'category_status': category,
                    'evidence_quote': (item['abstract'].split('. ', 1)[0] + '.') if item['abstract']
                                      else 'This fabricated quote is not in any abstract.',
                    'reason': 'The supplied abstract describes the requested retrieval topic.'})
                if self.omit_fit:
                    rows[-1].pop('fit_score')
            value = {'assessments': rows}
        elif stage == 'research_plan':
            # Existing cases test a narrow controlled experiment. Landscape
            # breadth and evidence classification have their own upgrade cases.
            value = {'scope':'focused','facets':[]}
        elif stage == 'synthesis':
            if self.synthesis_error:
                raise self.synthesis_error
            from tests.brief_fixture import packet_papers, wire_output
            value = wire_output(brief_output(packet_papers(data)), data)
            if self.fake_proposal:
                value['research_hypotheses'][0]['hypothesis'] = 'Experiments prove the proposed method improves retrieval.'
        elif stage == 'action':
            if self.actions:
                value = self.actions.pop(0)
            elif 'ASSESS' in data['allowed'] and data['observation']['pending_ids']:
                value = {'action': 'ASSESS', 'paper_ids': data['observation']['pending_ids'], 'reason': 'Assess new evidence.'}
            elif 'FINISH' in data['allowed']:
                value = {'action': 'FINISH', 'reason': 'Report the verified result and remaining gaps.'}
            else:
                previous = data['observation'].get('research_context', {}).get('previous_queries', [])
                value = {'action': 'SEARCH', 'query': 'visual retrieval ' + ('followup' if previous else 'discovery'),
                         'providers': ['arxiv'], 'intent': 'discovery', 'reason': 'Find source evidence.'}
        elif stage == 'refine':
            value = {'query': 'visual retrieval alternate query'}
        else:
            raise AssertionError('Unexpected offline model stage: ' + stage)
        validate(value)
        if stage == 'assess' and self.cancel_after_assess:
            self.budget.cancel_token.request()
        if stage == 'assess' and self.expire_after_assess:
            self.budget.started -= self.budget.seconds + 1
        return value

    def count(self, stage):
        return sum(call['stage'] == stage for call in self.calls)


class ResearchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'state.sqlite3'
        self.store = StateStore(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def chat(self, source=None, llm=None, *, task=None, research=True, session_id=None):
        source = source or OfflineSource()
        llm = llm or OfflineLLM()
        config = Config(research_mode=research, agent_runtime='legacy',
                        source=','.join(source.providers), model='offline-fixture', cache=False)
        chat = ChatSession(config, self.store, session_id=session_id, source=source, llm=llm)
        source.budget = llm.budget = chat.budget
        if task is not None:
            self.store.save_task(chat.session_id, chat.revision + 1,
                                 {**dict(task), **asdict(Request.from_dict(dict(task)))})
            chat._new_phase(first=True)
        return chat

    def prepared(self, **kwargs):
        papers = [paper(f'P{i}') for i in range(1, 4)]
        source = OfflineSource([papers])
        chat = self.chat(source, kwargs.pop('llm', None), task=kwargs.pop('task', TASK), **kwargs)
        chat._do_search('visual retrieval')
        chat._do_assess([item.id for item in papers])
        self.assertEqual(len(chat.select()), 3)
        return chat

    def workflow(self, papers=(), *, llm=None, source=None, task=None, method='S', budget=None):
        llm = llm or OfflineLLM()
        source = source or OfflineSource([list(papers)])
        budget = budget or Budget(60)
        source.budget = llm.budget = budget
        result = run_workflow('visual retrieval',
            Config(research_mode=True, agent_runtime='legacy', source=','.join(source.providers),
                   model='offline-fixture', cache=False), method=method,
            request_data=task or TASK, source=source, llm=llm, budget=budget)
        return result, llm, source

    def test_workflow_semantic_fit_beats_survey_mix_prestige_and_recency(self):
        items = [paper('weak-survey', authority=4, year=2026, venue='NeurIPS'),
                 paper('R1'), paper('R2'), paper('R3'), paper('lower-fit-survey')]
        ratings = {'weak-survey': (1, 100, 'survey', 'no'), 'R1': (2, 99, 'research', 'no'),
                   'R2': (2, 95, 'research', 'no'), 'R3': (2, 90, 'research', 'no'),
                   'lower-fit-survey': (2, 80, 'survey', 'no')}
        result, llm, _ = self.workflow(items, llm=OfflineLLM(ratings))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([item['id'] for item in result['papers']], ['R1', 'R2', 'R3'])
        self.assertEqual(result['gaps'], [])
        self.assertEqual(result['request']['reading_goal'], 'orientation')
        assess = next(call for call in llm.calls if call['stage'] == 'assess')
        self.assertIn('fit_score', assess['schema']['properties']['assessments']['items']['required'])
        self.assertEqual(llm.count('synthesis'), 1)
        self.assertEqual(result['research_brief']['source_level'], 'abstract_only')

    def test_explicit_category_quota_remains_hard_in_research(self):
        items = [paper('R1'), paper('R2'), paper('R3'), paper('evaluation')]
        ratings = {f'R{i}': (2, 99 - i, 'research', 'no') for i in range(1, 4)}
        ratings['evaluation'] = (1, 30, 'benchmark', 'yes')
        result, _, _ = self.workflow(items, llm=OfflineLLM(ratings),
            task={**TASK, 'category': 'evaluation', 'minimum_category_count': 1})
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([item['id'] for item in result['papers']], ['evaluation', 'R1', 'R2'])

    def test_chat_live_action_loop_retrieves_assesses_and_synthesizes(self):
        chat = self.chat(OfflineSource([[paper(f'P{i}') for i in range(1, 4)]]))
        result = chat.start(request_data=TASK)
        self.assertEqual(result['mode'], 'live')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['papers']), 3)
        # The last action is a program decision: once the delivery verifies,
        # the controller stops instead of spending another model action.
        self.assertEqual([call['stage'] for call in chat.llm.calls],
                         ['research_plan', 'action', 'assess', 'synthesis'])
        decisions = [event for event in chat.store.events(chat.session_id)
                     if event['action'] == 'decision']
        self.assertEqual(decisions[-1]['payload']['decision'], 'FINISH')
        self.assertEqual(decisions[-1]['payload']['context']['decision_source'], 'program')
        self.assertEqual(result['gaps'], [])
        self.assertEqual(chat.source.calls[0]['providers'], ['arxiv'])

    def test_action_context_includes_evidence_judgements_health_and_previous_queries(self):
        action = {'action': 'SEARCH', 'query': 'visual retrieval focused evidence',
                  'providers': ['openalex'], 'intent': 'fill_gap', 'reason': 'Use a configured healthy source.'}
        source = OfflineSource([[paper('P1')], GuideError('offline source fixture failed'),
                                [paper('P2', sources=['openalex'])]], providers=('arxiv', 'openalex'))
        llm = OfflineLLM(actions=[action])
        chat = self.chat(source, llm, task=TASK)
        chat._do_search('visual retrieval initial', providers=['arxiv'])
        chat._do_assess(['P1'])
        chat._do_search('visual retrieval failed facet', providers=['arxiv'])
        source.available_names.remove('arxiv')
        chat._new_phase(first=False)
        decision, stop = chat.next_decision()
        self.assertEqual(stop, '')
        self.assertTrue(decision.accepted)
        context = llm.calls[-1]['data']['observation']['research_context']
        self.assertEqual(context['configured_sources'], ['arxiv', 'openalex'])
        self.assertEqual(context['available_sources'], ['openalex'])
        self.assertEqual(context['previous_queries'], ['visual retrieval initial', 'visual retrieval failed facet'])
        self.assertEqual(context['candidates'][0]['paper_id'], 'P1')
        self.assertEqual(context['candidates'][0]['assessment']['fit_score'], 80)
        self.assertTrue(context['candidates'][0]['assessment']['evidence_verified'])
        self.assertIn('visual retrieval', context['candidates'][0]['abstract_excerpt'])
        self.assertEqual(context['recent_searches'][-1]['status'], 'error')
        self.assertIn('offline source fixture failed', context['source_health']['arxiv']['last_error'])
        self.assertIn('only explicit category quotas', context['reading_mix'])
        self.assertRegex(context['current_date'], r'^\d{4}-\d{2}-\d{2}$')
        chat.apply_decision(decision)
        self.assertEqual(source.calls[-1]['providers'], ['openalex'])
        self.assertEqual(source.calls[-1]['query'], action['query'])
        search = [event['payload'] for event in self.store.events(chat.session_id) if event['action'] == 'search'][-1]
        self.assertEqual(search['router']['plan']['steps'][0]['intent'], 'fill_gap')

    def test_action_cannot_execute_an_unconfigured_source(self):
        chat = self.chat(OfflineSource([[paper('P1')]]), OfflineLLM(actions=[{
            'action': 'SEARCH', 'query': 'visual retrieval unavailable venue',
            'providers': ['openreview'], 'intent': 'accepted', 'reason': 'Fixture invalid route.'}]), task=TASK)
        decision, _ = chat.next_decision()
        chat.apply_decision(decision)
        self.assertEqual(chat.source.calls[0]['providers'], ['arxiv'])
        self.assertTrue(all('openreview' not in call['providers'] for call in chat.source.calls))
        rejected = [event for event in self.store.events(chat.session_id) if event['action'] == 'source_hint_rejected']
        self.assertTrue(rejected)
        self.assertEqual(rejected[-1]['payload']['authorized'], ['arxiv'])
        event = [event for event in self.store.events(chat.session_id) if event['action'] == 'search'][-1]
        self.assertEqual(event['payload']['router']['plan']['providers'], ['arxiv'])

    def test_brief_cache_survives_store_reopen_without_model_or_llm_budget_cost(self):
        chat = self.prepared()
        selected = chat.select()
        first = attach_brief(chat, selected)
        self.assertIn(first['status'], ('ready', 'partial'))
        self.assertFalse(first['reused'])
        session_id, left = chat.session_id, chat.limits()['llm_left']
        self.store.close()
        self.store = StateStore(self.db)
        resumed = self.chat(session_id=session_id)
        self.assertEqual(resumed.limits()['llm_left'], left)
        resumed._consume(llm_left=left)
        cached = attach_brief(resumed, resumed.select())
        self.assertTrue(cached['reused'])
        self.assertEqual(cached['input_key'], first['input_key'])
        self.assertEqual(resumed.limits()['llm_left'], 0)
        self.assertEqual(resumed.llm.calls, [])
        cached['paper_notes'].clear()
        self.assertTrue(attach_brief(resumed, selected)['paper_notes'])

    def test_cache_purpose_and_abstract_changes_require_new_synthesis(self):
        chat = self.prepared()
        selected = chat.select()
        first = attach_brief(chat, selected)
        changed_task = {**chat.task(), 'purpose': 'prepare an implementation comparison'}
        self.store.save_task(chat.session_id, chat.revision + 1, changed_task)
        second = attach_brief(chat, selected)
        changed = deepcopy(selected)
        changed[0]['abstract'] += ' We also study a different retrieval input.'
        third = attach_brief(chat, changed)
        self.assertEqual(len({first['input_key'], second['input_key'], third['input_key']}), 3)
        self.assertEqual(chat.llm.count('synthesis'), 3)
        self.assertFalse(second['reused'])
        self.assertFalse(third['reused'])

    def test_cache_publication_evidence_and_research_constraints_changes_invalidate(self):
        chat = self.prepared()
        selected = chat.select()
        first = attach_brief(chat, selected)
        changed = deepcopy(selected)
        changed[0]['publication'] = {'records': [{'verified': True, 'status': 'accepted',
            'type': 'conference', 'venue_name': 'ICLR', 'date': '2025',
            'evidence': [{'url': 'https://openreview.net/forum?id=fixture'}]}]}
        metadata = attach_brief(chat, changed)
        self.assertNotEqual(metadata['input_key'], first['input_key'])
        self.assertFalse(metadata['reused'])
        self.store.save_task(chat.session_id, chat.revision + 1,
                             {**chat.task(), 'accepted_only': True, 'publication_type': 'conference'})
        constraints = attach_brief(chat, changed)
        self.assertNotEqual(constraints['input_key'], metadata['input_key'])
        self.assertFalse(constraints['reused'])
        self.assertEqual(chat.llm.count('synthesis'), 3)

    def test_synthesis_budget_exhaustion_keeps_verified_recommendations(self):
        chat = self.prepared()
        chat._consume(llm_left=chat.limits()['llm_left'])
        before = len(chat.llm.calls)
        result = chat._deliver('complete', 'requirements_met', chat.select())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['papers']), 3)
        self.assertEqual(result['research_brief']['status'], 'skipped')
        self.assertEqual(result['research_brief']['reason'], 'budget_exhausted')
        self.assertEqual(len(chat.llm.calls), before)
        self.assertEqual(chat.limits()['llm_left'], 0)
        self.assertNotIn('synthesis', chat.meter.summary().get('by_action', {}))

    def test_cancel_does_not_reuse_or_start_a_synthesis_and_keeps_verified_papers(self):
        chat = self.prepared()
        selected = chat.select()
        attach_brief(chat, selected)
        calls, left = len(chat.llm.calls), chat.limits()['llm_left']
        chat.request_stop()
        result = chat._deliver('complete', 'requirements_met', selected)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertEqual(len(result['papers']), 3)
        self.assertFalse(result['research_brief']['reused'])
        self.assertEqual(result['research_brief']['reason'], 'user_cancelled')
        self.assertEqual(len(chat.llm.calls), calls)
        self.assertEqual(chat.limits()['llm_left'], left)

    def test_model_failure_refunds_synthesis_cost_but_preserves_recommendations(self):
        chat = self.prepared(llm=OfflineLLM(synthesis_error=GuideError('offline model fixture failed')))
        left = chat.limits()['llm_left']
        result = chat._deliver('complete', 'requirements_met', chat.select())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['papers']), 3)
        self.assertEqual(result['research_brief']['status'], 'failed')
        self.assertEqual(chat.limits()['llm_left'], left - 1)
        synthesis = chat.meter.summary()['by_action']['synthesis']
        self.assertEqual(synthesis['settled'], 0)
        self.assertEqual(synthesis['refunded'], 1)

    def test_fit_scores_restore_and_assessment_prompt_versions_remain_isolated(self):
        ratings = {'P1': (2, 15, 'research', 'no'), 'P2': (2, 99, 'research', 'no'),
                   'P3': (2, 65, 'research', 'no')}
        chat = self.prepared(llm=OfflineLLM(ratings))
        session_id = chat.session_id
        self.assertEqual([item['id'] for item in chat.select()], ['P2', 'P3', 'P1'])
        self.store.close()
        self.store = StateStore(self.db)
        research = self.chat(session_id=session_id)
        self.assertEqual([item['fit_score'] for item in research.select()], [99, 65, 15])
        self.assertEqual(research._plan().stale_ids, [])
        legacy = self.chat(research=False, session_id=session_id)
        self.assertNotEqual(legacy.prompt_version, research.prompt_version)
        self.assertEqual(legacy._assessments(legacy.task()), {})
        self.assertEqual(set(legacy._plan().stale_ids), {'P1', 'P2', 'P3'})
        self.assertEqual(legacy.select(), [])
        research.rebind_provider(legacy.config, OfflineLLM())
        self.assertEqual(research.select(), [])

    def test_legacy_assessments_must_be_reassessed_before_research_delivery(self):
        legacy = self.prepared(research=False, task={**TASK, 'reading_goal': ''})
        research = self.chat(session_id=legacy.session_id)
        self.assertEqual(research.select(), [])
        self.assertEqual(set(research._plan().stale_ids), {'P1', 'P2', 'P3'})
        research._do_assess(research._plan().stale_ids)
        self.assertEqual(len(research.select()), 3)
        self.assertEqual(research.llm.count('assess'), 1)
        versions = self.store.con.execute('SELECT DISTINCT prompt_version FROM assessments').fetchall()
        self.assertEqual({row['prompt_version'] for row in versions},
                         {legacy.prompt_version, research.prompt_version})

    def test_no_available_source_never_fills_from_scripted_model_or_synthesizes(self):
        result, llm, source = self.workflow(source=OfflineSource(
            [[paper(f'P{i}') for i in range(1, 4)]], available=()))
        self.assertEqual(source.calls, [])
        self.assertEqual(result['status'], 'no_results')
        self.assertEqual(result['papers'], [])
        self.assertEqual(llm.count('assess'), 0)
        self.assertEqual(llm.count('synthesis'), 0)
        self.assertEqual(result['research_brief']['status'], 'skipped')
        self.assertTrue(any(gap['requirement'] == 'paper_count' for gap in result['gaps']))

    def test_extra_metadata_and_forged_quotes_do_not_complete_a_short_selection(self):
        items = [paper('P1'), paper('P2')] + [paper(f'M{i}', abstract='', metadata=True,
            venue='NeurIPS', authority=4, year=2026) for i in range(4)]
        result, llm, _ = self.workflow(items)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual({item['id'] for item in result['papers']}, {'P1', 'P2'})
        self.assertEqual(result['research_brief']['paper_ids'], ['P1', 'P2'])
        for identity in ('M0', 'M1', 'M2', 'M3'):
            self.assertEqual(result['assessments'][identity]['relevance'], 0)
            self.assertFalse(result['assessments'][identity]['evidence_verified'])
        self.assertEqual(len(llm.calls[-1]['data']['papers']), 2)
        self.assertTrue(any(gap.get('reason') == 'fewer_than_requested'
                            for gap in result['research_brief']['gaps']))

    def test_fake_observed_proposal_fails_brief_without_losing_recommendations(self):
        result, llm, _ = self.workflow([paper(f'P{i}') for i in range(1, 4)],
                                      llm=OfflineLLM(fake_proposal=True))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['papers']), 3)
        self.assertEqual(result['research_brief']['status'], 'failed')
        self.assertEqual(result['research_brief']['research_hypotheses'], [])
        self.assertEqual(llm.count('synthesis'), 1)

    def test_workflow_cancel_after_assessment_retains_evidence_and_skips_synthesis(self):
        result, llm, _ = self.workflow([paper(f'P{i}') for i in range(1, 4)],
                                      llm=OfflineLLM(cancel_after_assess=True))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertEqual(len(result['papers']), 3)
        self.assertEqual(result['research_brief']['status'], 'skipped')
        self.assertEqual(result['research_brief']['reason'], 'user_cancelled')
        self.assertEqual(llm.count('synthesis'), 0)

    def test_workflow_synthesis_timeout_keeps_selected_papers_without_an_extra_request(self):
        result, llm, _ = self.workflow([paper(f'P{i}') for i in range(1, 4)],
                                      llm=OfflineLLM(expire_after_assess=True))
        self.assertEqual(len(result['papers']), 3)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['research_brief']['status'], 'skipped')
        self.assertEqual(result['research_brief']['reason'], 'budget_exhausted')
        self.assertEqual(llm.count('synthesis'), 0)

    def test_research_assessment_missing_fit_is_rejected_instead_of_defaulted(self):
        result, llm, _ = self.workflow([paper(f'P{i}') for i in range(1, 4)],
                                      llm=OfflineLLM(omit_fit=True))
        self.assertEqual(result['papers'], [])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(llm.count('synthesis'), 0)
        self.assertTrue(any('fit_score' in item.get('error', '') for item in result['trace']))


if __name__ == '__main__':
    unittest.main()
