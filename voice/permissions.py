"""How much an agent may do without asking, read from its own record, never guessed upwards.

A chat that Henchline starts again (a resume) comes back in the mode it last had. A child that a boss deploys gets
no more than its boss. Anything that can't be read is treated as asking: never auto, never --force.

Flags alone are not the whole story. The Cursor CLI's own settings (`~/.cursor/cli-config.json` approvalMode) can run
everything with or without --force, and no per-launch option turns that off, so a Cursor child or resume that would
get more than it should is refused, not started. Codex's settings (`~/.codex/config.toml`, approvals_reviewer) are
overridden per launch with `-a`, `-s` and `-c`, which its own help says take precedence over that file.

Levels: 0 asks before acting, 1 edits files without asking, 2 runs commands without asking, 3 bypasses every check.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path

# Claude Code's --permission-mode values, by how much they allow without asking
CLAUDE_LEVEL = {"default": 0, "plan": 0, "dontAsk": 0, "acceptEdits": 1, "auto": 2, "bypassPermissions": 3}
# Codex's approval_policy, the same way: "never" asks for nothing
CODEX_LEVEL = {"untrusted": 0, "on-request": 1, "on-failure": 1, "never": 2}
TAIL = 4 * 1024 * 1024


def _tail(path: Path) -> bytes:
    try:
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - TAIL))
            return f.read()
    except OSError:
        return b""


def claude_mode(chat: str) -> str:
    """The top-level permissionMode of the last session-log line that has one, or "" when it can't be read."""
    if not chat or not re.fullmatch(r"[0-9a-f-]{36}", chat):
        return ""
    logs = list((Path.home() / ".claude/projects").glob(f"*/{chat}.jsonl"))
    if len(logs) != 1:
        return ""
    for line in reversed(_tail(logs[0]).splitlines()):
        if b'"permissionMode"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the cut first line, or a torn write
        mode = entry.get("permissionMode") if isinstance(entry, dict) else None
        if isinstance(mode, str):
            return mode if mode in CLAUDE_LEVEL else ""
    return ""


