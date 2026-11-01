"""Earlier source results survive a later ambiguous paid request."""
import unittest
from unittest.mock import patch

from common import Budget, BudgetExceeded, Config, GuideError, Paper, RunCancelled
from source import AcademicSource, retained_partial_batch


class MultiSourcePartialTests(unittest.TestCase):
    def test_unknown_paid_source_blocks_later_paid_source_but_not_free_source(self):
        source = AcademicSource(Config(source='multi', cache=False), Budget(4))
        seen = []
        def search(name, *args):
            seen.append(name)
            if name == 'lune':
                error = GuideError('synthetic transport failed')
                error.remote_outcome, error.retry_safe = 'unknown', False
                raise error
            return []
        with patch.object(source, 'available', return_value=True), \
             patch.object(source, '_search_one', side_effect=search):
            batch = source.search_batch('fixture', providers=['lune', 'deepseek', 'openalex'])
        self.assertEqual(seen, ['lune', 'openalex'])
        self.assertEqual(batch.runs[1].status, 'skipped')
        self.assertEqual(batch.runs[1].code, 'unresolved_paid_outcome')
        self.assertEqual(batch.runs[0].remote_outcome, 'unknown')

    def test_cancel_or_budget_after_later_unknown_keeps_prior_candidates(self):
        for error_type in (RunCancelled, BudgetExceeded):
            with self.subTest(error_type=error_type):
                source = AcademicSource(Config(source='multi', cache=False), Budget(4))
                paper = Paper('openalex:fixture', 'Prior candidate', 'Abstract',
                              'https://fixture.invalid/paper', sources=['openalex'])
                seen = []
                def search(name, *args):
                    seen.append(name)
                    if name == 'openalex':
                        return [paper]
                    error = error_type()
                    error.remote_outcome, error.retry_safe = 'unknown', False
                    raise error
                with patch.object(source, 'available', return_value=True), \
                     patch.object(source, '_search_one', side_effect=search):
                    with self.assertRaises(error_type) as caught:
                        source.search_batch('fixture', providers=['openalex', 'deepseek', 'lune'])
                self.assertEqual(seen, ['openalex', 'deepseek'])
                self.assertEqual(caught.exception.remote_outcome, 'unknown')
                self.assertIs(caught.exception.retry_safe, False)
                batch = retained_partial_batch(caught.exception, 'fixture',
                                               ['openalex', 'deepseek', 'lune'], 10)
                self.assertIsNotNone(batch)
                self.assertEqual([item.id for item in batch.papers], ['openalex:fixture'])
                self.assertEqual(batch.runs[-1].name, 'deepseek')
                self.assertEqual(batch.runs[-1].remote_outcome, 'unknown')
                self.assertIsNone(batch.runs[-1].cost_cny)


if __name__ == '__main__':
    unittest.main()
