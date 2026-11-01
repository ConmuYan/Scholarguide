"""Offline checks for the Activity story's page-owned translations."""
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DictionaryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_dictionary = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.in_dictionary = tag == "script" and dict(attrs).get("id") == "sg-page-i18n"

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_dictionary = False

    def handle_data(self, data):
        if self.in_dictionary:
            self.parts.append(data)


class ActivityStoryI18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parser = DictionaryParser()
        parser.feed((ROOT / "web/activity.html").read_text(encoding="utf-8"))
        cls.dictionary = json.loads("".join(parser.parts))
        cls.js = (ROOT / "web/activity-story.js").read_text(encoding="utf-8")

    def test_story_label_dictionary_entries_are_trilingual(self):
        labels = re.findall(r':\s*"([^"\n]+)"', self.js.split("const TYPE_LABELS", 1)[0])
        for key in labels:
            if re.search(r"[\u4e00-\u9fff]", key):
                self.assertIn(key, self.dictionary, key)
        for key, item in self.dictionary.items():
            self.assertTrue(item.get("en"), key)
            self.assertTrue(item.get("zh-Hant"), key)

    def test_generated_template_keys_exist(self):
        for key in re.findall(r'\bstoryT\("([^"\n]+)"', self.js):
            self.assertIn(key, self.dictionary, key)

    def test_selection_and_payload_are_not_reset_on_render(self):
        self.assertIn("const recordKey = JSON.stringify([state.selectedKind, state.selected]);", self.js)
        self.assertIn("const selectedKey = sameRecord ? tree.querySelector", self.js)
        self.assertIn("payload: event.payload", self.js)
        self.assertIn('JSON.stringify(span.event.payload || {}, null, 2)', self.js)


if __name__ == "__main__":
    unittest.main()
