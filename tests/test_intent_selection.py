"""Mixed-type selection driven by deterministic intent policy."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Paper, Request
from workflow import coverage, select_papers

QUOTE = 'This paper introduces a benchmark for retrieval augmented generation evaluation.'


def make(idx, ptype, year=2024, authority=0.8):
    return Paper(f'p{idx}', f'{ptype} {idx}', QUOTE, f'https://example.org/{idx}',
                 year=year, venue='ACL', authority=authority)


class MixedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.papers = ([make(i, 'survey') for i in range(1, 4)]
                       + [make(i, 'benchmark') for i in range(4, 7)]
                       + [make(i, 'research') for i in range(7, 12)])
        self.assessments = {p.id: {'relevance': 2, 'category_status': 'no',
                                   'paper_type': p.title.split()[0],
                                   'evidence_verified': True, 'evidence_quote': QUOTE,
                                   'reason': 'fixture'} for p in self.papers}

    def selected(self, goal, **kw):
        request = Request.from_dict({'topic': 'RAG', 'target_count': 5,
                                     'reading_goal': goal, **kw})
        return select_papers(self.papers, self.assessments, request), request

    def counts(self, selected):
        out = {'survey': 0, 'benchmark': 0, 'research': 0}
        for paper in selected:
            out[paper['paper_type']] = out.get(paper['paper_type'], 0) + 1
        return out

    def test_profiles_produce_expected_mix(self):
        expected = {
            'orientation': {'survey': 2, 'benchmark': 0, 'research': 3},
            'comparison': {'survey': 0, 'benchmark': 3, 'research': 2},
            'frontier': {'survey': 1, 'benchmark': 0, 'research': 4},
            'general': {'survey': 1, 'benchmark': 1, 'research': 3},
        }
        for goal, mix in expected.items():
            selected, _ = self.selected(goal)
            self.assertEqual(self.counts(selected), mix, goal)

    def test_explicit_category_quota_wins(self):
        assessed = {pid: dict(row) for pid, row in self.assessments.items()}
        for paper in self.papers:
            if paper.title.startswith('benchmark'):
                assessed[paper.id]['category_status'] = 'yes'
        request = Request.from_dict({'topic': 'RAG', 'target_count': 5,
                                     'reading_goal': 'orientation',
                                     'category': 'evaluation', 'minimum_category_count': 2})
        selected = select_papers(self.papers, assessed, request)
        benchmarks = [p for p in selected if p['paper_type'] == 'benchmark']
        self.assertEqual(len(benchmarks), 2)

    def test_explicit_quota_counts_toward_profile_not_extra(self):
        assessed = {pid: dict(row) for pid, row in self.assessments.items()}
        for paper in self.papers:
            if paper.title.startswith('benchmark'):
                assessed[paper.id]['category_status'] = 'yes'
        request = Request.from_dict({'topic': 'RAG', 'target_count': 5,
                                     'reading_goal': 'comparison',
                                     'category': 'evaluation', 'minimum_category_count': 2})
        selected = select_papers(self.papers, assessed, request)
        self.assertEqual(self.counts(selected), {'survey': 0, 'benchmark': 3, 'research': 2})

    def test_fallback_fills_and_reports_mix_gap(self):
        papers = [p for p in self.papers if p.title.startswith('research')][:3]
        assessments = {p.id: self.assessments[p.id] for p in papers}
        request = Request.from_dict({'topic': 'RAG', 'target_count': 5,
                                     'reading_goal': 'orientation'})
        selected = select_papers(papers, assessments, request)
        self.assertEqual(len(selected), 3)
        gaps = coverage(selected, request)
        self.assertTrue(any(g['requirement'] == 'mix' for g in gaps))

    def test_research_representative_order_uses_authority_before_relevance(self):
        high = make(20, 'research', authority=0.95)
        low = make(21, 'research', authority=0.20)
        assessed = {p.id: {'relevance': 2, 'category_status': 'unknown',
                           'paper_type': 'research', 'evidence_verified': True,
                           'evidence_quote': QUOTE, 'reason': 'fixture'} for p in (high, low)}
        assessed[high.id]['relevance'] = 1  # authority must beat lower relevance
        assessed[low.id]['relevance'] = 2
        request = Request.from_dict({'topic': 'RAG', 'target_count': 3,
                                     'reading_goal': 'frontier'})
        selected = select_papers([low, high], assessed, request)
        self.assertEqual(selected[0]['id'], high.id)

    def test_empty_goal_keeps_legacy_selection(self):
        request = Request.from_dict({'topic': 'RAG', 'target_count': 5})
        selected = select_papers(self.papers, self.assessments, request)
        self.assertEqual(len(selected), 5)
        self.assertFalse(any(g['requirement'] == 'mix' for g in coverage(selected, request)))

    def test_legacy_order_does_not_use_authority_tiebreak(self):
        low = make(40, 'research', year=2024, authority=0.1)
        high = make(41, 'research', year=2024, authority=0.99)
        assessed = {p.id: {'relevance': 2, 'category_status': 'unknown',
                           'paper_type': 'research', 'evidence_verified': True,
                           'evidence_quote': QUOTE, 'reason': 'fixture'} for p in (low, high)}
        request = Request.from_dict({'topic': 'RAG', 'target_count': 3})
        selected = select_papers([high, low], assessed, request)
        self.assertEqual([p['id'] for p in selected], ['p40', 'p41'])


if __name__ == '__main__':
    unittest.main()
