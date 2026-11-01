"""Independent extension registration; native Lune is part of the product registry."""
import unittest

import mcp_plugin
import source as source_mod
from common import Budget, Config, GuideError, Paper

NAME = 'mock_catalog'


def _stub(query, limit, filters):
    return [Paper(id='mock:t1', title='Stub title', abstract='Stub abstract.',
                  url='https://example.invalid/t1', source=NAME, sources=[NAME])][:limit]


class McpSeamTests(unittest.TestCase):
    def setUp(self):
        mcp_plugin.reset_for_tests()
        self.baseline = dict(source_mod.PROVIDERS)
        self.original = source_mod.AcademicSource._search_one

    def tearDown(self):
        mcp_plugin.reset_for_tests()

    def install(self):
        mcp_plugin.register(NAME, source_mod.ProviderSpec(NAME, 'Mock catalog', 'scholarly'), _stub)
        return mcp_plugin.activate()

    def test_extension_changes_only_the_declared_registration_seam(self):
        self.assertEqual(self.install(), ["source.PROVIDERS['mock_catalog']", 'source.AcademicSource._search_one'])
        self.assertIn('lune', source_mod.PROVIDERS)
        self.assertIn(NAME, source_mod.PROVIDERS)
        self.assertIsNot(source_mod.AcademicSource._search_one, self.original)

    def test_registered_provider_answers_without_external_calls(self):
        self.install()
        src = source_mod.AcademicSource(Config(cache=False), Budget(20))
        batch = src.search_batch('anything', limit=3, providers=[NAME])
        self.assertEqual([p.id for p in batch.papers], ['mock:t1'])
        self.assertEqual((batch.runs[0].name, batch.runs[0].status), (NAME, 'ok'))

    def test_unregistered_providers_keep_original_behavior(self):
        self.install()
        self.assertEqual(sorted(source_mod.PROVIDERS), sorted([*self.baseline, NAME]))
        with self.assertRaises(GuideError):
            source_mod.AcademicSource(Config(cache=False), Budget(20))._search_one('nope', 'q', 1)

    def test_duplicate_and_invalid_registration_are_refused(self):
        self.install()
        spec = source_mod.ProviderSpec(NAME, 'Mock catalog', 'scholarly')
        for name, handler in [(NAME, _stub), ('', _stub), ('other', 'not-callable')]:
            with self.assertRaises(GuideError):
                mcp_plugin.register(name, spec, handler)

    def test_reset_restores_original_registry_and_native_lune(self):
        self.install()
        mcp_plugin.reset_for_tests()
        self.assertEqual(source_mod.PROVIDERS, self.baseline)
        self.assertIs(source_mod.AcademicSource._search_one, self.original)
        self.assertEqual(mcp_plugin.MUTATIONS, [])


if __name__ == '__main__':
    unittest.main()
