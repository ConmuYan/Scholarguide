"""Offline checks for shared research/trace translations (no browser or network)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'


class SharedI18n(unittest.TestCase):
    def test_dictionary_and_placeholders(self):
        import re
        data = json.loads((WEB / 'i18n-shared.json').read_text(encoding='utf-8'))
        for key, translations in data.items():
            self.assertEqual(set(translations), {'en', 'zh-Hant'}, key)
            for value in translations.values():
                self.assertTrue(value, key)
                self.assertEqual(set(re.findall(r'\{\w+\}', key)), set(re.findall(r'\{\w+\}', value)), key)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_live_update_preserves_details_and_payload(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const root = process.argv[1], dictionary = JSON.parse(fs.readFileSync(root + '/web/i18n-shared.json', 'utf8'));
let lang = 'zh-CN', handlers = {};
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.dataset={}; this.attrs={}; this.className=''; this._text=''; this.open=false; }
  set textContent(v) { this._text=String(v); this.children=[]; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...xs) { this.children.push(...xs); }
  setAttribute(k,v) { this.attrs[k]=v; }
  addEventListener() {}
  querySelector() { return null; }
}
const body = new Element('body');
const all = n => [n, ...n.children.flatMap(all)];
global.document = {body, createElement: tag=>new Element(tag), createTextNode: s=>{const n=new Element('#text'); n.textContent=s; return n;},
  querySelectorAll: selector => all(body).filter(n => selector.includes('sg-research') ? ('sgResearchTranslation' in n.dataset || 'sgResearchAria' in n.dataset) : ('sgTraceTranslation' in n.dataset || 'sgTraceAria' in n.dataset)),
  addEventListener(){}, querySelector(){return null;}};
global.window = {addEventListener:(name,fn)=>(handlers[name] ||= []).push(fn), SGI18n:{t:(key,params={}) => (lang==='zh-CN' ? key : dictionary[key]?.[lang] || key).replace(/\{(\w+)\}/g, (_,k)=>params[k] ?? `{${k}}`)}};
for (const file of ['research.js','trace.js']) vm.runInThisContext(fs.readFileSync(root+'/web/'+file,'utf8'));
const paper={id:'p',title:'中文原始标题',abstract:'中文原始摘要',authors:['原作者']};
const brief=window.ScholarGuideResearch.render({status:'ready',source_level:'abstract_only',paper_notes:[{paper_id:'p',contribution:{text:'模型原文',evidence:[{paper_id:'p',quote:'原始引文'}]}}],gaps:[{section:'themes'}]}, {papers:[paper]});
const details=window.ScholarGuideResearch.paperDetails(paper);
const timeline=window.ScholarGuideTrace.render([{action:'search',payload:{query:'原始查询',returned:2}}], {className:'custom'});
body.append(brief,details,timeline);
const evidence=all(brief).find(n=>n.className==='research-evidence'); evidence.open=true;
const traceDetails=all(timeline).find(n=>n.className==='sg-trace-details'); traceDetails.open=true;
const pre=all(timeline).find(n=>n.tagName==='pre'), raw=pre.textContent;
const before=all(brief).find(n=>n.tagName==='summary');
const fire=()=>handlers['sg:language-change'].forEach(fn=>fn());
lang='en'; fire();
assert(before.textContent.includes('abstract evidence')); assert(evidence.open && traceDetails.open);
assert.strictEqual(pre.textContent, raw); assert(brief.textContent.includes('中文原始标题'));
assert(brief.textContent.includes('模型原文') && brief.textContent.includes('原始引文'));
assert(details.textContent.includes('中文原始摘要') && details.textContent.includes('Authors: 原作者'));
assert(timeline.textContent.includes('Searching papers') && timeline.textContent.includes('原始查询'));
lang='zh-Hant'; fire(); assert(before.textContent.includes('摘要依據')); assert(evidence.open && traceDetails.open);
lang='zh-CN'; fire(); assert(before.textContent.includes('查看摘要依据'));
'''
        subprocess.run(['node', '-e', script, str(ROOT).replace('\\', '/')], check=True, timeout=20)


if __name__ == '__main__':
    unittest.main()
