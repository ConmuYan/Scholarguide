"""Source completion accounting against the actual MCP client, offline."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from common import Config, GuideError
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, ServerConfig
from source_store import SourceStore


class SourceMcpErrorIntegrationTests(unittest.TestCase):
    def test_real_client_tool_error_finishes_as_error_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota = root / 'quota.sqlite'
            with LuneQuotaStore(quota) as ledger:
                ledger.confirm(3, time.time() + 60)
            seen = []
            class Response:
                headers = {'Content-Type': 'application/json'}
                def __init__(self, request):
                    message = json.loads(request.data)
                    seen.append(message['method'])
                    self.body = json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                            'result': {'isError': True,
                                                       'content': [{'type': 'text', 'text': 'synthetic-private'}]}}).encode()
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, size=-1): return self.body[:size]
            shared = McpClient(ServerConfig('lune', 'https://fixture.invalid/mcp', allow=('search_papers',)),
                               transport=lambda request, timeout: Response(request))
            provider = MagicMock()
            provider.client.return_value = shared
            provider._charge.return_value = 42
            env = {'SG_LUNE_QUOTA_DB': str(quota), 'SG_MCP_TOKEN_FILE': str(root / 'missing.json'),
                   'LUNE_API_KEY': 'synthetic-key'}
            with patch.dict(os.environ, env), patch('mcp_lune.install', return_value=provider):
                store = SourceStore(root / 'source.sqlite')
                try:
                    with patch.object(store, 'reserve', return_value=0), self.assertRaises(GuideError) as caught:
                        store.invoke('lune', 'search', {'query': 'synthetic-query'}, Config())
                    self.assertNotIn('synthetic-private', str(caught.exception))
                    self.assertEqual(caught.exception.remote_outcome, 'error')
                    self.assertIs(caught.exception.retry_safe, False)
                    self.assertEqual(seen, ['tools/call'])
                    provider._finish.assert_called_once_with(42, ok=False, status='error')
                finally:
                    store.close()
            with LuneQuotaStore(quota) as ledger:
                self.assertEqual(ledger.status()['remaining_from_snapshot'], 2)

    def test_real_client_jsonrpc_error_is_explicit_not_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota = root / 'quota.sqlite'
            with LuneQuotaStore(quota) as ledger:
                ledger.confirm(3, time.time() + 60)
            class Response:
                headers = {'Content-Type': 'application/json'}
                def __init__(self, request):
                    message = json.loads(request.data)
                    self.body = json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                                            'error': {'code': -32602, 'message': 'synthetic-private'}}).encode()
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, size=-1): return self.body[:size]
            shared = McpClient(ServerConfig('lune', 'https://fixture.invalid/mcp', allow=('search_papers',)),
                               transport=lambda request, timeout: Response(request))
            provider = MagicMock()
            provider.client.return_value = shared
            provider._charge.return_value = 43
            env = {'SG_LUNE_QUOTA_DB': str(quota), 'SG_MCP_TOKEN_FILE': str(root / 'missing.json'),
                   'LUNE_API_KEY': 'synthetic-key', 'SG_SKIP_PROJECT_ENV': '1'}
            with patch.dict(os.environ, env), patch('mcp_lune.install', return_value=provider):
                store = SourceStore(root / 'source.sqlite')
                try:
                    with patch.object(store, 'reserve', return_value=0), self.assertRaises(GuideError) as caught:
                        store.invoke('lune', 'search', {'query': 'synthetic-query'}, Config())
                    self.assertEqual(caught.exception.remote_outcome, 'error')
                    self.assertIs(caught.exception.retry_safe, False)
                    self.assertNotIn('synthetic-private', str(caught.exception))
                    provider._finish.assert_called_once_with(43, ok=False, status='error')
                finally:
                    store.close()


if __name__ == '__main__':
    unittest.main()
