"""voice/permissions.py: a resumed chat keeps its own mode, and a deployed child gets no more than its boss."""

import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # first: a throwaway HOME
from voice import permissions
from voice.desktop import tmux

assert sandbox.HOME

CLAUDE = "aaaaaaaa-1111-2222-3333-444444444444"
CURSOR = "bbbbbbbb-1111-2222-3333-444444444444"
CODEX = "cccccccc-1111-2222-3333-444444444444"


def claude_log(chat: str, *modes: str) -> None:
    folder = Path.home() / ".claude/projects/-proj"
    folder.mkdir(parents=True, exist_ok=True)
    lines = (json.dumps({"permissionMode": m}, separators=(",", ":")) + "\n" for m in modes)
    (folder / f"{chat}.jsonl").write_text("".join(lines))


def cursor_settings(approval: str | None) -> None:
    path = Path.home() / ".cursor/cli-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if approval is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps({"permissions": {"allow": []}, "approvalMode": approval}))


def cursor_store(chat: str, meta: dict) -> None:
    folder = Path.home() / ".cursor/chats/0123" / chat
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "store.db").unlink(missing_ok=True)
    db = sqlite3.connect(folder / "store.db")
    db.execute("create table meta (key text primary key, value text)")
    db.execute("insert into meta values ('0', ?)", (json.dumps(meta).encode().hex(),))
    db.commit()
    db.close()


class Resume(unittest.TestCase):
    def test_claude_comes_back_in_its_last_mode(self):
        claude_log(CLAUDE, "auto", "default")
        self.assertEqual(permissions.resume_flags("claude", CLAUDE), ["--permission-mode", "default"])
        claude_log(CLAUDE, "default", "auto")
        self.assertEqual(permissions.resume_flags("claude", CLAUDE), ["--permission-mode", "auto"])
        claude_log(CLAUDE, "plan")
        self.assertEqual(permissions.resume_flags("claude", CLAUDE), ["--permission-mode", "plan"])

    def test_claude_unknown_is_ask_mode_and_bypass_is_capped(self):
        unknown = "dddddddd-1111-2222-3333-444444444444"
        self.assertEqual(permissions.resume_flags("claude", unknown), ["--permission-mode", "default"])
        claude_log(CLAUDE, "bypassPermissions")
        self.assertEqual(permissions.resume_flags("claude", CLAUDE), ["--permission-mode", "auto"])
        claude_log(CLAUDE, "somethingNew")
        self.assertEqual(permissions.resume_flags("claude", CLAUDE), ["--permission-mode", "default"])

    def tearDown(self):
        cursor_settings(None)

    def test_claude_reads_the_top_level_mode_not_a_quoted_one(self):
        folder = Path.home() / ".claude/projects/-proj"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{CLAUDE}.jsonl").write_text(
            json.dumps({"permissionMode": "default"}) + "\n"
            + json.dumps({"message": {"content": 'it said "permissionMode":"auto"'}}) + "\n")
        self.assertEqual(permissions.resume_flags("claude", CLAUDE), ["--permission-mode", "default"])

    def test_a_cursor_chat_that_asked_is_not_reopened_while_settings_run_everything(self):
        cursor_store(CURSOR, {"isRunEverything": False, "approvalMode": "allowlist"})
        cursor_settings("unrestricted")
        self.assertIn("didn't reopen", permissions.resume_refusal("cursor", CURSOR))
        with mock.patch.object(tmux, "_window") as window:
            self.assertFalse(tmux.resume("cursor", Path.home(), CURSOR))
        window.assert_not_called()
        cursor_settings(None)  # unreadable settings: not shown to be lower, so refused too
        self.assertTrue(permissions.resume_refusal("cursor", CURSOR))
        cursor_settings("allowlist")
        self.assertEqual(permissions.resume_refusal("cursor", CURSOR), "")
        cursor_store(CURSOR, {"isRunEverything": True})
        cursor_settings("unrestricted")
        self.assertEqual(permissions.resume_refusal("cursor", CURSOR), "")  # it ran everything already

    def test_cursor_is_forced_only_when_its_store_says_so(self):
        cursor_settings("allowlist")
        cursor_store(CURSOR, {"mode": "default", "isRunEverything": True})
        self.assertEqual(permissions.resume_flags("cursor", CURSOR), ["--force"])
        cursor_store(CURSOR, {"mode": "default", "isRunEverything": False, "approvalMode": "allowlist"})
        self.assertEqual(permissions.resume_flags("cursor", CURSOR), [])
        self.assertEqual(permissions.resume_flags("cursor", "eeeeeeee-1111-2222-3333-444444444444"), [])

    def test_tmux_resume_uses_those_flags(self):
        claude_log(CLAUDE, "default")
        with mock.patch.object(tmux, "_window", return_value=tmux.Tab()) as window:
            tmux.resume("claude", Path.home(), CLAUDE)
        self.assertEqual(window.call_args.args[2], ["claude", "--permission-mode", "default", "--resume", CLAUDE])
        cursor_settings("allowlist")
        cursor_store(CURSOR, {"isRunEverything": False})
        with mock.patch.object(tmux, "_window", return_value=tmux.Tab()) as window:
            tmux.resume("cursor", Path.home(), CURSOR)
        self.assertEqual(window.call_args.args[2], ["cursor-agent", "--resume", CURSOR])


