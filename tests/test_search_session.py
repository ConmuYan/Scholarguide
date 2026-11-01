"""Search runs become resumable Chat sessions (unified history)."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from demo import demo_run
from webapp import ChatService, Jobs

GAD = ROOT / 'examples' / 'live_result.json'


class SearchSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.chat = ChatService(output=self.root / 'web')

    def tearDown(self):
        self.chat.close()
        self.tmp.cleanup()

    def test_completed_demo_search_is_imported_and_resumable(self):
        jobs = Jobs(output=self.root / 'runs', importer=self.chat.import_run)
        job_id = jobs.submit({'mode': 'demo', 'method': 'A', 'scenario': 'followup'})
        for _ in range(100):
            snap = jobs.snapshot(job_id)
            if snap and snap['state'] == 'done':
                break
            time.sleep(.05)
        self.assertEqual(snap['state'], 'done')
        session_id = snap['result']['chat_session_id']
        self.assertTrue(session_id)
        listed = self.chat.list_sessions()
        self.assertEqual(listed[0]['origin'], 'search')
        self.assertEqual(listed[0]['session_id'], session_id)
        resumed = self.chat.send(session_id, {'message': 'Add one survey paper.'})
        self.assertEqual(resumed['result']['status'], 'complete')
        self.assertEqual(len(self.chat.snapshot(session_id)['history']), 2)

    def test_gad_live_result_imports_with_candidates_and_assessments(self):
        result = json.loads(GAD.read_text(encoding='utf-8'))
        session_id = self.chat.import_run(result, result['run_id'])
        snapshot = self.chat.snapshot(session_id)
        self.assertEqual(snapshot['origin'], 'search')
        self.assertEqual(snapshot['status'], 'complete')
        self.assertEqual(len(snapshot['history']), 1)
        self.assertEqual(len(snapshot['history'][0]['papers']), 5)
        self.assertEqual(self.chat.import_run(result, result['run_id']), session_id)
        self.assertEqual(len(self.chat.list_sessions()), 1)


if __name__ == '__main__':
    unittest.main()
