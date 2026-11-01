"""Offline adversarial tests for the opt-in domain workflow catalog."""
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_skills
from common import GuideError
from agent_tools import ToolRegistry, ToolSpec, SOURCE_INPUT_SCHEMA
from scripts.verify_delivery import isolated_environment


class BuiltinDomainTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = isolated_environment(Path(tmp.name), inherited={})
        env.update(HOME=tmp.name, USERPROFILE=tmp.name, TEMP=tmp.name, TMP=tmp.name)
        guard = patch.dict(os.environ, env, clear=True)
        guard.start()
        self.addCleanup(guard.stop)
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def test_default_off_and_explicit_selection(self):
        self.assertEqual(mcp_skills.domain_registry().names(), [])
        registry = mcp_skills.domain_registry(enabled=True)
        self.assertEqual(registry.names(), ['evidence_reading', 'research_trends'])
        with self.assertRaises(GuideError):
            mcp_skills.domain_plan(registry, '../evidence_reading', {'topic': 'x', 'limit': 1})

    def test_plans_are_advice_with_evidence_and_partial_boundaries(self):
        registry = mcp_skills.domain_registry(enabled=True)
        tools = ToolRegistry([ToolSpec('search', 'aggregate', 'search', SOURCE_INPUT_SCHEMA,
                                       1, 1, 'unknown', ())])
        for name in registry.names():
            with self.subTest(name=name):
                skill = registry.get(name)
                self.assertTrue(skill.as_dict()['outputs'])
                plan = mcp_skills.domain_plan(registry, name, {'topic': 'synthetic', 'limit': 10})
                self.assertEqual(plan['status'], 'proposed')
                self.assertFalse(plan['executed'])
                self.assertGreaterEqual(len(plan['steps']), 2)
                for step in plan['steps']:
                    self.assertTrue(step['safety'])
                    self.assertTrue(step['on_failure'])
                    if step['kind'] == 'tool':
                        self.assertEqual(step['tool'], 'search')
                        tools.validate_input(step['tool'], step['arguments'])
                self.assertTrue(plan['evidence_boundary'])

    def test_argument_and_count_bounds(self):
        registry = mcp_skills.domain_registry(enabled=True)
        for limit in (0, 11, -1, True, '2', 1.5, None):
            with self.subTest(limit=limit), self.assertRaises(GuideError):
                mcp_skills.domain_plan(registry, 'research_trends', {'topic': 'x', 'limit': limit})
        for args in ({'topic': '', 'limit': 1}, {'topic': 'x' * 501, 'limit': 1},
                     {'topic': 'x', 'limit': 1, 'model': 'override'},
                     {'topic': 'x', 'limit': 1, 'providers': ['lune']}):
            with self.subTest(args=args), self.assertRaises(GuideError):
                mcp_skills.domain_plan(registry, 'evidence_reading', args)
        for limit in (1, 10):
            mcp_skills.domain_plan(registry, 'evidence_reading', {'topic': 'x', 'limit': limit})

    def test_external_or_forged_skill_never_accepted(self):
        registry = mcp_skills.domain_registry(enabled=True)
        for field in ('permissions', 'model', 'remote', 'credentials', 'budget', 'sources'):
            forged = mcp_skills.Skill(name='evil', description='x', steps=(
                {'kind': 'tool', 'tool': 'shell', field: 'override'},), trusted=True)
            with self.subTest(field=field), self.assertRaises(GuideError):
                registry.register(forged)
        with self.assertRaises(GuideError):
            mcp_skills.domain_plan(mcp_skills.SkillRegistry(), 'evidence_reading', {'topic': 'x', 'limit': 1})

    def test_forged_legacy_registry_and_detached_plans(self):
        registry = mcp_skills.domain_registry(enabled=True)
        legacy = mcp_skills.SkillRegistry()
        legacy.register(mcp_skills.Skill(name='evidence_reading', description='synthetic',
            steps=({'kind': 'tool', 'tool': 'shell', 'permissions': 'all'},), trusted=True))
        with self.assertRaises(GuideError):
            mcp_skills.domain_plan(legacy, 'evidence_reading', {'topic': 'x', 'limit': 1})
        first = mcp_skills.invoke(registry, 'evidence_reading', {'topic': 'x', 'limit': 1})
        first['steps'][0]['tool'] = 'shell'
        first['steps'][0]['arguments']['limit'] = 100
        fresh = mcp_skills.domain_plan(registry, 'evidence_reading', {'topic': 'x', 'limit': 1})
        self.assertEqual(fresh['steps'][0]['tool'], 'search')
        self.assertEqual(fresh['steps'][0]['arguments']['limit'], 1)
        tools = ToolRegistry([])
        self.assertEqual(tools.call('search', lambda: None, _input=fresh['steps'][0]['arguments']).status,
                         'unknown_tool')

    def test_compatibility_invocation_never_executes_domain_skill(self):
        registry = mcp_skills.domain_registry(enabled=True)
        with self.assertRaises(GuideError):
            mcp_skills.invoke(registry, 'evidence_reading', {'topic': 'x', 'limit': 1},
                              client=object(), execute=True, allow_dynamic=True)


if __name__ == '__main__':
    unittest.main()
