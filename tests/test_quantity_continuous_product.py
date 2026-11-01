"""Product behavior: natural quantity, continuous turns, constraints and events."""
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import Config, GuideError, Paper, Request
from intent import allocate
from quantities import quantity_constraints
from recovery import recovery_patch
from session import ChatSession
from state import StateStore
from tests.test_session import FakeLLM, FakeSource, PAPERS
from workflow import coverage
from execution_events import operation


class QuantityProductTests(unittest.TestCase):
    def test_every_supported_count_survives_task_and_type_allocation(self):
        for count in range(1, 11):
            for goal in ('general', 'orientation', 'comparison', 'frontier'):
                with self.subTest(count=count, goal=goal):
                    task = Request.from_dict({'topic': 'RAG', 'target_count': count})
                    self.assertEqual(task.target_count, count)
                    self.assertEqual(sum(allocate(count, goal).values()), count)

    def test_natural_counts_intervals_default_and_years(self):
        cases = [('推荐 1 篇 RAG 论文', 1), ('推荐十篇论文', 10),
                 ('Recommend ten recent RAG papers.', 10), ('Find two papers about agents', 2),
                 ('找三到七篇论文', 5), ('Give me 3-7 papers.', 5),
                 ('最多四篇', 4), ('至少六篇论文', 6),
                 ('推荐5篇，其中至少2篇关于评测', 5)]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(quantity_constraints(text)['target_count'], expected)
        self.assertEqual(quantity_constraints('CVPR 2025 papers', default=True)['target_count'], 5)
        self.assertEqual(quantity_constraints('Include two evaluation papers.'), {})
        self.assertEqual(quantity_constraints('Drop the third paper.'), {})
        for over_limit in ('推荐十一篇论文', '推荐1010篇论文', 'recommend 1000 papers'):
            with self.subTest(over_limit=over_limit), self.assertRaisesRegex(GuideError, '1–10'):
                quantity_constraints(over_limit)

    def test_inferred_acceptance_cannot_become_an_unrequested_hard_filter(self):
        from quantities import calibrate_user_requirements
        proposal = {'venue':'ACL','accepted_only':True,'presentation_types':['poster']}
        plain = calibrate_user_requirements(proposal, '推荐3篇ACL 2025关于RAG的论文')
        self.assertFalse(plain['accepted_only'])
        self.assertEqual(plain['presentation_types'], [])
        explicit = calibrate_user_requirements(proposal, '只要正式录用为poster的论文')
        self.assertTrue(explicit['accepted_only'])
        self.assertEqual(explicit['presentation_types'], ['poster'])
        self.assertTrue(calibrate_user_requirements({'accepted_only':True}, '改成2篇', {'accepted_only':True})['accepted_only'])

    def test_interval_completion_and_category_conflict(self):
        request = Request.from_dict({'topic': 'RAG', 'minimum_count': 3, 'maximum_count': 7})
        self.assertEqual(request.target_count, 5)
        selected = [{'category_status': 'unknown'}] * 3
        self.assertFalse(coverage(selected, request))
        with self.assertRaises(GuideError):
            Request.from_dict({'topic': 'RAG', 'target_count': 2, 'category': 'survey', 'minimum_category_count': 3})
        self.assertEqual(recovery_patch('lower_target', {'target_count': 5, 'category': 'survey', 'minimum_category_count': 5}, found=3), {})


class ContinuousChatProductTests(unittest.TestCase):
    def test_eight_turns_restart_and_old_exhausted_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            store = StateStore(path)
            chat = ChatSession(Config(agent_runtime='legacy'), store, source=FakeSource(), llm=FakeLLM())
            first = chat.start(request_data={'topic': 'RAG', 'target_count': 3})
            self.assertEqual(len(first['papers']), 3)
            identity = chat.session_id
            for index in range(7):
                result = chat.send('Keep the same requirements.')
                self.assertEqual(result['status'], 'complete')
                self.assertEqual(result['request']['target_count'], 3)
                self.assertTrue(chat.can_send())
            budget = chat.limits(); budget['feedback_phases_left'] = 0
            store.update_budget(identity, budget)
            store.close()
            store = StateStore(path)
            try:
                resumed = ChatSession(Config(agent_runtime='legacy'), store, identity, source=FakeSource(), llm=FakeLLM())
                self.assertTrue(resumed.can_send())
                self.assertEqual(resumed.send('Keep these papers.')['status'], 'complete')
                self.assertGreaterEqual(resumed.revision, 9)
            finally:
                store.close()

    def test_explicit_quantity_override_and_failed_update_are_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            llm = FakeLLM()
            chat = ChatSession(Config(agent_runtime='legacy'), store, source=FakeSource(), llm=llm)
            try:
                chat.start(request_data={'topic': 'RAG', 'target_count': 3})
                revision = chat.revision
                with patch.object(llm, 'generate', side_effect=GuideError('test updater failed')):
                    with self.assertRaises(GuideError):
                        chat.send('Adjust my topic.')
                self.assertEqual(chat.revision, revision)
                self.assertTrue(chat.can_send())
                result = chat.send('Recommend two papers.')
                self.assertEqual(result['request']['target_count'], 2)
                self.assertEqual(len(result['papers']), 2)
            finally:
                store.close()

    def test_real_tool_start_is_visible_before_the_operation_finishes(self):
        events = []
        class ObservedSource(FakeSource):
            def search(self, query, limit=10):
                with operation('search', source='fixture') as measured:
                    self.test.assertTrue(any(row['action'] == 'tool_start' for row in events))
                    measured['returned'] = 6
                    return super().search(query, limit)
        with tempfile.TemporaryDirectory() as directory:
            source = ObservedSource(); source.test = self
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                chat = ChatSession(Config(agent_runtime='legacy'), store, source=source,
                                   llm=FakeLLM(), progress=events.append)
                chat.start(request_data={'topic': 'RAG', 'target_count': 3})
                self.assertTrue(any(row['action'] == 'tool_end' for row in events))
                self.assertTrue(all(row.get('at') for row in events))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
