import unittest
from unittest.mock import patch

from common import Budget, BudgetExceeded, Config, Paper
from source import AcademicSource, retained_partial_batch


class ReceivedBudgetTests(unittest.TestCase):
    def test_deadline_between_sources_retains_only_received_papers(self):
        budget = Budget(5)
        client = AcademicSource(Config(source='multi', cache=False), budget)
        paper = Paper('first', 'First', '', 'https://paper.invalid/first', year=2025)
        def audit(run):
            budget.seconds = 0
        with patch.object(client, 'available', return_value=True), \
             patch.object(client, 'record_run', side_effect=audit), \
             patch.object(client, '_search_one', return_value=[paper]) as call:
            with self.assertRaises(BudgetExceeded) as caught:
                client.search_batch('query', providers=['lune', 'deepseek'])
        self.assertEqual(call.call_count, 1)
        batch = retained_partial_batch(caught.exception, 'query', ['lune', 'deepseek'], 10)
        self.assertEqual([p.id for p in batch.papers], ['first'])
        self.assertEqual(batch.runs[-1].remote_outcome, 'completed')
        self.assertIsNone(retained_partial_batch(caught.exception, 'query', ['deepseek'], 10))

    def test_received_results_survive_deadline_for_both_paid_sources(self):
        for provider in ('lune', 'deepseek'):
            with self.subTest(provider=provider):
                budget = Budget(5)
                client = AcademicSource(Config(source=provider, cache=False), budget)
                papers = [Paper('new', 'New', '', 'https://paper.invalid/new', year=2025),
                          Paper('old', 'Old', '', 'https://paper.invalid/old', year=2020)]
                def received(*args):
                    budget.seconds = 0
                    return papers
                with patch.object(client, 'available', return_value=True), \
                     patch.object(client, '_search_one', side_effect=received) as call:
                    with self.assertRaises(BudgetExceeded) as caught:
                        client.search_batch('query', providers=[provider], filters={'year_from': 2024})
                self.assertEqual(call.call_count, 1)
                batch = retained_partial_batch(caught.exception, 'query', [provider], 10)
                self.assertIsNotNone(batch)
                self.assertEqual([p.id for p in batch.papers], ['new'])
                self.assertEqual(batch.stop_reason, 'budget_exceeded')
                self.assertEqual(batch.runs[-1].remote_outcome, 'completed')
                self.assertIs(batch.runs[-1].retry_safe, False)
                self.assertIsNone(retained_partial_batch(caught.exception, 'other', [provider], 10))
