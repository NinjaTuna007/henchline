import unittest

from voice.desktop import cursor_bridge


class ProjectNames(unittest.TestCase):
    """Cursor's project folder names, as seen under ~/.cursor/projects (2026-11-16): a change in its scheme must
    show up as a refused delivery, so these pin the assumption."""

    def test_dashed_path(self):
        self.assertEqual(cursor_bridge.project_of("/home/me/ws/src/robot"),
                         "home-me-ws-src-robot")

    def test_dots_and_doubled_separators(self):
        self.assertEqual(cursor_bridge.project_of("//home/u/my.project__x/"), "home-u-my-project-x")

    def test_chat_of(self):
        chat = "1e6e96a3-90f8-476a-9fe9-d2be71fc2053"
        self.assertEqual(cursor_bridge.chat_of("cursor-" + chat), chat)
        self.assertEqual(cursor_bridge.chat_of("cursor-_robotproj"), "")


class DeadWindow(unittest.TestCase):
    def test_a_dead_note_is_not_a_second_window(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        folder = Path(tempfile.mkdtemp())
        (folder / "1.json").write_text('{"pid": 5, "folders": ["/home/me/ws/src/robot"]}')
        (folder / "2.json").write_text('{"pid": 9, "folders": ["/home/me/ws/src/robot"]}')
        (folder / "2.sock").write_bytes(b"")
        with patch.object(cursor_bridge, "DIR", folder), patch.object(cursor_bridge, "pid_alive", lambda pid: pid == 5):
            found = cursor_bridge.windows_of("home-me-ws-src-robot")
        self.assertEqual([note.name for note, _ in found], ["1.json"])
        self.assertFalse((folder / "2.json").exists())
        self.assertFalse((folder / "2.sock").exists())



class Answers(unittest.TestCase):
    """The bridge's answer comes back with the call, as a reason code can branch on (server.py's stop)."""

    def test_the_bridges_words_become_a_reason(self):
        from voice.delivery import Refusal
        self.assertIs(Refusal.from_bridge("not working"), Refusal.NOT_WORKING)
        self.assertIs(Refusal.from_bridge("another chat is selected"), Refusal.OTHER_CHAT_SELECTED)
        self.assertIs(Refusal.from_bridge("something new"), Refusal.BRIDGE_SAID)
        self.assertIs(Refusal.from_bridge(""), Refusal.NO_ANSWER)

    def test_a_chat_no_window_has_is_refused_with_why(self):
        from unittest import mock
        from voice.delivery import Refusal
        chat = "12345678-1234-1234-1234-123456789abc"
        with mock.patch.object(cursor_bridge, "owner_of", return_value="proj"), \
                mock.patch.object(cursor_bridge, "windows_of", return_value=[]):
            got = cursor_bridge.ask_window(chat, {"open": chat})
        self.assertFalse(got)
        self.assertIs(got.refusal, Refusal.NO_WINDOW)
        with mock.patch.object(cursor_bridge, "owner_of", return_value=""):
            self.assertIs(cursor_bridge.ask_window(chat, {"open": chat}).refusal, Refusal.UNKNOWN_CHAT)

    def test_a_stop_on_a_chat_that_just_finished_says_so(self):
        from unittest import mock
        from voice.delivery import Delivery, Refusal
        chat = "12345678-1234-1234-1234-123456789abc"
        cdp = cursor_bridge.cursor_cdp
        with mock.patch.object(cursor_bridge, "workspace_of", return_value="ws"), \
                mock.patch.object(cdp, "available", return_value=True), \
                mock.patch.object(cdp, "focus_chat", return_value=True), \
                mock.patch.object(cdp, "chat_status", return_value="idle"), \
                mock.patch.object(cursor_bridge, "ask_window", return_value=Delivery.done()) as asked:
            got = cursor_bridge.ask_selected(chat, {"stop": chat})
        self.assertIs(got.refusal, Refusal.NOT_WORKING)
        asked.assert_called_once()  # opened, never stopped


if __name__ == "__main__":
    unittest.main()
