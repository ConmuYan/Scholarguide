"""Offline checks for landing-page translations and animation integration."""
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'web' / 'landing.html').read_text(encoding='utf-8')
JS = (ROOT / 'web' / 'landing.js').read_text(encoding='utf-8')
CSS = (ROOT / 'web' / 'landing-demo.css').read_text(encoding='utf-8')
THEME = (ROOT / 'web' / 'theme.js').read_text(encoding='utf-8')

# theme.js does not support translating meta content; do not hide other metadata.
META_DESCRIPTION = 'ScholarGuide 是围绕论文证据工作的研究 Agent：从研究问题出发，规划多源检索、依据摘要筛选、综合研究，并在 Chat 中继续迭代。'


class MissingChineseMarkers(HTMLParser):
    """Audit source text, including illustrative examples and initial dynamic text."""
    void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
            'link', 'meta', 'param', 'source', 'track', 'wbr'}
    attributes = ('aria-label', 'alt', 'placeholder', 'title')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.missing = []
        self.markers = []

    @staticmethod
    def chinese(value):
        return bool(re.search(r'[\u3400-\u9fff]', value or ''))

    def problem(self, kind, text):
        self.missing.append((self.getpos()[0], kind, text))

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        skip = tag in {'script', 'style', 'svg'} or bool(self.stack and self.stack[-1][2])
        if not skip:
            if self.stack and 'data-i18n' in self.stack[-1][1]:
                self.problem('non-leaf marker', self.stack[-1][1]['data-i18n'])
            if 'data-i18n' in attrs:
                self.markers.append(attrs['data-i18n'])
            for name in self.attributes:
                marker = 'data-i18n-' + name
                if marker in attrs:
                    self.markers.append(attrs[marker])
                    if attrs.get(name) != attrs[marker]:
                        self.problem('attribute key mismatch', name)
                elif self.chinese(attrs.get(name)):
                    self.problem(name, attrs[name])
            if tag == 'meta' and self.chinese(attrs.get('content')):
                if not (attrs.get('name') == 'description' and attrs['content'] == META_DESCRIPTION):
                    self.problem('meta content', attrs['content'])
        if tag not in self.void:
            self.stack.append((tag, attrs, skip))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, text):
        if not self.stack or self.stack[-1][2] or not self.chinese(text):
            return
        _, attrs, _ = self.stack[-1]
        if 'data-i18n' not in attrs:
            self.problem('text', text.strip())
        elif text != attrs['data-i18n']:
            self.problem('text key mismatch', text.strip())


def dictionary():
    match = re.search(r'<script type="application/json" id="sg-page-i18n">(.*?)</script>', HTML, re.S)
    if not match:
        raise AssertionError('Missing page dictionary')
    return json.loads(match.group(1))


class LandingI18nTests(unittest.TestCase):
    def test_no_unmarked_chinese_ui(self):
        audit = MissingChineseMarkers()
        audit.feed(HTML)
        self.assertEqual([], audit.missing, '\n' + '\n'.join(
            f'landing.html:{line} [{kind}] {text}' for line, kind, text in audit.missing))
        entries = dictionary()
        for key in audit.markers:
            self.assertIn(key, entries, f'Missing translation for marker: {key}')

    def test_audit_does_not_ignore_examples_or_dynamic_nodes(self):
        audit = MissingChineseMarkers()
        audit.feed('<script type="application/json">{"样例":"来源"}</script>'
                   '<span id="film-input">虚拟样例</span><img alt="来源">'
                   '<p data-i18n="错">中文 <b>强调</b></p>')
        kinds = [item[1] for item in audit.missing]
        self.assertIn('text', kinds)
        self.assertIn('alt', kinds)
        self.assertIn('text key mismatch', kinds)
        self.assertIn('non-leaf marker', kinds)
        self.assertTrue(any(item[2] == '虚拟样例' for item in audit.missing))
        self.assertFalse(any(item[2] == '样例' for item in audit.missing))

    def test_meta_description_is_marked_for_translation(self):
        # The meta description is translated through the same explicit marker.
        self.assertIn('"content"', re.search(r'const attributes = \[(.*?)\]', THEME).group(1))
        audit = MissingChineseMarkers()
        audit.feed(f'<meta name="description" data-i18n-content="{META_DESCRIPTION}" content="{META_DESCRIPTION}">'
                   '<meta name="description" content="其他描述">')
        self.assertEqual(1, len(audit.missing))
        self.assertEqual('其他描述', audit.missing[0][2])

    def test_dictionary_is_valid_and_precedes_deferred_scripts(self):
        match = re.search(r'<script type="application/json" id="sg-page-i18n">(.*?)</script>', HTML, re.S)
        self.assertIsNotNone(match)
        self.assertLess(match.start(), HTML.index('src="/theme.js"'))
        entries = dictionary()
        for key, values in entries.items():
            self.assertTrue(key)
            self.assertTrue(values['en'].strip(), key)
            self.assertTrue(values['zh-Hant'].strip(), key)
        for key in ('暂停操作演示', '继续播放操作演示', '继续追问，收敛研究方向…'):
            self.assertIn(key, entries)

    def test_animation_uses_translations_without_resetting_position(self):
        self.assertIn('typed(t(query)', JS)
        self.assertIn('typed(t(followup)', JS)
        self.assertIn('text(caption, t(story[2]))', JS)
        self.assertIn('geometry.clear(); render();', JS)
        self.assertIn('content: attr(data-placeholder)', CSS)


if __name__ == '__main__':
    unittest.main()
