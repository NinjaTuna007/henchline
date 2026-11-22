"""Where a new chat can start on a computer (start_folders), and the chats a computer lists to a paired phone.
tmux is faked throughout: nothing here reaches a real tmux server."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

try:
    import aiohttp  # noqa: F401
except ImportError:
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

spec = importlib.util.spec_from_file_location("call_server_folders", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def no_real_tmux(*args, **kwargs):
    raise AssertionError(f"a test reached tmux: {args}")


class StartFolders(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.tmux_lines = ""
        for p in (mock.patch.object(server.Path, "home", return_value=self.home),
                  mock.patch.object(server.tmux, "tmux", no_real_tmux),
                  mock.patch.object(server.tmux, "out", lambda *a: self.tmux_lines),
                  mock.patch.object(server, "trusted_folders", lambda limit=30: list(self.claude))):
            p.start()
            self.addCleanup(p.stop)
        self.claude: list[Path] = []

    def folder(self, *parts):
        path = self.home.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cursor_trusts(self, folder, used):
        project = self.home / ".cursor/projects" / str(folder).replace("/", "-").strip("-")
        project.mkdir(parents=True, exist_ok=True)
        (project / ".workspace-trusted").write_text(json.dumps({"trustedAt": "2026-07-31T09:28:53Z", "workspacePath": str(folder)}))
        os.utime(project, (used, used))

    def test_without_claude_the_folders_cursor_trusts_and_the_working_ones_are_offered(self):
        site, kite, live = self.folder("site"), self.folder("kite"), self.folder("work", "live")
        self.cursor_trusts(site, 100)
        self.cursor_trusts(kite, 200)
        self.tmux_lines = f"1795000000 {live}\n1795000100 {self.home}\nbad line\n1795000050 {site}"
        # The working ones by their window's last activity, home itself never; then Cursor's, each folder once
        self.assertEqual(server.start_folders(), [site, live, kite])

    def test_claudes_list_is_kept_as_is_and_others_are_checked(self):
        mine, secret, hidden = self.folder("claude-proj"), self.folder(".ssh", "keys"), self.folder(".cache", "x")
        gone = self.home / "deleted"
        outside = Path(tempfile.mkdtemp())  # not under home or /media/<user>
        self.claude = [self.home, mine]  # Claude's own list, home included, as before
        for i, folder in enumerate((secret, hidden, gone, outside, mine)):
            self.cursor_trusts(folder, 100 + i)
        self.assertEqual(server.start_folders(), [self.home, mine])

    def test_a_broken_marker_is_skipped_and_the_list_is_capped(self):
        broken = self.home / ".cursor/projects/broken"
        broken.mkdir(parents=True)
        (broken / ".workspace-trusted").write_text("{not json")
        for i in range(40):
            self.cursor_trusts(self.folder(f"p{i}"), 1000 + i)
        found = server.start_folders()
        self.assertEqual(len(found), 30)
        self.assertEqual(found[0], self.home / "p39")  # most recently used first

    def test_tmux_is_asked_only_about_its_henchline_session(self):
        asked = []
        with mock.patch.object(server.tmux, "out", lambda *a: asked.append(a) or ""):
            server.start_folders()
        self.assertEqual(asked, [("list-panes", "-s", "-t", "henchline", "-F", "#{window_activity} #{pane_current_path}")])


class TrustIsTheUsers(unittest.TestCase):
    """A listed folder can come from anything running here: an agent's trust question is answered for the user
    only after their yes on the phone."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.claude, self.codex = [], []
        for p in (mock.patch.object(server.Path, "home", return_value=self.home),
                  mock.patch.object(server.tmux, "tmux", no_real_tmux),
                  mock.patch.object(server.tmux, "out", lambda *a: ""),
                  mock.patch.object(server, "trusted_folders", lambda limit=30: list(self.claude)),
                  mock.patch.object(server.minions, "_codex_trusted", lambda: list(self.codex))):
            p.start()
            self.addCleanup(p.stop)
        self.folder = self.home / "planted"
        self.folder.mkdir()

    def test_a_folder_the_agent_doesnt_trust_is_answered_only_on_the_users_yes(self):
        for agent in ("claude", "codex"):
            self.assertIs(server.trust_answer(agent, self.folder, said_yes=False), False)  # waits in tmux
            self.assertIs(server.trust_answer(agent, self.folder, said_yes=True), True)

    def test_a_folder_the_agent_trusts_itself_asks_nothing(self):
        self.claude, self.codex = [self.folder], [self.home / "planted" / ".." / "planted"]
        self.assertIsNone(server.trust_answer("claude", self.folder, said_yes=False))
        self.assertIsNone(server.trust_answer("codex", self.folder, said_yes=False))
        self.codex = []
        self.assertIs(server.trust_answer("codex", self.folder, said_yes=False), False)  # Claude's trust isn't Codex's

    def test_a_cursor_marker_never_answers_for_claude(self):
        StartFolders.cursor_trusts(self, self.folder, 100)
        self.assertEqual(server.trusted_by("cursor"), {self.folder.resolve()})
        self.assertIs(server.trust_answer("claude", self.folder, said_yes=False), False)
        self.assertIsNone(server.trust_answer("cursor", self.folder, said_yes=False))  # Cursor's own: not one asked here

    def test_the_folder_list_says_which_agents_trust_each(self):
        self.claude = [self.folder]
        app = server.web.Application()
        app.router.add_get("/crew/folders", server.crew_folders)

        async def ask():
            from aiohttp.test_utils import TestClient, TestServer
            async with TestClient(TestServer(app)) as client:
                answer = await client.get("/crew/folders", headers={"Origin": f"https://{client.host}:{client.port}"})
                return await answer.json()
        import asyncio
        with mock.patch.object(server.tmux, "available", return_value=["claude", "codex"]):
            body = asyncio.run(ask())
        self.assertEqual(body["folders"], [{"path": str(self.folder), "name": "planted",
                                            "trusted": {"claude": True, "codex": False}}])


class LaptopChats(unittest.TestCase):
    def test_running_agents_carry_their_boss_and_quiet_ones_are_not_running(self):
        listed = {"current": "claude-1",
                  "agents": [{"session": "claude-1", "name": "Claude, in site", "boss": "", "team": "boss"},
                             {"session": "cursor-2", "name": "Cursor, in site", "boss": "claude-1", "team": "crew"}],
                  "others": [{"session": "codex-3", "name": "Codex, in kite", "state": "gone"}]}
        with mock.patch.object(server, "agents", return_value=listed):
            chats = server.LocalHost().chats()
        self.assertEqual(chats[0], {"session_id": "claude-1", "name": "Claude, in site", "current": True, "running": True,
                                    "state": "running", "agent": "claude", "boss": "", "team": "boss"})
        self.assertEqual((chats[1]["boss"], chats[1]["agent"]), ("claude-1", "cursor"))
        self.assertEqual(chats[2], {"session_id": "codex-3", "name": "Codex, in kite", "current": False, "running": False,
                                    "state": "gone", "agent": "codex"})


if __name__ == "__main__":
    unittest.main()
