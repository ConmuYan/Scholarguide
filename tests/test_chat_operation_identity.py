"""Offline regressions for async operation identity and checkpoint failures."""
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from api_v1 import ProductApi
from webapp import ChatService, Jobs
from test_operation_outcomes import OutcomeError, wait


class IdentityTests(unittest.TestCase):
    def test_failed_turn_not_reassigned_after_reuse_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = ChatService(root / 'sessions')
            api = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), chat, root / 'api')
            first = wait(api, api.submit('chat', {'message': 'RAG', 'mode': 'demo'})['operation_id'])
            sid = first['session_id']
            original = chat.sessions[sid]['chat'].send
            def fail(*a, **kw):
                raise OutcomeError('secret')
            chat.sessions[sid]['chat'].send = fail
            second = wait(api, api.submit('chat', {'session_id': sid, 'message': 'second', 'mode': 'demo'})['operation_id'])
            entered, release = threading.Event(), threading.Event()
            def blocked(*a, **kw):
                entered.set()
                release.wait(5)
                return original(*a, **kw)
            chat.sessions[sid]['chat'].send = blocked
            third_op = api.submit('chat', {'session_id': sid, 'message': 'third', 'mode': 'demo'})
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(api.snapshot(second['operation_id'])['state'], 'failed')
            finally:
                release.set()
            third = wait(api, third_op['operation_id'])
            self.assertEqual(third['state'], 'completed')
            self.assertEqual(second['turn'], third['turn'])
            for current in (api,):
                now = current.snapshot(second['operation_id'])
                self.assertEqual(now['state'], 'failed')
                self.assertIsNone(now['result'])
                self.assertEqual(now['error'], second['error'])
            chat.close()
            reopened = ChatService(root / 'sessions')
            restored = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), reopened, root / 'api')
            self.assertEqual(restored.snapshot(second['operation_id'])['error'], second['error'])
            self.assertEqual(restored.snapshot(second['operation_id'])['state'], 'failed')
            reopened.close()

    def test_checkpoint_failure_retained_but_not_unverified_public_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def runner(data, progress):
                progress({'action': 'saved'}, checkpoint={'papers': ['not a paper'], 'secret': 'private'})
                raise OutcomeError('secret')
            api = ProductApi(Jobs(root / 'runs', runner=runner), None, root / 'api')
            op = api.submit('search', {'query': 'RAG', 'mode': 'demo'})
            value = wait(api, op['operation_id'])
            self.assertEqual(value['state'], 'failed')
            self.assertIsNone(value['result'])
            self.assertTrue(value['recovery']['checkpoint_available'])
            job = api.store.get(op['operation_id'])['target_id']
            self.assertIn('not a paper', (root / 'runs' / job / 'job.json').read_text(encoding='utf-8'))
            restarted = ProductApi(Jobs(root / 'runs', runner=runner), None, root / 'api')
            self.assertEqual(restarted.snapshot(op['operation_id'])['recovery'], value['recovery'])
            self.assertIsNone(restarted.snapshot(op['operation_id'])['result'])

    def test_terminal_write_failure_preserves_original_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = ChatService(root / 'sessions')
            api = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), chat, root / 'api')
            first = wait(api, api.submit('chat', {'message': 'RAG', 'mode': 'demo'})['operation_id'])
            sid = first['session_id']
            def fail(*a, **kw):
                raise OutcomeError('original')
            chat.sessions[sid]['chat'].send = fail
            from webapp import atomic_json
            def broken(path, value):
                if value.get('state') == 'failed' and Path(path).name in ('runtime.json', value.get('operation_id', '') + '.json'):
                    raise OSError('disk failure')
                return atomic_json(path, value)
            with patch('webapp.atomic_json', side_effect=broken):
                second = wait(api, api.submit('chat', {'session_id': sid, 'message': 'next', 'mode': 'demo'})['operation_id'])
            self.assertEqual(second['state'], 'failed')
            self.assertEqual(second['error']['remote_outcome'], 'unknown')
            self.assertFalse(second['error']['retryable'])
            self.assertFalse(second['error']['persistence_confirmed'])
            chat.close()
            reopened = ChatService(root / 'sessions')
            restored = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), reopened, root / 'api')
            value = restored.snapshot(second['operation_id'])
            self.assertNotEqual(value['state'], 'completed')
            self.assertFalse(value['error']['retryable'])
            reopened.close()
