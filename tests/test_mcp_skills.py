"""Skill registry: registration, validation, dry-run plans, MCP prompt ingestion."""
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import mcp_client
import mcp_skills
from common import GuideError
from tests.test_mcp_client import MockMcp, _result
from tests.lune_quota_fixture import confirmed_quota


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = mcp_skills.default_registry()

    def test_bundled_manifest_has_the_six_lune_prompts(self):
        self.assertEqual(self.registry.names(), [
            'compare_papers', 'find_related_work', 'literature_review',
            'research_methodology', 'trace_citations', 'verify_draft'])
        for skill in self.registry.skills():
            self.assertEqual(skill.source, 'lune')
            self.assertEqual(skill.server, 'lune')
            self.assertTrue(skill.params and skill.steps)

    def test_required_arguments_are_enforced_per_skill(self):
        self.assertEqual(self.registry.get('verify_draft').validate({'draft': 'x'})['draft'], 'x')
        for name in ('literature_review', 'verify_draft', 'trace_citations',
                     'research_methodology', 'compare_papers', 'find_related_work'):
            with self.subTest(skill=name):
                with self.assertRaises(GuideError) as caught:
                    self.registry.get(name).validate({})
                self.assertIn('missing required', str(caught.exception))

    def test_unknown_argument_lists_the_allowed_ones(self):
        with self.assertRaises(GuideError) as caught:
            self.registry.get('literature_review').validate({'topic': 't', 'venuez': 'NeurIPS'})
        message = str(caught.exception)
        self.assertIn('venuez', message)
        self.assertIn('venues', message)

    def test_optional_arguments_are_filled_empty(self):
        args = self.registry.get('literature_review').validate({'topic': 't'})
        self.assertEqual(args, {'topic': 't', 'venues': '', 'since_year': ''})

    def test_duplicate_registration_is_refused(self):
        skill = self.registry.get('verify_draft')
        with self.assertRaises(GuideError) as caught:
            self.registry.register(skill)
        self.assertIn('already registered', str(caught.exception))
        self.registry.register(skill, replace=True)          # explicit replacement is allowed

    def test_invalid_names_are_refused(self):
        for name in ('', 'has space', 'semi;colon'):
            with self.subTest(name=name):
                with self.assertRaises(GuideError):
                    self.registry.register(mcp_skills.Skill(name=name, description='x'))

    def test_unknown_skill_lists_available(self):
        with self.assertRaises(GuideError) as caught:
            self.registry.get('nope')
        self.assertIn('available', str(caught.exception))
        self.assertIn('literature_review', str(caught.exception))

    def test_dry_run_plan_needs_no_network(self):
        plan = mcp_skills.invoke(self.registry, 'trace_citations', {'paper': 'RAG survey'})
        self.assertFalse(plan['execute'])
        self.assertEqual(plan['steps'][0]['status'], 'planned')
        self.assertEqual(plan['steps'][0]['arguments'], {'paper': 'RAG survey'})

    def test_execute_without_a_client_is_refused(self):
        with self.assertRaises(GuideError) as caught:
            mcp_skills.invoke(self.registry, 'verify_draft', {'draft': 'x'}, execute=True)
        self.assertIn('needs a client', str(caught.exception))

    def test_unsupported_step_kind_is_refused(self):
        self.registry.register(mcp_skills.Skill(
            name='weird', description='x', params=(mcp_skills.Param('q', required=True),),
            steps=({'kind': 'shell', 'command': 'rm -rf /'},), source='test'), replace=True)
        with self.assertRaises(GuideError) as caught:
            mcp_skills.invoke(self.registry, 'weird', {'q': 'x'}, client=object(), execute=True)
        self.assertIn('unsupported step kind', str(caught.exception))


class McpPromptTests(unittest.TestCase):
    def setUp(self):
        self.quota = confirmed_quota(self)

    def test_prompts_list_refreshes_skills_without_losing_curated_text(self):
        prompts = [
            {'name': 'literature_review', 'description': 'SERVER TEXT',
             'arguments': [{'name': 'topic'}]},
            {'name': 'custom_flow', 'description': 'Server-only workflow',
             'arguments': [{'name': 'seed'}]},
        ]
        with MockMcp({'prompts/list': _result({'prompts': prompts})}) as mock:
            client = mcp_client.McpClient(mcp_client.ServerConfig(
                'lune', mock.url, timeout=10, allow_all=True), quota_store=self.quota)
            registry = mcp_skills.default_registry()
            mcp_skills.from_mcp_prompts(registry, client.list_prompts(), server='lune')
        self.assertIn('custom_flow', registry.names())
        self.assertEqual(len(registry), 7)
        self.assertNotEqual(registry.get('literature_review').description, 'SERVER TEXT')
        self.assertEqual(registry.get('literature_review').source, 'lune')
        self.assertEqual(registry.get('custom_flow').source, 'mcp-prompt')
        self.assertEqual(registry.get('custom_flow').steps[0]['kind'], 'prompt')

    def test_prompt_step_executes_against_a_server(self):
        with MockMcp({'prompts/get': _result({'messages': [{'role': 'user', 'content': 'x'}],
                                              'description': 'ok'})}) as mock:
            client = mcp_client.McpClient(mcp_client.ServerConfig(
                'lune', mock.url, timeout=10, allow_prompts=('verify_draft',)),
                quota_store=self.quota)
            registry = mcp_skills.default_registry()
            plan = mcp_skills.invoke(registry, 'verify_draft', {'draft': 'claim'},
                                     client=client, execute=True)
        self.assertEqual(plan['steps'][0]['status'], 'ok')
        self.assertEqual(plan['steps'][0]['messages'], 1)


class CliTests(unittest.TestCase):
    def _run(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = mcp_skills.main(argv)
        return code, buffer.getvalue()

    def test_list_and_check(self):
        code, text = self._run(['list'])
        self.assertEqual(code, 0)
        self.assertIn('literature_review', text)
        self.assertIn('6 skill(s)', text)
        code, text = self._run(['check'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)['without_params_or_steps'], [])

    def test_show_emits_json(self):
        code, text = self._run(['show', 'compare_papers'])
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertEqual(payload['name'], 'compare_papers')
        self.assertTrue(any(p['name'] == 'topic' for p in payload['params']))

    def test_invoke_dry_run_and_bad_arguments(self):
        code, text = self._run(['invoke', 'find_related_work', '--arg', 'abstract=we propose'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)['args']['abstract'], 'we propose')
        with self.assertRaises(GuideError):
            self._run(['invoke', 'find_related_work'])
        with self.assertRaises(GuideError):
            self._run(['invoke', 'find_related_work', '--arg', 'nonsense'])


if __name__ == '__main__':
    unittest.main()
