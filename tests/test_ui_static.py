"""Static UI checks: tokens, ids, accessibility hooks, and local assets."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'


def read(name):
    return (WEB / name).read_text(encoding='utf-8')


class UiStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = read('index.html')
        cls.chat = read('chat.html')
        cls.css = read('style.css')
        cls.app = read('app.js')
        cls.chatjs = read('chat.js')

    def test_shared_design_tokens(self):
        for token in ('--bg:', '--side:', '--card:', '--ink:', '--brick:', '--line:',
                      '--serif:', '--sans:'):
            self.assertIn(token, self.css)
        for page in (self.index, self.chat):
            self.assertIn('href="/style.css"', page)

    def test_required_ids_exist(self):
        index_ids = ['search-form', 'form-fields', 'topic', 'request', 'scenario', 'method',
                     'source', 'cache', 'submit-button', 'submit-text', 'form-error',
                     'trace-list', 'trace-count', 'trace-placeholder', 'result-count',
                     'downloads', 'download-md', 'download-json', 'empty-state', 'try-demo',
                     'running-state', 'running-title', 'running-detail', 'elapsed',
                     'stagebar', 'reconnect', 'retry-poll', 'result-section', 'outcome',
                     'metrics', 'gap-section', 'warning-section', 'paper-list',
                     'live-tab', 'demo-tab', 'toggle-input', 'guided-fields',
                     'natural-fields', 'category', 'minimum', 'venue', 'year-from', 'year-to',
                     'reading-goal', 'model-label',
                     'coverage-status', 'source-filter', 'retry-search', 'form-error-row']
        for item in index_ids:
            self.assertIn(f'id="{item}"', self.index, item)
        self.assertNotIn('id="reuse"', self.chat)
        self.assertNotIn('id="target-count"', self.index)
        chat_ids = ['composer', 'message', 'mode', 'scenario', 'send',
                    'new-session', 'status', 'question', 'question-text', 'turns',
                    'history', 'session-id', 'copy-session', 'running', 'running-text',
                    'running-time', 'empty-chat']
        for item in chat_ids:
            self.assertIn(f'id="{item}"', self.chat, item)

    def test_source_and_activity_pages_have_operable_hooks(self):
        sources = read('sources.html')
        sources_js = read('sources.js')
        for item in ('sources-list', 'source-form', 'source-name', 'source-key',
                     'source-editor-panel', 'lune-tools', 'catalog-content', 'skills-list',
                     'probe-query', 'routing-note', 'source-summary'):
            self.assertIn(f'id="{item}"', sources, item)
        self.assertIn('"source-row"', sources_js)
        self.assertIn('/api/sources/', sources_js)
        self.assertIn('source.requires_key', sources_js)
        activity = read('activity.html')
        story = read('activity-story.js')
        self.assertNotIn('STATEFUL SESSION', activity + story)
        self.assertIn('mergeOperations', story)
        self.assertIn('span_id', story)
        self.assertIn('activity-trace-tree', activity)

    def test_no_external_or_inline_assets(self):
        for page in (self.index, self.chat):
            self.assertFalse(re.search(r'<(?:script|link|img)[^>]+(?:src|href)="https?://', page))
            self.assertNotIn('style="', page)
        self.assertNotIn('@import', self.css)
        self.assertNotIn('url(http', self.css)

    def test_accessibility_hooks(self):
        for page in (self.index, self.chat):
            self.assertIn('aria-live', page)
            self.assertIn('lang="zh-CN"', page)
            self.assertIn('name="viewport"', page)
        for marker in ('prefers-reduced-motion', 'focus-visible',
                       '@media (max-width: 1150px)', '@media (max-width: 920px)',
                       '@media (max-width: 740px)', '@media (max-width: 430px)'):
            self.assertIn(marker, self.css)
        self.assertIn('for="topic"', self.index)
        self.assertIn('for="message"', self.chat)

    def test_default_category_does_not_force_benchmark(self):
        self.assertIn('value="none" selected', self.index)
        self.assertIn('id="reading-goal"', self.index)
        self.assertIn("reading_goal: $('reading-goal').value", self.app)
        self.assertIn("event.action === 'intent'", self.app)

    def test_provenance_hooks(self):
        for script in (self.app, self.chatjs):
            self.assertIn('SOURCE_LABELS', script)
            self.assertIn('open_access_url', script)
            self.assertIn('paper-provenance', script)
        self.assertIn('.source-badge', self.css)

    def test_theme_hooks(self):
        for page in (self.index, self.chat, read('activity.html')):
            self.assertIn('/theme.js', page)
        theme = read('theme.js')
        self.assertIn('scholarguide-theme', theme)
        self.assertIn('prefers-color-scheme', theme)
        self.assertIn('data-theme="dark"', self.css)

    def test_interaction_hooks(self):
        for snippet in ('stagebar', 'copyText', 'is-clamped'):
            self.assertIn(snippet, self.app)
        for snippet in ('history', 'copy-session', 'running-time', 'turn-note', 'is-clamped',
                        'evidence-missing', 'copyQuote', 'badge'):
            self.assertIn(snippet, self.chatjs)
        self.assertIn('clipboard', self.app + self.chatjs)

    def test_text_contrast_tokens(self):
        def luminance(value):
            value = value.lstrip('#')
            channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
        def ratio(a, b):
            la, lb = luminance(a), luminance(b)
            return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
        for token in ('#636a61', '#697068', '#a44a36'):
            self.assertGreaterEqual(ratio(token, '#f5f3ee'), 4.5, token)


if __name__ == '__main__':
    unittest.main()