def codex_rollout(chat: str, *turns: dict) -> None:
    folder = Path.home() / ".codex/sessions/2026/11/21"
    folder.mkdir(parents=True, exist_ok=True)
    lines = (json.dumps({"type": "turn_context", "payload": t}, separators=(",", ":")) + "\n" for t in turns)
    (folder / f"rollout-2026-11-21T10-00-00-{chat}.jsonl").write_text("".join(lines))


class CodexResume(unittest.TestCase):
    def test_a_codex_chat_comes_back_with_its_own_turn_settings(self):
        chat = "c0dec0de-1111-2222-3333-444444444444"
        codex_rollout(chat, {"approval_policy": "never", "approvals_reviewer": "auto_review",
                             "sandbox_policy": {"type": "danger-full-access"}},
                      {"approval_policy": "untrusted", "approvals_reviewer": "user",
                       "sandbox_policy": {"type": "workspace-write", "network_access": False}})
        want = ["-a", "untrusted", "-s", "workspace-write", "-c", 'approvals_reviewer="user"']
        self.assertEqual(permissions.resume_flags("codex", chat), want)
        with mock.patch.object(tmux, "_window", return_value=tmux.Tab()) as window:
            tmux.resume("codex", Path.home(), chat)
        self.assertEqual(window.call_args.args[2], ["codex", "resume", *want, chat])

    def test_an_unreadable_codex_record_comes_back_asking(self):
        self.assertEqual(permissions.resume_flags("codex", "c0dec0de-9999-2222-3333-444444444444"),
                         permissions.CODEX_ASKING[0])
        chat = "c0dec0de-8888-2222-3333-444444444444"
        codex_rollout(chat, {"approval_policy": "never", "approvals_reviewer": "auto_review",
                             "sandbox_policy": {"type": "external-sandbox"}})  # a sandbox -s can't name
        self.assertEqual(permissions.resume_flags("codex", chat), permissions.CODEX_ASKING[0])


