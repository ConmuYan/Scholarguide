"""LangGraph runtime tests: stage events, checkpoint, fallback and resume."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Config, GuideError
from demo import ChatDemoLLM, DemoSource
from session import ChatSession
from state import StateStore
from agent_runtime import LangGraphRuntime


class GraphRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = StateStore(self.root / 'state.sqlite3')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def chat(self, runtime='langgraph', store=None):
        return ChatSession(Config(agent_runtime=runtime), store or self.store,
                           source=DemoSource('followup'), llm=ChatDemoLLM())

    def test_graph_runs_explicit_stages_and_writes_checkpoint(self):
        chat = self.chat()
        result = chat.start(message='Recommend five RAG papers including one evaluation paper.')
        self.assertEqual(result['status'], 'complete')
        events = self.store.events(chat.session_id)
        nodes = [e['payload'].get('node') for e in events if e['action'] == 'agent_node']
        for node in ('planner', 'decide', 'critic', 'finalize'):
            self.assertIn(node, nodes)
        self.assertTrue((self.root / 'agent-checkpoints.sqlite3').exists())
        self.assertIn('agent_node', [e.get('action') for e in result['trace']])

    def test_checkpoint_is_read_on_same_thread(self):
        chat = self.chat()
        first = chat.start(message='Recommend five RAG papers including one evaluation paper.')
        runtime = chat.agent_runtime
        self.assertTrue(runtime.last_thread.endswith(':' + str(chat.revision)))
        resumed = runtime.run_turn()
        self.assertTrue(runtime.last_resumed)
        self.assertEqual(resumed['status'], first['status'])
        self.assertTrue(any(e['action'] == 'agent_node' and e['payload'].get('node') == 'checkpoint'
                            for e in self.store.events(chat.session_id)))

    def test_legacy_flag_skips_graph(self):
        chat = self.chat('legacy')
        result = chat.start(message='Recommend five RAG papers including one evaluation paper.')
        self.assertEqual(result['status'], 'complete')
        events = self.store.events(chat.session_id)
        self.assertNotIn('agent_node', [e['action'] for e in events])

    def test_critic_reflects_once_after_assessment_only(self):
        chat = self.chat()
        chat.store.save_task(chat.session_id, 1, {'topic': 'RAG', 'target_count': 5})
        chat._new_phase(first=True)
        chat._refine_query = lambda: 'new query'
        runtime = chat.agent_runtime
        actions_before = chat.limits()['actions_left']
        first = runtime.critic({'decision': {}, 'reflected': False})
        self.assertEqual(first['stage'], 'retrieve')
        self.assertTrue(first['reflected'])
        self.assertEqual(first['decision']['action'], 'SEARCH')
        self.assertEqual(chat.limits()['actions_left'], actions_before - 1)
        second = runtime.critic({'decision': {}, 'reflected': True})
        self.assertEqual(second['stage'], 'decide')

    def test_reflection_recovers_an_empty_first_search(self):
        class EmptyThenPapers(DemoSource):
            def __init__(self):
                super().__init__('followup')
                self.calls = 0
            def search(self, query, limit=10):
                self.calls += 1
                return [] if self.calls == 1 else super().search(query, limit)
        source = EmptyThenPapers()
        chat = ChatSession(Config(agent_runtime='langgraph'), self.store,
                           source=source, llm=ChatDemoLLM())
        result = chat.start(message='Recommend five RAG papers including one evaluation paper.')
        self.assertIn(result['status'], ('complete', 'partial'))
        self.assertGreaterEqual(source.calls, 2)
        self.assertTrue(result['papers'])
        nodes = [(e['payload'].get('node'), e['payload'].get('reflection'))
                 for e in self.store.events(chat.session_id) if e['action'] == 'agent_node']
        self.assertTrue(any(node == 'critic' and reason == 'new_query' for node, reason in nodes))

    def test_graph_keeps_session_resumable(self):
        chat = self.chat()
        first = chat.start(message='Recommend five RAG papers including one evaluation paper.')
        session_id = chat.session_id
        self.store.close()
        store = StateStore(self.root / 'state.sqlite3')
        try:
            resumed = ChatSession(Config(agent_runtime='langgraph'), store,
                                  session_id=session_id, source=DemoSource('followup'),
                                  llm=ChatDemoLLM())
            result = resumed.send('Add one survey paper.')
            self.assertEqual(result['session_id'], session_id)
            self.assertIn(result['status'], ('complete', 'partial', 'unsupported'))
            self.assertEqual(len(first['papers']), 5)
        finally:
            store.close()
            self.store = StateStore(self.root / 'state.sqlite3')

    def test_interrupted_graph_node_resumes_from_checkpoint(self):
        chat = self.chat()
        runtime = chat.agent_runtime
        original = runtime.retrieve
        def crash(_state):
            raise RuntimeError('synthetic graph crash')
        runtime.retrieve = crash
        first = chat.start(message='Recommend five RAG papers including one evaluation paper.')
        self.assertIn(first['status'], ('no_results', 'partial'))
        runtime.retrieve = original
        resumed = runtime.run_turn()
        self.assertTrue(runtime.last_resumed)
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(len(resumed['papers']), 5)

    def test_graph_failure_is_a_visible_partial(self):
        chat = self.chat()

        def broken():
            raise GuideError('synthetic planner failure')
        chat.next_decision = broken
        result = chat.start(message='Recommend five RAG papers.')
        self.assertIn(result['status'], ('partial', 'no_results'))
        self.assertTrue(any('LangGraph' in w or 'synthetic' in w for w in result['warnings']))


if __name__ == '__main__':
    unittest.main()
