"""Activity feed: index/detail over runs and SQLite sessions, plus HTTP routes."""
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from activity import ActivityFeed
from webapp import ChatService, create_server


class ActivityFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name) / 'runs'
        (self.runs / 'web').mkdir(parents=True)
        self.service = ChatService(output=self.runs / 'web')
        created = self.service.create({'mode': 'demo', 'scenario': 'followup',
                                       'message': 'Recommend five RAG papers.'})
        self.session_id = created['session_id']
        self.service.send(self.session_id, {'message': 'Add one survey paper as well.'})
        self.feed = ActivityFeed(self.runs)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def test_index_lists_session(self):
        index = self.feed.index()
        self.assertEqual(len(index['sessions']), 1)
        item = index['sessions'][0]
        self.assertEqual(item['id'], self.session_id)
        self.assertGreater(item['events'], 3)
        self.assertEqual(item['turns'], 2)
        self.assertTrue(item['title'])
        self.assertEqual(index['totals']['sessions'], 1)

    def test_session_detail_has_events_task_and_cost(self):
        detail = self.feed.session(self.session_id)
        actions = [event['action'] for event in detail['events']]
        self.assertIn('task_update', actions)
        self.assertIn('search', actions)
        self.assertIn('assess', actions)
        self.assertIn('delivered', actions)
        self.assertEqual(detail['task']['topic'], 'retrieval augmented generation')
        self.assertGreater(detail['cost']['settled'], 0)
        self.assertGreaterEqual(len(detail['queries']), 1)
        self.assertEqual(len(detail['history']), 2)
        self.assertTrue(detail['session']['title'])
        decision = [event for event in detail['events'] if event['action'] == 'decision']
        self.assertTrue(decision)
        self.assertNotIn('search', [event['action'] for event in decision])

    def test_query_does_not_mutate_global_totals(self):
        full = self.feed.index()
        filtered = self.feed.index('no-such-record')
        self.assertEqual(filtered['totals'], full['totals'])
        self.assertEqual(filtered['filtered'], {'runs': 0, 'sessions': 0})
        self.assertGreater(filtered['totals']['events'], 0)

    def test_malformed_result_is_skipped_not_fatal(self):
        path = self.runs / ('b' * 32)
        path.mkdir()
        (path / 'result.json').write_text('[1, 2]', encoding='utf-8')
        index = self.feed.index()
        self.assertFalse(any(item['id'] == 'b' * 32 for item in index['runs']))
        self.assertIsNone(self.feed.run('b' * 32))

    def test_runs_are_sorted_by_start_time_not_directory_name(self):
        for job, started in [('a' * 32, '2026-10-19T00:00:00Z'),
                             ('f' * 32, '2026-10-19T01:00:00Z')]:
            path = self.runs / job
            path.mkdir()
            result = {'status': 'complete', 'mode': 'demo', 'method': 'A',
                      'started_at': started, 'papers': [],
                      'request': {'topic': job[:4]}, 'metrics': {}}
            (path / 'result.json').write_text(json.dumps(result), encoding='utf-8')
        index = self.feed.index()
        self.assertEqual(index['runs'][0]['id'], 'f' * 32)
        self.assertEqual(index['totals']['runs'], 2)

    def test_run_index_and_detail(self):
        job = 'a' * 32
        path = self.runs / job
        path.mkdir()
        result = {'status': 'complete', 'mode': 'demo', 'method': 'A',
                  'started_at': '2026-10-18T00:00:00+00:00',
                  'metrics': {'elapsed_seconds': 1.2, 'llm_calls': 0},
                  'papers': [{'id': 'p1'}], 'request': {'topic': 'rag'},
                  'trace': [{'step': 1, 'action': 'plan'}, {'step': 2, 'action': 'search'}]}
        (path / 'result.json').write_text(json.dumps(result), encoding='utf-8')
        index = self.feed.index()
        self.assertEqual(len(index['runs']), 1)
        self.assertEqual(index['runs'][0]['id'], job)
        detail = self.feed.run(job)
        self.assertEqual(detail['result']['status'], 'complete')
        self.assertIsNone(self.feed.run('../etc'))


class ActivityHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        runs = Path(cls.tmp.name) / 'runs'
        (runs / 'web').mkdir(parents=True)
        cls.service = ChatService(output=runs / 'web')
        created = cls.service.create({'mode': 'demo', 'scenario': 'followup',
                                      'message': 'Recommend five RAG papers.'})
        cls.session_id = created['session_id']
        cls.feed = ActivityFeed(runs)
        cls.server = create_server(0, chat=cls.service, activity=cls.feed)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.service.close()
        cls.tmp.cleanup()

    def request(self, path):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request('GET', path, headers={'Host': f'127.0.0.1:{self.port}'})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, response.getheader('Content-Type'), body

    def test_assets_and_index(self):
        status, kind, body = self.request('/activity')
        self.assertEqual(status, 200)
        self.assertIn('text/html', kind)
        self.assertIn('研究活动记录'.encode(), body)
        status, kind, _ = self.request('/activity.js')
        self.assertEqual(status, 200)
        self.assertIn('text/javascript', kind)
        status, kind, _ = self.request('/activity-story.js')
        self.assertEqual(status, 200)
        self.assertIn('text/javascript', kind)
        status, _, body = self.request('/api/activity')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['sessions'])

    def test_index_supports_server_side_search(self):
        status, _, body = self.request('/api/activity?q=retrieval')
        self.assertEqual(status, 200)
        index = json.loads(body)
        self.assertTrue(index['sessions'])
        status, _, body = self.request('/api/activity?q=no-such-record')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['sessions'], [])
        self.assertEqual(json.loads(body)['runs'], [])

    def test_detail_and_404(self):
        status, _, body = self.request('/api/activity/sessions/' + self.session_id)
        self.assertEqual(status, 200)
        detail = json.loads(body)
        self.assertGreater(len(detail['events']), 3)
        status, _, _ = self.request('/api/activity/sessions/missing')
        self.assertEqual(status, 404)
        status, _, _ = self.request('/api/activity/runs/' + 'b' * 32)
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
