"""Tool-layer tests: outcomes, schema pinning and cost conservation.

No network. Fakes implement only the attributes the tools actually use.
"""
from dataclasses import dataclass
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import GuideError, Paper
from tools import CostError, CostMeter, assess, assess_batch, decision, search


@dataclass
class FakePaper:
    id: str
    title: str = 'Title'
    abstract: str = 'An abstract that is long enough to quote from safely.'


class FakeSource:
    def __init__(self, papers=None, error=None):
        self.papers = papers or []
        self.error = error
        self.calls = 0

    def search(self, query, limit):
        self.calls += 1
        if self.error:
            raise self.error
        return self.papers


class FakeLLM:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def generate(self, stage, prompt, data, schema, validate):
        self.calls.append({'stage': stage, 'data': data, 'schema': schema})
        if self.error:
            raise self.error
        return self.response


class CostMeterTests(unittest.TestCase):
    def test_summary_conserves_units(self):
        meter = CostMeter()
        a = meter.reserve('search', 1)
        b = meter.reserve('assess', 3)
        meter.settle(a, 1)
        meter.settle(b, 2)
        summary = meter.summary()
        self.assertEqual(summary['reserved'], 4.0)
        self.assertEqual(summary['settled'], 3.0)
        self.assertEqual(summary['refunded'], 1.0)
        self.assertEqual(summary['by_action']['assess']['refunded'], 1.0)

    def test_settle_cannot_exceed_reserved(self):
        meter = CostMeter()
        handle = meter.reserve('search', 1)
        with self.assertRaises(CostError):
            meter.settle(handle, 2)
        with self.assertRaises(CostError):
            meter.reserve('search', -1)


class SearchTests(unittest.TestCase):
    def test_success_charges_one_unit(self):
        meter = CostMeter()
        outcome = search(FakeSource([FakePaper('p1')]), 'rag', 10, meter)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.cost, 1.0)
        self.assertEqual(outcome.observation['status'], 'success')
        self.assertEqual(meter.summary()['settled'], 1.0)

    def test_empty_is_success_but_flagged(self):
        outcome = search(FakeSource([]), 'no such topic', 10, CostMeter())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.observation['status'], 'empty')

    def test_error_is_refunded_and_not_raised(self):
        meter = CostMeter()
        outcome = search(FakeSource(error=GuideError('http 503')), 'rag', 10, meter)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.cost, 0.0)
        self.assertEqual(outcome.refunded, 1.0)
        self.assertEqual(outcome.observation['status'], 'error')
        self.assertEqual(meter.summary()['settled'], 0.0)
        self.assertEqual(meter.summary()['refunded'], 1.0)


class AssessTests(unittest.TestCase):
    def _schema(self):
        return {
            'type': 'object',
            'properties': {'assessments': {
                'type': 'array',
                'items': {'type': 'object', 'properties': {
                    'paper_id': {'type': 'string'}, 'relevance': {'type': 'integer'},
                }},
            }},
        }

    def test_batch_pins_schema_to_the_supplied_papers(self):
        papers = [FakePaper('p1'), FakePaper('p2')]
        llm = FakeLLM({'assessments': [{'paper_id': 'p1'}, {'paper_id': 'p2'}]})
        meter = CostMeter()
        outcome = assess_batch(llm, papers, topic='rag', category='none',
                               category_definition='none', prompt='P', schema=self._schema(),
                               validate=lambda value, batch: None, meter=meter)
        schema = llm.calls[0]['schema']['properties']['assessments']
        self.assertEqual(schema['minItems'], 2)
        self.assertEqual(schema['maxItems'], 2)
        self.assertEqual(schema['items']['properties']['paper_id']['enum'], ['p1', 'p2'])
        self.assertEqual(outcome.cost, 2.0)
        self.assertEqual(len(outcome.items), 2)

    def test_failed_batch_is_refunded_then_reraised(self):
        llm = FakeLLM(error=GuideError('bad json'))
        meter = CostMeter()
        with self.assertRaises(GuideError):
            assess_batch(llm, [FakePaper('p1')], topic='rag', category='none',
                         category_definition='none', prompt='P', schema=self._schema(),
                         validate=lambda value, batch: None, meter=meter)
        self.assertEqual(meter.summary()['settled'], 0.0)
        self.assertEqual(meter.summary()['refunded'], 1.0)

    def test_assess_splits_batches_and_charges_per_paper(self):
        papers = [FakePaper(f'p{i}') for i in range(7)]
        llm = FakeLLM({'assessments': []})
        # Return exactly the requested ids each call.
        def generate(stage, prompt, data, schema, validate):
            ids = schema['properties']['assessments']['items']['properties']['paper_id']['enum']
            llm.calls.append({'ids': list(ids)})
            return {'assessments': [{'paper_id': pid} for pid in ids]}
        llm.generate = generate
        meter = CostMeter()
        outcome = assess(llm, papers, topic='rag', category='none', category_definition='none',
                         prompt='P', schema=self._schema(), validate=lambda value, batch: None,
                         batch_size=5, meter=meter)
        self.assertEqual([c['ids'] for c in llm.calls],
                         [['p0', 'p1', 'p2', 'p3', 'p4'], ['p5', 'p6']])
        self.assertEqual(outcome.cost, 7.0)
        self.assertEqual(meter.summary()['settled'], 7.0)


class DecisionTests(unittest.TestCase):
    def test_plan_and_refine_each_cost_one(self):
        meter = CostMeter()
        llm = FakeLLM({'query': 'rag'})
        decision('plan', llm, {'user_input': 'x'}, {}, lambda value: value, 'P', meter)
        decision('refine', llm, {'topic': 'rag'}, {}, lambda value: value, 'R', meter)
        summary = meter.summary()
        self.assertEqual(summary['settled'], 2.0)
        self.assertEqual(set(summary['by_action']), {'plan', 'refine'})

    def test_failed_decision_is_refunded(self):
        meter = CostMeter()
        llm = FakeLLM(error=GuideError('timeout'))
        with self.assertRaises(GuideError):
            decision('plan', llm, {}, {}, lambda value: value, 'P', meter)
        self.assertEqual(meter.summary()['settled'], 0.0)

    def test_unknown_stage_rejected(self):
        with self.assertRaises(CostError):
            decision('search', FakeLLM({}), {}, {}, lambda value: value, 'P', CostMeter())


if __name__ == '__main__':
    unittest.main()
