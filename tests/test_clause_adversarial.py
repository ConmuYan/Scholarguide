"""Adversarial matrix: no phrasing may turn a real constraint into "satisfied".

The allow-list decision has one path, so a filter, an exclusion, a negation, a
threshold or a different venue always leaves the clause unsupported. Legitimate
scope and presentation clauses stay recognized.
"""
import unittest

from quantities import clause_is_satisfied

PLAN = {'venue': 'AAAI, CVPR, ICCV, ICLR, ICML, NeurIPS', 'year_from': 2024,
        'year_to': 2026, 'ranking': 'citations'}

REAL_CONSTRAINTS = (
    'Only papers from 2024 to 2026 excluding surveys',
    'Only 2024-2026 papers that are not benchmarks',
    'Restrict to 2024-2026 but skip 2025',
    'Only papers from 2024 to 2026 without survey papers',
    'Only papers from 2024 to 2026, no surveys',
    'Only papers from 2024 to 2026 and no preprints',
    'Only papers from 2024 to 2026, never position papers',
    'Only papers from 2024 to 2026 outside of 2025',
    '只收 2024 到 2026 年的论文，不要综述',
    '排除 2025 年，只收 2024 到 2026 的会议论文',
    'Only AAAI and CVPR papers, not ICLR',
    'Eligible venues are the given ones, except CHI',
    'Only peer-reviewed journal articles, not conference papers',
    'Only oral presentations from 2024 to 2026',
    'Only papers that are open access',
    '只要正式发表的期刊长文，不收 workshop 短文',
    'Only from the author Jane Doe',
    'Only papers from the Stanford NLP group',
    '作者必须是高校，不要企业研究院',
    'Only papers with released code',
    '只收有开源代码的论文',
    'Only papers with a public dataset',
    'At least 100 citations',
    'Rank the papers with at least one hundred citations each',
    'Rank papers with a citation count above 1,000',
    '引用数超过 100 的论文优先',
    'Rank the papers, only those with at least 8 pages of main text',
    'Rank the papers from 2024 to 2026, excluding survey papers',
    'Rank the papers and use null for unknown fields; no survey papers',
    'Return a ranked JSON list, but skip position papers',
    'Number each paper; do not include work from industry labs',
    'Rank the papers; only papers with an official oral status',
    'Rank the papers by citations, but exclude benchmarks',
    'Gap 分析要写清楚，用 null 表示未知；但不要综述类论文',
    'Each paper should contain rank; only journal versions are eligible',
    'Rank, no surveys',
    'Ranked, no surveys',
    'null when unknown; no surveys',
    'Return ranked results as CSV',
    'Include benchmark labels and scores',
    'Eligible conferences: SIGGRAPH, CHI',
    '只收有代码的论文，未知信息用 null',
)

SUPPORTED = (
    'Only use the provided publication years and conference whitelist.',
    'Only papers from 2024 to 2026',
    'Restrict to 2024-2026',
    'Only AAAI papers',
    'Use null for unknown details.',
    'Each paper should contain rank',
    'Rank the papers from most to least significant.',
    'Rank the papers by citation count',
    'Do not include any benchmark labels or scores',
    'explicitly report the gap',
    'Return a ranked JSON object with a papers array',
    'Use only the listed venues and years',
    'Select papers between 2024 and 2026 from the given conferences',
    '用 null 表示未知信息',
    '给每篇编号',
    '按重要度排序',
    'Rank the papers',
    'Use the given conference list and publication years only',
    'Include the gap for each paper',
    'Ranked output with null for unknown values',
    'Each paper should contain an explicit rank',
    '基于给定会议和年份排序',
)


class AdversarialClauseTests(unittest.TestCase):
    def test_real_constraints_never_look_satisfied(self):
        for clause in REAL_CONSTRAINTS:
            with self.subTest(clause=clause):
                self.assertIs(clause_is_satisfied(PLAN, clause), False)
                # Without an applied plan nothing may look satisfied either.
                self.assertIs(clause_is_satisfied({}, clause), False)

    def test_supported_clauses_stay_recognized(self):
        for clause in SUPPORTED:
            with self.subTest(clause=clause):
                self.assertIs(clause_is_satisfied(PLAN, clause), True)

    def test_publication_type_is_only_satisfied_when_the_plan_carries_it(self):
        clause = 'These conference papers are the only eligible ones'
        self.assertIs(clause_is_satisfied(PLAN, clause), False)
        self.assertIs(clause_is_satisfied({**PLAN, 'publication_type': 'conference'}, clause), True)

    def test_display_words_do_not_bypass_a_filter(self):
        # The regression that motivated the single-path allow-list: a display
        # word plus an exclusion used to short-circuit to "satisfied".
        for clause in ('Rank, no surveys', 'Ranked, no surveys',
                       'null when unknown; no surveys', 'Rank the papers, no preprints'):
            with self.subTest(clause=clause):
                self.assertIs(clause_is_satisfied(PLAN, clause), False)


if __name__ == '__main__':
    unittest.main()
