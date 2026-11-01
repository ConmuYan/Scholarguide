"""Isolated Sources page language switch regression."""
import json, os, tempfile, threading, unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from scripts.verify_delivery import apply_browser_isolation
ROOT=Path(__file__).resolve().parents[1]
class Handler(SimpleHTTPRequestHandler):
    requests=[]
    def __init__(self,*a,**kw): super().__init__(*a,directory=str(ROOT/'web'),**kw)
    def log_message(self,*a): pass
    def do_GET(self):
        Handler.requests.append(self.path)
        if self.path=='/api/sources': return self.reply({'sources':[{'name':'arxiv','label':'arXiv','enabled':True,'configured':True,'capabilities':['abstract'],'health':{'status':'ok'}}]})
        if self.path=='/api/sources/routing': return self.reply({'order':['arxiv'],'cooling':[]})
        if self.path=='/api/sources/quota': return self.reply({})
        if self.path=='/sources': self.path='/sources.html'
        return super().do_GET()
    def reply(self,obj):
        data=json.dumps(obj).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_POST(self): Handler.requests.append(self.path); self.reply({})
class SourcesI18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();Handler.requests=[]
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        opts=Options();opts.add_argument('--headless=new');opts.add_argument('--no-sandbox');opts.add_argument('--disable-gpu');opts.add_argument('--window-size=1280,900');apply_browser_isolation(opts,Path(cls.tmp.name)/'profile');cls.driver=webdriver.Chrome(options=opts);cls.base=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls): cls.driver.quit();cls.server.shutdown();cls.server.server_close();cls.thread.join(2);cls.tmp.cleanup()
    def test_switch_preserves_filter_and_input_without_requests(self):
        d=self.driver;d.get(self.base+'/sources');WebDriverWait(d,10).until(lambda x:x.find_element('id','source-count').text!='正在加载来源…')
        d.execute_script("document.getElementById('probe-query').value='keep this query'; document.getElementById('capability-filter').querySelector('button')?.click()")
        before=list(Handler.requests)
        for lang,heading in [('en','Let each source play its part.'),('zh-Hant','讓每個來源，各司其職。'),('zh-CN','让每个来源，各司其职。')]:
            d.execute_script('SGI18n.set(arguments[0])',lang);self.assertEqual(d.find_element('css selector','.page-title').text,heading);self.assertEqual(d.find_element('id','probe-query').get_attribute('value'),'keep this query');self.assertEqual(d.find_element('id','sg-language-toggle').get_attribute('data-language'),lang)
        self.assertEqual(Handler.requests,before)
    def test_markers_have_dictionary_entries(self):
        d=self.driver;d.get(self.base+'/sources');bad=d.execute_script("const x=JSON.parse(document.querySelector('#sg-page-i18n').textContent);return [...document.querySelectorAll('[data-i18n],[data-i18n-aria-label],[data-i18n-placeholder]')].flatMap(e=>[...e.attributes].filter(a=>a.name.startsWith('data-i18n') && a.name !== 'data-i18n-params').map(a=>a.value)).filter(k=>!x[k]||!x[k].en||!x[k]['zh-Hant'])");self.assertEqual(bad,[])
if __name__=='__main__': unittest.main()
