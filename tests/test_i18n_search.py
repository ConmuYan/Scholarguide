import json
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SearchI18nTest(unittest.TestCase):
    def test_page_includes_shared_dictionary(self):
        html = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        page = json.loads(re.search(r'<script type="application/json" id="sg-page-i18n">(.*?)</script>', html, re.S).group(1))
        shared = json.loads((ROOT / 'web/i18n-shared.json').read_text(encoding='utf-8'))
        self.assertTrue(shared)
        for key, value in shared.items():
            self.assertEqual(page[key], value, key)
        for key, value in page.items():
            self.assertIsInstance(value, dict, key)
            for locale in ('en', 'zh-Hant'):
                self.assertIsInstance(value.get(locale), str, key)
                self.assertTrue(value[locale], (key, locale))
        self.assertIn('data-i18n="发现论文"', html)
        self.assertIn('data-i18n-aria-label="运行模式"', html)
        audit = subprocess.run(
            ['python', '-X', 'utf8', 'scripts/i18n_static_audit.py'],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        self.assertRegex(audit.stdout, r'(?m)^index\.html 0$')

    def test_search_added_keys_and_validation(self):
        html = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        page = json.loads(re.search(r'<script type="application/json" id="sg-page-i18n">(.*?)</script>', html, re.S).group(1))
        for key in re.findall(r'data-i18n(?:-placeholder|-aria-label)?="([^"]+)"', html):
            self.assertIn(key, page)
            self.assertTrue(page[key]['en'], key)
            self.assertTrue(page[key]['zh-Hant'], key)
        app = (ROOT / 'web/app.js').read_text(encoding='utf-8')
        self.assertIn('const error = new Error(t(message));', app)
        for key in re.findall(r"inputError\('([^']+)'", app):
            self.assertIn(key, page)
        handler = app.split("window.addEventListener('sg:language-change', () => {", 1)[1].split("\n});", 1)[0]
        for forbidden in ('fetch(', 'api(', 'requestSubmit(', 'renderResult(', 'applySourceFilter('):
            self.assertNotIn(forbidden, handler)
        self.assertIn('syncForm();', handler)
        self.assertIn('t(stageNames[latest])', app)
        self.assertIn("node.querySelector('.stage-label, :scope > span')", app)
        self.assertIn('t(stageNames[key])', app)
        for stage in ('理解与规划', '检索论文', '核对证据', '整理与交付'):
            self.assertIn(stage, page)

    def test_native_select_options_translate_without_changing_values(self):
        html = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        for option in soup.select('select option'):
            if any('\u4e00' <= char <= '\u9fff' for char in option.get_text()):
                self.assertTrue(option.has_attr('data-i18n'), option.get_text())
        minimum = soup.select('#minimum option')
        self.assertEqual([option['value'] for option in minimum], [str(n) for n in range(1, 11)])
        self.assertEqual([option['value'] for option in soup.select('#venue-scope option')],
                         ['any', 'preferred', 'preferred_only'])
        self.assertEqual([option['value'] for option in soup.select('#source option')],
                         ['multi', 'arxiv', 'openalex', 'semantic_scholar', 'dblp',
                          'openreview', 'acl_anthology', 'deepseek', 'lune'])

    def test_trace_labels_and_payload_are_isolated(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const events = {};
global.window = {addEventListener: (name, cb) => events[name] = cb};
global.document = {addEventListener: () => {}, querySelectorAll: () => []};
let lang = 'zh';
window.SGI18n = {t: (key, params = {}) => {
  let value = lang === 'en' ? ({'检索论文':'Search papers', '正在{label}':'Running {label}'})[key] || key : key;
  return value.replace(/\{(\w+)\}/g, (_, name) => params[name] ?? _);
}};
vm.runInThisContext(fs.readFileSync('web/trace.js', 'utf8'));
const trace = window.ScholarGuideTrace;
assert.strictEqual(trace.label({action:'search'}), '检索论文');
lang = 'en';
assert.strictEqual(trace.label({action:'search'}), 'Search papers');
assert.strictEqual(trace.label({action:'unknown',payload:{label:'检索论文'}}), '检索论文');
assert.strictEqual(trace.quantity('推荐 5 篇').count, 5);
assert.ok(events['sg:language-change']);
'''
        subprocess.run(['node', '-e', script], cwd=ROOT, check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