class Children(unittest.TestCase):
    def tearDown(self):
        cursor_settings(None)

    def test_a_cursor_child_under_a_boss_that_asks_is_refused_while_settings_run_everything(self):
        cursor_settings("unrestricted")
        self.assertIsNone(permissions.child_flags("cursor", 0))
        self.assertIsNone(permissions.child_flags("cursor", 1))
        self.assertEqual(permissions.child_flags("cursor", 2), ["--force"])
        cursor_settings(None)
        self.assertIsNone(permissions.child_flags("cursor", 0))
        cursor_settings("allowlist")
        self.assertEqual(permissions.child_flags("cursor", 0), [])

    def test_a_codex_child_under_a_boss_that_asks_overrides_codex_settings(self):
        flags = permissions.child_flags("codex", 0)
        self.assertEqual(flags[flags.index("-a") + 1], "untrusted")
        self.assertEqual(flags[flags.index("-s") + 1], "workspace-write")
        self.assertIn('approvals_reviewer="user"', flags)
        self.assertEqual(permissions.child_flags("codex", 1)[1], "on-request")
        self.assertIn("asks you", permissions.child_runs("codex", 0))
        self.assertIn("without asking", permissions.child_runs("codex", 2))

    def test_a_codex_child_at_level_two_never_gets_config_toml_settings(self):
        # Another agent's boss (Claude auto), or a Codex boss not passed in: explicit, sandboxed to the workspace
        self.assertEqual(permissions.child_flags("codex", 2), permissions.CODEX_UNASKING)
        self.assertEqual(permissions.child_flags("codex", 3), permissions.CODEX_UNASKING)
        # A Codex boss: its own -a and -s
        boss = {"approval_policy": "never", "sandbox": "danger-full-access", "approvals_reviewer": "auto_review"}
        self.assertEqual(permissions.child_flags("codex", 2, boss),
                         ["-a", "never", "-s", "danger-full-access", "-c", 'approvals_reviewer="user"'])
        self.assertEqual(permissions.child_flags("codex", 2, {"approval_policy": "never", "sandbox": "x"}),
                         permissions.CODEX_UNASKING)

    def test_a_sandboxed_codex_boss_that_never_asks_counts_as_one(self):
        cases = [("read-only", 1), ("workspace-write", 1), ("danger-full-access", 2), (None, 1)]
        for n, (sandbox, want) in enumerate(cases):
            chat = f"c0dec0de-777{n}-2222-3333-444444444444"
            turn = {"approval_policy": "never", "approvals_reviewer": "user"}
            if sandbox:
                turn["sandbox_policy"] = {"type": sandbox}
            codex_rollout(chat, turn)
            self.assertEqual(permissions.level("codex", chat), want, sandbox)
        # So children of `-a never -s read-only`: a Claude child asks before commands, a Codex child stays sandboxed
        chat = "c0dec0de-7770-2222-3333-444444444444"
        self.assertEqual(permissions.child_flags("claude", permissions.level("codex", chat)),
                         ["--permission-mode", "acceptEdits"])
        self.assertEqual(permissions.child_flags("codex", permissions.level("codex", chat)), permissions.CODEX_ASKING[1])
        # Asking policies keep their level whatever the sandbox
        chat = "c0dec0de-7779-2222-3333-444444444444"
        codex_rollout(chat, {"approval_policy": "untrusted", "sandbox_policy": {"type": "danger-full-access"}})
        self.assertEqual(permissions.level("codex", chat), 0)

    def test_a_child_gets_no_more_than_its_boss(self):
        cursor_settings("allowlist")
        claude_log(CLAUDE, "default")
        self.assertEqual(permissions.level("claude", CLAUDE), 0)
        self.assertEqual(permissions.child_flags("cursor", 0), [])
        self.assertEqual(permissions.child_flags("claude", 0), ["--permission-mode", "default"])
        claude_log(CLAUDE, "acceptEdits")
        self.assertEqual(permissions.child_flags("claude", permissions.level("claude", CLAUDE)),
                         ["--permission-mode", "acceptEdits"])
        self.assertEqual(permissions.child_flags("cursor", permissions.level("claude", CLAUDE)), [])
        claude_log(CLAUDE, "auto")
        self.assertEqual(permissions.child_flags("cursor", permissions.level("claude", CLAUDE)), ["--force"])
        cursor_store(CURSOR, {"isRunEverything": True})
        self.assertEqual(permissions.level("cursor", CURSOR), 2)
        self.assertEqual(permissions.level("cursor", CURSOR, in_editor=True), 0)  # an editor chat: not known
        cursor_store(CURSOR, {"isRunEverything": False})
        self.assertEqual(permissions.level("cursor", CURSOR), 0)
        cursor_settings("unrestricted")
        self.assertEqual(permissions.level("cursor", CURSOR), 2)  # its settings run everything anyway

    def test_codex_boss_from_its_session_file(self):
        folder = Path.home() / ".codex/sessions/2026/11/21"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"rollout-2026-11-21T10-00-00-{CODEX}.jsonl").write_text(
            json.dumps({"payload": {"approval_policy": "on-request"}}, separators=(",", ":")) + "\n")
        self.assertEqual(permissions.level("codex", CODEX), 1)
        self.assertEqual(permissions.level("codex", "ffffffff-1111-2222-3333-444444444444"), 0)

    def test_tmux_start_with_flags_replaces_the_new_chat_ones(self):
        with mock.patch.object(tmux, "_window", return_value=tmux.Tab()) as window:
            tmux.start("cursor", Path.home(), flags=[])
            self.assertEqual(window.call_args.args[2], ["cursor-agent"])
            tmux.start("claude", Path.home(), flags=["--permission-mode", "default"])
            self.assertEqual(window.call_args.args[2], ["claude", "--permission-mode", "default"])
            tmux.start("claude", Path.home())
            self.assertEqual(window.call_args.args[2], tmux.COMMANDS["claude"])


class Panes(unittest.TestCase):
    def test_only_a_missing_server_means_no_panes(self):
        def failed(stderr):
            return mock.Mock(returncode=1, stdout="", stderr=stderr)
        cases = {"no server running on /tmp/tmux-1000/default": {},
                 "error connecting to /tmp/tmux-1000/default (No such file or directory)": {},
                 "error connecting to /tmp/tmux-1000/default (Permission denied)": None,
                 "something else": None}
        for stderr, want in cases.items():
            with mock.patch.object(tmux, "tmux", return_value=failed(stderr)):
                self.assertEqual(tmux.pane_pids(), want, stderr)


if __name__ == "__main__":
    unittest.main()
