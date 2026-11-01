"""Regression contracts for opt-in CLI research, import provenance and graph reuse.

All model and academic-source interfaces are offline fakes. State and artifacts
are isolated in temporary directories; no paid API or real history is used.
"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import app
from common import Budget, Config
from providers import ProviderStore
from research_runtime import attach_brief
from session import ChatSession
from state import StateStore
from webapp import ChatService
from workflow import run as workflow_run
from test_research_runtime import OfflineLLM, OfflineSource, TASK, paper


class ResearchContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.items = [paper(f'P{i}') for i in range(1, 4)]
        self.task = {**TASK, 'reading_goal': ''}
        self.request = self.root / 'request.json'
        self.request.write_text(json.dumps(self.task), encoding='utf-8')
        self.config = Config(agent_runtime='legacy', model='offline-fixture', cache=False,
                             base_url='http://127.0.0.1:1')

    def tearDown(self):
        self.tmp.cleanup()

    def run_workflow(self, config=None):
        config = config or replace(self.config, research_mode=True)
        budget = Budget(60)
        source, llm = OfflineSource([self.items]), OfflineLLM()
        source.budget = llm.budget = budget
        result = workflow_run('visual retrieval', config, method='S',
                              request_data=self.task, source=source, llm=llm, budget=budget)
        self.assertEqual(result['status'], 'complete')
        return result

    def service(self):
        providers = ProviderStore(self.root / 'providers.json')
        providers.upsert({'id': 'offline-fixture', 'name': 'Offline fixture',
            'base_url': 'http://127.0.0.1:1', 'model': 'offline-fixture'}, activate=True)
        return ChatService(self.root / 'web', providers=providers)

    def test_cli_run_preserves_default_and_supports_explicit_research_override(self):
        cases = [(False, [], False), (False, ['--research-mode'], True),
                 (True, ['--no-research-mode'], False), (True, [], True)]
        for index, (environment, flags, expected) in enumerate(cases):
            with self.subTest(environment=environment, flags=flags):
                config = replace(self.config, research_mode=environment)
                configs = []

                def offline_run(user_input, actual, method, **kwargs):
                    configs.append(actual)
                    return self.run_workflow(actual)

                out = self.root / f'run-{index}'
                with patch('app.Config.from_env', return_value=config), \
                        patch('app.run', side_effect=offline_run), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = app.main(['run', '--request-file', str(self.request),
                                     '--method', 'S', '--source', 'arxiv',
                                     '--out', str(out), *flags])
                self.assertEqual(code, 0)
                saved = json.loads((out / 'result.json').read_text(encoding='utf-8'))
                self.assertEqual(configs[0].research_mode, expected)
                self.assertEqual(configs[0].source, 'arxiv')
                self.assertEqual('research_brief' in saved, expected)

    def test_cli_chat_opt_in_uses_research_runtime_and_exports_brief(self):
        chats = []

        def offline_chat(config, store, session_id, **kwargs):
            source, llm = OfflineSource([self.items]), OfflineLLM()
            chat = ChatSession(config, store, session_id, source=source, llm=llm)
            source.budget = llm.budget = chat.budget
            chats.append(chat)
            return chat

        out = self.root / 'cli-chat'
        with patch('app.Config.from_env', return_value=self.config), \
                patch('session.ChatSession', side_effect=offline_chat), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = app.main(['chat', '--request-file', str(self.request), '--research-mode',
                             '--source', 'arxiv', '--state', str(self.root / 'cli.sqlite3'),
                             '--out', str(out)])
        self.assertEqual(code, 0)
        self.assertTrue(chats[0].config.research_mode)
        saved = json.loads((out / 'turn-1' / 'result.json').read_text(encoding='utf-8'))
        self.assertIn(saved['research_brief']['status'], ('ready', 'partial'))
        self.assertEqual(saved['research_brief']['paper_ids'], ['P1', 'P2', 'P3'])

    def test_cli_closes_store_after_export_failure(self):
        import sqlite3
        chats = []
        def offline_chat(config, store, session_id, **kwargs):
            source, llm = OfflineSource([self.items]), OfflineLLM()
            chat = ChatSession(config, store, session_id, source=source, llm=llm)
            source.budget = llm.budget = chat.budget
            chats.append(chat)
            return chat
        with patch('app.Config.from_env', return_value=self.config), \
                patch('session.ChatSession', side_effect=offline_chat), \
                patch('app.save_run', side_effect=OSError('synthetic export failure')) as save, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = app.main(['chat', '--request-file', str(self.request), '--research-mode',
                             '--source', 'arxiv', '--state', str(self.root / 'failed-cli.sqlite3'),
                             '--out', str(self.root / 'failed-output')])
        self.assertEqual(code, 1)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(len(chats), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            chats[0].store.con.execute('SELECT 1')

    def test_search_import_reuses_validated_brief_after_service_restart_without_model(self):
        result = self.run_workflow()
        original = deepcopy(result)
        service = self.service()
        try:
            identity = service.import_run(result, 'offline-cache-import')
        finally:
            service.close()
        service = self.service()
        try:
            record = service._load_record(identity)
            self.assertIsNotNone(record)
            chat = record['chat']
            llm = OfflineLLM()
            llm.budget = chat.budget
            chat.llm = llm
            self.assertEqual(len(chat.select()), 3)
            chat._consume(llm_left=chat.limits()['llm_left'])
            cached = attach_brief(chat, chat.select())
            self.assertTrue(cached['reused'])
            self.assertEqual(cached['input_key'], original['research_brief']['input_key'])
            self.assertEqual(llm.calls, [])
            self.assertEqual(result, original)
        finally:
            service.close()

    def test_search_import_does_not_relabel_old_model_assessments_as_current(self):
        result = self.run_workflow(replace(self.config, research_mode=True, model='older-model'))
        service = self.service()
        try:
            identity = service.import_run(result, 'offline-model-change')
            chat = service.sessions[identity]['chat']
            self.assertEqual(chat.model, 'offline-fixture')
            self.assertEqual(chat.select(), [])
            self.assertEqual(set(chat._plan().stale_ids), {'P1', 'P2', 'P3'})
            rows = chat.store.assessments_for_paper('P1')
            self.assertEqual({row['model'] for row in rows}, {'older-model'})
        finally:
            service.close()

    def test_invalid_imported_brief_is_displayed_as_history_but_never_cached(self):
        result = self.run_workflow()
        result['research_brief']['paper_notes'][0]['contribution']['evidence'][0]['paper_id'] = 'FORGED-ID'
        service = self.service()
        try:
            identity = service.import_run(result, 'offline-forged-brief')
            chat = service.sessions[identity]['chat']
            self.assertFalse(any(event['action'] == 'research_brief'
                                 for event in chat.store.events(identity)))
            llm = OfflineLLM()
            llm.budget = chat.budget
            chat.llm = llm
            chat._new_phase(first=True)
            brief = attach_brief(chat, chat.select())
            self.assertFalse(brief['reused'])
            self.assertEqual(llm.count('synthesis'), 1)
        finally:
            service.close()

    def test_imported_legacy_mode_survives_provider_refresh(self):
        result = self.run_workflow(self.config)
        service = self.service()
        try:
            identity = service.import_run(result, 'offline-legacy-import')
            record = service.sessions[identity]
            self.assertFalse(record['chat'].config.research_mode)
            service._refresh_live_provider(record)
            self.assertFalse(record['chat'].config.research_mode)
            self.assertFalse(record['research_mode'])
        finally:
            service.close()

    def test_backend_switch_invalidates_same_name_assessment_and_brief(self):
        store = StateStore(self.root / 'backend.sqlite3')
        try:
            config = replace(self.config, research_mode=True)
            source, llm = OfflineSource([self.items]), OfflineLLM()
            chat = ChatSession(config, store, source=source, llm=llm)
            source.budget = llm.budget = chat.budget
            result = chat.start(request_data=self.task)
            selected = chat.select()
            other = OfflineLLM()
            other.budget = chat.budget
            chat.rebind_provider(replace(config, provider='ollama'), other)
            self.assertEqual(chat.select(), [])
            brief = attach_brief(chat, selected)
            self.assertFalse(brief['reused'])
            self.assertNotEqual(brief['input_key'], result['research_brief']['input_key'])
            self.assertEqual(other.count('synthesis'), 1)
        finally:
            store.close()

    def test_cancel_during_synthesis_retains_papers_and_reconciles_terminal_status(self):
        from common import RunCancelled

        class CancelSynthesis(OfflineLLM):
            def generate(self, stage, *args):
                if stage == 'synthesis':
                    self.budget.cancel_token.request()
                    raise RunCancelled()
                return super().generate(stage, *args)

        for runtime in ('search', 'legacy', 'langgraph'):
            with self.subTest(runtime=runtime):
                config = replace(self.config, research_mode=True,
                                 agent_runtime='langgraph' if runtime == 'langgraph' else 'legacy')
                source, llm = OfflineSource([self.items]), CancelSynthesis()
                store = StateStore(self.root / (runtime + '.sqlite3'))
                try:
                    if runtime == 'search':
                        budget = Budget(60)
                        source.budget = llm.budget = budget
                        result = workflow_run('visual retrieval', config, 'S', request_data=self.task,
                                              source=source, llm=llm, budget=budget)
                    else:
                        chat = ChatSession(config, store, source=source, llm=llm)
                        source.budget = llm.budget = chat.budget
                        result = chat.start(request_data=self.task)
                    self.assertEqual(result['status'], 'partial')
                    self.assertEqual(result['stop_reason'], 'user_cancelled')
                    self.assertEqual(len(result['papers']), 3)
                    self.assertEqual(result['research_brief']['reason'], 'user_cancelled')
                    self.assertTrue(any(row['action'] == 'cancelled' for row in result['trace']))
                finally:
                    store.close()

    def test_feedback_can_clear_year_bounds_without_dropping_publication_constraints(self):
        store = StateStore(self.root / 'update.sqlite3')
        try:
            chat = ChatSession(replace(self.config, research_mode=True), store,
                               source=OfflineSource(), llm=OfflineLLM())
            store.save_task(chat.session_id, 1, {**self.task, 'year_from': 2024,
                'year_to': 2025, 'accepted_only': True, 'publication_type': 'conference'})
            chat._new_phase(first=True)
            raw = {'patch': {'year_from': None, 'year_to': None},
                   'summary': 'Remove the year range.', 'unsupported': [], 'unresolved': []}
            with patch('session.run_decision', return_value=raw):
                updated = chat.update_task('Remove the year range, keeping accepted conference papers.')
            self.assertIsNone(updated['year_from'])
            self.assertIsNone(updated['year_to'])
            self.assertTrue(updated['accepted_only'])
            self.assertEqual(updated['publication_type'], 'conference')
        finally:
            store.close()

    def test_research_graph_preserves_selection_synthesis_and_reopens_checkpoint(self):
        store = StateStore(self.root / 'graph.sqlite3')
        try:
            source, llm = OfflineSource([self.items]), OfflineLLM()
            chat = ChatSession(replace(self.config, research_mode=True, agent_runtime='langgraph'),
                               store, source=source, llm=llm)
            source.budget = llm.budget = chat.budget
            self.assertIsNotNone(chat.agent_runtime)
            result = chat.start(request_data=self.task)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(llm.count('synthesis'), 1)
            self.assertEqual(len(result['papers']), 3)
            before = len(llm.calls)
            reopened = chat.agent_runtime.run_turn()
            self.assertTrue(chat.agent_runtime.last_resumed)
            self.assertEqual(reopened['research_brief']['input_key'], result['research_brief']['input_key'])
            self.assertEqual(len(llm.calls), before)
            self.assertTrue((self.root / 'agent-checkpoints.sqlite3').exists())
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
