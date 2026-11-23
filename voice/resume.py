"""A chat is its id, not the terminal it was last seen in.

Claude, the Cursor CLI and Codex can be started again with that id. The folder comes from the agent's own
record of the chat. A running process is recognised by the id in its environment, not by a tab number.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MARK = {"claude": "CLAUDE_CODE_SESSION_ID", "cursor": "CURSOR_CONVERSATION_ID", "codex": "CODEX_THREAD_ID"}


def chat_id(session: str) -> str:
    """The uuid in a session key (`cursor-<uuid>`), or "" when the key is a folder placeholder."""
    for prefix in ("cursor-", "claude-", "codex-"):
        if session.startswith(prefix) and UUID.fullmatch(session.removeprefix(prefix)):
            return session.removeprefix(prefix)
    return ""


def marked(environ: bytes) -> dict[str, str]:
    """The chat id and the terminal address carried in a process's environment."""
    found = {}
    for part in environ.split(b"\0"):
        if b"=" not in part:
            continue
        key, value = part.split(b"=", 1)
        name = key.decode(errors="replace")
        if name in (*MARK.values(), "TMUX_PANE", "KONSOLE_DBUS_SERVICE", "KONSOLE_DBUS_SESSION"):
            found[name] = value.decode(errors="replace")
    return found


def carries(agent: str, chat: str, environ: bytes) -> bool:
    """That process's environment is this chat, not one it inherited for a tool."""
    return marked(environ).get(MARK.get(agent, ""), "") == chat


def claude_record(pid: int) -> str | None:
    """The chat a Claude process is on now, from Claude's own record of it (`~/.claude/sessions/<pid>.json`).
    None when there is no record for this process (an older pid's record is not its). Its environment keeps the
    chat it started with, and a chat picked with `claude --resume` or `/resume` is in neither the environment
    nor the command line: without this, Henchline started a second copy of a chat that was running."""
    import json
    try:
        record = json.loads((Path.home() / f".claude/sessions/{pid}.json").read_text())
        started = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None
    if not isinstance(record, dict) or record.get("pid") != pid or str(record.get("procStart", "")) != started:
        return None
    chat = record.get("sessionId")
    return chat if isinstance(chat, str) and chat else None


def _is_chat(agent: str, chat: str, pid: int, environ: bytes, command: bytes) -> bool:
    if agent == "claude":
        current = claude_record(pid)
        if current is not None:
            return current == chat  # the record follows /resume; the environment doesn't
        # Not the environment: a claude never starts with its own id there (Henchline unsets it), so an id there
        # is the parent's, inherited (a claude in an editor's terminal carries the chat that opened the editor)
        return resumed(command, chat)
    if environ and carries(agent, chat, environ):
        return True
    return agent == "cursor" and (holds_store(pid, chat) or resumed(command, chat))


def folder_of(agent: str, chat: str) -> Path | None:
    """The folder that chat was working in, from the agent's own record. None when it isn't there."""
    if agent == "claude":
        logs = list((Path.home() / ".claude/projects").glob(f"*/{chat}.jsonl"))
        if len(logs) != 1:
            return None
        try:
            tail = logs[0].read_bytes()[-256_000:]
        except OSError:
            return None
        found = re.findall(rb'"cwd":"([^"\\]+)"', tail)
        path = Path(found[-1].decode()) if found else None
        return path if path and path.is_dir() else None
    if agent == "cursor":
        from voice.desktop.cursor_bridge import folders_by_project, owner_of
        path = folders_by_project().get(owner_of(chat), "")
        return Path(path) if path and Path(path).is_dir() else None
    if agent == "codex":
        rolls = [p for p in (Path.home() / ".codex/sessions").rglob(f"*{chat}.jsonl") if p.name.endswith(f"{chat}.jsonl")]
        if len(rolls) != 1:
            return None
        try:
            head = rolls[0].read_text(errors="replace")[:4000]
        except OSError:
            return None
        found = re.search(r'"cwd":"([^"\\]+)"', head)
        path = Path(found.group(1)) if found else None
        return path if path and path.is_dir() else None
    return None


def tab_of(environ: bytes):
    """The terminal that process is in, from its own environment. Empty when it names neither tmux nor Konsole."""
    from voice.desktop import Tab
    env = marked(environ)
    pane = env.get("TMUX_PANE", "")
    if pane.startswith("%"):
        return Tab("tmux", pane)
    service, session = env.get("KONSOLE_DBUS_SERVICE", ""), env.get("KONSOLE_DBUS_SESSION", "")
    if service and session.startswith("/Sessions/"):
        return Tab(service, session)
    return Tab()


def resumed(cmdline: bytes, chat: str) -> bool:
    """The process was started as this chat: `--resume <id>` or `--resume=<id>` on its command line. The
    environment and the open files are sometimes unreadable, and the id is still there."""
    parts = cmdline.split(b"\0")
    want = chat.encode()
    flag = b"--resume=" + want
    return any(part == flag or (part == b"--resume" and i + 1 < len(parts) and parts[i + 1] == want)
               for i, part in enumerate(parts))


def holds_store(pid: int, chat: str) -> bool:
    """The Cursor CLI keeps its chat's store open. The id is in that path when the environment doesn't say it."""
    try:
        links = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return False
    for link in links:
        try:
            target = os.readlink(link)
        except OSError:
            continue
        if chat in target and target.endswith("store.db"):
            return True
    return False


def find_running(agent: str, chat: str) -> list:
    """Every agent process that is this chat: (pid, terminal). Empty when none. More than one is the caller's
    to refuse. A terminal-less match is included, so a second copy is not started just because the tab is unknown."""
    from voice.agents import process
    from voice.desktop import Tab
    hits = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "environ").read_bytes()
        except OSError:
            raw = b""  # hidden environment: the command line can still name the chat
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            command = b""
        if not _is_chat(agent, chat, pid, raw, command):
            continue
        running = process(pid)
        if not running or running.agent != agent or running.is_editor:
            continue
        tab = tab_of(raw)
        if tab and not runs_in(tab, pid):
            # The environment is inherited: an agent in an editor's terminal carries the Konsole tab or tmux pane
            # the editor was started from, where another agent may run (2026-11-23: a helper's reports were typed
            # into a different Claude that way)
            tab = Tab()
        if not tab:
            tab = _tmux_pane(pid)
        hits.append((pid, tab))
    return hits


def runs_in(tab, pid: int) -> bool:
    """That tab's agent is this process: words typed there reach it and no other agent."""
    from voice.desktop import current as desktop
    try:
        return desktop().terminals.agent_pid(tab) == pid
    except Exception:  # noqa: BLE001 - unknown is not the same
        return False


def _tmux_pane(pid: int):
    """The tmux pane whose process is `pid`, when the process won't say its pane. Empty when it isn't in tmux."""
    from voice.desktop import Tab
    from voice.desktop.tmux import out
    for line in out("list-panes", "-a", "-F", "#{pane_id} #{pane_pid}").splitlines():
        pane, _, got = line.partition(" ")
        if got == str(pid) and pane.startswith("%"):
            return Tab("tmux", pane)
    return Tab()
