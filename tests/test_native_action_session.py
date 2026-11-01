"""Offline native action integration with real protocol parsing and fake transport."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from common import Config, GuideError, RunCancelled
from controller import PolicyError
from llm import JsonLLM, _Rejected
from scripts.verify_delivery import isolated_environment
from session import ChatSession
from state import StateStore
from test_session import FakeLLM, FakeSource


class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, isolated_environment(Path(self.tmp.name)), clear=True)
        env.start()
        self.addCleanup(env.stop)
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.store = StateStore(Path(self.tmp.name) / 'state.sqlite3')
        self.addCleanup(self.store.close)
        self.source = FakeSource()
        self.config = Config(protocol='responses', action_protocol='responses_native', model='fake')
        self.chat = ChatSession(self.config, self.store, source=self.source, llm=FakeLLM())
        self.llm = JsonLLM(self.config, self.chat.budget)
        self.chat.llm = self.llm
        self.requests = []
        self.llm._post = self.post
        self.store.save_task(self.chat.session_id, 1, {'topic': 'RAG', 'target_count': 5,
                                                       'category': 'none', 'minimum_category_count': 0})
        self.chat._new_phase(first=True)

    def post(self, url, payload, headers):
        self.requests.append(copy.deepcopy(payload))
        self.chat.budget.llm_calls += 1
        if len(self.requests) % 2:
            output = [{'type': 'function_call', 'call_id': 'private-call-id', 'name': 'sg_search',
                       'arguments': json.dumps({'query': 'new query', 'reason': 'private-reason'})}]
        else:
            output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'private-continuation'}]}]
        return {'status': 'completed', 'output': output,
                'usage': {'input_tokens': 3, 'output_tokens': 2}}

    def test_native_prompt_requests_tool_call_not_json(self):
        self.chat.next_decision()
        system = self.requests[0]['input'][0]['content']
        self.assertIn('function', system.lower())
        self.assertNotIn('Return ONLY the requested JSON object', system)
        self.assertNotIn('Return {"action"', system)

    def test_search_roundtrip_and_accounting(self):
        decision, stop = self.chat.next_decision()
        self.assertEqual((decision.action, stop), ('SEARCH', ''))
        self.chat.apply_decision(decision)
        self.assertEqual(len(self.requests), 2)
        output = self.requests[1]['input'][-1]
        self.assertEqual((output['type'], output['call_id']), ('function_call_output', 'private-call-id'))
        self.assertNotIn('new query', output['output'])
        self.assertEqual(self.chat.budget.usage_calls, 2)
        self.assertEqual(self.chat.limits()['llm_left'], 22)
        rows = self.store.con.execute('SELECT reserved, settled, refunded FROM budget_ledger WHERE action = ?', ('action',)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['reserved'] == row['settled'] + row['refunded'] for row in rows))
        events = repr(self.store.events(self.chat.session_id))
        self.assertNotIn('private-call-id', events)
        self.assertNotIn('private-continuation', events)

    def test_mutated_args_rejected(self):
        decision, _ = self.chat.next_decision()
        decision.args['query'] = 'other query'
        with self.assertRaises(PolicyError):
            self.chat.apply_decision(decision)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.source.calls)

    def test_replay_is_rejected(self):
        decision, _ = self.chat.next_decision()
        self.chat.apply_decision(decision)
        with self.assertRaises(PolicyError):
            self.chat.apply_decision(decision)
        self.assertEqual(len(self.source.calls), 1)

    def test_revision_change_rejected(self):
        decision, _ = self.chat.next_decision()
        self.store.save_task(self.chat.session_id, 2, {**self.chat.task(), 'purpose': 'changed'})
        with self.assertRaises(PolicyError):
            self.chat.apply_decision(decision)
        self.assertFalse(self.source.calls)
        self.assertEqual(len(self.requests), 1)

    def test_cancel_after_proposal_prevents_action(self):
        decision, _ = self.chat.next_decision()
        self.chat.request_stop()
        with self.assertRaises(RunCancelled):
            self.chat.apply_decision(decision)
        self.assertFalse(self.source.calls)
        self.assertEqual(len(self.requests), 1)

    def test_local_native_failure_closes_unstarted_marker(self):
        def fail(*args, **kwargs):
            raise GuideError('synthetic local validation failure')
        self.llm.propose_action = fail
        with self.assertRaises(GuideError):
            self.chat.next_decision()
        self.assertFalse(self.chat._native_operation_open())

    def test_native_response_failure_keeps_started_marker(self):
        def invalid(*args, **kwargs):
            self.chat.budget.llm_calls += 1
            return {'status': 'completed', 'output': [],
                    'usage': {'input_tokens': 1, 'output_tokens': 1}}
        self.llm._post = invalid
        with self.assertRaises(GuideError):
            self.chat.next_decision()
        self.assertTrue(self.chat._native_operation_open())

    def test_explicit_unsupported_fallback_closes_native_marker(self):
        from llm import _Rejected
        self.config.native_fallback = 'json_on_unsupported'
        self.llm._post = lambda *args, **kwargs: (_ for _ in ()).throw(
            _Rejected(400, '', 'function calling is not supported'))
        fallback = FakeLLM()
        self.llm.generate = fallback.generate
        decision, stop = self.chat.next_decision()
        self.assertEqual(stop, '')
        self.assertIsNotNone(decision)
        self.assertFalse(self.chat._native_operation_open())

    def test_cancelled_local_proposal_closes_unstarted_marker(self):
        def cancel(*args, **kwargs):
            self.chat.request_stop()
            raise RunCancelled()
        self.llm.propose_action = cancel
        with self.assertRaises(RunCancelled):
            self.chat.next_decision()
        self.assertFalse(self.chat._native_operation_open())

    def test_no_budget_for_continuation(self):
        from common import BudgetExceeded
        decision, _ = self.chat.next_decision()
        limits = self.chat.limits()
        limits['llm_left'] = 0
        self.chat._save_limits(limits)
        with self.assertRaises(BudgetExceeded):
            self.chat.apply_decision(decision)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(any(e['action'] == 'native_continuation' and e['payload']['status'] == 'budget_exceeded'
                            for e in self.store.events(self.chat.session_id)))

    def test_failed_search_is_not_continued(self):
        def failed(*args):
            raise GuideError('synthetic failure')
        self.source.search = failed
        decision, _ = self.chat.next_decision()
        self.chat.apply_decision(decision)
        self.assertEqual(len(self.requests), 1)

    def test_ask_and_finish_never_continue_even_if_finish_returns_none(self):
        for action in ('ASK', 'FINISH'):
            with self.subTest(action=action):
                self.requests.clear()
                original = self.post
                def post(url, payload, headers):
                    response = original(url, payload, headers)
                    args = ({'question': 'Which?', 'affected_field': 'purpose', 'reason': 'r'}
                            if action == 'ASK' else {'reason': 'r'})
                    response['output'] = [{'type': 'function_call', 'call_id': 'terminal-call',
                                           'name': 'sg_' + action.lower(), 'arguments': json.dumps(args)}]
                    return response
                self.llm._post = post
                current = self.chat.control_state()
                state = replace(current, gaps=(), unresolved=({'field': 'purpose', 'blocking': True},))
                with patch.object(self.chat, 'control_state', return_value=state), \
                     patch.object(self.chat, '_ask'), patch.object(self.chat, '_finish', return_value=None), \
                     patch.object(self.chat, '_result', return_value={'status': 'awaiting_user'}):
                    decision, _ = self.chat.next_decision()
                    result = self.chat.apply_decision(decision)
                self.assertEqual(decision.action, action)
                self.assertEqual(len(self.requests), 1)
                if action == 'FINISH':
                    self.assertIsNone(result)

    def test_provider_identity_drift_rejected(self):
        decision, _ = self.chat.next_decision()
        self.config.model = 'different'
        with self.assertRaises(PolicyError):
            self.chat.apply_decision(decision)
        self.assertFalse(self.source.calls)

    def test_empty_search_is_continued(self):
        self.source.papers = []
        decision, _ = self.chat.next_decision()
        self.chat.apply_decision(decision)
        self.assertEqual(len(self.requests), 2)

    def test_fallback_only_for_explicit_unsupported(self):
        for error, permitted in ((_Rejected(400, '', 'tools are not supported'), True),
                                 (_Rejected(400, '', 'invalid model'), False),
                                 (GuideError('synthetic timeout'), False)):
            with self.subTest(permitted=permitted):
                # Each failed/unfinished operation deliberately leaves its marker
                # open; use a distinct session rather than implicitly replaying it.
                self.config = Config(protocol='responses', action_protocol='responses_native', model='fake',
                                     native_fallback='json_on_unsupported')
                self.chat = ChatSession(self.config, self.store, source=FakeSource(), llm=FakeLLM())
                self.llm = JsonLLM(self.config, self.chat.budget)
                self.chat.llm = self.llm
                self.store.save_task(self.chat.session_id, 1, {'topic': 'RAG', 'target_count': 5,
                                                              'category': 'none', 'minimum_category_count': 0})
                self.chat._new_phase(first=True)
                fallback = FakeLLM()
                self.llm.generate = fallback.generate
                def fail(url, payload, headers):
                    self.chat.budget.llm_calls += 1
                    raise error
                self.llm._post = fail
                if permitted:
                    decision, _ = self.chat.next_decision()
                    self.assertEqual(decision.action, 'SEARCH')
                    self.assertIn('action', fallback.stages)
                else:
                    with self.assertRaises(GuideError):
                        self.chat.next_decision()
                    self.assertNotIn('action', fallback.stages)
                self.assertEqual(sum(e['action'] == 'native_fallback'
                                     for e in self.store.events(self.chat.session_id)), int(permitted))

    def test_both_runtimes_complete_search_and_assess_roundtrips(self):
        for runtime in ('legacy', 'langgraph'):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                store = StateStore(Path(directory) / 'state.sqlite3')
                try:
                    config = Config(protocol='responses', action_protocol='responses_native',
                                    agent_runtime=runtime, model='fake')
                    fake = FakeLLM()
                    chat = ChatSession(config, store, source=FakeSource(), llm=fake)
                    llm = JsonLLM(config, chat.budget)
                    llm.generate = fake.generate
                    chat.llm = llm
                    requests = []
                    def post(url, payload, headers):
                        requests.append(copy.deepcopy(payload))
                        chat.budget.llm_calls += 1
                        if payload['tool_choice'] == 'none':
                            output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'ignored'}]}]
                        else:
                            data = json.loads(payload['input'][1]['content'])
                            pending = data['observation']['pending_ids']
                            if pending:
                                name, args = 'sg_assess', {'paper_ids': pending[:5], 'reason': 'r'}
                            elif data['gaps']:
                                name, args = 'sg_search', {'query': 'new query', 'reason': 'r'}
                            else:
                                name, args = 'sg_finish', {'reason': 'r'}
                            output = [{'type': 'function_call', 'call_id': 'fixture-call',
                                       'name': name, 'arguments': json.dumps(args)}]
                        return {'status': 'completed', 'output': output,
                                'usage': {'input_tokens': 1, 'output_tokens': 1}}
                    llm._post = post
                    result = chat.start(request_data={'topic': 'RAG', 'target_count': 5,
                                                      'category': 'none', 'minimum_category_count': 0})
                    self.assertEqual(result['status'], 'complete')
                    continuations = [r for r in requests if r['tool_choice'] == 'none']
                    self.assertGreaterEqual(len(continuations), 2)
                    actions = [json.loads(request['input'][-1]['output'])['action'] for request in continuations]
                    self.assertEqual(actions[0], 'SEARCH')
                    self.assertTrue(all(action == 'ASSESS' for action in actions[1:]))
                    for request in continuations:
                        self.assertEqual(request['input'][-1]['call_id'], 'fixture-call')
                finally:
                    store.close()

    def test_cancel_after_action_prevents_continuation(self):
        decision, _ = self.chat.next_decision()
        original = self.chat._do_search
        def search_then_cancel(*args, **kwargs):
            original(*args, **kwargs)
            self.chat.request_stop()
        with patch.object(self.chat, '_do_search', side_effect=search_then_cancel), \
             self.assertRaises(RunCancelled):
            self.chat.apply_decision(decision)
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(len(self.requests), 1)

    def test_assessment_failure_is_not_continued(self):
        first, _ = self.chat.next_decision()
        self.chat.apply_decision(first)
        def propose_assess(url, payload, headers):
            self.requests.append(copy.deepcopy(payload))
            self.chat.budget.llm_calls += 1
            return {'status': 'completed', 'usage': {'input_tokens': 1, 'output_tokens': 1},
                    'output': [{'type': 'function_call', 'call_id': 'assess-call', 'name': 'sg_assess',
                                'arguments': json.dumps({'paper_ids': ['p1'], 'reason': 'r'})}]}
        self.llm._post = propose_assess
        self.llm.generate = lambda *args: (_ for _ in ()).throw(GuideError('assessment failed'))
        decision, _ = self.chat.next_decision()
        with self.assertRaises(GuideError):
            self.chat.apply_decision(decision)
        self.assertEqual(len(self.requests), 3)

    def test_cancellation_from_continuation_is_not_swallowed(self):
        original = self.llm._post
        def cancelled(url, payload, headers):
            if self.requests:
                self.chat.budget.llm_calls += 1
                raise RunCancelled()
            return original(url, payload, headers)
        self.llm._post = cancelled
        decision, _ = self.chat.next_decision()
        with self.assertRaises(RunCancelled):
            self.chat.apply_decision(decision)
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(self.chat.budget.llm_calls, 2)
        self.assertTrue(any(event['action'] == 'native_continuation' and
                            event['payload']['status'] == 'cancelled'
                            for event in self.store.events(self.chat.session_id)))

    def test_continuation_budget_exhaustion_propagates(self):
        from common import BudgetExceeded
        original = self.llm._post
        def exhausted(url, payload, headers):
            if self.requests:
                self.chat.budget.llm_calls += 1
                raise BudgetExceeded('synthetic budget')
            return original(url, payload, headers)
        self.llm._post = exhausted
        decision, _ = self.chat.next_decision()
        with self.assertRaises(BudgetExceeded):
            self.chat.apply_decision(decision)
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(self.chat.budget.llm_calls, 2)

    def test_continuation_audit_failure_does_not_mask_original_error(self):
        original_event = self.chat._event
        original_error = RunCancelled()
        def emit(action, payload=None):
            if action == 'native_continuation':
                raise OSError('synthetic private audit detail')
            return original_event(action, payload)
        decision, _ = self.chat.next_decision()
        with patch.object(self.chat, '_event', side_effect=emit), \
             patch.object(self.chat, '_do_search', side_effect=original_error):
            with self.assertRaises(RunCancelled) as caught:
                self.chat.apply_decision(decision)
        self.assertIs(caught.exception, original_error)
        self.assertNotIn('synthetic private', repr(self.chat._warnings))

    def test_graph_critic_program_search_has_bound_native_authorization(self):
        from demo import ChatDemoLLM, DemoSource
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                config = Config(protocol='responses', action_protocol='responses_native',
                                agent_runtime='langgraph', model='fake')
                source = DemoSource('satisfied')
                chat = ChatSession(config, store, source=source, llm=ChatDemoLLM())
                chat.store.save_task(chat.session_id, 1, {'topic': 'RAG', 'target_count': 5})
                chat._new_phase(first=True)
                chat._refine_query = lambda: 'new query'
                planned = chat.agent_runtime.critic({'decision': {}, 'reflected': False})
                self.assertEqual(planned['stage'], 'retrieve')
                state = chat.agent_runtime.retrieve(planned)
                self.assertEqual(state['stage'], 'critic')
                self.assertEqual(source.calls, 1)
            finally:
                store.close()

    def test_continuation_failure_stops_both_runtimes_without_another_call(self):
        for runtime in ('legacy', 'langgraph'):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                store = StateStore(Path(directory) / 'state.sqlite3')
                try:
                    config = Config(protocol='responses', action_protocol='responses_native',
                                    agent_runtime=runtime, model='fake')
                    source, fake = FakeSource(), FakeLLM()
                    chat = ChatSession(config, store, source=source, llm=fake)
                    llm = JsonLLM(config, chat.budget)
                    llm.generate = fake.generate
                    chat.llm = llm
                    calls = []
                    def post(url, payload, headers):
                        calls.append(payload['tool_choice'])
                        chat.budget.llm_calls += 1
                        if payload['tool_choice'] == 'none':
                            raise GuideError('synthetic continuation failure')
                        return {'status': 'completed', 'usage': {'input_tokens': 1, 'output_tokens': 1},
                                'output': [{'type': 'function_call', 'call_id': 'fixture', 'name': 'sg_search',
                                            'arguments': json.dumps({'query': 'fixture query', 'reason': 'r'})}]}
                    llm._post = post
                    result = chat.start(request_data={'topic': 'RAG', 'target_count': 5,
                                                      'category': 'none', 'minimum_category_count': 0})
                    self.assertEqual(calls, ['required', 'none'])
                    self.assertEqual(len(source.calls), 1)
                    self.assertNotEqual(result['status'], 'complete')
                    self.assertTrue(store.candidate_papers(chat.session_id))
                finally:
                    store.close()

    def test_json_fallback_never_records_model_reason(self):
        self.chat.config.native_fallback = 'json_on_unsupported'
        fallback = FakeLLM()
        def generate(stage, *args):
            value = fallback.generate(stage, *args)
            if stage == 'action':
                value['reason'] = 'SYNTHETIC_PRIVATE_REASON'
            return value
        self.llm.generate = generate
        def unsupported(url, payload, headers):
            self.chat.budget.llm_calls += 1
            raise _Rejected(400, '', 'tools are not supported')
        self.llm._post = unsupported
        decision, _ = self.chat.next_decision()
        self.assertEqual(decision.action, 'SEARCH')
        self.assertNotIn('SYNTHETIC_PRIVATE_REASON', repr(self.store.events(self.chat.session_id)))
        self.assertNotIn('SYNTHETIC_PRIVATE_REASON', repr(self.chat._trace))

    def test_continuation_failure_refunds_ledger(self):
        original = self.llm._post
        def fail_second(url, payload, headers):
            if self.requests:
                self.chat.budget.llm_calls += 1
                raise GuideError('synthetic continuation error')
            return original(url, payload, headers)
        self.llm._post = fail_second
        decision, _ = self.chat.next_decision()
        with self.assertRaises(GuideError):
            self.chat.apply_decision(decision)
        rows = self.store.con.execute('SELECT reserved, settled, refunded FROM budget_ledger WHERE action = ?', ('action',)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['reserved'] == row['settled'] + row['refunded'] for row in rows))
        self.assertEqual(rows[-1]['settled'], 0)
        self.assertEqual(self.chat.budget.llm_calls, 2)


if __name__ == '__main__':
    unittest.main()