def cursor_meta(chat: str) -> dict:
    """A Cursor CLI chat's settings from its own store, read only; {} when it can't be read."""
    if not chat or not re.fullmatch(r"[0-9a-f-]{36}", chat):
        return {}
    stores = list((Path.home() / ".cursor/chats").glob(f"*/{chat}/store.db"))
    if len(stores) != 1:
        return {}
    try:
        import sqlite3
        with contextlib.closing(sqlite3.connect(f"file:{stores[0]}?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("select value from meta where key = '0'").fetchone()
        raw = row[0] if row else ""
        meta = json.loads(bytes.fromhex(raw).decode() if isinstance(raw, str) and re.fullmatch(r"[0-9a-f]+", raw)
                          else raw)
    except Exception:  # noqa: BLE001 - unknown, not guessed
        return {}
    return meta if isinstance(meta, dict) else {}


def cursor_runs_everything() -> bool:
    """True when the Cursor CLI's own settings run every command without asking, whatever the flags. Unreadable
    settings count as True: the flags can't be shown to lower anything then."""
    try:
        config = json.loads((Path.home() / ".cursor/cli-config.json").read_text())
    except (OSError, ValueError):
        return True
    if not isinstance(config, dict):
        return True
    return config.get("approvalMode") not in ("allowlist",)


def cursor_forced(chat: str) -> bool:
    """True only when the chat's own store says it runs everything (what --force does). Unknown is False."""
    meta = cursor_meta(chat)
    return meta.get("isRunEverything") is True or meta.get("approvalMode") == "unrestricted"


CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
CODEX_REVIEWERS = ("user", "auto_review")


def codex_turn(chat: str) -> dict:
    """The settings of that Codex thread's last turn, from its session file: approval_policy, approvals_reviewer and
    the sandbox type, each only when it is a value Codex's own flags take. {} when there is no such record."""
    if not chat or not re.fullmatch(r"[0-9a-f-]{36}", chat):
        return {}
    files = list((Path.home() / ".codex/sessions").glob(f"*/*/*/rollout-*-{chat}.jsonl"))
    if len(files) != 1:
        return {}
    for line in reversed(_tail(files[0]).splitlines()):
        if b'"approval_policy"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        turn = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(turn, dict) or "approval_policy" not in turn:
            continue
        sandbox = turn.get("sandbox_policy")
        found = {"approval_policy": turn.get("approval_policy"), "approvals_reviewer": turn.get("approvals_reviewer"),
                 "sandbox": sandbox.get("type") if isinstance(sandbox, dict) else None}
        return {key: value for key, value in found.items() if isinstance(value, str) and value in
                {"approval_policy": CODEX_LEVEL, "approvals_reviewer": CODEX_REVIEWERS,
                 "sandbox": CODEX_SANDBOXES}[key]}
    return {}


def codex_policy(chat: str) -> str:
    """The last approval_policy in that Codex thread's session file, or "" when it can't be read."""
    return codex_turn(chat).get("approval_policy", "")


def level(agent: str, chat: str, in_editor: bool = False) -> int:
    """How much that chat may do without asking, from its own record. 0 when it can't be read."""
    if agent == "claude":
        return CLAUDE_LEVEL.get(claude_mode(chat), 0)
    if agent == "cursor" and not in_editor:
        return 2 if cursor_forced(chat) or cursor_runs_everything() else 0
    if agent == "codex":
        return CODEX_LEVEL.get(codex_policy(chat), 0)
    return 0


# Codex under a boss that asks: approvals go to the user (not Codex's own reviewer), writes stay in the workspace
CODEX_ASKING = {0: ["-a", "untrusted", "-s", "workspace-write", "-c", 'approvals_reviewer="user"'],
                1: ["-a", "on-request", "-s", "workspace-write", "-c", 'approvals_reviewer="user"']}


def resume_refusal(agent: str, chat: str) -> str:
    """Why that chat must not be reopened, or "". A Cursor chat whose own record says it asked before commands
    would come back running everything when the Cursor CLI's settings say so, and no flag prevents that."""
    if agent == "cursor" and not cursor_forced(chat) and cursor_runs_everything():
        return ("That Cursor chat asked before running commands, but Cursor's settings now run everything, "
                "so I didn't reopen it. Open it yourself if that's fine.")
    return ""


def resume_flags(agent: str, chat: str) -> list[str]:
    """The flags a resumed chat starts with: the mode it last had, capped at running commands without asking
    (a chat reopened by a message nobody is watching never comes back bypassing every check). Claude: ask mode
    when the record can't be read. Cursor: --force only when its record says so; its settings may still run
    everything, which resume_refusal checks first. Codex: its last turn's approval policy, sandbox and reviewer,
    which override its settings file; asking (CODEX_ASKING[0]) when any of them can't be read."""
    if agent == "claude":
        mode = claude_mode(chat) or "default"
        if mode == "bypassPermissions":
            mode = "auto"
        return ["--permission-mode", mode]
    if agent == "cursor":
        return ["--force"] if cursor_forced(chat) else []
    if agent == "codex":
        turn = codex_turn(chat)
        if len(turn) < 3:
            return list(CODEX_ASKING[0])
        return ["-a", turn["approval_policy"], "-s", turn["sandbox"],
                "-c", f'approvals_reviewer="{turn["approvals_reviewer"]}"']
    return []


def child_flags(agent: str, boss_level: int) -> list[str] | None:
    """The flags a deployed child starts with: no more than its boss may do. None when that agent can't be
    started that low (a Cursor child under a boss that asks, while Cursor's settings run everything)."""
    if agent == "claude":
        mode = "auto" if boss_level >= 2 else "acceptEdits" if boss_level == 1 else "default"
        return ["--permission-mode", mode]
    if agent == "cursor":
        if boss_level >= 2:
            return ["--force"]
        return None if cursor_runs_everything() else []
    if agent == "codex":
        return [] if boss_level >= 2 else list(CODEX_ASKING[max(0, boss_level)])
    return None


def child_runs(agent: str, boss_level: int) -> str:
    """What a child started with child_flags will do without asking, in one line for the phone."""
    if agent == "claude":
        return {0: "It asks before acting, as its boss would.",
                1: "It edits files without asking but asks before commands, as its boss would."}.get(
            boss_level, "It runs commands without asking, as its boss does.")
    if agent == "codex":
        if boss_level >= 2:
            return "It runs with Codex's own settings, which don't ask you."
        return "It asks you before commands outside its sandbox, and writes only in its folder."
    if boss_level >= 2:
        return "It runs commands without asking, as its boss does."
    return "It asks before running commands."
