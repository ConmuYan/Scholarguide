from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from webapp import ChatService
from test_chat_reading_contract import ChatReadingContractTests


class ChatCandidateCompatibilityTests(unittest.TestCase):
    def test_only_explicit_partial_undelivered_candidates_are_exposed(self):
        result = ChatReadingContractTests().result()
        other = deepcopy(result['papers'][0])
        other.update(id='new-id', title='New candidate', secret='do not expose')
        result['candidates'].append(other)
        self.assertNotIn('candidates', ChatService._public(result))
        result['trace'] = [{'action': 'search', 'status': 'partial', 'stop_reason': 'user_cancelled'}]
        result['stop_reason'] = 'user_cancelled'
        public = ChatService._public(result)
        self.assertEqual([p['paper_id'] for p in public['candidates']], ['new-id'])
        self.assertNotIn('secret', public['candidates'][0])
        result['trace'].append({'action': 'search', 'status': 'ok'})
        self.assertNotIn('candidates', ChatService._public(result))

    def test_actual_partial_search_result_is_visible(self):
        from common import Budget, Config
        from demo import ChatDemoLLM
        from session import ChatSession
        from state import StateStore
        from test_partial_product import PartialSource
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / 'state.sqlite')
            try:
                source = PartialSource(Budget(5))
                chat = ChatSession(Config(source='lune'), store, source=source, llm=ChatDemoLLM())
                source.budget = chat.budget
                result = chat.start(message='Recommend five RAG papers.')
                self.assertEqual([p['paper_id'] for p in ChatService._public(result)['candidates']], ['lune:first'])
            finally:
                store.close()

    def test_partial_import_survives_restart(self):
        result = ChatReadingContractTests().result()
        other = deepcopy(result['papers'][0])
        other.update(id='new-id', title='New candidate')
        result['candidates'].append(other)
        result['trace'] = [{'action': 'search', 'status': 'partial', 'stop_reason': 'user_cancelled'}]
        result['stop_reason'] = 'user_cancelled'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ChatService(root)
            try:
                sid = service.import_run(result, 'b' * 32)
                self.assertEqual(service.snapshot(sid)['history'][0]['candidates'][0]['paper_id'], 'new-id')
            finally:
                service.close()
            service = ChatService(root)
            try:
                self.assertEqual(service.snapshot(sid)['history'][0]['candidates'][0]['paper_id'], 'new-id')
            finally:
                service.close()
