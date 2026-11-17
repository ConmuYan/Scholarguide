"""The stop decision is structural: a closed kind plus a verbatim quote.

Nothing here asserts that a sentence "looks satisfied". The planner either names
a kind the product cannot retrieve or verify - and the search stops before any
source call - or the array is empty and the search runs. Display guarantees are
not exempted by text matching: a model that puts "number each paper" into
blocking_requirements still stops, because the program no longer reads the
wording at all.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from common import Config, GuideError, Paper
from quantities import BLOCKING_KINDS, BLOCKING_QUOTE_LIMIT, blocking_quotes
import prompts
from session import ChatSession
from state import StateStore
from workflow import run

QUOTE = 'This paper introduces a reproducible benchmark for visual language adaptation.'
PAPERS = [
    Paper('p1', 'First visual benchmark', QUOTE, 'https://example.test/p1', year=2024, venue='CVPR'),
    Paper('p2', 'Second visual benchmark', QUOTE, 'https://example.test/p2', year=2025, venue='CVPR'),
    Paper('p3', 'A new visual method', QUOTE, 'https://example.test/p3', year=2025, venue='CVPR'),
]
PLAN = {'topic': 'visual language adaptation', 'query': 'visual adaptation benchmark',
        'target_count': 2, 'category': 'none', 'minimum_category_count': 0,
        'reading_goal': 'general', 'blocking_requirements': []}


class Source:
    mode = 'demo'

    def __init__(self):
        self.queries = []
        self.calls = self.queries  # the session records queries here too

    def search(self, query, limit=10):
        self.queries.append(query)
        return list(PAPERS)


class Planner:
    """Offline planner that honours the schema by calling validate like the LLM."""
    mode = 'demo'

    def __init__(self, blocking=None):
        self.raw = {**deepcopy(PLAN),
                    'blocking_requirements': deepcopy(blocking or [])}

    def generate(self, stage, prompt, data, schema, validate):
        if stage == 'plan':
            value = deepcopy(self.raw)
        elif stage == 'assess':
            value = {'assessments': [
                {'paper_id': paper['paper_id'], 'relevance': 2, 'category_status': 'no',
                 'paper_type': 'benchmark', 'evidence_quote': QUOTE,
                 'reason': 'The abstract describes a visual benchmark.'}
                for paper in data['papers']]}
        else:
            raise AssertionError(f'unexpected stage {stage}')
        validate(value)
        return value


class JsonObjectPlanner(Planner):
    """The OAuth json_object path: no provider-side schema, local validate only.

    ``strict_schema_compatible`` is False for PLAN_SCHEMA, so a provider that
    cannot enforce the schema sends a plain JSON object. The contract must still
    be enforced locally by the same validate callback.
    """


def message_for(quote):
    return f'Find visual adaptation papers; {quote}.'


class StructuralStopTests(unittest.TestCase):
    def test_every_kind_stops_before_search_and_keeps_the_quote(self):
        for kind in BLOCKING_KINDS:
            with self.subTest(kind=kind):
                source = Source()
                result = run(message_for('released code'), Config(), 'S', source=source,
                             llm=Planner([{'kind': kind, 'quote': 'released code'}]))
                self.assertEqual(result['status'], 'unsupported')
                self.assertEqual(result['stop_reason'], 'unsupported_requirements')
                self.assertEqual(result['request']['unsupported'], ['released code'])
                self.assertEqual(source.queries, [])
                self.assertTrue(any('released code' in warning for warning in result['warnings']))

    def test_empty_array_reaches_search(self):
        source = Source()
        result = run('Find visual adaptation papers.', Config(), 'S', source=source,
                     llm=Planner([]))
        self.assertNotEqual(result['status'], 'unsupported')
        self.assertEqual(result['request']['unsupported'], [])
        self.assertTrue(source.queries)

    def test_display_requirements_are_not_exempted_by_text(self):
        # These sentences are guaranteed by the delivered result, so the prompt
        # forbids listing them. If the model lists them anyway, the program must
        # still stop: there is no "this phrase is already satisfied" branch.
        display = ('number each paper', 'use null for unknown values',
                   'report the gap', 'do not include labels or scores')
        text = 'Recommend visual papers: ' + ', '.join(display) + '.'
        for phrase in display:
            with self.subTest(phrase=phrase):
                source = Source()
                result = run(text, Config(), 'S', source=source,
                             llm=Planner([{'kind': 'other', 'quote': phrase}]))
                self.assertEqual(result['status'], 'unsupported')
                self.assertEqual(result['stop_reason'], 'unsupported_requirements')
                self.assertEqual(result['request']['unsupported'], [phrase])
                self.assertEqual(source.queries, [])

    def test_an_unknown_kind_is_rejected_without_a_search(self):
        source = Source()
        result = run(message_for('released code'), Config(), 'S', source=source,
                     llm=Planner([{'kind': 'code', 'quote': 'released code'}]))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('blocking_requirements kind must be one of', result['stop_reason'])
        self.assertEqual(source.queries, [])

    def test_a_quote_must_be_verbatim_and_short(self):
        long_quote = 'x' * (BLOCKING_QUOTE_LIMIT + 1)
        cases = (['released code'],                           # legacy string array
                 [{'kind': 'code_release', 'quote': 'chrome bug'}],  # not in input
                 [{'kind': 'code_release', 'quote': '   '}],        # empty
                 [{'kind': 'code_release', 'quote': 'released code', 'why': 'x'}],
                 [{'kind': 'code_release', 'quote': long_quote}],
                 ['released code'])
        for items in cases:
            with self.subTest(items=items):
                source = Source()
                result = run(message_for('released code'), Config(), 'S', source=source,
                             llm=Planner(items))
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(source.queries, [])

    def test_the_validator_is_the_single_shared_contract(self):
        text = 'Only papers with released code from 2024 or 2026.'
        with self.assertRaises(GuideError):
            blocking_quotes(None, text)  # a missing field is never "no restrictions"
        self.assertEqual(blocking_quotes([], text), [])
        self.assertEqual(blocking_quotes(
            [{'kind': 'code_release', 'quote': 'released code'},
             {'kind': 'year_subset', 'quote': '2024 or 2026'},
             {'kind': 'code_release', 'quote': 'released code'}], text),
            ['released code', '2024 or 2026'])
        for bad in ({'kind': 'nope', 'quote': 'released code'},
                    {'kind': 'code_release', 'quote': 'from 2023 or 2025'},
                    {'kind': 'code_release'},
                    {'kind': 'code_release', 'quote': 'released code', 'extra': 1},
                    {'kind': ['code_release'], 'quote': 'released code'},
                    'released code', 42):
            with self.subTest(bad=bad), self.assertRaises(GuideError):
                blocking_quotes([bad], text)

    def test_plan_never_produces_satisfied_requirements(self):
        result = run('Find visual adaptation papers.', Config(), 'S', source=Source(),
                     llm=Planner([]))
        self.assertNotIn('satisfied_requirements', result['request'])
        plan_step = next(step for step in result['trace'] if step['action'] == 'plan')
        self.assertNotIn('satisfied_requirements', plan_step['llm_plan'])
        self.assertNotIn('satisfied_requirements', ' '.join(result['warnings']))


class SessionUpdater:
    """Session-side planner: same structured array, same validate path."""
    mode = 'demo'

    def __init__(self, blocking=None):
        self.blocking = deepcopy(blocking or [])

    def generate(self, stage, prompt, data, schema, validate):
        if stage == 'update':
            patch = {}
            if not (data.get('task') or {}).get('topic'):
                patch['topic'] = 'visual language adaptation'
            out = {'patch': patch, 'summary': 'fixture', 'assumptions': [],
                   'blocking_requirements': deepcopy(self.blocking), 'unresolved': []}
        elif stage == 'action':
            allowed = data['allowed']
            pending = (data.get('observation') or {}).get('pending_ids') or []
            if 'ASSESS' in allowed and pending:
                out = {'action': 'ASSESS', 'paper_ids': pending[:5], 'reason': 'scripted'}
            elif 'SEARCH' in allowed:
                out = {'action': 'SEARCH', 'query': 'visual adaptation benchmark',
                       'reason': 'scripted'}
            else:
                out = {'action': 'FINISH', 'reason': 'scripted'}
        elif stage == 'assess':
            out = {'assessments': [
                {'paper_id': paper['paper_id'], 'relevance': 2, 'category_status': 'unknown',
                 'evidence_quote': QUOTE, 'reason': 'grounded'} for paper in data['papers']]}
        else:
            raise AssertionError(f'unexpected stage {stage}')
        validate(out)
        return out


class BothEntryPointsAgreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / 'state.sqlite3')
        self.config = Config(provider='openai', model='fake-model', source='arxiv')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def chat(self, llm, source):
        return ChatSession(self.config, self.store, source=source, llm=llm), source

    def test_same_blocking_array_gives_the_same_stop_from_both_entry_points(self):
        message = message_for('released code repository')
        blocking = [{'kind': 'code_release', 'quote': 'released code repository'}]
        source = Source()
        workflow_result = run(message, Config(), 'S', source=source, llm=Planner(blocking))
        chat, session_source = self.chat(SessionUpdater(blocking), Source())
        session_result = chat.start(message=message)
        self.assertEqual(workflow_result['status'], session_result['status'])
        self.assertEqual(workflow_result['stop_reason'], session_result['stop_reason'])
        self.assertEqual(workflow_result['request']['unsupported'],
                         session_result['request']['unsupported'])
        self.assertEqual(session_result['status'], 'unsupported')
        self.assertEqual(session_result['stop_reason'], 'unsupported_requirements')
        self.assertEqual(session_result['unsupported'], ['released code repository'])
        self.assertEqual(source.queries, [])
        self.assertEqual(session_source.calls, [])

    def test_same_empty_array_lets_both_entry_points_search(self):
        workflow_source = Source()
        workflow_result = run('Find visual adaptation papers.', Config(), 'S',
                             source=workflow_source, llm=Planner([]))
        chat, session_source = self.chat(SessionUpdater([]), Source())
        session_result = chat.start(message='Find visual adaptation papers.')
        for result in (workflow_result, session_result):
            self.assertEqual(result['request']['unsupported'], [])
            self.assertNotEqual(result['stop_reason'], 'unsupported_requirements')
        self.assertTrue(workflow_source.queries)
        self.assertTrue(session_source.calls)

    def test_same_invalid_array_is_rejected_by_both_entry_points(self):
        message = message_for('released code')
        cases = (([{'kind': 'code', 'quote': 'released code'}], 'kind must be one of'),
                 ([{'kind': 'code_release', 'quote': 'chrome bug'}],
                  'exact excerpt of the user input'),
                 ([{'kind': 'code_release', 'quote': 'x' * 301}], 'at most 300 characters'),
                 (['released code'], 'exactly kind and quote'))
        for blocking, expected in cases:
            with self.subTest(blocking=blocking):
                workflow_result = run(message, Config(), 'S', source=Source(),
                                      llm=Planner(blocking))
                self.assertEqual(workflow_result['status'], 'failed')
                self.assertIn(expected, workflow_result['stop_reason'])
                chat, session_source = self.chat(SessionUpdater(blocking), Source())
                with self.assertRaises(GuideError) as caught:
                    chat.start(message=message)
                self.assertIn(expected, str(caught.exception))
                self.assertEqual(str(caught.exception), workflow_result['stop_reason'])
                self.assertEqual(chat.revision, 0)
                self.assertEqual(session_source.calls, [])

    def test_legacy_structured_request_with_string_unsupported_still_runs(self):
        source = Source()
        result = run('papers with code', Config(), 'S', source=source, llm=Planner([]),
                     request_data={'topic': 'RAG', 'query': 'rag',
                                   'unsupported': ['with code']})
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(result['stop_reason'], 'unsupported_requirements')
        self.assertEqual(result['request']['unsupported'], ['with code'])
        self.assertEqual(source.queries, [])
        chat, session_source = self.chat(SessionUpdater([]), Source())
        session_result = chat.start(request_data={'topic': 'RAG',
                                                  'unsupported': ['with code']})
        self.assertEqual(session_result['status'], 'unsupported')
        self.assertEqual(session_result['stop_reason'], 'unsupported_requirements')
        self.assertEqual(session_result['request']['unsupported'], ['with code'])
        self.assertEqual(session_source.calls, [])


class SchemaContractTests(unittest.TestCase):
    def test_both_schemas_require_the_same_closed_array(self):
        for schema in (prompts.PLAN_SCHEMA, prompts.TASK_UPDATE_SCHEMA):
            with self.subTest(schema=schema is prompts.PLAN_SCHEMA):
                properties = schema['properties']
                self.assertNotIn('unsupported', properties)
                array = properties['blocking_requirements']
                self.assertEqual(array['type'], 'array')
                item = array['items']
                self.assertEqual(item['required'], ['kind', 'quote'])
                self.assertIs(item['additionalProperties'], False)
                self.assertEqual(item['properties']['kind']['enum'], list(BLOCKING_KINDS))
                self.assertEqual(item['properties']['quote']['maxLength'],
                                 BLOCKING_QUOTE_LIMIT)
                self.assertIn('blocking_requirements', schema['required'])

    def test_prompts_describe_the_same_contract_and_kinds(self):
        for prompt in (prompts.PLAN, prompts.TASK_UPDATE):
            with self.subTest(prompt=prompt[:40]):
                self.assertIn('blocking_requirements', prompt)
                self.assertNotIn('unsupported:', prompt)
                for kind in BLOCKING_KINDS:
                    self.assertIn(kind, prompt)
        self.assertIn('MUST NOT appear here', prompts.PLAN)
        self.assertIn('never record those', prompts.TASK_UPDATE)

    def test_json_object_path_is_still_validated_locally(self):
        from llm import strict_schema_compatible
        self.assertFalse(strict_schema_compatible(prompts.PLAN_SCHEMA))
        source = Source()
        result = run(message_for('released code'), Config(), 'S', source=source,
                     llm=JsonObjectPlanner([{'kind': 'code', 'quote': 'released code'}]))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(source.queries, [])
        source = Source()
        result = run(message_for('released code'), Config(), 'S', source=source,
                     llm=JsonObjectPlanner([{'kind': 'code_release',
                                             'quote': 'released code'}]))
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(source.queries, [])


if __name__ == '__main__':
    unittest.main()
