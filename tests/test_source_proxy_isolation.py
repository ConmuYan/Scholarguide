"""The opt-in academic proxy must not receive model HTTP traffic."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

from common import Budget, Config, GuideError
import llm
import mcp_client
import source


class RecordingProxy(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.paths.append(self.path)
        body = b'local proxy response'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.paths.append(self.path)
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = b'{"choices":[{"message":{"content":"{}"}}]}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SourceProxyIsolationTests(unittest.TestCase):
    def test_academic_and_mcp_use_proxy_but_model_does_not(self):
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), RecordingProxy)
        proxy.paths = []
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(proxy.shutdown)
        settings = {'SG_SOURCE_PROXY': f'http://127.0.0.1:{proxy.server_port}',
                    'NO_PROXY': 'localhost,127.0.0.1', 'no_proxy': 'localhost,127.0.0.1'}
        with patch.dict(os.environ, settings):
            for opener in (source.urlopen, mcp_client.urlopen):
                with opener(Request('http://scholarguide-proxy-check.invalid/paper'), timeout=4) as response:
                    self.assertEqual(response.read(), b'local proxy response')
            # Direct DNS for the reserved .invalid domain must fail. The fake
            # proxy could have answered it, so success here means leakage.
            with self.assertRaises(URLError):
                llm.urlopen(Request('http://scholarguide-proxy-check.invalid/model'), timeout=4)
            model = llm.JsonLLM(Config(provider='openai',
                base_url='http://scholarguide-proxy-check.invalid/v1', model='offline-fixture',
                api_key='not-a-real-key', protocol='chat_completions', request_timeout=4), Budget(10))
            with self.assertRaises(GuideError):
                model._call([{'role': 'user', 'content': 'offline proxy isolation check'}], {})
        self.assertEqual(len(proxy.paths), 2)
        self.assertTrue(all(path.endswith('/paper') for path in proxy.paths))


if __name__ == '__main__':
    unittest.main()
