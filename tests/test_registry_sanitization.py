"""Registry metadata is detached; rejected tool aliases are not reflected."""
import unittest

from agent_tools import ToolRegistry, ToolSpec
from tool_governance import ToolGovernanceError


class RegistrySanitizationTests(unittest.TestCase):
    def test_unknown_alias_is_not_reflected_and_wrong_type_fails_closed(self):
        registry = ToolRegistry([])
        for alias in ('synthetic-private-query', ['not', 'a', 'name']):
            observation = registry.call(alias, lambda: None, _input={})
            self.assertEqual(observation.status, 'unknown_tool')
            self.assertNotIn('synthetic-private-query', repr(observation))
        with self.assertRaises(ToolGovernanceError) as caught:
            registry.validate_input('synthetic-private-query', {})
        self.assertNotIn('synthetic-private-query', str(caught.exception))

    def test_described_schema_cannot_mutate_validation_policy(self):
        spec = ToolSpec(name='fixture', kind='local', description='test',
                        input_schema={'type': 'object', 'additionalProperties': False,
                                      'properties': {'n': {'type': 'integer', 'maximum': 1}}},
                        timeout_s=1, max_retries=1, cost_model='unknown', provenance=())
        registry = ToolRegistry([spec])
        registry.describe()[0]['input_schema']['properties']['n']['maximum'] = 100
        with self.assertRaises(ToolGovernanceError):
            registry.validate_input('fixture', {'n': 10})


if __name__ == '__main__':
    unittest.main()
