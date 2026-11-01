"""Public API contracts: admission, idempotency, real event delivery and ranked JSON."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from api_v1 import ApiError, OperationStore, ProductApi, public_result
from report import markdown
from providers import ProviderStore
from webapp import Jobs, create_server, validate_input, ChatService


class PublicResultTests(unittest.TestCase):
    def test_ten_unique_papers_are_ranked_without_truncating(self):
        result = public_result({'status': 'complete', 'papers': [
            {'id': f'p{i}', 'title': f'Paper {i}', 'url': f'https://example.org/{i}',
             'evidence_quote': 'Actual abstract quote', 'evidence_verified': True}
            for i in range(10)]})
        self.assertEqual(len(result['papers']), 10)
        self.assertEqual([p['rank'] for p in result['papers']], list(range(1, 11)))
        self.assertEqual(result['papers'][-1]['paper_id'], 'p9')
        self.assertTrue(result['papers'][0]['evidence'][0]['quote_verified'])

    def test_new_event_sequence_exports_without_legacy_step(self):
        text = markdown({"mode":"demo", "method":"V2", "status":"complete", "stop_reason":"requirements_met", "trace":[{"seq":1,"action":"tool_end","payload":{"tool":"search"}}]})
        self.assertIn("tool", text)

    def test_invalid_json_types_are_rejected_before_admission(self):
        for field, value in [('source', []), ('source', {}), ('filters', []), ('filters', None),
                             ('count', True), ('count', 11), ('count', 0), ('session_id', {}),
                             ('request_data', []), ('stream', 'true')]:
            with self.subTest(field=field, value=value), self.assertRaises((ApiError, Exception)):
                ProductApi._input({'query': 'RAG', field: value}, 'search')
        for value in ([], {}):
            with self.assertRaises(Exception):
                validate_input({'request': 'RAG', 'source': value})
            with self.assertRaises(Exception):
                ChatService.validate({'message': 'RAG', 'source': value})

    def test_idempotency_reservation_is_atomic_and_conflicts_do_not_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / 'ops.sqlite3')
            with ThreadPoolExecutor(max_workers=8) as pool:
                rows = list(pool.map(lambda _: store.reserve('shared-request', 'search', {'query': 'RAG'}), range(8)))
            self.assertEqual(sum(fresh for _, fresh in rows), 1)
            self.assertEqual(len({row['id'] for row, _ in rows}), 1)
            with self.assertRaises(ApiError) as error:
                store.reserve('shared-request', 'search', {'query': 'different'})
            self.assertEqual(error.exception.code, 'idempotency_conflict')


class PublicApiHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.providers = ProviderStore(self.root / 'providers.json')
        self.calls = 0
        def runner(data, progress):
            self.calls += 1
            from demo import demo_run
            return demo_run('satisfied', 'A', progress=progress)
        self.jobs = Jobs(self.root / 'runs', runner=runner, providers=self.providers)
        self.server = create_server(0, jobs=self.jobs, providers=self.providers)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        con = HTTPConnection('127.0.0.1', self.port, timeout=10)
        values = {'Content-Type': 'application/json', **(headers or {})}
        con.request(method, path, json.dumps(body).encode() if body is not None else None, values)
        response = con.getresponse()
        content = response.read()
        status = response.status
        con.close()
        return status, json.loads(content)

    def wait(self, operation_id):
        for _ in range(150):
            status, value = self.request('GET', '/api/v1/operations/' + operation_id)
            if value['state'] not in ('running', 'queued'):
                return status, value
            time.sleep(.02)
        self.fail('Operation did not finish')

    def test_search_and_retry_use_one_execution_and_saved_result(self):
        body = {'query': 'Recommend five RAG papers.', 'mode': 'demo', 'client_request_id': 'retry-safe'}
        status, first = self.request('POST', '/api/v1/search', body)
        self.assertEqual(status, 202, first)
        status, result = self.wait(first['operation_id'])
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(result['result']['returned_count'], 5)
        _, repeated = self.request('POST', '/api/v1/search', body)
        self.assertEqual(first['operation_id'], repeated['operation_id'])
        self.assertEqual(self.calls, 1)
        status, conflict = self.request('POST', '/api/v1/search', {**body, 'query': 'another topic'})
        self.assertEqual(status, 409, conflict)
        self.assertEqual(conflict['error']['code'], 'idempotency_conflict')
        # Operation record and result remain readable after application object loss.
        replacement = ProductApi(self.jobs, self.server.product_api.chat, self.root / 'runs' / 'api')
        self.assertEqual(replacement.snapshot(first['operation_id'])['result']['returned_count'], 5)

    def test_failed_search_http_exposes_only_safe_outcome(self):
        class Failure(Exception):
            code = 'remote_success_local_failure'
            remote_outcome = 'completed'
            transaction_outcome = 'unknown'
            retry_safe = False
        def fail(data, progress):
            raise Failure('SECRET remote response query')
        self.jobs.runner = fail
        status, first = self.request('POST', '/api/v1/search', {'query': 'private topic', 'mode': 'demo'})
        self.assertEqual(status, 202)
        _, result = self.wait(first['operation_id'])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error']['code'], 'remote_success_local_failure')
        self.assertEqual(result['error']['remote_outcome'], 'completed')
        self.assertEqual(result['error']['transaction_outcome'], 'unknown')
        self.assertFalse(result['error']['retryable'])
        self.assertFalse(result['error']['retry_safe'])
        _, events = self.request('GET', first['links']['events'])
        self.assertNotIn('SECRET', json.dumps([result['error'], events]))
        self.assertNotIn('private topic', json.dumps([result['error'], events]))

    def test_synchronous_chat_failure_http_does_not_echo_remote_text(self):
        from unittest.mock import patch
        from common import GuideError
        class Failure(GuideError):
            code = 'remote_outcome_unknown'
            remote_outcome = 'unknown'
            retry_safe = False
        with patch('webapp.ChatSession.start', side_effect=Failure('SECRET remote response')):
            status, value = self.request('POST', '/api/v1/chat',
                                         {'message': 'private query', 'mode': 'demo', 'stream': False,
                                          'client_request_id': 'sync-failure'})
        self.assertEqual(status, 400)
        self.assertEqual(value['error']['remote_outcome'], 'unknown')
        self.assertFalse(value['error']['retryable'])
        self.assertNotIn('SECRET', json.dumps(value))
        _, repeated = self.request('POST', '/api/v1/chat',
                                   {'message': 'private query', 'mode': 'demo', 'stream': False,
                                    'client_request_id': 'sync-failure'})
        self.assertEqual(repeated['state'], 'failed')
        self.assertEqual(repeated['error']['remote_outcome'], 'unknown')
        self.assertNotIn('SECRET', json.dumps(repeated))

    def test_bad_payload_returns_4xx_json_without_creating_job(self):
        for body in ({'query': 'RAG', 'source': []}, {'query': 'RAG', 'count': 11},
                     {'query': 'RAG', 'filters': {'made_up': True}}, {'query': 'RAG', 'unknown': 1}):
            status, result = self.request('POST', '/api/v1/search', body)
            self.assertEqual(status, 400, result)
            self.assertIn('code', result['error'])
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.jobs.items, {})

    def test_schema_and_sources_have_no_implicit_model_calls(self):
        status, schema = self.request('GET', '/openapi.json')
        self.assertEqual(status, 200)
        self.assertIn('/api/v1/search', schema['paths'])
        self.assertIn('/api/v1/chat', schema['paths'])
        status, sources = self.request('GET', '/api/v1/sources')
        self.assertEqual(status, 200, sources)
        self.assertIsInstance(sources['sources'], list)
        self.assertEqual(self.calls, 0)

    def test_old_operation_cannot_cancel_or_claim_events_from_a_new_turn(self):
        _, first = self.request('POST', '/api/v1/chat', {'message': 'Recommend five RAG papers.', 'mode': 'demo'})
        _, completed = self.wait(first['operation_id'])
        sid = completed['session_id']
        _, original_events = self.request('GET', first['links']['events'])
        end = max(row['seq'] for row in original_events['events'])
        entered, release = threading.Event(), threading.Event()
        record = self.server.product_api.chat.sessions[sid]
        original_send = record['chat'].send
        def blocked(message, **kwargs):
            entered.set()
            release.wait(5)
            return original_send(message, **kwargs)
        record['chat'].send = blocked
        try:
            _, second = self.request('POST', '/api/v1/chat', {'session_id': sid, 'message': 'Keep the same topic.', 'mode': 'demo'})
            self.assertTrue(entered.wait(2))
            _, receipt = self.request('POST', first['links']['cancel'], {})
            self.assertFalse(receipt['accepted'])
            self.assertFalse(record['chat'].cancel_requested)
            self.assertEqual(second['turn'], 2)
        finally:
            release.set()
        _, second_done = self.wait(second['operation_id'])
        self.assertEqual(second_done['state'], 'completed')
        _, first_events = self.request('GET', first['links']['events'])
        self.assertTrue(all(row['seq'] <= end for row in first_events['events']))
        _, second_events = self.request('GET', second['links']['events'])
        self.assertTrue(all(row['seq'] > end for row in second_events['events']))

    def test_terminal_sse_flushes_the_tail_committed_after_the_first_read(self):
        from io import BytesIO
        from types import SimpleNamespace
        handler = self.server.RequestHandlerClass
        fake = SimpleNamespace(wfile=BytesIO(), close_connection=False,
                               _cursor=lambda:0, _streaming=lambda:True,
                               send_response=lambda status:None,
                               send_header=lambda *args:None, end_headers=lambda:None)
        reads = []
        def loader(after):
            reads.append(after)
            rows = [{'seq':1,'action':'tool_start'}] if len(reads) == 1 else [{'seq':2,'action':'delivered'}]
            return [row for row in rows if row['seq'] > after]
        handler._event_response(fake, loader, lambda:{'state':'completed'})
        text = fake.wfile.getvalue().decode()
        self.assertIn('delivered', text)
        self.assertLess(text.index('delivered'), text.index('event: done'))

    def test_event_started_is_observable_while_real_operation_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(data, progress):
            progress({'action': 'tool_start', 'payload': {'span_id': 'real-tool', 'tool': 'search', 'source': 'fixture'}})
            entered.set()
            release.wait(5)
            progress({'action': 'tool_end', 'payload': {'span_id': 'real-tool', 'tool': 'search', 'returned': 1}})
            return {'status': 'complete', 'papers': [], 'request': {'topic': 'fixture'}, 'trace': []}
        self.jobs.runner = blocked
        _, value = self.request('POST', '/api/v1/search', {'query': 'fixture', 'mode': 'demo'})
        self.assertTrue(entered.wait(2))
        con = HTTPConnection('127.0.0.1', self.port, timeout=10)
        con.request('GET', value['links']['events'], headers={'Accept': 'text/event-stream'})
        response = con.getresponse()
        try:
            self.assertEqual(response.status, 200)
            self.assertIn('text/event-stream', response.getheader('Content-Type'))
            packet = b''
            for _ in range(4):
                packet += response.readline()
                if packet.endswith(b'\n\n'):
                    break
            self.assertIn(b'tool_start', packet)
            self.assertFalse(release.is_set())
            release.set()
            remainder = response.read()
            self.assertIn(b'tool_end', remainder)
            self.assertIn(b'event: done', remainder)
        finally:
            release.set()
            con.close()
        _, events = self.request('GET', value['links']['events'] + '?after=1')
        self.assertTrue(all(row['seq'] > 1 for row in events['events']))


if __name__ == '__main__':
    unittest.main()
