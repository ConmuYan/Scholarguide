"""Offline outcome propagation across persisted jobs and chat operations."""
import json
from pathlib import Path
import tempfile
import time
import unittest

from api_v1 import ProductApi, execution_error
from webapp import Jobs, ChatService


class OutcomeError(Exception):
    code = 'remote_outcome_unknown'
    remote_outcome = 'unknown'
    transaction_outcome = 'unknown'
    retry_safe = False


def wait(api, identity):
    for _ in range(300):
        value = api.snapshot(identity)
        if value['state'] not in ('running', 'queued'):
            return value
        time.sleep(.01)
    raise AssertionError('operation did not finish')


class OutcomeTests(unittest.TestCase):
    def test_unrecognized_classifications_and_codes_are_not_reflected(self):
        error = OutcomeError('SECRET')
        error.code = 'SECRET'
        error.remote_outcome = 'SECRET'
        error.transaction_outcome = 'SECRET'
        error.retry_safe = True
        error.retryable = True
        value = execution_error(error)
        self.assertEqual(value['code'], 'execution_error')
        self.assertFalse(value['retryable'])
        self.assertNotIn('remote_outcome', value)
        self.assertNotIn('transaction_outcome', value)
        self.assertNotIn('retry_safe', value)
        self.assertNotIn('SECRET', json.dumps(value))

    def test_job_outcomes_are_safe_and_durable(self):
        for remote in ('unknown', 'completed'):
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                def runner(data, progress):
                    exc = OutcomeError('SECRET query remote response')
                    exc.remote_outcome = remote
                    raise exc
                jobs = Jobs(root / 'runs', runner=runner)
                api = ProductApi(jobs, None, root / 'api')
                op = api.submit('search', {'query': 'private query', 'mode': 'demo'})
                value = wait(api, op['operation_id'])
                self.assertEqual(value['error']['remote_outcome'], remote)
                self.assertEqual(value['error']['transaction_outcome'], 'unknown')
                self.assertFalse(value['error']['retryable'])
                self.assertFalse(value['error']['retry_safe'])
                restarted = ProductApi(Jobs(root / 'runs', runner=runner), None, root / 'api')
                self.assertEqual(restarted.snapshot(op['operation_id'])['error'], value['error'])
                for name in ('job.json', 'failure.json'):
                    path = root / 'runs' / jobs.items[next(iter(jobs.items))]['id'] / name
                    if path.exists():
                        # job input is intentionally persisted for recovery; check only failure records.
                        doc = json.loads(path.read_text(encoding='utf-8'))
                        text = json.dumps(doc.get('job', doc))
                        self.assertNotIn('SECRET', text)
                        self.assertNotIn('remote response', text)

    def test_chat_failed_turn_preserves_previous_result_and_outcomes_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = ChatService(root / 'sessions')
            api = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), chat, root / 'api')
            first = api.submit('chat', {'message': 'RAG', 'mode': 'demo'})
            first = wait(api, first['operation_id'])
            self.assertEqual(first['state'], 'completed')
            sid = first['session_id']
            def fail(*args, **kwargs):
                exc = OutcomeError('SECRET query remote response')
                exc.remote_outcome = 'completed'
                raise exc
            chat.sessions[sid]['chat'].send = fail
            second = api.submit('chat', {'session_id': sid, 'message': 'follow up', 'mode': 'demo'})
            second = wait(api, second['operation_id'])
            self.assertEqual(second['state'], 'failed')
            self.assertEqual(second['error']['remote_outcome'], 'completed')
            self.assertEqual(second['error']['transaction_outcome'], 'unknown')
            self.assertFalse(second['error']['retry_safe'])
            self.assertFalse(second['error']['retryable'])
            self.assertEqual(api.snapshot(first['operation_id'])['state'], 'completed')
            terminal = (root / 'sessions' / sid / 'operations' / (second['operation_id'] + '.json')).read_text(encoding='utf-8')
            self.assertNotIn('SECRET', terminal)
            chat.close()
            reopened = ChatService(root / 'sessions')
            restored = ProductApi(Jobs(root / 'runs', runner=lambda *_: None), reopened, root / 'api')
            self.assertEqual(restored.snapshot(second['operation_id'])['error'], second['error'])
            self.assertEqual(restored.snapshot(first['operation_id'])['state'], 'completed')
            reopened.close()


if __name__ == '__main__':
    unittest.main()
