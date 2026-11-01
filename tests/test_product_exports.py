"""Reading-list export and honest public task context, entirely offline."""
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from demo import demo_run
from providers import ProviderStore
from report import bibtex, markdown
from webapp import ChatService, Jobs, create_server


class BibliographyTests(unittest.TestCase):
    def test_metadata_is_preserved_and_missing_fields_are_not_invented(self):
        paper = {'id': 'arxiv:2401.12345', 'title': 'RAG & {Evidence}_100%',
                 'authors': ['Alice Smith', 'Bob Jones', '王小明'], 'year': 2024,
                 'doi': 'https://doi.org/10.1234/paper', 'url': 'https://arxiv.org/abs/2401.12345'}
        text = bibtex({'papers': [paper, paper, {'id': 'unknown', 'title': 'Metadata only'}]})
        self.assertEqual(text.count('@misc{'), 2)
        self.assertIn('Alice Smith and Bob Jones and 王小明', text)
        self.assertIn(r'RAG \& \{Evidence\}\_100\%', text)
        self.assertIn('doi = {10.1234/paper}', text)
        self.assertIn('archivePrefix = {arXiv}', text)
        self.assertNotIn('None', text)
        self.assertNotIn('Unknown Author', text)
        self.assertEqual(text, bibtex({'papers': [paper, paper, {'id': 'unknown', 'title': 'Metadata only'}]}))

    def test_stopped_report_preserves_current_requirements_and_evidence(self):
        result = demo_run('followup')
        result['stop_reason'] = 'user_cancelled'
        result['request']['purpose'] = 'Design an experiment'
        text = markdown(result)
        self.assertIn('Stopped at your request', text)
        self.assertIn('Design an experiment', text)
        self.assertIn('Requested: 5', text)
        for paper in result['papers']:
            self.assertIn(paper['title'], text)
            self.assertIn(paper['evidence_quote'], text)

    def test_public_task_context_uses_actual_fields_and_excludes_private_unknowns(self):
        public = ChatService._public({'request': {'topic': 'RAG', 'target_count': 3,
            'excluded_paper_ids': ['real-paper-id'], 'venue': 'ACL', 'api_key': 'private',
            'base_url': 'private-url'}, 'papers': [], 'metrics': {}})
        self.assertEqual(public['request'], {'topic': 'RAG', 'target_count': 3,
            'excluded_paper_ids': ['real-paper-id'], 'venue': 'ACL'})
        self.assertNotIn('private', json.dumps(public))


class ExportApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.providers = ProviderStore(self.root / 'providers.json')
        self.chat = ChatService(self.root / 'web', providers=self.providers)
        self.jobs = Jobs(self.root / 'runs', providers=self.providers)
        self.server = create_server(0, jobs=self.jobs, chat=self.chat, providers=self.providers)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        self.chat.close(); self.temp.cleanup()

    def get(self, path):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        connection.request('GET', path)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode('utf-8')
        connection.close()
        return result

    def test_each_saved_chat_turn_downloads_its_own_full_result_without_mutation(self):
        created = self.chat.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        session_id = created['session_id']
        first = self.chat.turn_result(session_id, 1)
        self.chat.send(session_id, {'mode': 'demo', 'message': 'Add a survey paper.'})
        path = self.root / 'web' / session_id / 'turn-1' / 'result.json'
        saved = path.read_bytes()
        for artifact, marker in (('report.md', '# ScholarGuide'), ('result.json', '"papers"'),
                                  ('references.bib', '@misc{')):
            status, headers, text = self.get(f'/api/chat/sessions/{session_id}/turns/1/{artifact}')
            self.assertEqual(status, 200)
            self.assertIn(marker, text)
            self.assertIn('attachment;', headers['Content-Disposition'])
            if artifact == 'result.json':
                self.assertEqual(json.loads(text), first)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.get(f'/api/chat/sessions/{session_id}/turns/99/report.md')[0], 404)
        self.assertEqual(self.get(f'/api/chat/sessions/{"0"*32}/turns/1/result.json')[0], 404)

    def test_single_search_bibtex_download_contains_entire_saved_reading_list(self):
        job_id = self.jobs.submit({'mode': 'demo', 'scenario': 'followup'})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.jobs.snapshot(job_id)['state'] == 'running':
            time.sleep(.01)
        result = self.jobs.snapshot(job_id)['result']
        self.assertIsNotNone(result)
        status, headers, text = self.get(f'/api/jobs/{job_id}/references.bib')
        self.assertEqual(status, 200)
        self.assertIn('application/x-bibtex', headers['Content-Type'])
        self.assertEqual(text.count('@misc{'), len(result['papers']))
        self.assertEqual(text, bibtex(result))

    def test_recovery_with_an_unreadable_early_turn_preserves_later_identity_and_files(self):
        created = self.chat.create({'mode': 'demo', 'message': 'Recommend five RAG papers.'})
        session_id = created['session_id']
        self.chat.send(session_id, {'mode': 'demo', 'message': 'Add a survey paper.'})
        directory = self.root / 'web' / session_id
        later = directory / 'turn-2' / 'result.json'
        before = later.read_bytes()
        (directory / 'turn-1' / 'result.json').write_text('corrupt fixture', encoding='utf-8')
        self.chat.close()
        self.assertEqual(self.get(f'/api/chat/sessions/{session_id}/turns/1/result.json')[0], 404)
        status, _, body = self.get(f'/api/chat/sessions/{session_id}/turns/2/result.json')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['turn'], 2)
        self.chat.send(session_id, {'mode': 'demo', 'message': 'Keep the request and continue.'})
        self.assertEqual(later.read_bytes(), before)
        self.assertTrue((directory / 'turn-3' / 'result.json').exists())
        self.assertEqual(self.chat.turn_result(session_id, 3)['turn'], 3)


if __name__ == '__main__':
    unittest.main()
