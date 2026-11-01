"""Host-owned native action configuration; all fixtures are synthetic."""
from dataclasses import replace
import json
import os
import unittest
from unittest.mock import patch

from common import Config, GuideError
from providers import ProviderProfile


class NativeActionConfigTests(unittest.TestCase):
    def test_defaults_preserve_json_and_do_not_authorize_fallback(self):
        config = Config()
        config.validate()
        self.assertEqual((config.action_protocol, config.native_fallback), ('json', 'disabled'))
        self.assertEqual(replace(config, protocol='responses').native_fallback, 'disabled')
        self.assertEqual(replace(config, protocol='responses', action_protocol='responses_native').native_fallback, 'disabled')

    def test_valid_explicit_native_settings(self):
        for fallback in ('disabled', 'json_on_unsupported'):
            with self.subTest(fallback=fallback):
                Config(protocol='responses', action_protocol='responses_native', native_fallback=fallback).validate()

    def test_rejects_unsupported_combinations_and_invalid_types(self):
        invalid = [
            {'provider': 'ollama', 'protocol': 'responses', 'action_protocol': 'responses_native'},
            {'provider': 'other', 'protocol': 'responses', 'action_protocol': 'responses_native'},
            {'protocol': 'chat_completions', 'action_protocol': 'responses_native'},
            {'protocol': 'responses', 'native_fallback': 'json_on_unsupported'},
            {'action_protocol': 'native'}, {'action_protocol': ''}, {'action_protocol': None},
            {'native_fallback': 'true'}, {'native_fallback': ''}, {'native_fallback': True},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(GuideError) as caught:
                    Config(api_key='synthetic-secret-key', **changes).validate()
                self.assertNotIn('synthetic-secret-key', str(caught.exception))

    def test_environment_defaults_and_normalization(self):
        with patch.dict(os.environ, {'SG_SKIP_PROJECT_ENV': '1', 'SG_API_KEY': 'synthetic-secret-key',
                                    'SG_PROVIDER': 'openai', 'SG_PROTOCOL': 'responses'}, clear=True):
            config = Config.from_env()
            self.assertEqual((config.action_protocol, config.native_fallback), ('json', 'disabled'))
            os.environ.update(SG_ACTION_PROTOCOL='  RESPONSES_NATIVE  ',
                              SG_NATIVE_FALLBACK='  JSON_ON_UNSUPPORTED  ')
            config = Config.from_env()
            self.assertEqual((config.action_protocol, config.native_fallback),
                             ('responses_native', 'json_on_unsupported'))
            config.validate()

    def test_environment_invalid_values_and_combinations_fail_closed(self):
        cases = [
            {'SG_ACTION_PROTOCOL': ''}, {'SG_ACTION_PROTOCOL': 'synthetic-secret-key'},
            {'SG_NATIVE_FALLBACK': 'synthetic-secret-key'}, {'SG_NATIVE_FALLBACK': 'json_on_unsupported'},
            {'SG_ACTION_PROTOCOL': 'responses_native', 'SG_PROTOCOL': 'chat_completions'},
            {'SG_ACTION_PROTOCOL': 'responses_native', 'SG_PROVIDER': 'ollama'},
            {'SG_ACTION_PROTOCOL': 'responses_native', 'SG_PROVIDER': 'other'},
        ]
        base = {'SG_SKIP_PROJECT_ENV': '1', 'SG_API_KEY': 'synthetic-secret-key',
                'SG_PROVIDER': 'openai', 'SG_PROTOCOL': 'responses'}
        for changes in cases:
            with self.subTest(changes=changes), patch.dict(os.environ, {**base, **changes}, clear=True):
                with self.assertRaises(GuideError) as caught:
                    Config.from_env()
                self.assertNotIn('synthetic-secret-key', str(caught.exception))

    def test_public_and_profile_rebind_preserve_host_permissions(self):
        base = Config(protocol='responses', action_protocol='responses_native',
                      native_fallback='json_on_unsupported', api_key='synthetic-secret-key')
        profile = ProviderProfile('test', 'Test', 'responses', 'https://example.invalid/v1',
                                  'dynamic-model', 'synthetic-profile-key')
        rebound = profile.config(base)
        rebound.validate()
        public = rebound.public()
        self.assertEqual((public['action_protocol'], public['native_fallback']),
                         ('responses_native', 'json_on_unsupported'))
        self.assertNotIn('synthetic-secret-key', json.dumps(public))
        self.assertNotIn('synthetic-profile-key', json.dumps(public))
        with self.assertRaises(GuideError) as caught:
            replace(profile, protocol='chat_completions').config(base)
        self.assertNotIn('synthetic-profile-key', str(caught.exception))
        no_permission = profile.config(Config())
        self.assertEqual((no_permission.action_protocol, no_permission.native_fallback), ('json', 'disabled'))


if __name__ == '__main__':
    unittest.main()
