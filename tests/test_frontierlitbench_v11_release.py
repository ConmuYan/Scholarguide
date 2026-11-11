import json
from collections import Counter
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / 'frontierlitbench'
CORPORA = {
    'A': ('corpus_v1.1/gold_video.jsonl', 163, {'S': 33, 'A': 65, 'B': 65}),
    'B': ('corpus_v1.1/gold_world_models.jsonl', 128, {'S': 26, 'A': 51, 'B': 51}),
    'C': ('corpus_v1.1/gold_agents.jsonl', 134, {'S': 27, 'A': 54, 'B': 53}),
}


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines()
            if line.strip()]


class FrontierLitBenchV11ReleaseTests(unittest.TestCase):
    def test_reviewed_corpora_have_expected_counts_and_valid_labels(self):
        for track, (relative, expected_count, expected_tiers) in CORPORA.items():
            with self.subTest(track=track):
                rows = load_jsonl(ROOT / relative)
                self.assertEqual(len(rows), expected_count)
                self.assertEqual(dict(Counter(
                    row.get('tier') or row['impact']['tier'] for row in rows)),
                    expected_tiers)
                ids = [row.get('paper_id') or row.get('lune_id') for row in rows]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(ids))
                for row in rows:
                    self.assertIn(row['year'], {2024, 2025, 2026})
                    self.assertTrue(row.get('title'))
                    self.assertIn(row.get('tier') or row['impact']['tier'],
                                  {'S', 'A', 'B'})

    def test_shared_adjudications_cover_all_three_tracks_without_duplicate_ids(self):
        rows = json.loads((ROOT / 'review/final_adjudications_v1.1.json').read_text(
            encoding='utf-8-sig'))
        self.assertEqual(len(rows), 76)
        self.assertEqual(Counter(row['track'] for row in rows),
                         {'A': 32, 'B': 4, 'C': 40})
        self.assertEqual(Counter(row['decision'] for row in rows),
                         {'eligible': 75, 'invalid': 1})
        self.assertTrue(all(row.get('identity_verified') is True
                            for row in rows if row['decision'] == 'eligible'))
        self.assertTrue(all(row['tier'] in {'S', 'A', 'B'}
                            for row in rows if row['decision'] == 'eligible'))
        invalid = [row for row in rows if row['decision'] == 'invalid']
        self.assertTrue(all(row.get('tier') is None for row in invalid))
        keys = []
        for row in rows:
            key = (str(row.get('doi') or '').casefold(),
                   str(row.get('arxiv_id') or '').casefold(),
                   str(row.get('url') or '').casefold())
            self.assertTrue(any(key), row['title'])
            keys.append(key)
        self.assertEqual(len(keys), len(set(keys)))

        report = json.loads((ROOT / 'review/final_adjudication_report_v1.1.json').read_text(
            encoding='utf-8-sig'))
        self.assertEqual(report['status'], 'reviewed')
        self.assertEqual(report['new_decisions'], 62)
        self.assertEqual(report['pending_positions'], 112)
        self.assertEqual(report['tiers'], {'S': 23, 'A': 17, 'B': 22})


if __name__ == '__main__':
    unittest.main()
