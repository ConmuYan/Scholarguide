"""Real process interruption of native operations, entirely offline."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from common import Config, GuideError
from controller import Decision
from llm import JsonLLM
from scripts.verify_delivery import isolated_environment
from session import ChatSession
from state import StateStore
from test_session import FakeLLM, FakeSource


def worker(root, runtime, boundary, restart_mode):
    root = Path(root)
    restart = restart_mode != 'start'
    def charge(kind):
        with (root / 'charges').open('a', encoding='utf-8') as stream:
            stream.write(kind + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    def gate():
        (root / 'gate').write_text(boundary, encoding='utf-8')
        while True:
            time.sleep(.05)
    class Source(FakeSource):
        def search(self, query, limit=10):
            charge('source')
            if boundary == 'search_sent' and not restart:
                gate()
            return super().search(query, limit)
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
        store = StateStore(root / 'state.sqlite3')
        try:
            config = Config(protocol='responses', action_protocol=('json' if restart_mode == 'restart-json' else 'responses_native'),
                            agent_runtime=runtime, model='fixture', cache=False,
                            api_key='SYNTHETIC_SECRET')
            sid = (root / 'session-id').read_text(encoding='utf-8') if restart else None
            chat = ChatSession(config, store, session_id=sid, source=Source(), llm=FakeLLM())
            llm = JsonLLM(config, chat.budget)
            def post(url, payload, headers):
                charge('model')
                if restart:
                    raise AssertionError('model replayed')
                continuation = payload['tool_choice'] == 'none'
                if (boundary == 'proposal_sent' and not continuation or
                        boundary == 'continuation_sent' and continuation):
                    gate()
                chat.budget.llm_calls += 1
                output = ([{'type': 'message', 'content': [{'type': 'output_text',
                           'text': 'SYNTHETIC_TRANSCRIPT'}]}] if continuation else
                          [{'type': 'function_call', 'call_id': 'SYNTHETIC_CALL_ID',
                            'name': 'sg_search', 'arguments': json.dumps(
                                {'query': 'fixture query', 'reason': 'SYNTHETIC_PRIVATE_REASON'})}])
                return {'status': 'completed', 'usage': {'input_tokens': 1, 'output_tokens': 1},
                        'output': output}
            llm._post = post
            chat.llm = llm
            if restart:
                result = chat.turn()
                (root / 'result').write_text(json.dumps({'result': result,
                    'candidates': len(store.candidate_papers(chat.session_id)),
                    'events': store.events(chat.session_id)}), encoding='utf-8')
            else:
                (root / 'session-id').write_text(chat.session_id, encoding='utf-8')
                if boundary == 'proposal_received':
                    original = chat.apply_decision
                    def apply(decision):
                        gate()
                        return original(decision)
                    chat.apply_decision = apply
                if boundary == 'before_continuation':
                    chat._continue_native = lambda *args: gate()
                chat.start(request_data={'topic': 'fixture', 'target_count': 5,
                                         'category': 'none', 'minimum_category_count': 0})
        finally:
            store.close()


class NativeRestartTests(unittest.TestCase):
    def check_runtime(self, runtime):
        for boundary in ('proposal_sent', 'proposal_received', 'search_sent',
                         'before_continuation', 'continuation_sent'):
            with self.subTest(runtime=runtime, boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
                        directory, runtime, boundary]
                env = isolated_environment(root)
                child = subprocess.Popen(args + ['start'], cwd=ROOT, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 20
                    while not (root / 'gate').exists() and time.monotonic() < deadline:
                        if child.poll() is not None:
                            self.fail(repr(child.communicate()))
                        time.sleep(.02)
                    self.assertTrue((root / 'gate').exists(), 'boundary not reached')
                finally:
                    child.kill()
                    child.communicate(timeout=5)
                before = (root / 'charges').read_text(encoding='utf-8')
                self.assertEqual(before.splitlines().count('model'),
                                 2 if boundary == 'continuation_sent' else 1)
                self.assertEqual(before.splitlines().count('source'),
                                 1 if boundary in ('search_sent', 'before_continuation',
                                                   'continuation_sent') else 0)
                resumed = subprocess.run(args + ['restart'], cwd=ROOT, env=env,
                                         capture_output=True, timeout=20)
                self.assertEqual(resumed.returncode, 0, (resumed.stdout, resumed.stderr))
                observed = json.loads((root / 'result').read_text(encoding='utf-8'))
                self.assertEqual(observed['result']['stop_reason'], 'pending_operation_not_replayed')
                self.assertEqual((root / 'charges').read_text(encoding='utf-8'), before)
                if boundary in ('before_continuation', 'continuation_sent'):
                    self.assertGreater(observed['candidates'], 0)
                markers = [e for e in observed['events'] if e['action'] == 'native_operation']
                self.assertTrue(markers)
                self.assertTrue(all(e['revision'] == 1 and e['payload'] == {'status': 'started'}
                                    for e in markers))
                for secret in ('SYNTHETIC_SECRET', 'SYNTHETIC_CALL_ID', 'SYNTHETIC_TRANSCRIPT',
                               'SYNTHETIC_PRIVATE_REASON'):
                    self.assertNotIn(secret, repr(observed['events']))
                    for database in root.glob('*.sqlite3*'):
                        self.assertNotIn(secret.encode(), database.read_bytes(), database.name)

    def test_legacy_restart(self):
        self.check_runtime('legacy')

    def test_graph_restart(self):
        self.check_runtime('langgraph')

    def test_switch_to_json_does_not_replay_unknown_native_operation(self):
        for runtime in ('legacy', 'langgraph'):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
                        directory, runtime, 'search_sent']
                env = isolated_environment(root)
                child = subprocess.Popen(args + ['start'], cwd=ROOT, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 20
                    while not (root / 'gate').exists() and time.monotonic() < deadline:
                        if child.poll() is not None:
                            self.fail('worker failed before boundary')
                        time.sleep(.02)
                    self.assertTrue((root / 'gate').exists())
                finally:
                    child.kill(); child.communicate(timeout=5)
                before = (root / 'charges').read_text(encoding='utf-8')
                resumed = subprocess.run(args + ['restart-json'], cwd=ROOT, env=env,
                                         capture_output=True, timeout=20)
                self.assertEqual(resumed.returncode, 0, (resumed.stdout, resumed.stderr))
                result = json.loads((root / 'result').read_text(encoding='utf-8'))['result']
                self.assertEqual(result['stop_reason'], 'pending_operation_not_replayed')
                self.assertEqual((root / 'charges').read_text(encoding='utf-8'), before)

    def test_direct_entrypoints_respect_unresolved_operation_after_reopen(self):
        for protocol in ('responses_native', 'json'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, isolated_environment(Path(directory)), clear=True), \
                    patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
                store = StateStore(Path(directory) / 'state.sqlite3')
                try:
                    native = Config(protocol='responses', action_protocol='responses_native', model='fixture')
                    chat = ChatSession(native, store, source=FakeSource(), llm=FakeLLM())
                    store.save_task(chat.session_id, 1, {'topic': 'fixture', 'target_count': 5})
                    chat._new_phase(first=True)
                    chat._native_operation_start()
                    source, fake = FakeSource(), FakeLLM()
                    config = Config(protocol='responses', action_protocol=protocol, model='fixture')
                    reopened = ChatSession(config, store, session_id=chat.session_id, source=source, llm=fake)
                    with patch.object(fake, 'propose_action', create=True, side_effect=AssertionError('model replayed')):
                        decision, stop = reopened.next_decision()
                    self.assertIsNone(decision)
                    self.assertEqual(stop, 'pending_operation_not_replayed')
                    self.assertEqual(fake.stages, [])
                    with self.assertRaises(GuideError):
                        reopened.apply_decision(Decision('SEARCH', {'query': 'another'}, True))
                    self.assertEqual(source.calls, [])
                finally:
                    store.close()

    def test_program_authorization_cannot_adopt_another_instances_marker(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, isolated_environment(Path(directory)), clear=True), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                config = Config(protocol='responses', action_protocol='responses_native', model='fixture')
                source = FakeSource()
                chat = ChatSession(config, store, source=source, llm=FakeLLM())
                store.save_task(chat.session_id, 1, {'topic': 'fixture', 'target_count': 5})
                chat._new_phase(first=True)
                decision = Decision('SEARCH', {'query': 'fixture query'}, True, 'program')
                chat._authorize_program_decision(decision)
                other = ChatSession(config, store, session_id=chat.session_id,
                                    source=FakeSource(), llm=FakeLLM())
                other._native_operation_start()
                with self.assertRaises(GuideError):
                    chat.apply_decision(decision)
                self.assertEqual(source.calls, [])
                self.assertTrue(other._native_operation_open())
            finally:
                store.close()

    def test_program_authorization_cannot_claim_new_revision(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, isolated_environment(Path(directory)), clear=True), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            path = Path(directory) / 'state.sqlite3'
            store = StateStore(path)
            other = None
            try:
                config = Config(protocol='responses', action_protocol='responses_native', model='fixture')
                source = FakeSource()
                chat = ChatSession(config, store, source=source, llm=FakeLLM())
                store.save_task(chat.session_id, 1, {'topic': 'fixture', 'target_count': 5})
                chat._new_phase(first=True)
                other = StateStore(path, init=False)
                decision = Decision('SEARCH', {'query': 'fixture query'}, True, 'program')
                chat._authorize_program_decision(decision)
                original = chat._native_operation_start
                def change_revision(*args, **kwargs):
                    other.save_task(chat.session_id, 2, {'topic': 'new fixture', 'target_count': 5})
                    return original(*args, **kwargs)
                with patch.object(chat, '_native_operation_start', side_effect=change_revision):
                    with self.assertRaises(GuideError):
                        chat.apply_decision(decision)
                self.assertEqual(source.calls, [])
                self.assertFalse(chat._native_operation_open())
            finally:
                if other is not None: other.close()
                store.close()

    def test_concurrent_instances_cannot_both_start_same_revision(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, isolated_environment(Path(directory)), clear=True), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            path = Path(directory) / 'state.sqlite3'
            first = StateStore(path)
            second = None
            try:
                config = Config(protocol='responses', action_protocol='responses_native', model='fixture')
                chat1 = ChatSession(config, first, source=FakeSource(), llm=FakeLLM())
                first.save_task(chat1.session_id, 1, {'topic': 'fixture', 'target_count': 5})
                chat1._new_phase(first=True)
                second = StateStore(path, init=False)
                chat2 = ChatSession(config, second, session_id=chat1.session_id,
                                    source=FakeSource(), llm=FakeLLM())
                ready = threading.Barrier(2)
                race = threading.Barrier(2)
                outcomes = []
                def worker(chat, delay):
                    original = chat._native_operation_open
                    def gated_open():
                        result = original()
                        if not chat.store.con.in_transaction and not result:
                            race.wait(timeout=3)
                            if delay:
                                time.sleep(.15)
                        return result
                    chat._native_operation_open = gated_open
                    try:
                        ready.wait(timeout=3)
                        chat._native_operation_start()
                        outcomes.append('started')
                    except GuideError:
                        outcomes.append('rejected')
                    except Exception as exc:
                        outcomes.append(type(exc).__name__)
                threads = [threading.Thread(target=worker, args=(chat1, False)),
                           threading.Thread(target=worker, args=(chat2, True))]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=8)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertCountEqual(outcomes, ['started', 'rejected'])
                markers = [e for e in first.events(chat1.session_id) if e['action'] == 'native_operation']
                self.assertEqual([e['payload']['status'] for e in markers], ['started'])
            finally:
                if second is not None: second.close()
                first.close()

    def test_audit_failure_precedes_model_and_timeout_stays_open(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, isolated_environment(Path(directory)), clear=True), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                config = Config(protocol='responses', action_protocol='responses_native', model='fixture')
                chat = ChatSession(config, store, source=FakeSource(), llm=FakeLLM())
                llm = JsonLLM(config, chat.budget)
                chat.llm = llm
                store.save_task(chat.session_id, 1, {'topic': 'fixture', 'target_count': 5})
                chat._new_phase(first=True)
                calls = []
                def timeout(*args):
                    calls.append('model')
                    raise GuideError('synthetic timeout')
                llm._post = timeout
                original = chat._event
                def failed_event(action, payload=None):
                    if action == 'native_operation':
                        raise OSError('audit unavailable')
                    return original(action, payload)
                with patch.object(chat, '_event', side_effect=failed_event):
                    with self.assertRaises(OSError):
                        chat.next_decision()
                self.assertEqual(calls, [])
                with self.assertRaises(GuideError):
                    chat.next_decision()
                self.assertEqual(calls, ['model'])
                self.assertTrue(chat._native_operation_open())
                self.assertEqual(chat.turn()['stop_reason'], 'pending_operation_not_replayed')
                self.assertEqual(calls, ['model'])
            finally:
                store.close()

    def test_new_message_new_revision_and_preserves_verified_evidence(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, isolated_environment(Path(directory)), clear=True), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                config = Config(protocol='responses', action_protocol='responses_native', model='fixture')
                fake = FakeLLM()
                chat = ChatSession(config, store, source=FakeSource(), llm=fake)
                store.save_task(chat.session_id, 1, {'topic': 'fixture', 'target_count': 5})
                chat._new_phase(first=True)
                chat._do_search('fixture query')
                chat._do_assess(['p1'])
                chat._native_operation_start()
                previous = store.events(chat.session_id)
                chat = ChatSession(config, store, session_id=chat.session_id,
                                   source=FakeSource(), llm=fake)
                llm = JsonLLM(config, chat.budget)
                llm.generate = fake.generate
                calls = []
                def post(*args):
                    calls.append('model')
                    raise GuideError('synthetic timeout')
                llm._post = post
                chat.llm = llm
                result = chat.turn()
                self.assertEqual(result['stop_reason'], 'pending_operation_not_replayed')
                self.assertTrue(any(p['id'] == 'p1' and p['evidence_verified'] for p in result['papers']))
                self.assertEqual(calls, [])
                chat.send('Please adjust the experiment focus.')
                self.assertEqual(chat.revision, 2)
                self.assertEqual(calls, ['model'])
                self.assertEqual(store.events(chat.session_id)[:len(previous)], previous)
                self.assertTrue(store.candidate_papers(chat.session_id))
            finally:
                store.close()

    def test_program_search_marker_stays_open_on_unknown_error(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, isolated_environment(Path(directory)), clear=True), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                config = Config(protocol='responses', action_protocol='responses_native', model='fixture')
                chat = ChatSession(config, store, source=FakeSource(), llm=FakeLLM())
                store.save_task(chat.session_id, 1, {'topic': 'fixture', 'target_count': 5})
                chat._new_phase(first=True)
                decision = Decision('SEARCH', {'query': 'fixture query'}, True, 'program')
                chat._authorize_program_decision(decision)
                with patch.object(chat, '_do_search', side_effect=GuideError('unknown outcome')):
                    with self.assertRaises(GuideError):
                        chat.apply_decision(decision)
                self.assertTrue(chat._native_operation_open())
                with self.assertRaises(GuideError):
                    chat.apply_decision(decision)
            finally:
                store.close()


if __name__ == '__main__':
    if len(sys.argv) == 6 and sys.argv[1] == '--worker':
        worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5])
    else:
        unittest.main()
