"""A locally interrupted Sources response remains observable without retry."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from common import Config, GuideError, RunCancelled
from lune_quota import LuneQuotaStore
from mcp_client import McpClient, ServerConfig
from mcp_lune import LuneProvider
from providers import ProviderStore
from source_store import SourceStore
from webapp import ChatService, Jobs, create_server


class SourcePartialApiTests(unittest.TestCase):
    def test_retained_result_is_returned_as_error_not_automatic_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quota_path = root / 'quota.sqlite'
            with LuneQuotaStore(quota_path) as quota:
                quota.confirm(2, time.time() + 60)
            calls = []
            def transport(request, timeout):
                calls.append(1)
                message = json.loads(request.data)
                result = {'structuredContent': {'papers': [{'id': 'one', 'title': 'First'}],
                                                 'api_key': 'synthetic-secret'}}
                return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode())
            client = McpClient(ServerConfig('lune', 'https://fixture.invalid', allow=('search_papers',)),
                               transport=transport)
            provider = LuneProvider(client=client, ledger_path=root / 'ledger.json')
            sources = SourceStore(root / 'sources.sqlite')
            providers = ProviderStore(root / 'providers.json')
            chat = ChatService(root / 'web', providers=providers)
            server = None
            thread = None
            try:
                with patch.dict(os.environ, {'SG_LUNE_QUOTA_DB': str(quota_path),
                                             'SG_MCP_TOKEN_FILE': str(root / 'missing-token'),
                                             'SG_SKIP_PROJECT_ENV': '1'}), \
                     patch('mcp_lune.install', return_value=provider), \
                     patch.object(sources, '_ensure_ready', return_value=Config(source='lune', source_store=sources)), \
                     patch.object(LuneQuotaStore, 'finish', side_effect=RunCancelled()):
                    server = create_server(0, jobs=Jobs(root / 'runs', providers=providers),
                                           chat=chat, providers=providers, sources=sources)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    try:
                        conn.request('POST', '/api/v1/sources/lune/skills/search',
                                     body=json.dumps({'arguments': {'query': 'fixture'}}),
                                     headers={'Content-Type': 'application/json'})
                        response = conn.getresponse()
                        payload = json.loads(response.read())
                    finally:
                        conn.close()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(payload['error']['remote_outcome'], 'completed')
                    self.assertFalse(payload['error']['retryable'])
                    self.assertFalse(payload['error']['retry_safe'])
                    self.assertEqual(payload['partial_result']['result']['papers'][0]['id'], 'one')
                    self.assertEqual(payload['partial_result']['audit_status'], 'failed')
                    self.assertNotIn('synthetic-secret', repr(payload))
                    self.assertEqual(calls, [1])
                    spoof = GuideError('Synthetic failure')
                    spoof.remote_outcome, spoof.retry_safe = 'completed', False
                    spoof.partial_source_result = {'name': 'lune', 'audit_status': 'failed',
                                                   'result': {'secret': 'synthetic-private'}}
                    with patch.object(sources, 'invoke', side_effect=spoof):
                        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                        try:
                            conn.request('POST', '/api/v1/sources/lune/skills/search',
                                         body=json.dumps({'arguments': {'query': 'fixture'}}),
                                         headers={'Content-Type': 'application/json'})
                            denied = json.loads(conn.getresponse().read())
                        finally:
                            conn.close()
                    self.assertNotIn('partial_result', denied)
                    self.assertNotIn('synthetic-private', repr(denied))
            finally:
                if server is not None:
                    server.shutdown(); server.server_close()
                if thread is not None:
                    thread.join(3)
                chat.close()
                sources.close()


if __name__ == '__main__':
    unittest.main()
