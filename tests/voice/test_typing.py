"""Words typed into agents carry no control characters: an ESC[201~ would end a bracketed paste early, and a
carriage return typed as a key would send the line before the rest arrives. Every typing path strips them."""

import unittest
from unittest import mock

from tests import sandbox  # first: a throwaway HOME
from voice.desktop import Desktop, Tab, typeable

assert sandbox.HOME

ATTACK = "look at this\x1b[201~\r!rm -rf ~\r\x9b2J\x07done"


def clean(text: str) -> bool:
    return not any(ord(c) < 32 and c not in "\n\t" or 0x7f <= ord(c) <= 0x9f for c in text)


class Typeable(unittest.TestCase):
    def test_controls_go_and_carriage_returns_become_newlines(self):
        self.assertEqual(typeable(ATTACK), "look at this[201~\n!rm -rf ~\n2Jdone")
        self.assertEqual(typeable("a\r\nb\rc\nd"), "a\nb\nc\nd")

    def test_tabs_stay_in_a_paste_and_become_spaces_as_plain_keys(self):
        self.assertEqual(typeable("a\tb"), "a\tb")
        self.assertEqual(typeable("a\tb\r", raw=True), "a b\n")

    def test_ordinary_words_are_untouched(self):
        words = "Zażółć gęślą jaźń — “quotes”, emoji 🙂, a\nsecond line"
        self.assertEqual(typeable(words), words)


class EachPath(unittest.TestCase):
    def test_tmux_pastes_no_escape(self):
        from voice.desktop import tmux
        calls = []

        def run(*args, given=None, **_kwargs):
            calls.append((args, given))
            return mock.Mock(returncode=0, stdout="")
        with mock.patch.object(tmux, "tmux", side_effect=run), mock.patch.object(tmux.time, "sleep"):
            self.assertTrue(tmux.WithTmux(mock.Mock()).type_line(Tab(tmux.OWNER, "%1"), ATTACK))
        loaded = next(given for args, given in calls if args[0] == "load-buffer")
        self.assertTrue(clean(loaded))
        self.assertNotIn("\r", loaded)
        self.assertIn("[201~", loaded)  # the words stay; only the ESC that made them a key sequence goes

    def test_cursor_cli_paste_has_no_escape(self):
        from voice import cursor_cli
        from voice.desktop import tmux
        calls = []

        def run(*args, given=None, **_kwargs):
            calls.append((args, given))
            return mock.Mock(returncode=0, stdout="")
        with mock.patch.object(tmux, "tmux", side_effect=run):
            self.assertTrue(cursor_cli._paste("%1", ATTACK))
        self.assertTrue(clean(next(given for args, given in calls if args[0] == "load-buffer")))

    def test_konsole_gets_no_carriage_return_inside_the_words(self):
        from voice.desktop import kde_x11
        sent = []
        with mock.patch.object(kde_x11, "qdbus_ok", side_effect=lambda *a: sent.append(a[3]) or True), \
                mock.patch.object(kde_x11.time, "sleep"):
            self.assertTrue(kde_x11.Konsole(mock.Mock()).type_line(Tab("org.kde.konsole", "/Sessions/1"), ATTACK))
        words, enter = sent
        self.assertTrue(clean(words))
        self.assertNotIn("\r", words)
        self.assertNotIn("\t", words)
        self.assertEqual(enter, "\r")  # the one Enter, after the words

    def test_a_clipboard_paste_carries_no_escape(self):
        class Fake(Desktop):
            def __init__(self):
                self.clip = None

            def focused(self): ...
            def windows(self, app): return []

            def set_clipboard(self, text):
                self.clip = text
                return True

            def press(self, *keys): ...
            def raise_window(self, window_id): ...
            def is_front(self, window_id): return True
            def locked(self): return False
        fake = Fake()
        with mock.patch("voice.desktop.time.sleep"):
            self.assertTrue(fake.paste_into("0x1", ATTACK, ("ctrl", "v")))
        self.assertTrue(clean(fake.clip))

    def test_cursor_debugging_port_gets_no_escape(self):
        from voice.desktop import cursor_cdp
        chat = "12345678-1234-1234-1234-123456789abc"
        got = {}

        async def send(_url, _chat, message, _urgent=False):
            got["message"] = message
            return ""
        with mock.patch.object(cursor_cdp, "page_of", return_value="ws://x"), \
                mock.patch.object(cursor_cdp, "_send", side_effect=send):
            self.assertEqual(cursor_cdp.send("proj", chat, ATTACK), "")
        self.assertTrue(clean(got["message"]))


class Delivered(unittest.TestCase):
    def test_deliver_strips_before_any_path(self):
        import routing
        from voice.state import Speaker
        speaker = Speaker("claude-x", "claude", "job", Tab("tmux", "%1"))
        terms = mock.Mock()
        terms.agent_running.return_value = True
        terms.type_line.return_value = True
        with mock.patch.object(routing, "place", return_value=""), \
                mock.patch.object(routing, "desktop", return_value=mock.Mock(terminals=terms)), \
                mock.patch.object(routing, "is_command", return_value=False):
            self.assertTrue(routing.deliver_words(ATTACK, routing.Heard.SHARED, to=speaker))
        self.assertTrue(clean(terms.type_line.call_args.args[1]))


if __name__ == "__main__":
    unittest.main()
