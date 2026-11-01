import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import Budget, Config, GuideError
from source import AcademicSource
from source_store import SourceStore


class EndpointAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SourceStore(Path(self.tmp.name) / 'sources.sqlite')
        self.addCleanup(self.store.close)

    def client(self):
        return AcademicSource(self.store.configure(Config(source='deepseek', cache=False)), Budget(5))

    def test_saved_key_cannot_follow_endpoint_change(self):
        self.store.upsert({'name': 'deepseek', 'api_key': 'synthetic-secret'})
        with self.assertRaises(GuideError):
            self.store.upsert({'name': 'deepseek', 'endpoint': 'https://other.invalid/search'})
        self.assertEqual(self.store.credentials('deepseek')['api_key'], 'synthetic-secret')
        self.assertNotIn('synthetic-secret', repr(self.store.public_state()))

    def test_implicit_environment_key_cannot_follow_custom_target(self):
        with patch.dict(os.environ, {'DEEPSEEK_SEARCH_KEY': 'synthetic-env'}):
            self.store.upsert({'name': 'deepseek', 'endpoint': 'https://other.invalid/search'})
            client = self.client()
            with patch.object(client, '_json') as transport:
                with self.assertRaises(GuideError):
                    client._deepseek_search('query', 1)
                transport.assert_not_called()

    def test_http_never_receives_key(self):
        self.store.upsert({'name': 'deepseek', 'api_key': 'synthetic-secret',
                           'endpoint': 'http://other.invalid/search'})
        client = self.client()
        with patch.object(client, '_json') as transport:
            with self.assertRaises(GuideError) as caught:
                client._deepseek_search('query', 1)
            transport.assert_not_called()
            self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_explicit_custom_binding_does_not_authorize_fetch(self):
        self.store.upsert({'name': 'deepseek', 'api_key': 'synthetic-secret',
                           'endpoint': 'https://other.invalid/search'})
        client = self.client()
        with patch.object(client, '_json', return_value={'results': []}) as transport:
            client._deepseek_search('query', 1)
            self.assertEqual(transport.call_count, 1)
            with self.assertRaises(GuideError):
                client.fetch('https://paper.invalid/article')
            self.assertEqual(transport.call_count, 1)

    def test_official_and_same_origin_legacy_key_are_compatible(self):
        with self.store._db() as db:
            db.execute('UPDATE sources SET endpoint=?, api_key=? WHERE name=?',
                       ('https://api.deepseek.com/custom', 'synthetic-old', 'deepseek'))
        client = self.client()
        with patch.object(client, '_json', return_value={'results': [], 'content': 'fixture'}) as transport:
            client._deepseek_search('query', 1)
            client.fetch('https://paper.invalid/article')
            self.assertEqual(transport.call_count, 2)

    def test_model_key_is_never_used_as_source_key(self):
        config = Config(source='deepseek', cache=False, api_key='synthetic-model-key',
                        base_url='https://api.deepseek.com/v1', deepseek_search_key='')
        source = AcademicSource(config, Budget(3))
        self.assertFalse(config.deepseek_search_enabled)
        self.assertFalse(source.available('deepseek'))
        with patch.object(source, '_json') as transport:
            with self.assertRaises(GuideError):
                source._deepseek_search('fixture', 1)
            with self.assertRaises(GuideError):
                source.fetch('https://paper.invalid/fixture')
            transport.assert_not_called()
        self.assertEqual(config.api_key, 'synthetic-model-key')

    def test_standalone_environment_key_has_no_custom_binding(self):
        with patch.dict(os.environ, {'DEEPSEEK_SEARCH_KEY': 'synthetic-env'}):
            config = Config.from_env()
            config.deepseek_search_endpoint = 'https://other.invalid/search'
            client = AcademicSource(config, Budget(5))
            with patch.object(client, '_json') as transport:
                with self.assertRaises(GuideError):
                    client._deepseek_search('query', 1)
                transport.assert_not_called()

    def test_config_snapshot_cannot_move_bound_key(self):
        self.store.upsert({'name': 'deepseek', 'api_key': 'synthetic-secret',
                           'endpoint': 'https://other.invalid/search'})
        client = self.client()
        client.config.deepseek_search_endpoint = 'https://third.invalid/search'
        with patch.object(client, '_json') as transport:
            with self.assertRaises(GuideError):
                client._deepseek_search('query', 1)
            transport.assert_not_called()

    def test_cleared_or_rotated_key_invalidates_existing_client_snapshot(self):
        for change in ('clear', 'rotate'):
            with self.subTest(change=change):
                with tempfile.TemporaryDirectory() as tmp:
                    store = SourceStore(Path(tmp) / 'sources.sqlite')
                    try:
                        store.upsert({'name': 'deepseek', 'api_key': 'synthetic-old'})
                        source = AcademicSource(store.configure(Config(source='deepseek', cache=False)), Budget(3))
                        store.upsert({'name': 'deepseek', **({'clear_key': True} if change == 'clear'
                                                           else {'api_key': 'synthetic-new'})})
                        with patch.object(source, '_json') as transport, self.assertRaises(GuideError):
                            source._deepseek_search('fixture', 1)
                        transport.assert_not_called()
                    finally:
                        store.close()

    def test_unbound_legacy_custom_credentials_are_not_shown_as_ready(self):
        with self.store._db() as db:
            db.execute('UPDATE sources SET endpoint=?, api_key=? WHERE name=?',
                       ('https://old.invalid/search', 'synthetic-old', 'deepseek'))
        state = next(item for item in self.store.public_state()['sources'] if item['name'] == 'deepseek')
        self.assertFalse(state['configured'])
        self.assertTrue(state['auth_configured'])

    def test_legacy_custom_key_fails_closed(self):
        with self.store._db() as db:
            db.execute('UPDATE sources SET endpoint=?, api_key=? WHERE name=?',
                       ('https://old.invalid/search', 'synthetic-old', 'deepseek'))
        client = self.client()
        with patch.object(client, '_json') as transport:
            with self.assertRaises(GuideError):
                client._deepseek_search('query', 1)
            transport.assert_not_called()
