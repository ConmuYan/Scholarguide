"""Native checkpoint interruption must never replay a detached decision."""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from common import Config
from demo import ChatDemoLLM, DemoSource
from scripts.verify_delivery import isolated_environment
from session import ChatSession
from state import StateStore


def _native_worker(directory, boundary):
    from common import Budget
    from llm import JsonLLM
    from test_session import FakeLLM, FakeSource
    root = Path(directory)
    class Source(FakeSource):
        def search(self, query, limit=10):
            with (root / 'search-calls').open('a', encoding='utf-8') as stream:
                stream.write('search\n')
                stream.flush()
                os.fsync(stream.fileno())
            return super().search(query, limit)
    store = StateStore(root / 'state.sqlite3')
    try:
        config = Config(protocol='responses', action_protocol='responses_native',
                        agent_runtime='langgraph', model='synthetic', cache=False)
        chat = ChatSession(config, store, source=Source(), llm=FakeLLM())
        llm = JsonLLM(config, chat.budget)
        def post(url, payload, headers):
            with (root / 'model-calls').open('a', encoding='utf-8') as stream:
                stream.write('request\n')
                stream.flush()
                os.fsync(stream.fileno())
            chat.budget.llm_calls += 1
            return {'status': 'completed', 'usage': {'input_tokens': 1, 'output_tokens': 1},
                    'output': [{'type': 'function_call', 'call_id': 'synthetic-private-call-id',
                                'name': 'sg_search',
                                'arguments': json.dumps({'query': 'fixture query', 'reason': 'fixture'})}]}
        llm._post = post
        chat.llm = llm
        (root / 'session-id').write_text(chat.session_id, encoding='utf-8')
        def stop_at_boundary():
            (root / 'boundary').write_text(boundary, encoding='utf-8')
            while True:
                time.sleep(.05)
        if boundary == 'before_action':
            original = chat.agent_runtime.retrieve
            def retrieve(state):
                stop_at_boundary()
                return original(state)
            chat.agent_runtime.retrieve = retrieve
        else:
            chat._continue_native = lambda *args: stop_at_boundary()
        chat.start(request_data={'topic': 'fixture', 'target_count': 5,
                                 'category': 'none', 'minimum_category_count': 0})
    finally:
        store.close()


class NativeRecoveryTests(unittest.TestCase):
    def test_real_restart_never_replays_native_proposal_or_completed_action(self):
        from llm import JsonLLM
        from test_session import FakeLLM, FakeSource
        for boundary in ('before_action', 'before_continuation'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = isolated_environment(root)
                child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                                          '--native-worker', str(root), boundary],
                                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline and not (root / 'boundary').exists():
                        if child.poll() is not None:
                            self.fail('native worker terminated before boundary')
                        time.sleep(.02)
                    self.assertTrue((root / 'boundary').exists(), 'native worker did not reach boundary')
                finally:
                    child.kill()
                    child.communicate(timeout=5)
                session_id = (root / 'session-id').read_text(encoding='utf-8')
                before_search = (root / 'search-calls').read_text(encoding='utf-8') if (root / 'search-calls').exists() else ''
                before_model = (root / 'model-calls').read_text(encoding='utf-8')
                self.assertEqual(before_model.count('request'), 1)
                self.assertEqual(before_search.count('search'), 0 if boundary == 'before_action' else 1)
                store = StateStore(root / 'state.sqlite3')
                try:
                    config = Config(protocol='responses', action_protocol='responses_native',
                                    agent_runtime='langgraph', model='synthetic', cache=False)
                    source = FakeSource()
                    chat = ChatSession(config, store, session_id=session_id, source=source, llm=FakeLLM())
                    llm = JsonLLM(config, chat.budget)
                    with patch.object(llm, '_post', side_effect=AssertionError('model must not be replayed')):
                        chat.llm = llm
                        result = chat.agent_runtime.run_turn()
                    self.assertIn(result['status'], ('partial', 'no_results'))
                    self.assertEqual(result['stop_reason'], 'pending_operation_not_replayed')
                    self.assertEqual(source.calls, [])
                    self.assertNotIn('synthetic-private-call-id', repr(store.events(session_id)))
                    if boundary == 'before_continuation':
                        self.assertTrue(store.candidate_papers(session_id))
                finally:
                    store.close()
                self.assertEqual((root / 'model-calls').read_text(encoding='utf-8'), before_model)
                after_search = (root / 'search-calls').read_text(encoding='utf-8') if (root / 'search-calls').exists() else ''
                self.assertEqual(after_search, before_search)

    def test_unstarted_pending_node_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, isolated_environment(Path(directory)), clear=True), \
                 patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
                store = StateStore(Path(directory) / 'state.sqlite3')
                try:
                    config = Config(protocol='responses', agent_runtime='langgraph')
                    chat = ChatSession(config, store, source=DemoSource('followup'), llm=ChatDemoLLM())
                    runtime = chat.agent_runtime
                    def crash(_state):
                        raise RuntimeError('synthetic interruption before node entry')
                    runtime.retrieve = crash
                    chat.start(message='Recommend five RAG papers including one evaluation paper.')
                    config.action_protocol = 'responses_native'
                    runtime.retrieve = lambda state: self.fail('pending node replayed')
                    result = runtime.run_turn()
                    self.assertIn(result['status'], ('partial', 'no_results'))
                    self.assertTrue(any(e['payload'].get('resumed') == 'pending_not_replayed'
                                        for e in store.events(chat.session_id) if e['action'] == 'agent_node'))
                finally:
                    store.close()


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--native-worker':
        _native_worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
