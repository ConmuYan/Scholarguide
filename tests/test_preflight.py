"""Metadata-only preflight must avoid impossible assessment without guessing."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Config, Paper
from controller import BudgetView, ControlState, allowed_actions
from demo import DemoSource
from preflight import constraint_papers
from recovery import recovery_patch
from session import ChatSession
from state import StateStore


def paper(venue='', year=None):
    return SimpleNamespace(venue=venue, year=year)


class PreflightTests(unittest.TestCase):
    def test_known_metadata_without_match_blocks_assess(self):
        result = constraint_papers(
            [paper('arXiv', 2024), paper('ICLR', 2025)],
            {'venue': 'CVPR', 'year_from': 2026, 'year_to': 2026})
        self.assertTrue(result['blocked'])
        self.assertEqual(result['eligible'], 0)
        self.assertEqual(result['known'], 2)

    def test_unknown_metadata_is_not_silently_excluded(self):
        result = constraint_papers(
            [paper('', None), paper('CVPR', 2026)],
            {'venue': 'CVPR', 'year_from': 2026, 'year_to': 2026})
        self.assertFalse(result['blocked'])
        self.assertEqual(result['unknown'], 1)
        self.assertEqual(result['eligible'], 1)

    def test_no_constraints_never_blocks(self):
        result = constraint_papers([paper('arXiv', 2024)], {'topic': 'RAG'})
        self.assertFalse(result['blocked'])
        self.assertFalse(result['has_constraints'])

    def test_empty_source_never_blocks_and_satisfiable_pool_passes(self):
        request = {'venue': 'CVPR', 'year_from': 2026, 'year_to': 2026}
        self.assertFalse(constraint_papers([], request)['blocked'])
        result = constraint_papers([paper('CVPR', 2026)], request)
        self.assertFalse(result['blocked'])
        self.assertEqual(result['eligible'], 1)

    def test_controller_removes_assess_when_preflight_blocks(self):
        state = ControlState(
            candidates=2, stale=2, gaps=({'requirement': 'paper_count'},),
            budget=BudgetView(seconds_left=60, llm_left=10, searches_left=0),
            candidate_ids=('a', 'b'), metadata_eligible=0, preflight_blocked=True)
        legal = allowed_actions(state)
        self.assertNotIn('ASSESS', legal)
        self.assertIn('FINISH', legal)


class AssessOnlyLLM:
    mode = 'demo'

    def generate(self, stage, prompt, data, schema, validate):
        out = {'action': 'ASSESS', 'paper_ids': ['p1'], 'reason': 'must be rejected'}
        validate(out)
        return out


class SessionPreflightIntegrationTests(unittest.TestCase):
    def test_control_state_blocks_assess_for_known_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / 'state.sqlite3')
            try:
                chat = ChatSession(Config(), store, source=DemoSource('satisfied'))
                task = {'topic': '3D generation', 'category': 'none',
                        'minimum_category_count': 0, 'target_count': 3,
                        'venue': 'CVPR', 'year_from': 2026, 'year_to': 2026}
                store.save_task(chat.session_id, 1, task)
                store.add_candidate(chat.session_id, Paper(
                    'p1', 'Paper', 'abstract', 'https://example.org/p1',
                    year=2024, venue='ICLR').to_dict(), 'q')
                state = chat.control_state()
                self.assertTrue(state.preflight_blocked)
                self.assertNotIn('ASSESS', allowed_actions(state))
                chat.llm = AssessOnlyLLM()
                chat._new_phase(first=True)
                decision, reason = chat.next_decision()
                self.assertIsNone(decision)
                self.assertEqual(reason, 'no_legal_action')
                self.assertNotIn('FINISH', allowed_actions(chat.control_state()))
                actions = [event['action'] for event in store.events(chat.session_id)]
                self.assertIn('invalid_action', actions)
                self.assertNotIn('ASSESS', allowed_actions(chat.control_state()))
            finally:
                store.close()


class RecoveryPatchTests(unittest.TestCase):
    def test_single_constraint_patches_are_explicit(self):
        self.assertEqual(recovery_patch('relax_year', {'year_from': 2026, 'year_to': 2026}),
                         {'year_from': None, 'year_to': None})
        self.assertEqual(recovery_patch('relax_venue', {'venue': 'CVPR'}), {'venue': ''})
        self.assertEqual(recovery_patch('lower_target', {'target_count': 5}, found=2),
                         {'target_count': 2, 'minimum_count': None, 'maximum_count': None})
