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


class InheritedTab(unittest.TestCase):
    """An agent in an editor's terminal carries the Konsole tab the editor was started from (2026-11-23)."""

    def test_a_tab_whose_agent_is_another_process_is_not_this_ones(self):
        import os
        from unittest import mock
        me = os.getpid()
        terms = mock.Mock()
        terms.agent_pid.return_value = me + 1  # another Claude is that tab's foreground
        agent = mock.Mock(agent="claude", is_editor=False)
        with mock.patch.object(resume, "_is_chat", side_effect=lambda a, c, pid, e, cmd: pid == me), \
                mock.patch.object(resume, "tab_of", return_value=Tab(":1.83", "/Sessions/1")), \
                mock.patch.object(resume, "_tmux_pane", return_value=Tab()), \
                mock.patch("voice.agents.process", return_value=agent), \
                mock.patch("voice.desktop.current", return_value=mock.Mock(terminals=terms)):
            self.assertEqual(resume.find_running("claude", "7f867b40-b715-4c23-92d6-66484e6c3381"), [(me, Tab())])
            terms.agent_pid.return_value = me
            self.assertEqual(resume.find_running("claude", "7f867b40-b715-4c23-92d6-66484e6c3381"),
                             [(me, Tab(":1.83", "/Sessions/1"))])


class KonsoleTitle(unittest.TestCase):
    """The tab found by its "<folder> : claude" title must run the calling agent itself."""

    def caller_tab(self, tab_agent: int):
        import os
        from pathlib import Path
        from unittest import mock
        from voice.desktop import kde_x11
        me = mock.Mock(pid=4242, is_editor=False, folder=Path("/home/u/henchline/call"))

        def qdbus(*args):
            if args[-1:] == ("foregroundProcessId",):
                return "999"  # the tab named in the environment runs something else
            if args[-2:] == ("title", "1"):
                return "henchline : claude" if args[1] == "/Sessions/3" else "zsh"
            if len(args) == 1:
                return "/Sessions/1\n/Sessions/3"
            return ""
        with mock.patch.dict(os.environ, {"KONSOLE_DBUS_SERVICE": ":1.83", "KONSOLE_DBUS_SESSION": "/Sessions/1"}), \
                mock.patch.object(kde_x11, "nearest_agent", return_value=me), \
                mock.patch.object(kde_x11, "ancestors", return_value=[me]), \
                mock.patch.object(kde_x11, "cmdline_has", return_value=False), \
                mock.patch.object(kde_x11, "qdbus", side_effect=qdbus), \
                mock.patch.object(kde_x11.Konsole, "agent_pid", return_value=tab_agent):
            return kde_x11.Konsole.__new__(kde_x11.Konsole).tab_of_caller()

    def test_a_title_match_running_another_claude_is_not_the_callers(self):
        self.assertEqual(self.caller_tab(tab_agent=15211), Tab())

    def test_a_title_match_running_the_caller_is_its_tab(self):
        self.assertEqual(self.caller_tab(tab_agent=4242), Tab(":1.83", "/Sessions/3"))


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
