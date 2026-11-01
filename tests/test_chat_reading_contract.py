"""Selected-paper reading data must survive the real Chat HTTP boundary."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from activity import ActivityFeed
from common import Paper
from providers import ProviderStore
from webapp import ChatService, Jobs, create_server


class ChatReadingContractTests(unittest.TestCase):
    def result(self):
        paper=Paper('s2:'+'a'*40,'A real-sized reading identifier',
            'We describe the complete abstract. '*80,'https://example.test/paper',
            authors=['Author One','Author Two','Author Three','Author Four'],
            doi='10.1234/example',year=2025).to_dict()
        return {'mode':'demo','method':'A','user_input':'visual learning',
            'gaps':[],'warnings':[],'trace':[],'metrics':{},'stop_reason':'search_budget_exhausted',
            'request':{'topic':'visual learning','target_count':3},
            'status':'partial','papers':[paper],'candidates':[paper], 'assessments':{},
            'research_coverage':{'scope':'landscape','status':'partial','facets':[
                {'id':'masked','label':'Masked image learning','query':'masked image learning'},
                {'id':'contrastive','label':'Contrastive learning','query':'contrastive learning'}],
                'covered':['masked'],'missing':['contrastive'],'assignments':[]},
            'config':{'source':'arxiv'},'queries':['visual learning']}

    def test_public_response_preserves_reading_data_and_copies_coverage(self):
        original=self.result()
        public=ChatService._public(original)
        self.assertEqual(public['papers'][0]['abstract'],original['papers'][0]['abstract'])
        self.assertEqual(public['research_coverage'],original['research_coverage'])
        public['research_coverage']['facets'][0]['label']='changed by caller'
        self.assertNotEqual(public['research_coverage'],original['research_coverage'])
        self.assertNotIn('candidates',public)
        self.assertNotIn('config',public)

    def test_search_import_and_rehydrated_http_snapshot_keep_complete_abstract_and_coverage(self):
        original=self.result()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            providers=ProviderStore(root/'providers.json')
            service=ChatService(root/'web',providers=providers)
            identity=service.import_run(deepcopy(original),'b'*32)
            for restart in (False,True):
                if restart:service=ChatService(root/'web',providers=providers)
                server=create_server(0,jobs=Jobs(root/'runs',providers=providers),
                    chat=service,providers=providers,activity=ActivityFeed(root/'runs'))
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    with urlopen(f'http://127.0.0.1:{server.server_port}/api/chat/sessions/{identity}',timeout=5) as response:
                        public=json.load(response)['history'][0]
                    self.assertEqual(public['papers'][0]['abstract'],original['papers'][0]['abstract'])
                    self.assertEqual(public['papers'][0]['authors'],original['papers'][0]['authors'])
                    self.assertEqual(public['research_coverage'],original['research_coverage'])
                finally:
                    server.shutdown();server.server_close();thread.join(2);service.close()


if __name__=='__main__':unittest.main()
