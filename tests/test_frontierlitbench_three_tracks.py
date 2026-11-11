import json
from pathlib import Path
import unittest

from evaluation.frontierlitbench_score import score

ROOT = Path(__file__).resolve().parents[1] / 'frontierlitbench' / 'corpus'


def load(name):
    return [json.loads(line) for line in (ROOT / name).read_text(encoding='utf-8').splitlines() if line.strip()]


class ThreeTrackTests(unittest.TestCase):
    def test_video_scope_uses_iccv_and_excludes_unscored_records(self):
        gold = load('gold_video.jsonl')
        iccv = next(p for p in gold if p.get('venue') == 'ICCV' and p.get('tier') in ('S', 'A', 'B'))
        result = score({'papers': [{'title': iccv['title'], 'year': iccv['year']}]}, gold, 'FB-13')
        self.assertEqual(result['eligible_gold_count'], 57)
        self.assertEqual(result['positions'][0]['status'], 'matched')

    def test_world_model_nested_records_and_venue_subset(self):
        gold = load('gold_world_models.jsonl')
        p = next(p for p in gold if p['venue'] == 'ICLR')
        result = score({'papers': [{'title': p['title'], 'year': p['year']}]}, gold, 'FB-14')
        self.assertEqual(result['eligible_gold_count'], 65)
        self.assertEqual(result['positions'][0]['tier'], p['impact']['tier'])
        self.assertEqual(result['positions'][0]['status'], 'matched')

    def test_failed_run_is_not_reported_as_scored(self):
        result = score({'status': 'failed', 'papers': []}, load('gold_world_models.jsonl'), 'FB-02')
        self.assertEqual(result['status'], 'run_incomplete')
        self.assertIsNone(result['metrics'])

    def test_exhausted_search_without_results_is_incomplete(self):
        result = score({'status': 'no_results', 'stop_reason': 'search_budget_exhausted',
                        'papers': []}, load('gold_world_models.jsonl'), 'FB-02')
        self.assertEqual(result['status'], 'run_incomplete')
        self.assertIsNone(result['metrics'])

    def test_completed_bounded_search_with_fewer_papers_is_scored(self):
        gold = load('gold_world_models.jsonl')
        p = gold[0]
        result = score({'status': 'partial', 'stop_reason': 'search_budget_exhausted',
                        'papers': [{'title': p['title'], 'year': p['year']}]}, gold, 'FB-02')
        self.assertEqual(result['status'], 'scored')
        self.assertEqual([x['status'] for x in result['positions'][1:]], ['missing'] * 4)


if __name__ == '__main__':
    unittest.main()
