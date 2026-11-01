"""Deterministic intent quota policy; the LLM never chooses these numbers."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intent import allocate, resolve_goal, mix_text


class IntentPolicyTests(unittest.TestCase):
    def test_k5_default_mixes(self):
        self.assertEqual(dict(allocate(5, 'orientation')), {'survey': 2, 'research': 3})
        self.assertEqual(dict(allocate(5, 'comparison')), {'benchmark': 3, 'research': 2})
        self.assertEqual(dict(allocate(5, 'frontier')), {'survey': 1, 'research': 4})
        self.assertEqual(dict(allocate(5, 'general')), {'survey': 1, 'benchmark': 1, 'research': 3})

    def test_every_k_is_valid(self):
        for goal in ('orientation', 'comparison', 'frontier', 'general'):
            for target in (3, 4, 5):
                mix = allocate(target, goal)
                self.assertEqual(sum(mix.values()), target)
                self.assertTrue(all(count >= 0 for count in mix.values()))

    def test_auto_resolves_to_general_and_unknown_is_general(self):
        self.assertEqual(resolve_goal('auto'), 'general')
        self.assertEqual(resolve_goal(''), 'general')
        self.assertEqual(resolve_goal('made_up'), 'general')
        self.assertEqual(resolve_goal('frontier'), 'frontier')

    def test_mix_text_is_explicit(self):
        text = mix_text(allocate(5, 'orientation'))
        self.assertIn('综述 2', text)
        self.assertIn('研究论文 3', text)


if __name__ == '__main__':
    unittest.main()
