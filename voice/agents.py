"""Which agent is running us, where it lives, and whether it is still there.

Talkback asks this when an agent speaks (whose voice, which workspace); the
KDE desktop (voice/desktop/kde_x11.py) uses it to find the agent's Konsole tab. The
answers come from the process tree, because environment markers lie: an
editor started from a Claude shell inherits CLAUDECODE, and a session
moved into Claude's background daemon inherits another tab's Konsole address.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Agent programs by command line, checked nearest ancestor first. Node-based
# CLIs run as "node /path/to/codex", so the first two words are checked.
AGENT_PROCESSES = [
    (re.compile(r"(^|/)claude(/|$)"), "claude"),
    (re.compile(r"cursor-agent|(^|/)agent$|(^|/)cursor$"), "cursor"),
    (re.compile(r"(^|/)codex$"), "codex"),
    (re.compile(r"(^|/)gemini$"), "gemini"),
    (re.compile(r"(^|/)opencode$"), "opencode"),
]
# Each agent's talkback voice (Kokoro), so the user knows who speaks; any other agent gets the default
AGENT_VOICES = {"claude": "af_heart", "cursor": "af_bella", "codex": "bf_alice"}
DEFAULT_VOICE = "af_sky"
SPOKEN_TO = ("claude", "cursor", "codex")  # agents hands-free can switch to by name
CURSOR_EDITOR = re.compile(r"(^|/)cursor$")  # the editor itself, not its CLI agent
CLAUDE_DAEMON = b"bg-pty-host"  # Claude Code's background-session host


@dataclass
class Process:
    pid: int
    words: list[str]  # the first two words of its command line

    @property
    def agent(self) -> str:
        for word in self.words:
            for pattern, name in AGENT_PROCESSES:
                if pattern.search(word):
                    return name
        return ""

    @property
    def is_editor(self) -> bool:
        return any(CURSOR_EDITOR.search(word) for word in self.words)

    @property
    def folder(self) -> Path:
        return Path(os.readlink(f"/proc/{self.pid}/cwd"))


SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh"}


def comm(pid: int | str) -> str:
    """A process's program name, e.g. "bash"."""
    return Path(f"/proc/{pid}/comm").read_text().strip()


def _stat(pid: int, field: int) -> int:
    """A number from /proc/<pid>/stat, counted from the field after the name."""
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[field])


def parent_of(pid: int) -> int:
    return _stat(pid, 1)


def terminal_foreground(pid: int) -> int:
    """The process group in the foreground of pid's terminal."""
    return _stat(pid, 5)


def ancestors(start: int | None = None) -> list[Process]:
    """Our parent, its parent, … up to init, nearest first."""
    chain, pid = [], start or os.getppid()
    while pid > 1 and (found := process(pid)):
        chain.append(found)
        try:
            pid = parent_of(pid)
        except (OSError, ValueError, IndexError):
            break
    return chain


def started(pid: int) -> int:
    """When a process started (clock ticks since boot): with its pid, it names that one process, never a later one
    given the same pid. 0 when it isn't running."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return 0


def nearest_agent() -> Process | None:
    return next((p for p in ancestors() if p.agent), None)


def agent_folder() -> Path:
    """The workspace of the calling agent, wherever its shell happens to be."""
    agent = nearest_agent()
    if agent is None:
        return Path.cwd()
    if agent.is_editor:
        # One Cursor editor process serves every window, from whatever folder it
        # was first started in; the agent's own shell starts in the workspace
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True).stdout.strip()
        return Path(top) if top else Path.cwd()
    try:
        return agent.folder
    except OSError:
        return Path.cwd()


def agent_where() -> str:
    """The calling agent's place, as named out loud: its workspace folder's name ("home" for the home folder), or,
    for a Cursor editor chat (whose shell may sit in /tmp), its window's workspace label."""
    label = os.environ.get("CURSOR_WORKSPACE_LABEL", "")
    if label:
        return label
    folder = agent_folder()
    return "home" if folder == Path.home() else folder.name


