"""Static contract for the ChatGPT sign-in entry in the settings UI.

The page must offer a third-party sign-in card, localize every new string in
zh / en / zh-Hant, wait on the server state without ever handling tokens, and
poll the documented status endpoint. No browser or network is used.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'
SETTINGS = (WEB / 'settings.html').read_text(encoding='utf-8')
SETTINGS_JS = (WEB / 'settings.js').read_text(encoding='utf-8')
CSS = (WEB / 'style.css').read_text(encoding='utf-8')

OAUTH_SECTION = SETTINGS[SETTINGS.index('id="settings-oauth"'):SETTINGS.index('id="provider-list"')]


class MarkerParser(HTMLParser):
    """Collect every data-i18n* marker declared by the page (except params)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.markers = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name.startswith('data-i18n') and name != 'data-i18n-params' and value:
                self.markers.append(value)


class WebOAuthUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dictionary = json.loads(re.search(
            r'<script type="application/json" id="sg-page-i18n">(.*?)</script>',
            SETTINGS, re.S).group(1))

    def test_sign_in_section_has_labelled_controls_and_live_status(self):
        for marker in ('id="settings-oauth"', 'id="oauth-form"', 'id="oauth-model"',
                       'id="oauth-proxy"', 'id="oauth-login"', 'id="oauth-status"',
                       'id="oauth-badge"', 'id="oauth-connected"'):
            self.assertIn(marker, OAUTH_SECTION, marker)
        self.assertIn('for="oauth-model"', OAUTH_SECTION)
        self.assertIn('for="oauth-proxy"', OAUTH_SECTION)
        self.assertIn('aria-describedby="oauth-model-help"', OAUTH_SECTION)
        self.assertIn('role="status"', OAUTH_SECTION)
        self.assertIn('aria-live="polite"', OAUTH_SECTION)
        self.assertIn('type="checkbox"', OAUTH_SECTION)
        self.assertIn('>登录 ChatGPT<', OAUTH_SECTION)

    def test_page_never_asks_for_tokens_or_codes(self):
        self.assertNotIn('type="password"', OAUTH_SECTION)
        self.assertIsNone(re.search(
            r'<(?:input|textarea)[^>]*(?:token|otp|one[-_ ]?time|verification|code)',
            OAUTH_SECTION, re.I))
        for secret in ('access_token', 'refresh_token', 'id_token', 'authorization_code'):
            self.assertNotIn(secret, SETTINGS_JS)

    def test_every_new_string_is_localized_in_three_languages(self):
        parser = MarkerParser()
        parser.feed(SETTINGS)
        self.assertGreater(len(parser.markers), 0)
        for key in parser.markers:
            entry = self.dictionary.get(key)
            self.assertIsInstance(entry, dict, key)
            for language in ('en', 'zh-Hant'):
                self.assertIsInstance(entry.get(language), str, (key, language))
                self.assertTrue(entry[language].strip(), (key, language))
        # Keys produced by settings.js also need dictionary entries.
        for key in ('正在等待授权', '已连接', '登录失败', '未连接', '等待授权…',
                    '正在等待浏览器完成授权…', '登录未完成：{message}', 'ChatGPT 账号已连接。',
                    '已通过 ChatGPT 授权，令牌保存在系统钥匙串。', '钥匙串令牌',
                    '正在登录', 'ChatGPT 登录'):
            entry = self.dictionary.get(key)
            self.assertIsInstance(entry, dict, key)
            self.assertTrue(entry.get('en'), key)
            self.assertTrue(entry.get('zh-Hant'), key)
        # Placeholder parameters stay identical across languages.
        for key in ('已连接模型：{model}', '登录未完成：{message}'):
            if key not in self.dictionary:
                continue
            for value in self.dictionary[key].values():
                self.assertEqual(set(re.findall(r'\{\w+\}', key)),
                                 set(re.findall(r'\{\w+\}', value)), key)

    def test_settings_script_polls_and_handles_conflict(self):
        self.assertIn('/api/providers/oauth/openai', SETTINGS_JS)
        self.assertIn('/api/providers/oauth/openai/login', SETTINGS_JS)
        self.assertIn('use_system_proxy', SETTINGS_JS)
        self.assertIn('setTimeout', SETTINGS_JS)
        self.assertIn('startOAuthPolling', SETTINGS_JS)
        self.assertIn('stopOAuthPolling', SETTINGS_JS)
        self.assertIn('error.status === 409', SETTINGS_JS)
        self.assertIn('aria-busy', SETTINGS_JS)
        self.assertIn('dataset.i18n', SETTINGS_JS)
        # The OAuth profile gets a recognizable, non-network mark in the list.
        self.assertIn('provider-mark', SETTINGS_JS)
        self.assertIn('/assets/logos/chatgpt-logo.svg', SETTINGS_JS)
        self.assertIn('/assets/logos/chatgpt-logo.svg', OAUTH_SECTION)
        self.assertNotIn('>GPT</span>', OAUTH_SECTION)
        self.assertIn('openai-oauth', SETTINGS_JS)
        self.assertIsNone(re.search(r'<img|https?://', SETTINGS_JS))

    def test_oauth_styles_need_no_new_dependencies(self):
        self.assertIn('.settings-oauth', CSS)
        self.assertIn('grid-column: 1 / -1', CSS)
        self.assertIn('[data-oauth-state="running"]', CSS)
        self.assertIn('.oauth-status[data-kind="error"]', CSS)
        self.assertIn('.provider-mark', CSS)
        self.assertNotIn('@import', CSS)
        self.assertNotIn('url(http', CSS)
        # The provider/sign-in grid must collapse on narrow screens.
        narrow = [chunk for chunk in CSS.split('@media')[1:]
                  if chunk.lstrip().startswith(('(max-width: 740px)', '(max-width: 920px)'))]
        self.assertTrue(any('.settings-oauth' in chunk for chunk in narrow))


if __name__ == '__main__':
    unittest.main()
