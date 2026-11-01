"""Memory OpenAPI contracts match the store-identity concurrency boundary."""
import unittest

from jsonschema import Draft202012Validator

from api_schema import openapi_document


class MemorySchemaTests(unittest.TestCase):
    def test_snapshot_and_mutations_require_store_identity(self):
        paths = openapi_document()['paths']
        base = '/api/v1/memory/preferences'
        snapshot = paths[base]['get']['responses']['200']['content']['application/json']['schema']
        identity = 'a' * 64
        Draft202012Validator(snapshot).validate({'api_version': 'v1', 'preferences': {},
                                                  'active': {}, 'store_id': identity})
        self.assertFalse(Draft202012Validator(snapshot).is_valid(
            {'api_version': 'v1', 'preferences': {}, 'active': {}}))
        for suffix in ('', '/revoke', '/delete'):
            schema = paths[base + '/{key}' + suffix]['post']['requestBody']['content']['application/json']['schema']
            data = {'expected_revision': 0, 'store_id': identity}
            if not suffix:
                data.update(value='general', consent=True, expires_at=9999999999)
            self.assertTrue(Draft202012Validator(schema).is_valid(data))
            self.assertFalse(Draft202012Validator(schema).is_valid({k: v for k, v in data.items() if k != 'store_id'}))
            self.assertFalse(Draft202012Validator(schema).is_valid({**data, 'store_id': 'invalid'}))


if __name__ == '__main__':
    unittest.main()
