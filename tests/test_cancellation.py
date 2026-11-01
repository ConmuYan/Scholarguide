"""Cooperative Stop uses isolated local fixtures; no public APIs or paid calls."""
import json
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from agent_tools import ToolRegistry
from common import Budget, CancellationToken, Config, RunCancelled
from demo import ChatDemoLLM, DemoLLM, DemoSource
from llm import JsonLLM
from providers import ProviderStore
from retrieval import RetrievalRouter
from session import ChatSession
from source import AcademicSource, SearchBatch, SourceRun
from state import StateStore
from workflow import run
from webapp import ChatService, Jobs, create_server


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / 'state.sqlite3')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def chat(self, runtime='langgraph', llm=None):
        return ChatSession(Config(agent_runtime=runtime, model='cancel-fixture-' + runtime,
                                  batch_size=2), self.store,
                           source=DemoSource('satisfied'), llm=llm or ChatDemoLLM())

    def test_token_is_idempotent_and_interrupts_retry_backoff(self):
        token = CancellationToken()
        budget = Budget(60, cancel_token=token)
        entered = threading.Event()
        stopped = threading.Event()
        def backoff():
            entered.set()
            try:
                budget.wait(30)
            except RunCancelled:
                stopped.set()
        worker = threading.Thread(target=backoff)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertTrue(token.request())
        self.assertFalse(token.request())
        self.assertTrue(stopped.wait(1))
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_stop_after_admission_before_worker_starts_prevents_model_work(self):
        class NeverLLM:
            def generate(self, *args):
                raise AssertionError('a stopped admitted execution made a model call')
        chat = self.chat(llm=NeverLLM())
        token = chat.prepare_execution()
        self.assertTrue(chat.request_stop())
        result = chat.start(message='Find RAG papers.')
        self.assertIs(chat.budget.cancel_token, token)
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertEqual(result['status'], 'no_results')
        self.assertEqual(result['user_input'], 'Find RAG papers.')
        self.assertEqual(self.store.get_session(chat.session_id)['status'], 'finished')
        self.assertIn('cancelled', [e['action'] for e in self.store.events(chat.session_id)])

    def test_stop_waits_for_current_batch_commits_it_and_blocks_further_batches(self):
        for runtime in ('legacy', 'langgraph'):
            with self.subTest(runtime=runtime):
                entered, release = threading.Event(), threading.Event()
                class BlockingLLM(ChatDemoLLM):
                    batches = 0
                    def generate(self, stage, *args):
                        if stage == 'assess':
                            self.batches += 1
                            if self.batches == 1:
                                entered.set()
                                release.wait(3)
                        return super().generate(stage, *args)
                llm = BlockingLLM()
                chat = self.chat(runtime, llm)
                chat.prepare_execution()
                completed, errors = [], []
                def work():
                    try:
                        completed.append(chat.start(message='Find five RAG papers.'))
                    except Exception as exc:
                        errors.append(exc)
                worker = threading.Thread(target=work)
                worker.start()
                try:
                    self.assertTrue(entered.wait(3), 'assessment did not start')
                    self.assertTrue(chat.request_stop())
                    self.assertTrue(worker.is_alive(), 'Stop must not hard-kill the request')
                    self.assertFalse(completed)
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertFalse(errors)
                result = completed[0]
                self.assertEqual(result['stop_reason'], 'user_cancelled')
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(llm.batches, 1)
                self.assertEqual(len(result['papers']), 2)
                self.assertTrue(all(p['evidence_verified'] and p['evidence_quote']
                                    for p in result['papers']))
                self.assertNotIn('error', [e['action'] for e in result['trace']])
                self.assertEqual(len(self.store.valid_assessments(
                    chat.session_id, [p['id'] for p in result['papers']],
                    task=chat.task(), model=chat.model, prompt_version=chat.prompt_version)), 2)
                resumed = chat.send('Keep the request and continue.')
                self.assertFalse(chat.cancel_requested)
                self.assertEqual(resumed['status'], 'complete')
                self.assertEqual(resumed['session_id'], result['session_id'])

    def test_stop_during_intent_update_does_not_deliver_stale_assessments(self):
        chat = self.chat()
        first = chat.start(message='Find five RAG papers.')
        self.assertEqual(first['status'], 'complete')
        class UpdatingLLM:
            def generate(self, stage, prompt, data, schema, validate):
                self.assert_stage = stage
                if stage != 'update':
                    raise AssertionError('model work continued after Stop')
                chat.request_stop()
                result = {'patch': {'topic': 'protein design'}, 'summary': 'new topic',
                          'unresolved': []}
                validate(result)
                return result
        llm = UpdatingLLM()
        chat.llm = llm
        result = chat.send('Switch to protein design.')
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertEqual(result['request']['topic'], 'protein design')
        self.assertEqual(result['papers'], [])
        self.assertEqual(result['assessments'], {})
        self.assertTrue(result['candidates'])

    def test_cancellation_wins_over_elapsed_budget(self):
        chat = self.chat()
        chat._begin_turn_metrics()
        chat._new_phase(first=True)
        chat.store.save_task(chat.session_id, 1, {'topic': 'RAG', 'target_count': 5})
        limits = chat.limits()
        limits['phase_deadline'] = 0
        chat._save_limits(limits)
        chat.request_stop()
        result = chat.turn()
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertEqual(result['metrics']['llm_calls'], 0)

    def test_cancelled_invalid_http_output_cannot_start_json_repair(self):
        token = CancellationToken()
        budget = Budget(10, cancel_token=token)
        class Response:
            status = 200
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, _limit):
                token.request()
                return json.dumps({'choices': [{'message': {'content': 'not JSON'}}],
                                   'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}).encode()
        llm = JsonLLM(Config(provider='openai', base_url='http://localhost:1/v1'), budget)
        with patch('llm.urlopen', return_value=Response()) as opener:
            with self.assertRaises(RunCancelled):
                llm.generate('fixture', 'Return JSON', {}, {}, lambda value: None)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(budget.llm_calls, 1)
        self.assertEqual(budget.usage_calls, 1)
        self.assertEqual(budget.input_tokens, 7)

    def test_successful_inflight_model_response_is_not_discarded(self):
        token = CancellationToken()
        budget = Budget(10, cancel_token=token)
        class Response:
            status = 200
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, _limit):
                token.request()
                return json.dumps({'choices': [{'message': {'content': '{"ok":true}'}}],
                                   'usage': {'prompt_tokens': 5, 'completion_tokens': 2}}).encode()
        llm = JsonLLM(Config(provider='openai', base_url='http://localhost:1/v1'), budget)
        with patch('llm.urlopen', return_value=Response()) as opener:
            value = llm.generate('fixture', 'Return JSON', {}, {}, lambda value: None)
        self.assertEqual(value, {'ok': True})
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(budget.usage_calls, 1)

    def test_search_workflow_retains_verified_partial_and_skips_next_batch(self):
        token = CancellationToken()
        budget = Budget(30, cancel_token=token)
        class StoppingLLM(DemoLLM):
            batches = 0
            def generate(self, stage, *args):
                value = super().generate(stage, *args)
                if stage == 'assess':
                    self.batches += 1
                    token.request()
                return value
        llm = StoppingLLM()
        source = DemoSource('satisfied')
        result = run('Find RAG papers.', Config(batch_size=2),
                     request_data={'topic': 'RAG', 'target_count': 5},
                     source=source, llm=llm, budget=budget)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertEqual(len(result['papers']), 2)
        self.assertTrue(all(p['evidence_verified'] for p in result['papers']))
        self.assertEqual(llm.batches, 1)
        self.assertEqual(source.calls, 1)
        self.assertIn('cancelled', [e['action'] for e in result['trace']])
        self.assertNotIn('error', [e['action'] for e in result['trace']])

    def test_search_stop_after_abstract_update_drops_incompatible_prior_judgement(self):
        token = CancellationToken()
        budget = Budget(30, cancel_token=token)
        original = DemoSource('satisfied').papers[:3]
        class ChangingSource:
            mode = 'demo'
            calls = 0
            def search(self, query, limit=10):
                self.calls += 1
                if self.calls == 1:
                    return original
                token.request()
                return [replace(original[0], abstract=(
                    'A replacement abstract requiring a fresh assessment. ' * 100))]
        result = run('Find five RAG papers.', Config(batch_size=3),
                     request_data={'topic': 'RAG', 'target_count': 5},
                     source=ChangingSource(), llm=DemoLLM(), budget=budget)
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertEqual(len(result['papers']), 2)
        self.assertNotIn(original[0].id, [p['id'] for p in result['papers']])
        self.assertNotIn(original[0].id, result['assessments'])
        self.assertTrue(all(p['evidence_quote'] in p['abstract'] for p in result['papers']))

    def test_cancelled_source_retry_cannot_mark_provider_unhealthy(self):
        token = CancellationToken()
        source = AcademicSource(Config(source='semantic_scholar', cache=False),
                                Budget(30, cancel_token=token))
        def cancelled_request(*args, **kwargs):
            token.request()
            raise HTTPError('http://localhost/fixture', 429, 'fixture', {}, None)
        with patch.object(source, '_wait_for'), patch('source.urlopen', side_effect=cancelled_request) as opener:
            with self.assertRaises(RunCancelled):
                source._fetch('http://localhost/fixture', provider='semantic_scholar')
        self.assertEqual(opener.call_count, 1)
        self.assertFalse(source.cooldown)
        self.assertFalse(source.health_snapshot())

    def test_source_stop_keeps_first_provider_and_never_runs_the_next(self):
        token = CancellationToken()
        source = AcademicSource(Config(source='multi', cache=False), Budget(30, cancel_token=token))
        paper = DemoSource('satisfied').papers[0]
        called = []
        def completed_provider(name, *args):
            called.append(name)
            token.request()
            return [paper]
        with patch.object(source, '_search_one', side_effect=completed_provider):
            batch = source.search_batch('RAG', providers=['arxiv', 'openalex'])
        self.assertEqual(called, ['arxiv'])
        self.assertEqual([p.id for p in batch.papers], [paper.id])
        self.assertEqual([row.status for row in batch.runs], ['ok'])
        self.assertEqual(batch.summary()['stop_reason'], 'user_cancelled')
        self.assertEqual(batch.runs[0].remote_outcome, 'completed')
        self.assertIs(batch.runs[0].retry_safe, False)
        self.assertEqual(source.health_snapshot()['arxiv']['failures'], 0)

    def test_router_stop_keeps_completed_batch_without_query_or_paid_fallback(self):
        token = CancellationToken()
        paper = DemoSource('satisfied').papers[0]
        class Source:
            providers = ['arxiv', 'openalex', 'deepseek']
            deepseek_calls = 0
            budget = Budget(30, cancel_token=token)
            calls = []
            def available(self, name):
                return True
            def search_batch(self, query, limit=10, providers=None, filters=None):
                self.calls.append((query, providers))
                token.request()
                return SearchBatch(query, providers or self.providers, [paper],
                                   [SourceRun('arxiv', 'ok', 1)])
        source = Source()
        router = RetrievalRouter(Config(source='multi', cache=False), source)
        router.tools = ToolRegistry.from_source(source)
        result = router.retrieve({'topic': 'RAG', 'category': 'evaluation', 'target_count': 5}, 'RAG')
        self.assertEqual(len(source.calls), 1)
        self.assertEqual([p.id for p in result.papers], [paper.id])
        self.assertFalse(result.errors)
        self.assertEqual(router.tools.health()['arxiv']['failures'], 0)


class CancellationHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.chat = ChatService(self.root / 'chat')
        self.jobs = Jobs(self.root / 'search')
        self.server = create_server(0, jobs=self.jobs, chat=self.chat,
                                    providers=ProviderStore(self.root / 'providers.json'))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.chat.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Host': f'127.0.0.1:{self.server.server_port}'}
        if body is not None:
            body = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)

    def finished(self, path, field='status'):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, snapshot = self.request('GET', path)
            self.assertEqual(status, 200)
            if snapshot[field] != 'running':
                return snapshot
            time.sleep(.02)
        self.fail('cooperatively stopped worker did not finish')

    def test_chat_cancel_waits_preserves_history_is_idempotent_and_can_continue(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingLLM(ChatDemoLLM):
            batches = 0
            def generate(self, stage, *args):
                if stage == 'assess':
                    self.batches += 1
                    if self.batches == 1:
                        entered.set()
                        release.wait(8)
                return super().generate(stage, *args)
        llm = BlockingLLM()
        with patch.object(self.chat, '_new_chat', return_value=(
                Config(batch_size=2, model='http-cancel-fixture'), DemoSource('satisfied'), llm)):
            status, created = self.request('POST', '/api/chat/sessions', {
                'mode': 'demo', 'message': 'Find five RAG papers.', 'stream': True})
        self.assertEqual(status, 201)
        session_id = created['session_id']
        path = '/api/chat/sessions/' + session_id
        try:
            self.assertTrue(entered.wait(3))
            for _ in range(2):
                status, stopped = self.request('POST', path + '/cancel', {})
                self.assertEqual(status, 200)
                self.assertTrue(stopped['accepted'])
                self.assertEqual(stopped['cancellation_state'], 'stopping')
            status, pending = self.request('GET', path)
            self.assertEqual(pending['status'], 'running')
            self.assertEqual(pending['cancellation_state'], 'stopping')
            self.assertTrue(pending['cancel_requested'])
            self.assertEqual(pending['history'], [])
        finally:
            release.set()
        completed = self.finished(path)
        self.assertEqual(completed['cancellation_state'], 'stopped')
        self.assertEqual(len(completed['history']), 1)
        result = completed['history'][0]
        self.assertEqual(result['stop_reason'], 'user_cancelled')
        self.assertEqual(len(result['papers']), 2)
        self.assertTrue(all(p['evidence_verified'] for p in result['papers']))
        self.assertEqual(llm.batches, 1)
        saved = self.root / 'chat' / session_id / 'turn-1' / 'result.json'
        self.assertEqual(json.loads(saved.read_text(encoding='utf-8'))['stop_reason'], 'user_cancelled')
        before_events = self.chat.events(session_id)
        status, idle = self.request('POST', path + '/cancel', {})
        self.assertEqual(status, 200)
        self.assertFalse(idle['accepted'])
        self.assertEqual(self.chat.events(session_id), before_events)
        status, continuation = self.request('POST', path + '/messages', {
            'message': 'Keep the request and continue.', 'mode': 'demo'})
        self.assertEqual(status, 200)
        self.assertEqual(continuation['result']['status'], 'complete')
        history = self.finished(path)['history']
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['stop_reason'], 'user_cancelled')
        self.assertEqual(continuation['session_id'], session_id)

    def test_http_stop_before_chat_worker_starts_is_not_reset_by_start(self):
        entered, release = threading.Event(), threading.Event()
        original = ChatSession.start
        class NeverLLM:
            def generate(self, *args):
                raise AssertionError('Stop before worker start reached the model')
        def delayed(chat, *args, **kwargs):
            chat.llm = NeverLLM()
            entered.set()
            release.wait(8)
            return original(chat, *args, **kwargs)
        with patch.object(ChatSession, 'start', delayed):
            status, created = self.request('POST', '/api/chat/sessions', {
                'mode': 'demo', 'message': 'Find five RAG papers.', 'stream': True})
            self.assertEqual(status, 201)
            path = '/api/chat/sessions/' + created['session_id']
            try:
                self.assertTrue(entered.wait(2))
                status, stopped = self.request('POST', path + '/cancel', {})
                self.assertTrue(stopped['accepted'])
            finally:
                release.set()
            completed = self.finished(path)
        self.assertEqual(completed['history'][0]['stop_reason'], 'user_cancelled')
        self.assertEqual(completed['history'][0]['papers'], [])
        self.assertFalse(completed['error'])

    def test_search_cancel_saves_verified_partial_and_imports_one_chat_turn(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingLLM(DemoLLM):
            batches = 0
            def generate(self, stage, *args):
                if stage == 'assess':
                    self.batches += 1
                    entered.set()
                    release.wait(8)
                return super().generate(stage, *args)
        llm = BlockingLLM()
        def execute(data, progress):
            return run('Find five RAG papers.', Config(batch_size=2),
                       request_data={'topic': 'RAG', 'target_count': 5},
                       source=DemoSource('satisfied'), llm=llm,
                       budget=Budget(30, cancel_token=data['_cancel_token']), progress=progress)
        self.jobs.runner = execute
        status, created = self.request('POST', '/api/jobs', {
            'mode': 'demo', 'scenario': 'satisfied', 'method': 'A'})
        self.assertEqual(status, 202)
        job_id = created['id']
        path = '/api/jobs/' + job_id
        try:
            self.assertTrue(entered.wait(3))
            for _ in range(2):
                status, stopped = self.request('POST', path + '/cancel', {})
                self.assertEqual(status, 200)
                self.assertTrue(stopped['accepted'])
            status, pending = self.request('GET', path)
            self.assertEqual(pending['state'], 'running')
            self.assertEqual(pending['cancellation_state'], 'stopping')
            self.assertIsNone(pending['result'])
        finally:
            release.set()
        completed = self.finished(path, 'state')
        self.assertEqual(completed['state'], 'done')
        self.assertEqual(completed['cancellation_state'], 'stopped')
        self.assertEqual(completed['result']['stop_reason'], 'user_cancelled')
        self.assertEqual(len(completed['result']['papers']), 2)
        self.assertEqual(llm.batches, 1)
        saved = self.root / 'search' / job_id / 'result.json'
        self.assertEqual(json.loads(saved.read_text(encoding='utf-8'))['stop_reason'], 'user_cancelled')
        session_id = completed['result']['chat_session_id']
        imported = self.chat.snapshot(session_id)
        self.assertEqual(len(imported['history']), 1)
        self.assertEqual(imported['history'][0]['stop_reason'], 'user_cancelled')
        status, idle = self.request('POST', path + '/cancel', {})
        self.assertFalse(idle['accepted'])

    def test_unknown_cancel_targets_return_404(self):
        for prefix in ('jobs', 'chat/sessions'):
            status, payload = self.request('POST', '/api/' + prefix + '/' + 'f' * 32 + '/cancel', {})
            self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
