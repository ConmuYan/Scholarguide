"""End-to-end session tests with fakes: no network, no model, deterministic.

Covers the two paths the CLI must support:
1. initial request -> recommendation -> feedback -> reuse/recompute -> recommendation
2. clarification (ASK) -> saved state -> answer -> resume -> recommendation
"""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Config, Paper
from session import ChatSession
from state import StateStore

QUOTE = 'This paper introduces a benchmark for retrieval augmented generation evaluation.'
PAPERS = [
    Paper(id=f'p{i}', title=f'Paper {i} on retrieval augmented generation',
          abstract=QUOTE, url=f'https://arxiv.org/abs/2401.0000{i}', year=2024)
    for i in range(1, 7)
]


class FakeSource:
    mode = 'demo'

    def __init__(self, papers=None):
        self.papers = papers or PAPERS
        self.calls = []

    def search(self, query, limit=10):
        self.calls.append(query)
        return self.papers[:limit]


class FakeLLM:
    """Deterministic policy + assessor; records which stages ran."""
    mode = 'demo'

    def __init__(self, *, ask_first=False, update_category=None):
        self.stages = []
        self.ask_first = ask_first
        self.update_category = update_category
        self.asked = False

    def generate(self, stage, prompt, data, schema, validate):
        self.stages.append(stage)
        if stage == 'update':
            patch = {}
            message = str(data.get('message') or '')
            if not (data.get('task') or {}).get('topic'):
                patch['topic'] = 'retrieval augmented generation'
            if 'experiment' in message:
                patch['purpose'] = 'design an experiment'
            if self.update_category and 'survey' in message.lower():
                patch['category'] = self.update_category
                patch['minimum_category_count'] = 1
            unresolved = []
            if self.ask_first and not self.asked:
                unresolved = [{'field': 'purpose', 'question': 'Is this for a survey or an experiment?',
                               'blocking': True}]
            out = {'patch': patch, 'summary': 'fixture update', 'unresolved': unresolved}
            validate(out)
            return out

        if stage == 'action':
            allowed = data['allowed']
            observation = data.get('observation') or {}
            pending = observation.get('pending_ids') or []
            if self.ask_first and not self.asked and 'ASK' in allowed:
                self.asked = True
                out = {'action': 'ASK', 'question': 'Is this for a survey or an experiment?',
                       'affected_field': 'purpose', 'reason': 'purpose changes the query'}
            elif 'ASSESS' in allowed and pending:
                out = {'action': 'ASSESS', 'paper_ids': pending[:5], 'reason': 'judge new candidates'}
            elif 'SEARCH' in allowed:
                out = {'action': 'SEARCH', 'query': 'retrieval augmented generation evaluation',
                       'reason': 'need candidates'}
            elif 'FINISH' in allowed:
                out = {'action': 'FINISH', 'reason': 'requirements met'}
            else:
                out = {'action': 'FINISH', 'reason': 'nothing else is legal'}
            validate(out)
            return out

        if stage == 'assess':
            rows = []
            for paper in data['papers']:
                rows.append({'paper_id': paper['paper_id'], 'relevance': 2, 'category_status': 'yes',
                             'evidence_quote': QUOTE, 'reason': 'fixture grounded reason'})
            out = {'assessments': rows}
            validate(out)
            return out

        raise AssertionError(f'unexpected stage {stage}')


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / 'state.sqlite3')
        self.config = Config(provider='openai', model='fake-model', source='arxiv')
        self.llm = FakeLLM()
        self.source = FakeSource()
        self.chat = ChatSession(self.config, self.store, source=self.source, llm=self.llm)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _start(self):
        return self.chat.start(request_data={'topic': 'retrieval augmented generation',
                                             'category': 'evaluation',
                                             'minimum_category_count': 1,
                                             'target_count': 5})

    def test_initial_turn_searches_assesses_and_delivers(self):
        result = self._start()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['stop_reason'], 'requirements_met')
        self.assertEqual(len(result['papers']), 5)
        self.assertEqual(result['session_id'], self.chat.session_id)
        self.assertTrue(all(p['evidence_verified'] for p in result['papers']))
        actions = [event['action'] for event in self.store.events(self.chat.session_id)]
        self.assertIn('search', actions)
        self.assertIn('assess', actions)
        self.assertIn('reuse', actions)
        self.assertEqual(result['metrics']['cost_units']['settled'],
                         result['metrics']['cost_units']['reserved'])

    def test_feedback_updates_task_and_recomputes_only_stale(self):
        self._start()
        first = len(self.llm.stages)
        result = self.chat.send('Actually this is for experiment design.')
        self.assertEqual(result['revision'], 2)
        self.assertEqual(result['request']['purpose'], 'design an experiment')
        self.assertGreater(len(self.llm.stages), first)
        events = self.store.events(self.chat.session_id)
        reuse_events = [e for e in events if e['action'] == 'reuse']
        self.assertTrue(reuse_events)
        reasons = {item['reason'] for item in reuse_events[-1]['payload']['stale']}
        self.assertTrue(reasons.issubset({'semantic_key_changed', 'missing', 'content_changed',
                                          'model_changed', 'prompt_changed'}))
        self.assertEqual(result['status'], 'complete')

    def test_ask_saves_state_and_resumes_without_resetting_budget(self):
        llm = FakeLLM(ask_first=True)
        chat = ChatSession(self.config, self.store, source=self.source, llm=llm)
        result = chat.start(message='Find RAG papers; I am not sure whether for a survey.')
        self.assertEqual(result['status'], 'awaiting_user')
        self.assertEqual(result['stop_reason'], 'needs_clarification')
        self.assertIn('survey', result['question'])
        session = self.store.get_session(chat.session_id)
        self.assertEqual(session['status'], 'awaiting_user')
        before = chat.limits()['searches_left']

        resumed = chat.send('It is for a survey, one survey paper please.')
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(resumed['session_id'], chat.session_id)
        self.assertEqual(chat.limits()['searches_left'], before - 1)  # spent one, never reset
        actions = [e['action'] for e in self.store.events(chat.session_id)]
        self.assertIn('ask', actions)
        self.assertIn('resume', actions)

    def test_reopening_a_finished_session_works(self):
        self._start()
        again = self.chat.send('Add a survey paper too, please.')
        self.assertEqual(again['session_id'], self.chat.session_id)
        self.assertIn(again['status'], ('complete', 'partial'))

    def test_budget_exhaustion_returns_partial_with_reason(self):
        self.store.update_budget(self.chat.session_id,
                                 {'llm_left': 0, 'searches_left': 0, 'candidates_left': 0})
        result = self.chat.start(request_data={'topic': 'rag', 'category': 'none',
                                               'minimum_category_count': 0, 'target_count': 5})
        self.assertIn(result['status'], ('partial', 'no_results'))
        self.assertIn(result['stop_reason'], ('budget_exhausted', 'llm_budget_exhausted'))


if __name__ == '__main__':
    unittest.main()
