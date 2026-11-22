"""A chat is recognised by its id."""

import unittest

from tests import sandbox  # noqa: F401
from voice import resume
from voice.desktop import Tab


class Identity(unittest.TestCase):
    def test_a_session_key_is_the_chat_id(self):
        chat = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"
        self.assertEqual(resume.chat_id("cursor-" + chat), chat)
        self.assertEqual(resume.chat_id("claude-" + chat), chat)
        self.assertEqual(resume.chat_id("cursor-_henchline"), "")

    def test_the_environment_names_the_chat_and_its_terminal(self):
        chat = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"
        raw = b"\0".join([
            b"CURSOR_CONVERSATION_ID=" + chat.encode(),
            b"KONSOLE_DBUS_SERVICE=:1.110",
            b"KONSOLE_DBUS_SESSION=/Sessions/5",
            b"OTHER=1",
        ])
        self.assertTrue(resume.carries("cursor", chat, raw))
        self.assertFalse(resume.carries("claude", chat, raw))
        self.assertEqual(resume.tab_of(raw), Tab(":1.110", "/Sessions/5"))
        pane = b"TMUX_PANE=%12\0CURSOR_CONVERSATION_ID=" + chat.encode()
        self.assertEqual(resume.tab_of(pane), Tab("tmux", "%12"))

    def test_a_resume_on_the_command_line_is_that_chat(self):
        chat = "2ed6eebc-f2ce-43e5-8730-fc126b9f6794"
        command = b"\0".join([b"cursor-agent", b"--force", b"--resume", chat.encode()])
        self.assertTrue(resume.resumed(command, chat))
        self.assertTrue(resume.resumed(b"cursor-agent\0--resume=" + chat.encode(), chat))
        self.assertFalse(resume.resumed(command, "521b6ab2-0692-4903-aef2-de675cd6e972"))
        self.assertFalse(resume.resumed(b"cursor-agent\0--resume\0", chat))
        self.assertFalse(resume.resumed(b"--resume=" + chat.encode() + b"-extra", chat))


class ClaudeRecord(unittest.TestCase):
    """A Claude chat picked with /resume is named only by Claude's own record of the process."""
    CHAT = "13388111-493f-41d1-9791-11259dab5a68"
    OTHER = "edb146cd-f9c9-463a-94e7-19c6818a1bed"

    def setUp(self):
        import json
        import os
        from pathlib import Path
        self.pid = os.getpid()
        self.started = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        folder = Path.home() / ".claude/sessions"
        folder.mkdir(parents=True, exist_ok=True)
        self.record = folder / f"{self.pid}.json"
        self.write = lambda **kw: self.record.write_text(json.dumps(
            {"pid": self.pid, "sessionId": self.CHAT, "procStart": self.started, **kw}))
        self.addCleanup(lambda: self.record.unlink(missing_ok=True))

    def test_the_record_names_the_chat_the_process_is_on(self):
        self.write()
        self.assertEqual(resume.claude_record(self.pid), self.CHAT)
        env = b"CLAUDE_CODE_SESSION_ID=" + self.OTHER.encode()  # the chat it started with, before /resume
        self.assertTrue(resume._is_chat("claude", self.CHAT, self.pid, env, b"claude"))
        self.assertFalse(resume._is_chat("claude", self.OTHER, self.pid, env, b"claude"))

    def test_a_record_left_by_an_earlier_process_with_this_pid_is_not_it(self):
        self.write(procStart="1")
        self.assertIsNone(resume.claude_record(self.pid))
        env = b"CLAUDE_CODE_SESSION_ID=" + self.OTHER.encode()
        self.assertFalse(resume._is_chat("claude", self.CHAT, self.pid, env, b"claude"))
        resumed = b"claude\0--resume\0" + self.OTHER.encode()
        self.assertTrue(resume._is_chat("claude", self.OTHER, self.pid, env, resumed))

    def test_without_a_record_only_the_command_line_names_it(self):
        self.assertIsNone(resume.claude_record(self.pid))
        command = b"claude\0--resume\0" + self.CHAT.encode()
        self.assertTrue(resume._is_chat("claude", self.CHAT, self.pid, b"", command))
        self.assertFalse(resume._is_chat("claude", self.CHAT, self.pid, b"", b"claude"))

    def test_an_inherited_environment_without_a_record_is_not_this_chat(self):
        inherited = b"CLAUDE_CODE_SESSION_ID=" + self.CHAT.encode()  # the chat that opened the editor
        self.assertFalse(resume._is_chat("claude", self.CHAT, self.pid, inherited, b"claude"))


if __name__ == "__main__":
    unittest.main()