def editor_workspace() -> str | None:
    """A Cursor editor chat runs its commands in a sandbox that hides the editor, so it is known by its folder:
    the editor window whose workspace is that folder or one above it gets the key hands-free found it under
    ("/<workspace>", as routing.discover names a window). Several windows matching, or none: None, never a guess
    (a guess sent a text to another Cursor's chat, the user's catch, 2026-11-16). Two chats in one window share it."""
    from voice.desktop import current as desktop  # here: the desktop imports this module

    try:
        names = {w.title.removesuffix(" - Cursor").rsplit(" - ", 1)[-1] for w in desktop().windows("cursor")}
    except Exception:  # no display (a service), or X11 refusing: unknown
        return None
    names.discard("")
    folder = agent_folder()
    for place in (folder, *folder.parents):
        matches = [n for n in names if n == place.name]
        if len(matches) == 1:
            return f"/{matches[0]}"
        if matches:
            return None
    return None


def in_cursor_sandbox() -> bool:
    """Cursor runs an agent's commands confined by an AppArmor profile named cursorsandbox."""
    try:
        return "cursorsandbox" in Path("/proc/self/attr/current").read_text()
    except OSError:
        return False


def identity(agent: str = "", session: str = "") -> tuple[str, str]:
    """(agent kind, session key) of the caller; given values win."""
    if not agent:
        process = nearest_agent()
        if os.environ.get("TALKBACK_AGENT"):
            agent = os.environ["TALKBACK_AGENT"].lower()
        elif process is not None:
            agent = process.agent  # the process we really run under beats inherited markers
        elif os.environ.get("CLAUDECODE"):
            agent = "claude"
        elif "cursor" in os.environ.get("AI_AGENT", "").lower() or any(k.startswith("CURSOR_") for k in os.environ):
            agent = "cursor"
        elif in_cursor_sandbox():  # Cursor's sandbox hides its process tree and environment: "agent" before
            agent = "cursor"
        else:
            agent = "agent"
    if not session:
        # A Cursor editor chat runs its commands with its own chat id: each chat is its own agent (the user's wish,
        # 2026-11-16: several chats in one window, each talked to apart)
        session = ((os.environ.get("CLAUDE_CODE_SESSION_ID") if agent == "claude" else None)
                   or (os.environ.get("CURSOR_CONVERSATION_ID") if agent == "cursor" else None)
                   or (os.environ.get("CURSOR_TRACE_ID") if agent == "cursor" else None)
                   or (editor_workspace() if agent == "cursor" and in_cursor_sandbox() else None)
                   or str(agent_folder()))
    return agent, _key(agent, session)


def run_by_agent() -> bool:
    """Is an agent behind this command, by the process tree (or Cursor's sandbox, which hides it)? Never by the
    environment: a variable anyone can set must not make an agent's command pass for a timer's or a script's."""
    return nearest_agent() is not None or in_cursor_sandbox()


def vouched(agent: str, session: str) -> str:
    """"" when the process tree backs the caller's claim to be that agent session, else why not. The session comes
    from environment variables a child can set to another's (CLAUDE_CODE_SESSION_ID), so the agent process this
    command runs under must itself be that chat: Claude's own record of its pid, or the id the process carries
    (resume._is_chat), or, for a folder key, that process's folder."""
    from voice import resume
    if agent == "cursor" and in_cursor_sandbox():
        # Cursor's editor sandbox hides the tree; nothing started outside it (a terminal agent) runs inside it
        return ""
    mine = nearest_agent()
    if mine is None:
        return "not run by an agent"
    if mine.agent != agent:
        return f"run by {mine.agent}, not {agent}"
    if mine.is_editor:  # one Cursor editor process serves all its chats: the chat can't be told from the tree
        return ""
    wrong = f"this command runs under another {agent} session than {session}"
    chat = resume.chat_id(session)
    if chat:
        if agent == "claude":
            # The nearest Claude that keeps a record decides (a background-session host may keep none of its own)
            for up in (p for p in ancestors() if p.agent == "claude"):
                if (record := resume.claude_record(up.pid)) is not None:
                    return "" if record == chat else wrong
        try:
            environ = Path(f"/proc/{mine.pid}/environ").read_bytes()
        except OSError:
            environ = b""
        try:
            command = Path(f"/proc/{mine.pid}/cmdline").read_bytes()
        except OSError:
            command = b""
        return "" if resume._is_chat(agent, chat, mine.pid, environ, command) else wrong
    try:
        folder = mine.folder
    except OSError:
        return wrong
    return "" if session == _key(agent, str(folder)) else wrong


def _key(agent: str, session: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{agent}-{session}")[-120:]


def process(pid: int) -> Process | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:2]
    except OSError:
        return None
    return Process(pid, [w.decode(errors="replace") for w in raw])


def cmdline_has(pid: int, needle: bytes) -> bool:
    try:
        return needle in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
