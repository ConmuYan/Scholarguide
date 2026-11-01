"""Transport contract tests with a local HTTP server; no external services."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import Budget, Config
from llm import JsonLLM


class TransportTests(unittest.TestCase):
    def test_cross_origin_redirect_does_not_forward_authorization(self):
        from urllib.error import HTTPError
        from urllib.request import Request
        from network import urlopen
        received = []
        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                received.append(self.headers.get('Authorization'))
                self.send_response(200); self.end_headers()
        sink = HTTPServer(('127.0.0.1', 0), Sink)
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{sink.server_port}/sink')
                self.end_headers()
        origin = HTTPServer(('127.0.0.1', 0), Redirect)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (origin, sink)]
        for thread in threads: thread.start()
        try:
            request = Request(f'http://127.0.0.1:{origin.server_port}/', data=b'{}',
                              headers={'Authorization': 'Bearer synthetic-only'})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=5, source_request=True)
            caught.exception.close()
            self.assertEqual(received, [])
        finally:
            for server in (origin, sink): server.shutdown(); server.server_close()
            for thread in threads: thread.join(2)

    def test_ollama_and_openai_compatible_payloads_and_usage(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, body))
                output = '{"query":"RAG evaluation"}'
                if self.path == '/api/chat':
                    response = {'message': {'content': output}, 'prompt_eval_count': 12, 'eval_count': 4}
                else:
                    response = {'choices':[{'message':{'content':output}, 'finish_reason':'stop'}],
                                'usage':{'prompt_tokens':12,'completion_tokens':4}}
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

        server = HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for provider, suffix in [('ollama', ''), ('openai', '/v1')]:
                config = Config(provider=provider, base_url=f'http://127.0.0.1:{server.server_port}{suffix}')
                budget = Budget(10)
                llm = JsonLLM(config, budget)
                result = llm.generate('query', 'Return JSON.', {}, {'type':'object'}, lambda value: None)
                self.assertEqual(result['query'], 'RAG evaluation')
                self.assertEqual(budget.llm_calls, 1)
                self.assertEqual(budget.metrics()['input_tokens'], 12)
            self.assertEqual([c[0] for c in calls], ['/api/chat', '/v1/chat/completions'])
            self.assertFalse(calls[0][1]['stream'])
            self.assertEqual(calls[1][1]['response_format'], {'type':'json_object'})
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == '__main__':
    unittest.main()
