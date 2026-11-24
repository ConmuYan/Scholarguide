"""A planner may restate scope the request already applies; only narrowing blocks."""
import unittest

from scholarguide.quantities import redundant_scope_requirements

PLAN = {'venue': 'AAAI, CVPR, ICCV, ICLR, ICML, NeurIPS', 'year_from': 2024, 'year_to': 2026}


def keeps(plan, items):
    kept, dropped = redundant_scope_requirements(plan, items)
    return len(kept), dropped


class RedundantScopeTests(unittest.TestCase):
    def test_restating_the_applied_scope_is_redundant(self):
        cases = [
            {'kind': 'venue_subset', 'quote': 'conference whitelist'},
            {'kind': 'venue_subset', 'quote': 'Eligible conferences: AAAI, CVPR, ICCV, ICLR, ICML, NeurIPS'},
            {'kind': 'venue_subset', 'quote': 'given venues'},
            {'kind': 'year_subset', 'quote': '2024, 2025, 2026'},
            {'kind': 'year_subset', 'quote': 'publication years'},
            {'kind': 'year_subset', 'quote': '2024-2026'},
        ]
        for item in cases:
            with self.subTest(item=item):
                kept, dropped = keeps(PLAN, [item])
                self.assertEqual(kept, 0)
                self.assertEqual(dropped, [item['quote']])

    def test_a_narrower_scope_still_blocks(self):
        cases = [
            {'kind': 'venue_subset', 'quote': 'only CVPR and ICCV'},
            {'kind': 'venue_subset', 'quote': 'all papers must be from CVPR'},
            {'kind': 'year_subset', 'quote': '2025 only'},
            {'kind': 'year_subset', 'quote': '2024 or 2026'},
        ]
        for item in cases:
            with self.subTest(item=item):
                kept, dropped = keeps(PLAN, [item])
                self.assertEqual(kept, 1)
                self.assertEqual(dropped, [])

    def test_other_kinds_are_never_dropped(self):
        for kind in ('code_release', 'dataset', 'award', 'topic_filter', 'output_format',
                     'citation_threshold', 'language', 'version_or_status'):
            with self.subTest(kind=kind):
                item = {'kind': kind, 'quote': 'conference whitelist 2024-2026'}
                kept, dropped = keeps(PLAN, [item])
                self.assertEqual((kept, dropped), (1, []))

    def test_empty_plan_absorbs_the_named_scope(self):
        # Without applied values the quote is the only statement of the scope,
        # so it is applied rather than silently dropped or blindly trusted.
        venue_plan = {}
        self.assertEqual(keeps(venue_plan, [{'kind': 'venue_subset', 'quote': 'only CVPR and ICCV'}]),
                         (0, ['only CVPR and ICCV']))
        self.assertEqual(venue_plan['venue'], 'CVPR, ICCV')
        year_plan = {}
        self.assertEqual(keeps(year_plan, [{'kind': 'year_subset', 'quote': '2024, 2025, 2026'}]),
                         (0, ['2024, 2025, 2026']))
        self.assertEqual((year_plan['year_from'], year_plan['year_to']), (2024, 2026))
        # A phrase naming no venue/year cannot be applied and still blocks.
        self.assertEqual(keeps({}, [{'kind': 'venue_subset', 'quote': 'conference whitelist'}]), (1, []))

    def test_mixed_entries_keep_only_the_real_one(self):
        items = [{'kind': 'venue_subset', 'quote': 'conference whitelist'},
                 {'kind': 'code_release', 'quote': 'released code'}]
        kept, dropped = redundant_scope_requirements(PLAN, items)
        self.assertEqual([row['kind'] for row in kept], ['code_release'])
        self.assertEqual(dropped, ['conference whitelist'])


if __name__ == '__main__':
    unittest.main()
