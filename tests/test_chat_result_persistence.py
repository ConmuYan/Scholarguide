"""Offline regressions for chat result persistence and operation identity."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from api_v1 import ProductApi
from webapp import ChatService, Jobs
from test_operation_outcomes import OutcomeError, wait


class ResultPersistenceTests(unittest.TestCase):
    def test_save_failure_survives_restart_and_later_turn(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            chat = ChatService(root / 'sessions')
            self.addCleanup(chat.close)
            api = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), chat, root / 'api')
            first = wait(api, api.submit('chat', {'message': 'RAG', 'mode': 'demo'})['operation_id'])
            self.assertEqual(first['state'], 'completed')
            sid = first['session_id']
            with patch('webapp.save_run', side_effect=OSError('synthetic disk failure')) as save:
                failed = wait(api, api.submit('chat', {'session_id': sid, 'message': 'second', 'mode': 'demo'})['operation_id'])
                self.assertEqual(save.call_count, 1)
            self.assertIn(failed['state'], ('failed', 'interrupted'))
            self.assertFalse(failed['error']['retryable'])
            self.assertFalse(failed['error']['persistence_confirmed'])
            self.assertFalse(chat.snapshot(sid)['capabilities']['can_retry'])
            self.assertEqual(len(chat.snapshot(sid)['history']), 2)
            self.assertEqual(api.snapshot(first['operation_id'])['state'], 'completed')
            self.assertFalse((root / 'sessions' / sid / 'turn-2' / 'result.json').exists())
            chat.close()
            reopened = ChatService(root / 'sessions')
            self.addCleanup(reopened.close)
            restored = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), reopened, root / 'api')
            self.assertIn(restored.snapshot(failed['operation_id'])['state'], ('failed', 'interrupted'))
            self.assertEqual(restored.snapshot(first['operation_id'])['state'], 'completed')
            later = wait(restored, restored.submit('chat', {'session_id': sid, 'message': 'third', 'mode': 'demo'})['operation_id'])
            self.assertEqual(later['state'], 'completed')
            self.assertIn(restored.snapshot(failed['operation_id'])['state'], ('failed', 'interrupted'))
            self.assertIsNone(restored.snapshot(failed['operation_id'])['result'])
            self.assertEqual(restored.snapshot(first['operation_id'])['state'], 'completed')
            reopened.close()


    def test_partial_directory_has_distinct_next_turn(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            chat = ChatService(root / 'sessions')
            self.addCleanup(chat.close)
            api = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), chat, root / 'api')
            first = wait(api, api.submit('chat', {'message': 'RAG', 'mode': 'demo'})['operation_id'])
            sid = first['session_id']
            def fail_after_mkdir(result, directory):
                directory.mkdir(parents=True)
                raise OSError('synthetic disk failure')
            with patch('webapp.save_run', side_effect=fail_after_mkdir):
                failed = wait(api, api.submit('chat', {'session_id': sid, 'message': 'second', 'mode': 'demo'})['operation_id'])
            self.assertEqual(failed['state'], 'interrupted')
            later = wait(api, api.submit('chat', {'session_id': sid, 'message': 'third', 'mode': 'demo'})['operation_id'])
            self.assertEqual(later['state'], 'completed')
            self.assertEqual(later['turn'], 3)
            self.assertEqual(api.snapshot(failed['operation_id'])['state'], 'interrupted')
            chat.close()
            restored_chat = ChatService(root / 'sessions')
            self.addCleanup(restored_chat.close)
            restored = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), restored_chat, root / 'api')
            self.assertEqual(restored.snapshot(first['operation_id'])['state'], 'completed')
            self.assertEqual(restored.snapshot(failed['operation_id'])['state'], 'interrupted')
            self.assertEqual(restored.snapshot(later['operation_id'])['state'], 'completed')

    def test_unrepairable_terminal_blocks_new_execution(self):
        from webapp import atomic_json
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            chat = ChatService(root / 'sessions')
            self.addCleanup(chat.close)
            api = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), chat, root / 'api')
            first = wait(api, api.submit('chat', {'message': 'RAG', 'mode': 'demo'})['operation_id'])
            sid = first['session_id']
            def broken(path, value):
                if value.get('state') == 'failed':
                    raise OSError('terminal write failure')
                return atomic_json(path, value)
            with patch.object(chat.sessions[sid]['chat'], 'send', side_effect=OutcomeError('original')):
                with patch('webapp.atomic_json', side_effect=broken):
                    failed = wait(api, api.submit('chat', {'session_id': sid, 'message': 'second', 'mode': 'demo'})['operation_id'])
            before = (root / 'sessions' / sid / 'runtime.json').read_bytes()
            with patch.object(chat.sessions[sid]['chat'], 'send') as send:
                with patch('webapp.atomic_json', side_effect=broken):
                    with self.assertRaises(OSError):
                        api.submit('chat', {'session_id': sid, 'message': 'third', 'mode': 'demo'})
                send.assert_not_called()
            self.assertEqual(before, (root / 'sessions' / sid / 'runtime.json').read_bytes())
            self.assertEqual(api.snapshot(failed['operation_id'])['error']['remote_outcome'], 'unknown')
            chat.close()

    def test_terminal_write_failures_keep_classification_and_concurrency(self):
        from webapp import atomic_json
        for broken_names in ({'runtime.json'}, {'operation'}, {'runtime.json', 'operation'}):
            with self.subTest(broken_names=broken_names), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                root = Path(tmp)
                chat = ChatService(root / 'sessions')
                self.addCleanup(chat.close)
                api = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), chat, root / 'api')
                first = wait(api, api.submit('chat', {'message': 'RAG', 'mode': 'demo'})['operation_id'])
                sid = first['session_id']
                original = chat.sessions[sid]['chat'].send
                def fail(*args, **kwargs):
                    raise OutcomeError('original')
                chat.sessions[sid]['chat'].send = fail
                def broken(path, value):
                    name = 'runtime.json' if Path(path).name == 'runtime.json' else 'operation'
                    if value.get('state') == 'failed' and name in broken_names:
                        raise OSError('terminal write failure')
                    return atomic_json(path, value)
                with patch('webapp.atomic_json', side_effect=broken):
                    failed = wait(api, api.submit('chat', {'session_id': sid, 'message': 'second', 'mode': 'demo'})['operation_id'])
                self.assertEqual(failed['state'], 'failed')
                self.assertEqual(failed['error']['remote_outcome'], 'unknown')
                self.assertFalse(failed['error']['persistence_confirmed'])
                entered, release = threading.Event(), threading.Event()
                def blocked(*args, **kwargs):
                    entered.set()
                    release.wait(5)
                    return original(*args, **kwargs)
                chat.sessions[sid]['chat'].send = blocked
                later = api.submit('chat', {'session_id': sid, 'message': 'third', 'mode': 'demo'})
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(api.snapshot(later['operation_id'])['state'], 'running')
                    self.assertEqual(api.snapshot(failed['operation_id'])['error'], failed['error'])
                finally:
                    release.set()
                self.assertEqual(wait(api, later['operation_id'])['state'], 'completed')
                self.assertEqual(api.snapshot(failed['operation_id'])['state'], 'failed')
                chat.close()
                restored_chat = ChatService(root / 'sessions')
                self.addCleanup(restored_chat.close)
                restored = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), restored_chat, root / 'api')
                value = restored.snapshot(failed['operation_id'])
                self.assertEqual(value['state'], 'failed')
                self.assertEqual(value['error']['remote_outcome'], 'unknown')
                self.assertEqual(restored.snapshot(first['operation_id'])['state'], 'completed')
                restored_chat.close()


if __name__ == '__main__':
    unittest.main()
