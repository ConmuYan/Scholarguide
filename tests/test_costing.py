"""Cost estimates are explicit, reconciled, and never silently zero."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import Config
from costing import cost_breakdown


class CostTests(unittest.TestCase):
    def test_tokens_prices_and_tool_costs(self):
        config = Config(input_price=2.0, output_price=8.0, currency='CNY',
                        profile_id='test', protocol='responses')
        metrics = {'input_tokens': 1_000_000, 'output_tokens': 500_000,
                   'usage_observed': True}
        trace = [{'router': {'batches': [{'runs': [
            {'name': 'deepseek', 'cost_cny': 0.005},
            {'name': 'arxiv', 'cost_cny': 0},
        ]}]}}]
        cost = cost_breakdown(config, metrics, trace)
        self.assertEqual(cost['model'], 6.0)
        self.assertEqual(cost['tools'], 0.005)
        self.assertEqual(cost['total'], 6.005)
        self.assertTrue(cost['estimate'])
        self.assertEqual(cost['currency'], 'CNY')

    def test_missing_usage_or_prices_is_unknown_not_zero(self):
        config = Config(input_price=2.0, output_price=8.0)
        missing = cost_breakdown(config, {'input_tokens': None, 'output_tokens': None})
        self.assertIsNone(missing['model'])
        self.assertIsNone(missing['total'])
        no_price = cost_breakdown(Config(), {'input_tokens': 10, 'output_tokens': 10})
        self.assertIsNone(no_price['model'])
        self.assertIsNone(no_price['total'])

    def test_public_config_never_contains_secrets(self):
        config = Config(api_key='sk-never-return', input_price=1, output_price=2)
        public = config.public()
        self.assertNotIn('sk-never-return', str(public))
        self.assertEqual(public['input_price'], 1)
        self.assertEqual(public['currency'], 'CNY')

    def test_chat_and_search_results_carry_estimated_cost(self):
        import tempfile
        from pathlib import Path
        from demo import ChatDemoLLM, DemoLLM, DemoSource
        from session import ChatSession
        from state import StateStore
        from workflow import run

        class TokenChatLLM(ChatDemoLLM):
            def __init__(self):
                super().__init__()
                self.budget = None
            def generate(self, stage, *args):
                if self.budget is not None:
                    self.budget.llm_calls += 1
                    if self.emit if hasattr(self, 'emit') else True:
                        self.budget.usage_observed = True
                        self.budget.usage_calls += 1
                        self.budget.input_tokens += 100
                        self.budget.output_tokens += 20
                return super().generate(stage, *args)

        class TokenSearchLLM(DemoLLM):
            def __init__(self):
                self.budget = None
            def generate(self, stage, *args):
                if self.budget is not None:
                    self.budget.llm_calls += 1
                    if self.emit if hasattr(self, 'emit') else True:
                        self.budget.usage_observed = True
                        self.budget.usage_calls += 1
                        self.budget.input_tokens += 100
                        self.budget.output_tokens += 20
                return super().generate(stage, *args)

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                llm = TokenChatLLM()
                chat = ChatSession(Config(input_price=2, output_price=8), store,
                                   source=DemoSource(), llm=llm)
                llm.budget = chat.budget
                result = chat.start(message='Recommend five RAG papers including one evaluation paper.')
                self.assertEqual(result['cost']['input_tokens'], result['metrics']['input_tokens'])
                self.assertIsNotNone(result['cost']['total'])
                self.assertEqual(result['cost']['model'], round(result['cost']['total'], 6))
                before_tokens = chat.budget.input_tokens
                second = chat.send('Add one survey paper.')
                self.assertEqual(second['metrics']['input_tokens'],
                                 chat.budget.input_tokens - before_tokens)
                self.assertEqual(second['cost']['input_tokens'], second['metrics']['input_tokens'])
                self.assertNotEqual(second['cost']['total'], result['cost']['total'] + result['cost']['total'])
            finally:
                store.close()

        from common import Budget
        budget = Budget(30)
        llm = TokenSearchLLM()
        llm.budget = budget
        result = run('Recommend five RAG papers including one evaluation paper.',
                     Config(input_price=2, output_price=8), 'S',
                     source=DemoSource(), llm=llm, budget=budget)
        self.assertIsNotNone(result['cost']['total'])
        self.assertEqual(result['cost']['total'], result['cost']['model'])
        self.assertTrue(result['cost']['known'])

    def test_single_source_paid_search_cost_is_not_dropped(self):
        from source import SearchBatch, SourceRun
        from tools import search
        class Paid:
            def __init__(self):
                self.last_batch = None
            def search(self, query, limit):
                self.last_batch = SearchBatch(query, ['deepseek'], [],
                                              [SourceRun('deepseek', 'ok', 0, cost_cny=.005)])
                return []
        source = Paid()
        outcome = search(source, 'RAG', 10)
        cost = cost_breakdown(Config(input_price=1, output_price=1), {},
                              [{'action': 'search_result', 'cost_cny': outcome.cost_cny}])
        self.assertEqual(cost['tools'], .005)
        self.assertIsNone(cost['total'])  # model usage is absent, so total remains unknown

    def test_later_turn_without_usage_is_unknown_not_zero(self):
        import tempfile
        from pathlib import Path
        from demo import ChatDemoLLM, DemoSource
        from session import ChatSession
        from state import StateStore

        class ToggleLLM(ChatDemoLLM):
            def __init__(self):
                super().__init__()
                self.budget = None
                self.emit = True
            def generate(self, stage, *args):
                if self.budget is not None:
                    self.budget.llm_calls += 1
                    if self.emit:
                        self.budget.usage_observed = True
                        self.budget.usage_calls += 1
                        self.budget.input_tokens += 100
                        self.budget.output_tokens += 20
                return super().generate(stage, *args)

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                llm = ToggleLLM()
                chat = ChatSession(Config(input_price=2, output_price=8), store,
                                   source=DemoSource(), llm=llm)
                llm.budget = chat.budget
                first = chat.start(message='Recommend five RAG papers including one evaluation paper.')
                self.assertGreater(first['metrics']['input_tokens'], 0)
                llm.emit = False
                second = chat.send('Add one survey paper.')
                self.assertIsNone(second['metrics']['input_tokens'])
                self.assertIsNone(second['metrics']['output_tokens'])
                self.assertIsNone(second['cost']['model'])
                self.assertIsNone(second['cost']['total'])
            finally:
                store.close()

    def test_partial_usage_is_unknown_not_a_partial_cost(self):
        from common import Budget
        budget = Budget(10)
        budget.llm_calls = 5
        budget.usage_calls = 1
        budget.usage_observed = True
        budget.input_tokens = 100
        budget.output_tokens = 20
        metrics = budget.metrics()
        self.assertFalse(metrics['usage_complete'])
        self.assertIsNone(metrics['input_tokens'])
        cost = cost_breakdown(Config(input_price=2, output_price=8), metrics)
        self.assertIsNone(cost['model'])
        self.assertIsNone(cost['total'])

    def test_workflow_multi_source_keeps_paid_run_cost(self):
        from demo import DemoLLM, DemoSource
        from source import SearchBatch, SourceRun
        from workflow import run

        class PaidMultiSource:
            providers = ['arxiv', 'deepseek']
            def __init__(self):
                self.papers = DemoSource().papers
            def search_batch(self, query, limit=10, providers=None, filters=None):
                return SearchBatch(query, list(providers or self.providers), self.papers[:5],
                                   [SourceRun('arxiv', 'ok', 5),
                                    SourceRun('deepseek', 'ok', 2, cost_cny=.005)])

        result = run('Recommend five RAG papers.', Config(source='multi'), 'S',
                     source=PaidMultiSource(), llm=DemoLLM())
        self.assertEqual(result['cost']['tools'], .005)
        search_result = next(event for event in result['trace'] if event['action'] == 'search_result')
        self.assertEqual(search_result['cost_cny'], .005)
        self.assertTrue(search_result['router']['batches'])

    def test_workflow_single_source_paid_search_cost_is_in_result(self):
        from demo import DemoLLM, DemoSource
        from source import SearchBatch, SourceRun
        from workflow import run

        class PaidSource(DemoSource):
            def search(self, query, limit=10):
                found = super().search(query, limit)
                self.last_batch = SearchBatch(query, ['deepseek'], found,
                                              [SourceRun('deepseek', 'ok', len(found),
                                                         cost_cny=.005)])
                return found

        result = run('Recommend five RAG papers.', Config(input_price=1, output_price=1),
                     'S', source=PaidSource(), llm=DemoLLM())
        self.assertEqual(result['cost']['tools'], .005)
        self.assertIsNone(result['cost']['total'])  # no usage in this fake model

    def test_partial_turn_then_full_turn_uses_only_second_turn_delta(self):
        import tempfile
        from pathlib import Path
        from demo import ChatDemoLLM, DemoSource
        from session import ChatSession
        from state import StateStore

        class PartialThenFull(ChatDemoLLM):
            def __init__(self):
                super().__init__()
                self.budget = None
                self.phase = 1
                self.used = 0
            def generate(self, stage, *args):
                if self.budget is not None:
                    self.budget.llm_calls += 1
                    if self.phase == 2 or self.used == 0:
                        self.budget.usage_calls += 1
                        self.budget.usage_observed = True
                        self.budget.input_tokens += 100
                        self.budget.output_tokens += 20
                        self.used += 1
                return super().generate(stage, *args)

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite3')
            try:
                llm = PartialThenFull()
                chat = ChatSession(Config(input_price=2, output_price=8), store,
                                   source=DemoSource(), llm=llm)
                llm.budget = chat.budget
                first = chat.start(message='Recommend five RAG papers including one evaluation paper.')
                self.assertIsNone(first['metrics']['input_tokens'])
                self.assertIsNone(first['cost']['total'])
                before = chat.budget.input_tokens
                llm.phase = 2
                second = chat.send('Add one survey paper.')
                self.assertTrue(second['metrics']['usage_complete'])
                self.assertEqual(second['metrics']['input_tokens'], chat.budget.input_tokens - before)
                self.assertIsNotNone(second['cost']['total'])
            finally:
                store.close()

if __name__ == '__main__':
    unittest.main()
