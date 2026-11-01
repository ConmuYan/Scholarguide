"""Offline contracts for the unfinished opt-in session lifecycle."""
import unittest

from mcp_client import McpClient, McpError, ServerConfig, _validate_session_id, _validate_session_protocol


class LifecycleFailClosedTests(unittest.TestCase):
    def test_protocol_allowlist(self):
        for version in ('2025-11-25', '2025-03-26'):
            self.assertEqual(_validate_session_protocol(version), version)
        for version in ('2024-11-05', '2099-01-01', '', None, 20251125):
            with self.subTest(version=version), self.assertRaises(McpError):
                _validate_session_protocol(version)

    def test_session_id_only_visible_ascii_and_never_in_error(self):
        for value in ('A', 'A-z_09~!', 'x' * 256):
            self.assertEqual(_validate_session_id(value), value)
        for value in ('', 'with space', 'x\nsecret', 'x\x7fsecret', 'x\u00e9secret', None, 42):
            with self.subTest(value=repr(value)), self.assertRaises(McpError) as caught:
                _validate_session_id(value)
            self.assertNotIn('secret', str(caught.exception))

    def test_stateful_option_must_be_boolean(self):
        config = ServerConfig('offline', 'https://example.invalid')
        for value in (None, 0, 1, 'false'):
            with self.subTest(value=value), self.assertRaises(McpError):
                McpClient(config, stateful=value)


if __name__ == '__main__':
    unittest.main()
