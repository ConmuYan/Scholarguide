"""Offline browser regressions and static localization coverage for chat."""
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from test_chat_draft_safety import ChatDraftSafetyTests, FixtureHandler, WebDriverWait


WEB = Path(__file__).resolve().parents[1] / 'web'
CHAT_HTML = (WEB / 'chat.html').read_text(encoding='utf-8')


class ChatStaticI18nParser(HTMLParser):
    """Find untranslated application-owned Chinese text and attributes."""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
            'link', 'meta', 'param', 'source', 'track', 'wbr'}
    ATTRIBUTES = ('aria-label', 'alt', 'placeholder', 'title')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.markers = []
        self.missing = []

    @staticmethod
    def chinese(value):
        return bool(re.search(r'[\u3400-\u9fff]', value or ''))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        skip = tag in {'script', 'style', 'svg'} or bool(self.stack and self.stack[-1][2])
        if not skip:
            if self.stack and 'data-i18n' in self.stack[-1][1]:
                self.missing.append((self.getpos()[0], 'non-leaf marker', self.stack[-1][1]['data-i18n']))
            if 'data-i18n' in attrs:
                self.markers.append(attrs['data-i18n'])
            for name in self.ATTRIBUTES:
                marker = 'data-i18n-' + name
                if marker in attrs:
                    self.markers.append(attrs[marker])
                    if attrs.get(name) != attrs[marker]:
                        self.missing.append((self.getpos()[0], 'attribute key mismatch', name))
                elif self.chinese(attrs.get(name)):
                    self.missing.append((self.getpos()[0], name, attrs[name]))
        if tag not in self.VOID:
            self.stack.append((tag, attrs, skip))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack or self.stack[-1][2] or not self.chinese(data):
            return
        _, attrs, _ = self.stack[-1]
        if 'data-i18n' not in attrs:
            self.missing.append((self.getpos()[0], 'text', data.strip()))
        elif data != attrs['data-i18n']:
            self.missing.append((self.getpos()[0], 'text key mismatch', data.strip()))


class ChatStaticI18nTests(unittest.TestCase):
    def test_all_static_chinese_is_marked_and_translated(self):
        parser = ChatStaticI18nParser()
        parser.feed(CHAT_HTML)
        self.assertEqual([], parser.missing, '\n' + '\n'.join(
            f'chat.html:{line} [{kind}] {text}' for line, kind, text in parser.missing))
        dictionary = json.loads(re.search(
            r'<script type="application/json" id="sg-page-i18n">(.*?)</script>',
            CHAT_HTML, re.S).group(1))
        for key in parser.markers:
            self.assertIn(key, dictionary, key)
            self.assertTrue(dictionary[key].get('en'), key)
            self.assertTrue(dictionary[key].get('zh-Hant'), key)


class ChatI18nTests(ChatDraftSafetyTests):
    def test_switch_preserves_draft_result_focus_and_requests(self):
        driver = self.driver
        driver.execute_script("""
          setDraft('草稿 draft');
          document.getElementById('message').focus();
          window.i18nResult = document.querySelector('#turn-1');
        """)
        before = len(FixtureHandler.posts)
        for lang, title, send, keyboard_help, page_label, mode_labels, scenario_labels in [
            ('en', 'Follow the evidence, further.', 'Send',
             'Enter to send · Shift+Enter for a new line · IME confirmation does not send', 'CHAT',
             ('Offline RAG demo', 'Live search'), ('Follow-up', 'Clarify')),
            ('zh-Hant', '沿著證據，繼續。', '傳送',
             'Enter 傳送 · Shift+Enter 換行 · 中文輸入確認不會傳送', '對話',
             ('離線 RAG 示範', '即時檢索'), ('後續追問', '釐清問題')),
            ('zh-CN', '沿着证据，继续。', '发送',
             'Enter 发送 · Shift+Enter 换行 · 中文输入确认不会发送', '对话',
             ('离线 RAG 演示', '实时检索'), ('后续追问', '澄清问题')),
        ]:
            driver.execute_script('window.SGI18n.set(arguments[0])', lang)
            self.assertEqual(driver.find_element('css selector', '.page-title').text, title)
            self.assertEqual(driver.find_element('css selector', '.page-label').text, page_label)
            self.assertEqual(driver.find_element('id', 'send').text, send)
            self.assertEqual(driver.find_element('id', 'composer-keyboard-help').text, keyboard_help)
            self.assertEqual(tuple(driver.find_element('css selector', f'#mode option[value="{value}"]').text
                                   for value in ('demo', 'live')), mode_labels)
            self.assertEqual(tuple(driver.find_element('css selector', f'#scenario option[value="{value}"]').text
                                   for value in ('followup', 'ask')), scenario_labels)
            self.assertEqual(driver.find_element('id', 'message').get_attribute('value'), '草稿 draft')
            self.assertTrue(driver.execute_script("return document.querySelector('#turn-1') === window.i18nResult"))
            self.assertEqual(driver.execute_script('return document.activeElement.id'), 'message')
        self.assertEqual(len(FixtureHandler.posts), before)

    def test_dynamic_controls_and_delete_confirmation_without_rebuilding(self):
        driver = self.driver
        driver.execute_script("""
          state.title = '用户自定义标题'; syncSessionTitle();
          renderSaved([{session_id:'keep-id', title:'用户会话', status:'active', turns:1}]);
          document.querySelector('.turn-trace').dataset.loaded = '1';
          document.querySelector('.turn-trace').open = true;
          document.getElementById('session-title-form').hidden = false;
          document.getElementById('session-title-input').value = '未保存标题';
          document.getElementById('session-title-input').focus();
          window.savedNode = document.querySelector('.saved-item');
          window.confirmText = '';
          window.confirm = text => { window.confirmText = text; return false; };
          window.i18nFetches = 0;
          window.originalFetch = window.fetch;
          window.fetch = (...args) => { window.i18nFetches++; return window.originalFetch(...args); };
        """)
        try:
            driver.execute_script("window.SGI18n.set('en')")
            self.assertEqual(driver.find_element('css selector', '.saved-delete').text, 'Delete')
            self.assertEqual(driver.find_element('css selector', '.turn-retry').text, 'Retry this turn')
            self.assertEqual(driver.find_element('id', 'session-title').text, '用户自定义标题')
            self.assertEqual(driver.find_element('id', 'session-title-input').get_attribute('value'), '未保存标题')
            self.assertTrue(driver.execute_script("return document.querySelector('.turn-trace').open && document.querySelector('.saved-item') === window.savedNode"))
            self.assertEqual(driver.execute_script('return document.activeElement.id'), 'session-title-input')
            self.assertEqual(driver.execute_script('return window.i18nFetches'), 0)
            driver.execute_script("document.querySelector('.saved-delete').click()")
            self.assertIn('Remote data, backups', driver.execute_script('return window.confirmText'))
            self.assertEqual(driver.execute_script('return window.i18nFetches'), 0)
            driver.execute_script("window.SGI18n.set('zh-Hant')")
            self.assertEqual(driver.find_element('css selector', '.saved-delete').text, '刪除')
        finally:
            driver.execute_script('window.fetch = window.originalFetch')
