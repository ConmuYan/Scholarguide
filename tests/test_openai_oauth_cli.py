"""Offline checks for the optional CLI profile; no credentials or network."""
import unittest
from unittest.mock import MagicMock, patch

from scholarguide import app


class OpenAIOAuthCliTests(unittest.TestCase):
    @patch('scholarguide.providers.ProviderStore')
    @patch('scholarguide.openai_oauth.login')
    def test_login_adds_profile_without_activating(self, login, store_class):
        store = store_class.return_value
        store.get.return_value = None
        self.assertEqual(app.main(['openai-oauth-login', '--model', 'example-model']), 0)
        login.assert_called_once_with()
        profile = store.upsert.call_args.args[0]
        self.assertEqual(profile['id'], 'openai-oauth')
        self.assertEqual(profile['base_url'], 'https://api.openai.com/v1')
        self.assertEqual(profile['api_key'], '')
        self.assertEqual(profile['model'], 'example-model')
        self.assertFalse(store.upsert.call_args.kwargs['activate'])

    @patch('scholarguide.providers.ProviderStore')
    @patch('scholarguide.openai_oauth.login')
    def test_existing_conflicting_profile_fails_before_login(self, login, store_class):
        store = store_class.return_value
        store.get.return_value = MagicMock(base_url='https://example.com', protocol='responses', api_key='')
        self.assertEqual(app.main(['openai-oauth-login', '--model', 'example-model']), 1)
        login.assert_not_called()
        store.upsert.assert_not_called()

    def test_saved_profile_is_explicit_and_optional(self):
        parser = app.build_parser()
        default = parser.parse_args(['run', '--request', 'test'])
        selected = parser.parse_args(['run', '--request', 'test', '--profile', 'openai-oauth'])
        self.assertIsNone(default.profile)
        self.assertEqual(selected.profile, 'openai-oauth')


if __name__ == '__main__':
    unittest.main()
