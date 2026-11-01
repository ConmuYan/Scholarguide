"""Source fallback is exercised through a live-mode fake provider, not demo."""
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Config, Paper
from webapp import ChatService
from session import ChatSession
from state import StateStore

QUOTE = 'This paper introduces a benchmark for retrieval augmented generation evaluation.'


class LiveSource:
    mode = 'live'

    def __init__(self, providers):
        self.providers = list(providers)

    def search(self, query, limit=10):
        return [Paper(f'{self.providers[0]}:p{i}', f'Paper {i}', QUOTE,
                      f'https://example.org/{i}', year=2025,
                      venue='CVPR' if self.providers[0] == 'openreview' else 'arXiv')
                for i in range(3)]


class LiveLLM:
    mode = 'live'

    def generate(self, stage, prompt, data, schema, validate):
        if stage == 'update':
            out = {'patch': {'topic': 'retrieval augmented generation'},
                   'summary': 'fixture', 'unresolved': []}
        elif stage == 'action':
            allowed = data['allowed']
            pending = (data.get('observation') or {}).get('pending_ids') or []
            if 'ASSESS' in allowed and pending:
                out = {'action': 'ASSESS', 'paper_ids': pending[:3], 'reason': 'assess'}
            elif 'SEARCH' in allowed:
                out = {'action': 'SEARCH', 'query': 'retrieval augmented generation', 'reason': 'search'}
            else:
                out = {'action': 'FINISH', 'reason': 'finish'}
        elif stage == 'assess':
            out = {'assessments': [
                {'paper_id': p['paper_id'], 'relevance': 2, 'category_status': 'yes',
                 'evidence_quote': QUOTE, 'paper_type': 'research'}
                for p in data['papers']]}
        else:
            out = {'query': 'retrieval augmented generation'}
        validate(out)
        return out


class SourceForkLiveTests(unittest.TestCase):
    def test_live_new_chat_selects_the_requested_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ChatService(output=Path(tmp) / 'web')
            config, source, llm = service._new_chat({'mode': 'live', 'source': 'openreview'})
            self.assertEqual(config.source, 'openreview')
            self.assertIsNone(source)
            store = StateStore(Path(tmp) / 'provider.sqlite3')
            try:
                chat = ChatSession(config, store)
                self.assertEqual(chat.source.providers, ['openreview'])
            finally:
                store.close()
                service.close()

    def test_live_mode_fork_preserves_task_and_changes_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ChatService(output=Path(tmp) / 'web')
            original_new_chat = service._new_chat

            def fake_new_chat(data):
                source_name = data.get('source') or 'arxiv'
                config = Config(provider='openai', model='fake-live',
                                source=source_name, base_url='http://127.0.0.1:1')
                return config, LiveSource([source_name]), LiveLLM()

            service._new_chat = fake_new_chat
            try:
                created = service.create({
                    'mode': 'live', 'scenario': 'followup', 'source': 'arxiv',
                    'message': 'Recommend RAG papers.',
                    'request_data': {'topic': 'retrieval augmented generation',
                                     'category': 'none', 'minimum_category_count': 0,
                                     'target_count': 3, 'unsupported': []}})
                old = created['session_id']
                old_task = service.snapshot(old)['history'][0]
                # The public snapshot intentionally hides the structured task;
                # read the persisted result for the source-fork invariant.
                import json
                old_result = json.loads((Path(tmp) / 'web' / old / 'turn-1' / 'result.json')
                                        .read_text(encoding='utf-8'))
                forked = service.fork_source(old, 'openreview')
                new = forked['session_id']
                self.assertNotEqual(old, new)
                for _ in range(100):
                    snap = service.snapshot(new)
                    if snap['status'] != 'running':
                        break
                    time.sleep(.02)
                self.assertNotEqual(snap['status'], 'running')
                self.assertEqual(snap['source'], 'openreview')
                result = json.loads((Path(tmp) / 'web' / new / 'turn-1' / 'result.json')
                                    .read_text(encoding='utf-8'))
                self.assertEqual(result['request']['topic'], old_result['request']['topic'])
                self.assertEqual(result['request']['target_count'], 3)
                self.assertEqual(service.snapshot(old)['history'][0], old_task)
            finally:
                service._new_chat = original_new_chat
                service.close()
