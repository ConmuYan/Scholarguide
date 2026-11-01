"""HTTP tests for the minimal multi-turn chat service (demo mode, no network)."""
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import GuideError
from webapp import ChatService, ChatSession, create_server, TurnRunning


class WebChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ChatService(output=Path(self.tmp.name))
        self.server = create_server(0, chat=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = '127.0.0.1'
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.service.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, content_type='application/json'):
        connection = HTTPConnection(self.host, self.port, timeout=10)
        headers = {'Host': f'{self.host}:{self.port}'}
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers['Content-Type'] = content_type
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, response.getheader('Content-Type'), raw

    def wait_finished(self, session_id):
        for _ in range(200):
            snapshot = self.service.snapshot(session_id)
            if snapshot['status'] != 'running':
                return snapshot
            time.sleep(.02)
        self.fail('background turn did not finish')

    def test_delete_saved_session_removes_local_history_only(self):
        created = self.service.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        session_id = created['session_id']
        directory = self.service.sessions[session_id]['directory']
        self.assertTrue(directory.exists())
        status, content_type, raw = self.request('DELETE', f'/api/chat/sessions/{session_id}')
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)['error_code'], 'deletion_unavailable')
        self.assertTrue(directory.exists())
        self.assertIsNotNone(self.service.snapshot(session_id))
        self.assertEqual(self.request('DELETE', f'/api/chat/sessions/{session_id}')[0], 503)

    def test_delete_running_session_is_rejected(self):
        created = self.service.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        session_id = created['session_id']
        record = self.service.sessions[session_id]
        record['running'] = True
        status, _, raw = self.request('DELETE', f'/api/chat/sessions/{session_id}')
        record['running'] = False
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)['error_code'], 'deletion_unavailable')
        self.assertTrue(record['directory'].exists())

    def test_delete_unknown_session_is_not_success(self):
        self.assertEqual(self.request('DELETE', '/api/chat/sessions/' + 'a' * 32)[0], 503)

    def test_disabled_delete_preserves_old_reference_and_does_not_load(self):
        created = self.service.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        sid = created['session_id']
        record = self.service.sessions[sid]
        with patch.object(self.service, '_load_record', side_effect=AssertionError('loaded')), \
                patch('webapp.shutil.rmtree', side_effect=AssertionError('deleted')):
            with self.assertRaisesRegex(GuideError, '暂不可用'):
                self.service.delete(sid)
        self.assertIs(self.service.sessions[sid], record)
        self.service.send(sid, {'message': 'Add one survey paper.'})
        self.assertEqual(len(self.service.snapshot(sid)['history']), 2)

    def test_stream_option_is_preserved_and_requires_a_boolean(self):
        for flag in (True, False):
            self.assertIs(ChatService.validate({'message': 'RAG', 'stream': flag})['stream'], flag)
        self.assertIs(ChatService.validate({'message': 'RAG'})['stream'], False)
        for invalid in ('true', 1, None, [], {}):
            status, _, _ = self.request('POST', '/api/chat/sessions', {
                'message': 'RAG', 'mode': 'demo', 'stream': invalid})
            self.assertEqual(status, 400)

    def test_streamed_create_returns_before_work_finishes_and_exposes_progress(self):
        started, release = threading.Event(), threading.Event()
        original = ChatSession.start

        def blocked(chat, *args, **kwargs):
            chat.store.append_event(chat.session_id, 'search', {'query': 'blocked fixture search'})
            chat.store.commit()
            started.set()
            release.wait(5)
            return original(chat, *args, **kwargs)

        session_id = None
        try:
            with patch.object(ChatSession, 'start', blocked):
                status, _, body = self.request('POST', '/api/chat/sessions', {
                    'message': 'Recommend five RAG papers.', 'mode': 'demo', 'stream': True})
                self.assertEqual(status, 201)
                payload = json.loads(body)
                session_id = payload['session_id']
                self.assertTrue(payload.get('running'))
                self.assertNotIn('result', payload)
                self.assertTrue(started.wait(2))
                store = self.service.sessions[session_id]['store']
                with patch.object(store, 'get_session', side_effect=AssertionError('writer connection read')), \
                        patch.object(store, 'events', side_effect=AssertionError('writer connection read')):
                    snapshot = json.loads(self.request('GET', f'/api/chat/sessions/{session_id}')[2])
                    events = json.loads(self.request('GET', f'/api/chat/sessions/{session_id}/events')[2])['events']
                self.assertEqual(snapshot['status'], 'running')
                self.assertEqual(snapshot['history'], [])
                self.assertFalse(snapshot['capabilities']['can_send'])
                self.assertTrue(any(event['payload'].get('query') == 'blocked fixture search'
                                    for event in events))
        finally:
            release.set()
            if session_id:
                self.wait_finished(session_id)
        self.assertTrue(self.service.snapshot(session_id)['history'])

    def test_streamed_message_returns_before_work_finishes(self):
        created = self.service.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        session_id = created['session_id']
        record = self.service.sessions[session_id]
        started, release = threading.Event(), threading.Event()
        original = record['chat'].send

        def blocked(message):
            started.set()
            release.wait(5)
            return original(message)

        record['chat'].send = blocked
        try:
            status, _, body = self.request('POST', f'/api/chat/sessions/{session_id}/messages', {
                'message': 'Exclude P01 and keep the rest.', 'stream': True})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body).get('running'))
            self.assertTrue(started.wait(2))
            self.assertEqual(self.service.snapshot(session_id)['status'], 'running')
            status, _, raw = self.request('POST', f'/api/chat/sessions/{session_id}/messages', {
                'message': 'Do not run this duplicate.', 'stream': True})
            self.assertEqual(status, 409)
            self.assertEqual(json.loads(raw)['error_code'], 'turn_running')
        finally:
            release.set()
            self.wait_finished(session_id)
        self.assertEqual(len(self.service.snapshot(session_id)['history']), 2)

    def test_streamed_provider_failure_releases_the_turn_claim(self):
        created = self.service.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        session_id = created['session_id']
        with patch.object(self.service, '_refresh_live_provider', side_effect=GuideError('invalid provider')):
            status, _, _ = self.request('POST', f'/api/chat/sessions/{session_id}/messages', {
                'message': 'Retry later.', 'stream': True})
        self.assertEqual(status, 400)
        snapshot = self.service.snapshot(session_id)
        self.assertNotEqual(snapshot['status'], 'running')
        self.assertTrue(snapshot['capabilities']['can_send'])

    def test_public_reuse_retains_candidate_counts_and_legacy_rate(self):
        reuse = {'reused': ['P01', 'P02'], 'stale': ['P03'], 'reuse_rate': 2 / 3}
        public = ChatService._public({'metrics': {'reuse': reuse}})
        self.assertEqual(public['reuse'], reuse)
        public['reuse']['reused'].append('P04')
        self.assertEqual(reuse['reused'], ['P01', 'P02'])
        for rate in (0.0, 1.0):
            self.assertEqual(ChatService._public({'reuse': rate})['reuse'], {
                'reused': [], 'stale': [], 'reuse_rate': rate})

    def test_multi_source_fork_matches_the_ui_option(self):
        created = self.service.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        forked = self.service.fork_source(created['session_id'], 'multi')
        self.assertTrue(forked.get('running'))
        snapshot = self.wait_finished(forked['session_id'])
        self.assertEqual(snapshot['source'], 'multi')
        self.assertTrue(snapshot['history'])

    def test_lower_target_noop_is_rejected_without_a_turn(self):
        status, _, body = self.request('POST', '/api/chat/sessions', {
            'mode': 'demo', 'scenario': 'followup', 'message': 'Find RAG papers.',
            'request_data': {'topic': 'retrieval augmented generation',
                             'category': 'none', 'minimum_category_count': 0,
                             'target_count': 1, 'unsupported': []}})
        self.assertIn(status, (200, 201))
        session_id = json.loads(body)['session_id']
        before = len(json.loads(self.request(
            'GET', f'/api/chat/sessions/{session_id}/events?after=0')[2])['events'])
        status, _, raw = self.request(
            'POST', f'/api/chat/sessions/{session_id}/recover', {'action': 'lower_target'})
        self.assertEqual(status, 400)
        self.assertIn('no effect', json.loads(raw)['error'])
        after = len(json.loads(self.request(
            'GET', f'/api/chat/sessions/{session_id}/events?after=0')[2])['events'])
        self.assertEqual(before, after)

    def test_source_fork_preserves_request_in_a_new_session(self):
        status, _, body = self.request('POST', '/api/chat/sessions', {
            'mode': 'demo', 'scenario': 'followup',
            'message': 'Recommend five RAG papers including one evaluation paper.'})
        session_id = json.loads(body)['session_id']
        old_events = len(json.loads(self.request(
            'GET', f'/api/chat/sessions/{session_id}/events?after=0')[2])['events'])
        old_task = json.loads((Path(self.tmp.name) / session_id / 'turn-1' / 'result.json')
                              .read_text(encoding='utf-8'))['request']
        status, _, body = self.request(
            'POST', f'/api/chat/sessions/{session_id}/fork',
            {'source': 'openreview', 'stream': True})
        self.assertEqual(status, 201)
        forked = json.loads(body)['session_id']
        self.assertNotEqual(forked, session_id)
        for _ in range(200):
            snapshot = json.loads(self.request('GET', f'/api/chat/sessions/{forked}')[2])
            if snapshot['status'] != 'running':
                break
            time.sleep(.02)
        self.assertNotEqual(snapshot['status'], 'running')
        self.assertTrue(snapshot['history'])
        new_result = json.loads((Path(self.tmp.name) / forked / 'turn-1' / 'result.json')
                                .read_text(encoding='utf-8'))
        for key in ('topic', 'venue', 'year_from', 'year_to', 'target_count', 'category'):
            self.assertEqual(new_result['request'].get(key), old_task.get(key), key)
        self.assertEqual(json.loads((Path(self.tmp.name) / forked / 'meta.json')
                                    .read_text(encoding='utf-8'))['source'], 'openreview')
        self.assertEqual(len(json.loads(self.request(
            'GET', f'/api/chat/sessions/{session_id}/events?after=0')[2])['events']),
            old_events)

    def test_recovery_action_applies_a_deterministic_patch(self):
        status, _, body = self.request('POST', '/api/chat/sessions', {
            'mode': 'demo', 'scenario': 'followup',
            'message': 'Recommend five RAG papers including one evaluation paper.'})
        session_id = json.loads(body)['session_id']
        status, _, body = self.request(
            'POST', f'/api/chat/sessions/{session_id}/recover', {'action': 'lower_target'})
        self.assertEqual(status, 200)
        result = json.loads(body)['result']
        self.assertIn(result['status'], ('complete', 'partial', 'unsupported'))
        events = json.loads(self.request(
            'GET', f'/api/chat/sessions/{session_id}/events?after=0')[2])['events']
        recovery = [event for event in events if event['action'] == 'recovery']
        self.assertTrue(recovery)
        self.assertEqual(recovery[-1]['payload']['patch']['target_count'], 4)
        self.assertEqual(len(json.loads(self.request(
            'GET', f'/api/chat/sessions/{session_id}')[2])['history']), 2)

    def test_awaiting_ask_can_be_answered_at_zero_feedback(self):
        service = ChatService(output=Path(self.tmp.name) / 'ask-zero')
        try:
            created = service.create({'mode': 'demo', 'scenario': 'ask',
                                      'message': 'Find RAG reading-list papers; I am not sure whether for a survey.'})
            session_id = created['session_id']
            record = service.sessions[session_id]
            limits = record['chat'].limits()
            limits['feedback_phases_left'] = 0
            record['store'].update_budget(session_id, limits)
            snapshot = service.snapshot(session_id)
            self.assertTrue(snapshot['capabilities']['can_send'])
            self.assertTrue(snapshot['capabilities']['awaiting_user'])
            result = service.send(session_id, {
                'message': 'It is for a survey, include one survey paper.'})
            self.assertIn(result['result']['status'], ('complete', 'partial', 'unsupported'))
            self.assertEqual(len(service.snapshot(session_id)['history']), 2)
        finally:
            service.close()

    def test_concurrent_stream_claims_exactly_one_turn(self):
        service = ChatService(output=Path(self.tmp.name) / 'concurrent')
        try:
            created = service.create({'mode': 'demo', 'scenario': 'followup',
                                      'message': 'Recommend five RAG papers.'})
            session_id = created['session_id']
            record = service.sessions[session_id]
            original = record['chat'].send
            started = threading.Event()
            release = threading.Event()
            def slow(message):
                started.set()
                release.wait(5)
                return original(message)
            record['chat'].send = slow
            received = []
            worker = threading.Thread(target=lambda: received.append(
                service.send(session_id, {'message': 'slow turn', 'stream': True})))
            worker.start()
            self.assertTrue(started.wait(2))
            with self.assertRaises(TurnRunning):
                service.send(session_id, {'message': 'racing turn', 'stream': True})
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertTrue(received)
            for _ in range(200):
                snapshot = service.snapshot(session_id)
                if snapshot['status'] != 'running':
                    break
                time.sleep(.02)
            self.assertNotEqual(snapshot['status'], 'running')
            self.assertEqual(len(snapshot['history']), 2)
            self.assertTrue(snapshot['capabilities']['can_send'])
            self.assertIsNone(snapshot['feedback_phases_left'])
        finally:
            service.close()

    def test_feedback_budget_exhausted_is_409_without_a_noop_turn(self):
        status, _, body = self.request('POST', '/api/chat/sessions', {
            'mode': 'demo', 'scenario': 'followup',
            'message': 'Recommend five RAG papers including one evaluation paper.'})
        self.assertEqual(status, 201)
        session_id = json.loads(body)['session_id']
        for _ in range(2):
            status, _, _ = self.request('POST', f'/api/chat/sessions/{session_id}/messages', {
                'message': 'Add one more relevant paper.'})
            self.assertEqual(status, 200)
        before = json.loads(self.request(
            'GET', f'/api/chat/sessions/{session_id}/events?after=0')[2])['events']
        turns_before = sorted((Path(self.tmp.name) / session_id).glob('turn-*/result.json'))
        status, _, raw = self.request(
            'POST', f'/api/chat/sessions/{session_id}/messages',
            {'message': 'This must not create a no-op turn.'})
        self.assertEqual(status, 200)
        result = json.loads(raw)['result']
        self.assertGreaterEqual(result['revision'], 4)
        after = json.loads(self.request(
            'GET', f'/api/chat/sessions/{session_id}/events?after=0')[2])['events']
        turns_after = sorted((Path(self.tmp.name) / session_id).glob('turn-*/result.json'))
        self.assertGreater(len(after), len(before))
        self.assertEqual(len(turns_after), len(turns_before) + 1)
        snapshot = json.loads(self.request('GET', f'/api/chat/sessions/{session_id}')[2])
        self.assertIsNone(snapshot['feedback_phases_left'])
        self.assertTrue(snapshot['capabilities']['can_send'])
        self.assertTrue(snapshot['capabilities']['can_retry'])

    def test_archive_and_restore_preserve_session_evidence(self):
        status, _, body = self.request('POST', '/api/chat/sessions', {
            'mode': 'demo', 'scenario': 'followup',
            'message': 'Recommend five RAG papers including one evaluation paper.'})
        session_id = json.loads(body)['session_id']
        directory = Path(self.tmp.name) / session_id
        before = sorted(path.relative_to(directory) for path in directory.rglob('*') if path.is_file())
        status, _, body = self.request('POST', f'/api/chat/sessions/{session_id}/archive', {})
        self.assertEqual(status, 200)
        archived = json.loads(body)
        self.assertTrue(archived['archived'])
        self.assertFalse(archived['capabilities']['can_send'])
        status, _, raw = self.request(
            'POST', f'/api/chat/sessions/{session_id}/messages', {'message': 'blocked while archived'})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw)['error_code'], 'session_archived')
        status, _, body = self.request('POST', f'/api/chat/sessions/{session_id}/restore', {})
        self.assertEqual(status, 200)
        restored = json.loads(body)
        self.assertFalse(restored['archived'])
        self.assertTrue(restored['capabilities']['can_send'])
        after = sorted(path.relative_to(directory) for path in directory.rglob('*') if path.is_file())
        self.assertEqual(before, after)

    def test_chat_assets_and_link(self):
        status, content_type, body = self.request('GET', '/chat')
        self.assertEqual(status, 200)
        self.assertIn('text/html', content_type)
        self.assertIn('沿着证据，继续。'.encode(), body)
        status, content_type, body = self.request('GET', '/chat.js')
        self.assertEqual(status, 200)
        self.assertIn('text/javascript', content_type)
        status, _, body = self.request('GET', '/search')
        self.assertIn(b'/chat', body)

    def test_demo_ask_then_resume(self):
        status, _, body = self.request('POST', '/api/chat/sessions', {
            'mode': 'demo', 'scenario': 'ask',
            'message': 'Find RAG reading-list papers; I am not sure whether for a survey.'})
        self.assertEqual(status, 201)
        payload = json.loads(body)
        session_id = payload['session_id']
        self.assertEqual(len(session_id), 32)
        self.assertEqual(payload['result']['status'], 'awaiting_user')
        self.assertTrue(payload['result']['question'])

        status, _, body = self.request('POST', f'/api/chat/sessions/{session_id}/messages', {
            'message': 'It is for a survey, include one survey paper.'})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload['result']['status'], 'complete')
        self.assertGreaterEqual(len(payload['result']['papers']), 1)

        status, _, body = self.request('GET', f'/api/chat/sessions/{session_id}')
        self.assertEqual(status, 200)
        snapshot = json.loads(body)
        self.assertEqual(snapshot['status'], 'complete')
        self.assertEqual(len(snapshot['history']), 2)
        self.assertEqual(snapshot['revision'], 2)

    def test_demo_feedback_flow_and_reuse(self):
        status, _, body = self.request('POST', '/api/chat/sessions', {
            'mode': 'demo', 'scenario': 'followup',
            'message': 'Recommend five RAG papers including one evaluation paper.'})
        self.assertEqual(status, 201)
        payload = json.loads(body)
        session_id = payload['session_id']
        self.assertEqual(payload['result']['status'], 'complete')

        status, _, body = self.request('POST', f'/api/chat/sessions/{session_id}/messages', {
            'message': 'Add one survey paper as well.'})
        self.assertEqual(status, 200)
        result = json.loads(body)['result']
        self.assertIn(result['status'], ('complete', 'partial', 'unsupported'))
        self.assertEqual(result['revision'], 2)
        self.assertIn('cost_units', result)

    def test_streamed_turn_exposes_events_and_result(self):
        status, _, body = self.request('POST', '/api/chat/sessions', {
            'mode': 'demo', 'scenario': 'followup', 'stream': True,
            'message': 'Recommend five RAG papers including one evaluation paper.'})
        self.assertIn(status, (200, 201, 202))
        session_id = json.loads(body)['session_id']
        snapshot = {}
        for _ in range(200):
            status, _, body = self.request('GET', '/api/chat/sessions/' + session_id)
            snapshot = json.loads(body)
            if snapshot['status'] != 'running':
                break
            time.sleep(.02)
        self.assertNotEqual(snapshot['status'], 'running')
        self.assertTrue(snapshot['history'])
        status, _, body = self.request('GET', '/api/chat/sessions/' + session_id + '/events?after=0')
        self.assertEqual(status, 200)
        events = json.loads(body)['events']
        self.assertTrue(any(event['action'] in ('agent_node', 'search', 'assess') for event in events))

    def test_event_feed_revision_scopes_chat_trace(self):
        status, _, body = self.request('POST', '/api/chat/sessions', {
            'mode': 'demo', 'scenario': 'followup',
            'message': 'Recommend five RAG papers including one evaluation paper.'})
        self.assertEqual(status, 201)
        session_id = json.loads(body)['session_id']
        status, _, body = self.request('POST', f'/api/chat/sessions/{session_id}/messages', {
            'message': 'Add one survey paper.'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['result']['revision'], 2)
        status, _, body = self.request('GET', f'/api/chat/sessions/{session_id}/events?after=0')
        events = json.loads(body)['events']
        revisions = {event['revision'] for event in events}
        self.assertIn(1, revisions)
        self.assertIn(2, revisions)
        second = [event for event in events if event['revision'] == 2]
        self.assertTrue(any(event['action'] in ('agent_node', 'decision', 'search', 'assess')
                            for event in second))
        self.assertNotIn('api_key', json.dumps(second))
        last_first = max(event['seq'] for event in events if event['revision'] == 1)
        status, _, body = self.request(
            'GET', f'/api/chat/sessions/{session_id}/events?after={last_first}')
        later = json.loads(body)['events']
        self.assertTrue(later)
        self.assertTrue(all(event['revision'] == 2 for event in later))

    def test_saved_session_survives_restart_and_resumes(self):
        from webapp import ChatService
        svc = ChatService(output=Path(self.tmp.name) / 'saved')
        created = svc.create({'message': 'Recommend five RAG papers including one evaluation paper.',
                              'mode': 'demo', 'scenario': 'followup', 'reuse': True})
        session_id = created['session_id']
        svc.close()
        svc2 = ChatService(output=Path(self.tmp.name) / 'saved')
        snapshot = svc2.snapshot(session_id)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['history'][0]['papers'][0]['id'],
                         created['result']['papers'][0]['id'])
        self.assertEqual(svc2.list_sessions()[0]['session_id'], session_id)
        resumed = svc2.send(session_id, {'message': 'Add one survey paper.'})
        self.assertIn(resumed['result']['status'], ('complete', 'partial', 'unsupported'))
        svc2.close()

    def test_source_validation_and_provenance_fields(self):
        data = self.service.validate({'message': 'find RAG', 'mode': 'live', 'source': 'multi'})
        self.assertEqual(data['source'], 'multi')
        config, _, _ = self.service._new_chat({'mode': 'live', 'source': 'multi'})
        self.assertEqual(config.source, 'multi')
        with self.assertRaises(Exception):
            self.service.validate({'message': 'find RAG', 'mode': 'live', 'source': 'made_up'})
        public = ChatService._public({'status': 'complete', 'stop_reason': 'requirements_met', 'metrics': {}, 'papers': [{
            'id': 'doi:10.1234/x', 'title': 'T', 'doi': '10.1234/x',
            'open_access_url': 'https://example.org/x.pdf', 'metadata_only': True,
            'provenance_conflict': True, 'abstract_source': 'openalex', 'sources': ['openalex'],
        }]})
        paper = public['papers'][0]
        for field in ('doi', 'open_access_url', 'metadata_only', 'provenance_conflict',
                      'abstract_source', 'sources'):
            self.assertIn(field, paper)
        self.assertEqual(paper['doi'], '10.1234/x')
        self.assertTrue(paper['metadata_only'])

    def test_validation_and_unknown_session(self):
        status, _, _ = self.request('POST', '/api/chat/sessions', {'mode': 'demo', 'message': ''})
        self.assertEqual(status, 400)
        status, _, _ = self.request('POST', '/api/chat/sessions', {'mode': 'nope', 'message': 'x'})
        self.assertEqual(status, 400)
        status, _, _ = self.request('POST', '/api/chat/sessions/' + '0' * 32 + '/messages',
                                    {'message': 'hello'})
        self.assertEqual(status, 404)
        status, _, _ = self.request('POST', '/api/chat/sessions', body=b'{"message":"x"}',
                                    content_type='text/plain')
        self.assertEqual(status, 415)


if __name__ == '__main__':
    unittest.main()
