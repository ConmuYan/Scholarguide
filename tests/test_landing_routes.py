"""The public home page connects to the real local workspace without inference."""
from http.client import HTTPConnection
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from providers import ProviderStore
from webapp import ChatService, Jobs, create_server


class LandingRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        providers = ProviderStore(root / 'providers.json')
        cls.jobs = Jobs(root, providers=providers)
        cls.chat = ChatService(root / 'web', providers=providers)
        cls.server = create_server(0, jobs=cls.jobs, chat=cls.chat, providers=providers)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.chat.close()
        cls.tmp.cleanup()

    def request(self, path, host=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        connection.request('GET', path, headers={'Host': host or f'127.0.0.1:{self.server.server_port}'})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode('utf-8')
        connection.close()
        return result

    def test_home_is_passive_and_workspace_has_a_distinct_canonical_route(self):
        status, headers, page = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn('id="landing-main"', page)
        self.assertNotIn('id="search-form"', page)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        status, _, page = self.request('/search')
        self.assertEqual(status, 200)
        self.assertIn('id="search-form"', page)
        self.assertEqual(self.jobs.items, {})
        self.assertEqual(self.chat.sessions, {})
        self.assertEqual(self.request('/', host='outside.example')[0], 403)

    def test_old_demo_bookmarks_preserve_their_query_without_starting_a_job(self):
        status, headers, body = self.request('/?demo=followup&from=bookmark')
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/search?demo=followup&from=bookmark')
        self.assertEqual(body, '')
        self.assertEqual(self.jobs.items, {})

    def test_all_landing_assets_are_served_with_correct_types(self):
        for path, kind in (('/landing.css', 'text/css'), ('/landing-demo.css', 'text/css'), ('/landing.js', 'text/javascript'),
                           ('/theme.js', 'text/javascript')):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(headers['Content-Type'].startswith(kind))
                self.assertTrue(body.strip())
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')


if __name__ == '__main__':
    unittest.main()
