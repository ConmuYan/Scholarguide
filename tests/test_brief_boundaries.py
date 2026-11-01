"""Adversarial abstract-boundary regressions; offline inputs only."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from research_brief import synthesize
from report import markdown
from test_research_runtime import TASK, OfflineLLM, brief_output, paper
from tests.brief_fixture import packet_papers, wire_output


class BriefBoundaryTests(unittest.TestCase):
    def brief(self, transform, abstract=None):
        items = [paper(f'P{i}').to_dict() for i in range(1, 4)]
        if abstract:
            items[0]['abstract'] = abstract

        class MutatedLLM(OfflineLLM):
            def generate(self, stage, prompt, data, schema, validate):
                raw = brief_output(packet_papers(data))
                transform(raw)
                raw = wire_output(raw, data)
                validate(raw)
                return raw

        return synthesize(MutatedLLM(), TASK, items)

    def test_background_limitation_and_author_solution_are_not_author_limitation(self):
        def mutation(raw):
            raw['evidence'][0]['quote'] = 'Existing methods are limited to static scenes; we introduce a general dynamic model.'
            raw['paper_notes'][0]['author_limitation'] = {
                'text': 'Our method is limited to static scenes.', 'evidence_ids': ['E0']}
        result = self.brief(mutation, 'We introduce visual retrieval. Existing methods are limited to static scenes; we introduce a general dynamic model.')
        self.assertIsNone(result['paper_notes'][0]['author_limitation'])

    def test_number_token_cannot_be_borrowed_from_larger_number(self):
        def mutation(raw):
            raw['paper_notes'][0]['contribution']['text'] = 'Trains using 10 labels.'
        result = self.brief(mutation, 'We train visual retrieval using 100 labels. Our approach is limited to static scenes.')
        self.assertEqual(result['status'], 'failed')

    def test_every_proposal_field_rejects_claims_of_observed_results(self):
        for field in ('hypothesis', 'rationale', 'experiment', 'baseline', 'measure', 'falsifier'):
            with self.subTest(field=field):
                def mutation(raw):
                    raw['research_hypotheses'][0][field] = 'Experiments demonstrate Alpha outperforms Beta with 99.9% accuracy.'
                self.assertEqual(self.brief(mutation)['status'], 'failed')

    def test_markdown_treats_model_markup_as_plain_text(self):
        result = self.brief(lambda raw: None)
        result['paper_notes'][0]['contribution']['text'] = '[Fake official source](javascript:alert%281%29)\n# Forged section ![tracker](https://example.test/track.png)'
        payload = {'mode': 'live', 'method': 'S', 'status': 'complete', 'stop_reason': 'requirements_met',
                   'request': TASK, 'papers': [], 'research_brief': result}
        text = markdown(payload)
        self.assertNotIn('[Fake official source](', text)
        self.assertNotIn('\n# Forged section', text)
        self.assertNotIn('![tracker](', text)


if __name__ == '__main__':
    unittest.main()
