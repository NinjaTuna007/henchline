import io
import json
import unittest
from unittest import mock

from voice.desktop import cursor_cdp

CHAT = "1e6e96a3-90f8-476a-9fe9-d2be71fc2053"


def listing(*titles: str, kind: str = "page"):
    targets = [{"type": kind, "title": t, "webSocketDebuggerUrl": f"ws://127.0.0.1/{i}"} for i, t in enumerate(titles)]
    return mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(targets).encode()))


class WindowByTitle(unittest.TestCase):
    """Cursor's debug targets are picked by window title, as its X11 windows are: exactly one, or none."""

    def test_the_workspace_window(self):
        with listing("Cursor", "Cursor Settings - robotproj - Cursor"):
            self.assertEqual(cursor_cdp.page_of("robotproj"), "ws://127.0.0.1/1")

    def test_bare_title(self):
        with listing("robotproj - Cursor"):
            self.assertEqual(cursor_cdp.page_of("robotproj"), "ws://127.0.0.1/0")

    def test_not_a_suffix_of_another_name(self):
        with listing("x - notrobotproj - Cursor"):
            self.assertEqual(cursor_cdp.page_of("robotproj"), "")

    def test_two_windows_is_none(self):
        with listing("a - robotproj - Cursor", "b - robotproj - Cursor"):
            self.assertEqual(cursor_cdp.page_of("robotproj"), "")

    def test_workers_are_not_windows(self):
        with listing("robotproj - Cursor", kind="worker"):
            self.assertEqual(cursor_cdp.page_of("robotproj"), "")

    def test_no_port(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            self.assertFalse(cursor_cdp.available("robotproj"))


class SameWords(unittest.TestCase):
    """The box reads paragraphs back as blank lines and may hold non-breaking spaces."""

    def test_paragraphs(self):
        self.assertTrue(cursor_cdp.same_words("one\n\ntwo\n", "one\ntwo"))

    def test_nbsp(self):
        self.assertTrue(cursor_cdp.same_words("a\u00a0b", "a b"))

    def test_different(self):
        self.assertFalse(cursor_cdp.same_words("draft hi", "hi"))


class Refusals(unittest.TestCase):
    def test_not_a_chat_id(self):
        self.assertEqual(cursor_cdp.send("robotproj", "x'] ; alert(1)", "hi"), "not a chat id")

    def test_no_window(self):
        with mock.patch.object(cursor_cdp, "page_of", return_value=""):
            self.assertIn("debugging port", cursor_cdp.send("robotproj", CHAT, "hi"))

    def test_selector_holds_only_the_id(self):
        self.assertIn(f'[data-composer-id="{CHAT}"]', cursor_cdp._box(CHAT))


if __name__ == "__main__":
    unittest.main()
