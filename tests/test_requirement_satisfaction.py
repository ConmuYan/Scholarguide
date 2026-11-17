"""A planner may echo a supported instruction; only real constraints stop search."""
import unittest

from quantities import calibrate_ranking_requirements, clause_is_satisfied


PLAN = {'venue': 'AAAI, CVPR, ICCV, ICLR, ICML, NeurIPS', 'year_from': 2024,
        'year_to': 2026, 'ranking': 'citations', 'target_count': 5}


class SatisfiedClauseTests(unittest.TestCase):
    def test_delivery_contract_clauses_are_satisfied(self):
        for clause in ('Each paper should contain rank',
                       'Use null for unknown details.',
                       'Do not include any benchmark labels or scores',
                       'explicitly report the gap',
                       'Return a ranked JSON object with a papers array'):
            with self.subTest(clause=clause):
                self.assertTrue(clause_is_satisfied(PLAN, clause))

    def test_scope_that_the_plan_already_carries_is_satisfied(self):
        for clause in ('Only use the provided publication years and conference whitelist.',
                       'Eligible conferences: AAAI, CVPR, ICCV, ICLR, ICML, NeurIPS',
                       'Restrict to 2024-2026'):
            with self.subTest(clause=clause):
                self.assertTrue(clause_is_satisfied(PLAN, clause))
        # Years outside the applied range, or no year range at all, are not covered.
        self.assertFalse(clause_is_satisfied({**PLAN, 'year_from': None, 'year_to': None},
                                             'Only papers from 2024 to 2026'))
        self.assertFalse(clause_is_satisfied(PLAN, 'Only papers from 2020 to 2023'))

    def test_real_eligibility_constraints_stay_unsupported(self):
        for clause in ('Only papers with released code',
                       'Must have won a best paper award',
                       'Only from the author Jane Doe',
                       'At least 100 citations',
                       'Provide the list as CSV'):
            with self.subTest(clause=clause):
                self.assertFalse(clause_is_satisfied(PLAN, clause))

    def test_ranking_clauses_need_the_matching_plan(self):
        self.assertTrue(clause_is_satisfied(PLAN, 'Rank the papers from most to least significant.'))
        self.assertFalse(clause_is_satisfied({**PLAN, 'ranking': 'relevance'},
                                             'Rank the papers by citation count'))

    def test_acceptance_counterexamples_stay_unsupported(self):
        for clause in ('Return ranked results as CSV',
                       'Include benchmark labels and scores',
                       'Eligible conferences: SIGGRAPH, CHI',
                       'Only papers from 2025 to 2025',
                       '只收有代码的论文，未知信息用 null',
                       '请输出为表格，并给每篇编号',
                       'Only papers from 2024 to 2026 excluding surveys',
                       'Only 2024-2026 papers that are not benchmarks',
                       'Only papers from 2024 to 2026 excluding the year 2025',
                       'Restrict to 2024-2026 but skip 2025',
                       'Only papers from 2024 to 2026 without survey papers',
                       'Only papers from 2024 to 2026, no surveys',
                       'Only papers from 2024 to 2026, no benchmark papers',
                       'Only 2024-2026 papers, none of them surveys',
                       'Only papers from 2024 to 2026 and no preprints',
                       'Only papers from 2024 to 2026, never position papers',
                       'Only papers from 2024 to 2026, avoid survey articles',
                       'Only papers from 2024 to 2026 outside of 2025',
                       'Only papers from 2024 to 2026 about robotics',
                       '只收 2024 到 2026 年的论文，不要综述'):
            with self.subTest(clause=clause):
                self.assertFalse(clause_is_satisfied(PLAN, clause))
        for clause in ('Only AAAI papers', 'Only 2024-2026 papers'):
            with self.subTest(clause=clause):
                self.assertTrue(clause_is_satisfied(PLAN, clause))
        # A narrower applied window is inside a wider request, never the opposite.
        self.assertTrue(clause_is_satisfied({**PLAN, 'year_from': 2024, 'year_to': 2026},
                                            'Only papers from 2024 to 2026'))
        self.assertFalse(clause_is_satisfied({**PLAN, 'year_from': 2024, 'year_to': 2026},
                                             'Only papers from 2025 to 2025'))

    def test_both_entry_points_share_ranking_calibration(self):
        message = 'Recommend the most influential and research-significant papers.'
        plan = {'ranking': 'relevance'}
        remaining = calibrate_ranking_requirements(
            plan, message,
            ['most influential and research-significant', 'research-significant',
             'Only papers with released code'])
        self.assertEqual(plan['ranking'], 'citations')
        self.assertEqual(remaining, ['Only papers with released code'])
        # A standalone significance clause without the paired wording stays hard.
        plan = {'ranking': 'relevance'}
        self.assertEqual(calibrate_ranking_requirements(plan, 'Find research-significant papers.',
                                                        ['research-significant']),
                         ['research-significant'])
        self.assertEqual(plan['ranking'], 'relevance')

    def test_empty_or_malformed_clauses_are_not_dropped(self):
        self.assertFalse(clause_is_satisfied(PLAN, ''))
        self.assertFalse(clause_is_satisfied(PLAN, None))
        self.assertFalse(clause_is_satisfied(None, 'Each paper should contain rank'))


if __name__ == '__main__':
    unittest.main()
