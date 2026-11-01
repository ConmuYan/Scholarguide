"""Chat QA plumbing uses actual ChatSession/SQLite with offline source/model fakes."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from common import Budget, Config, GuideError, source_names
from scripts.validate_chat_quality import (INITIAL_MESSAGE, EXPECTED, bind_chat, chat_checks,
                                          followup_message, main, run_validation)
from scripts.validate_research_quality import GuardedLLM, MonetaryGuard
from state import StateStore
from tests.test_research_runtime import OfflineLLM, OfflineSource, paper


def posters():
    result = []
    for index in range(4):
        identity = 'OFFLINE_CHAT_' + str(index)
        record = {'type': 'conference', 'status': 'accepted', 'venue_name': 'ICLR',
                  'venue_id': 'ICLR.cc/2025/Conference', 'date': '2025',
                  'presentation': 'poster', 'verified': True, 'identity_verified': True,
                  'evidence': [{'source': 'openreview', 'url': 'https://openreview.net/forum?id=' + identity,
                                'field': 'decision', 'value': 'Accept (Poster)'}]}
        result.append(paper(identity, venue='ICLR', year=2025, sources=['openreview'],
                            publication={**record, 'records': [record]}))
    return result


class NaturalLLM(OfflineLLM):
    def __init__(self, config, budget, guard, case):
        super().__init__()
        self.budget = budget

    def generate(self, stage, prompt, data, schema, validate):
        if stage != 'update':
            return super().generate(stage, prompt, data, schema, validate)
        self.budget.check_cancelled()
        self.budget.request_guard()
        self.budget.llm_calls += 1
        self.calls.append({'stage': stage, 'data': deepcopy(data)})
        patch = {'topic': 'visual representation learning', 'purpose': '比较视觉表征学习方法',
                 'reading_goal': 'comparison', **EXPECTED}
        match = re.search(r'paper ID为“([^”]+)”', data['message'])
        if match:
            patch['excluded_paper_ids'] = [match[1]]
        value = {'patch': patch, 'summary': 'Offline natural-language fixture.', 'unresolved': [],
                 'unsupported': [], 'assumptions': []}
        validate(value)
        return value


def source(config, budget):
    result = OfflineSource([posters()] * 4, providers=source_names(config.source))
    result.budget = budget
    return result


class ChatQualityValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(model='offline-chat-quality', research_mode=True,
                             agent_runtime='legacy', source='multi', cache=False,
                             cache_dir=self.root / 'cache', input_price=.15, output_price=.6,
                             currency='USD', base_url='http://localhost:1/v1')
        self.guard = MonetaryGuard(self.root / 'ledger.json', 10, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_natural_two_turns_exclusion_sqlite_rehydration_and_artifacts(self):
        output = self.root / 'qa'
        with patch('llm.urlopen', side_effect=AssertionError('offline validator test made an HTTP call')):
            manifest = run_validation(self.config, output, self.guard,
                                      source_factory=source, llm_factory=NaturalLLM)
        self.assertEqual(len(manifest['turns']), 2)
        self.assertFalse(manifest['structured_request_data_supplied'])
        self.assertEqual(manifest['initial_message'], INITIAL_MESSAGE)
        recovery = manifest['recovery']
        for name in ('saved_result_unchanged', 'saved_result_equals_previous', 'task_restored',
                     'selected_ids_restored', 'limits_counters_restored', 'ledger_entries_unchanged',
                     'protected_cost_unchanged', 'brief_cache_valid'):
            self.assertTrue(recovery[name], name)
        self.assertEqual(recovery['external_calls_during_read'], 0)
        for row in manifest['turns']:
            self.assertEqual(row['status'], 'complete')
            for name in ('target_count_retained', 'venue_retained', 'year_retained', 'publication_type_retained',
                         'acceptance_required', 'poster_required', 'requested_count_delivered',
                         'original_publication_constraints', 'all_quotes_present'):
                self.assertTrue(row['checks'][name], name)
        second = manifest['turns'][1]
        self.assertTrue(second['checks']['exclusion_recorded'])
        self.assertTrue(second['checks']['excluded_paper_absent'])
        self.assertTrue(second['same_session'])
        self.assertTrue(second['revision_advanced'])
        self.assertEqual(second['limits']['feedback_phases_left'], manifest['turns'][0]['limits']['feedback_phases_left'])
        for turn in (1, 2):
            for name in ('result.json', 'report.md', 'events.json', 'timed-events.json', 'model-events.json', 'checks.json'):
                self.assertTrue((output / f'turn-{turn}' / name).exists(), (turn, name))
        self.assertEqual(json.loads((output / 'manifest.json').read_text(encoding='utf-8'))['session_id'],
                         recovery['session_id'])

    def test_ask_does_not_fabricate_a_paper_to_exclude_or_send_an_extra_turn(self):
        class AskLLM(NaturalLLM):
            def generate(self, stage, prompt, data, schema, validate):
                if stage == 'update':
                    self.budget.request_guard()
                    self.budget.llm_calls += 1
                    self.calls.append({'stage': stage})
                    value = {'patch': {}, 'summary': 'Needs clarification.',
                             'unresolved': [{'field': 'topic', 'question': 'Which visual task?', 'blocking': True}],
                             'unsupported': [], 'assumptions': []}
                    validate(value)
                    return value
                return super().generate(stage, prompt, data, schema, validate)
        manifest = run_validation(self.config, self.root / 'ask', self.guard,
                                  source_factory=source, llm_factory=AskLLM)
        self.assertEqual(len(manifest['turns']), 1)
        self.assertEqual(manifest['turns'][0]['status'], 'awaiting_user')
        self.assertIn('No first selected paper', manifest['followup_skipped_reason'])
        self.assertFalse(manifest['scope_completed']['exclusion_turn_recorded'])
        self.assertFalse((self.root / 'ask' / 'turn-2').exists())

    def test_default_bound_clients_share_actual_session_budget_and_guard(self):
        with StateStore(self.root / 'isolated.sqlite3') as store:
            chat = bind_chat(self.config, store, self.guard, 'unexecuted')
            self.assertIsInstance(chat.llm, GuardedLLM)
            self.assertIs(chat.llm.guard, self.guard)
            self.assertIs(chat.llm.budget, chat.budget)
            self.assertIs(chat.source.budget, chat.budget)
            self.assertEqual(chat.budget.llm_calls, 0)
            self.assertEqual(self.guard.used(), 0)

    def test_checks_use_original_conditions_even_when_updater_silently_relaxes_them(self):
        first = posters()[0].to_dict()
        relaxed = {'request': {'topic': 'visual methods', 'target_count': 3, 'venue': '',
                               'accepted_only': False, 'presentation_types': []},
                   'papers': [first], 'research_brief': {}, 'status': 'partial'}
        checked = chat_checks(relaxed)
        self.assertFalse(checked['venue_retained'])
        self.assertFalse(checked['acceptance_required'])
        self.assertFalse(checked['poster_required'])
        bad = deepcopy(first)
        bad['publication'] = {}
        relaxed['papers'] = [bad]
        self.assertFalse(chat_checks(relaxed)['original_publication_constraints'])

    def test_budget_limit_and_nonempty_output_fail_before_loading_real_profile(self):
        with patch('scripts.validate_chat_quality.ProviderStore', side_effect=AssertionError('profile accessed')):
            with self.assertRaises(SystemExit):
                main(['--out', str(self.root / 'never'), '--ledger', str(self.root / 'ledger.json'),
                      '--budget-cny', '10.01'])
            occupied = self.root / 'occupied'
            occupied.mkdir()
            (occupied / 'evidence.json').write_text('{}')
            with self.assertRaises(SystemExit):
                main(['--out', str(occupied), '--ledger', str(self.root / 'ledger.json'), '--budget-cny', '10'])
        self.assertFalse((self.root / 'never').exists())


if __name__ == '__main__':
    unittest.main()
