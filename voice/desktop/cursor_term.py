"""Agents in Cursor's own terminal panel: claude, cursor-agent or codex started in a terminal of a Cursor window.

Such a terminal has no tmux pane and no Konsole tab (its Konsole variables are inherited from whatever started
Cursor, and are never trusted). Its tab is owner "cursor-term" and address "<shell pid>@<shell start time>": the
terminal's top process, which Cursor's pty host started (`terminal.processId` in an extension), and when it started,
so a reused pid never matches. Which window shows it is asked again at each use (terminals move between windows,
and a window reloads): every Cursor window's bridge (cursor-bridge/) lists its own terminals, and exactly one may
list this shell.

Words go in through that window's bridge (bridge 0.2.0 and later): it checks, in the process that writes, right
before each write, that the same program and agent are still in front of the terminal (pids and start times) and
nothing else in front reads the terminal, then writes the
words as one bracketed paste and Enter, the bytes tmux sends. A shell or any other program in front: nothing typed.
No screen is read back: the agent's own log says whether the words arrived (voice/pickup.py).

A window still running an older bridge is reloaded (Cursor's own Reload Window, run by Henchline's small reload
helper extension in that window, which also says which terminals are its own: never keys), in the background, and
only while nothing in it works: its terminals survive a reload, no Cursor chat generating, no agent in its Cursor's
terminals mid-turn. Unsure counts as working.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path

from voice.agents import CLAUDE_DAEMON, Process
from voice.desktop import Tab, Terminals, Window, typeable

OWNER = "cursor-term"
NEEDED = (0, 2, 0)  # the first bridge that lists terminals and types into them
PROC = Path("/proc")  # tests point this at a fake tree
CLAIMS_FOR = 2.0  # seconds the bridges' lists of terminals are kept for agent_tabs
MOST_LINE = 1 << 20  # bytes in one request line with the words: the bridge takes no more
RELOAD_WAIT = 45.0  # seconds a reloaded window's new bridge may take to come up
RELOAD_AGAIN = 600.0  # seconds before the same Cursor is reloaded again when its bridge didn't come back new
CHECKING_FOR = 60.0  # seconds a background reload's checks may take before another one may start
BUSY_AGAIN = 30.0  # seconds a window found working counts as working before it is checked again
CHAT_QUIET = 60.0  # seconds since a Cursor chat's transcript last changed before it counts as not working
CHAT_RECENT = 6 * 3600.0  # chats touched in this long are looked up in Cursor's store
AGENT_QUIET = 10.0  # seconds since an agent's log last changed before its log is read for the turn's end
CURSOR_CLI_QUIET = 60.0  # the Cursor CLI's store, which says nothing readable about a turn: quiet this long
CPU_SAMPLE = 1.5  # seconds the Cursor CLI's CPU time is watched; it draws its spinner while it works
CPU_BUSY = 0.05  # share of one CPU over that time that counts as working

RELOADS = "reloads"  # each Cursor's last reload and its outcome; not .json: every .json in the bridge folder is a note
_why: dict[tuple[int, str], str] = {}  # (thread, tab address) → the note on that thread's last typing
_claims: tuple[float, dict[int, int]] = (0.0, {})


# The process tree, read from PROC

def _fields(pid: int) -> list[str]:
    """/proc/<pid>/stat after the program's name (state first); [] when it isn't there."""
    try:
        return (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return []


def _field(pid: int, index: int) -> int:
    fields = _fields(pid)
    try:
        return int(fields[index])
    except (IndexError, ValueError):
        return 0


def parent(pid: int) -> int:
    return _field(pid, 1)


def tty(pid: int) -> int:
    return _field(pid, 4)


def foreground(pid: int) -> int:
    """The process group in front of pid's terminal (tpgid); 0 or less when none."""
    return _field(pid, 5)


def born(pid: int) -> int:
    """When it started (clock ticks since boot): with the pid, that one process, never a later one."""
    return _field(pid, 19)


def _cmdline(pid: int) -> bytes:
    try:
        return (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return b""


def _program(pid: int) -> Process | None:
    raw = _cmdline(pid)
    if not raw:
        return None
    return Process(pid, [w.decode(errors="replace") for w in raw.split(b"\0")[:2]])


def _agent(pid: int) -> str:
    """The agent that process is (never the Cursor editor itself, which matches "cursor" too); "" for none."""
    found = _program(pid)
    return found.agent if found and not found.is_editor else ""


def _children(pid: int) -> list[int]:
    try:
        return [int(c) for c in (PROC / str(pid) / "task" / str(pid) / "children").read_text().split()]
    except (OSError, ValueError):
        return []


def _in_vscode_terminal(pid: int) -> bool:
    """Started by a VS Code family terminal: TERM_PROGRAM=vscode in the environment it was started with."""
    try:
        return b"TERM_PROGRAM=vscode" in (PROC / str(pid) / "environ").read_bytes().split(b"\0")
    except OSError:
        return False


def is_pty_host(pid: int) -> bool:
    """One of Cursor's utility processes (its pty host serves every terminal of every window of that Cursor)."""
    try:
        exe = os.readlink(PROC / str(pid) / "exe").removesuffix(" (deleted)")
    except OSError:
        return False
    return Path(exe).name == "cursor" and b"--type=utility" in _cmdline(pid).split(b"\0")


def shell_of(pid: int) -> int:
    """The Cursor terminal's top process that pid runs under, on the same terminal: the process whose parent is
    Cursor's pty host and which was started as a terminal's (TERM_PROGRAM=vscode). 0 when pid isn't in a Cursor
    terminal, or Claude's background daemon is in between (its sessions are elsewhere), or anything can't be read."""
    here, line = pid, tty(pid)
    if line <= 0:
        return 0
    for _ in range(64):
        if CLAUDE_DAEMON in _cmdline(here):
            return 0
        up = parent(here)
        if up <= 1:
            return 0
        if is_pty_host(up):
            return here if _in_vscode_terminal(here) and tty(here) == line else 0
        here = up
    return 0


def tab_of_shell(shell: int) -> Tab:
    started = born(shell) if shell > 0 else 0
    return Tab(OWNER, f"{shell}@{started}") if started else Tab()


def parse(tab: Tab) -> tuple[int, int]:
    """(shell pid, its start time) from a tab's address; (0, 0) for another owner's tab or a malformed one."""
    found = re.fullmatch(r"(\d+)@(\d+)", tab.address) if tab and tab.owner == OWNER else None
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)


def front_of(tab: Tab) -> tuple[int, int]:
    """(the process in front of that terminal, the agent it is or starts) when an agent is in front; (0, 0) when
    the shell is gone, is another process now (start time), or anything but an agent is in front."""
    shell, started = parse(tab)
    if not shell or born(shell) != started:
        return 0, 0
    front = foreground(shell)
    if front <= 0:
        return 0, 0
    if front == shell:  # the terminal runs the agent itself, or its shell is waiting for a command
        return (shell, shell) if _agent(shell) else (0, 0)
    if tty(front) != tty(shell):
        return 0, 0
    if _agent(front):
        return front, front
    agents = [c for c in _children(front) if _agent(c) and _field(c, 2) == front]
    return (front, agents[0]) if len(agents) == 1 else (0, 0)  # a wrapper that started the agent


def agent_pid(tab: Tab) -> int:
    return front_of(tab)[1]


def tab_for(pid: int) -> Tab:
    """The Cursor terminal tab whose agent in front is that process; empty when it isn't one."""
    tab = tab_of_shell(shell_of(pid))
    return tab if tab and agent_pid(tab) == pid else Tab()


def tab_of_caller() -> Tab | None:
    """The calling agent's Cursor terminal tab. None when the caller isn't in a Cursor terminal (another terminal
    is then asked); an empty tab when it is in one but that terminal's agent in front isn't the caller: then no
    other terminal's inherited address may be taken either."""
    from voice.agents import nearest_agent
    agent = nearest_agent()
    if agent is None or agent.is_editor:
        return None
    shell = shell_of(agent.pid)
    if not shell:
        return None
    tab = tab_of_shell(shell)
    return tab if agent_pid(tab) == agent.pid else Tab()


def _table() -> dict[int, int]:
    """Every process with its parent."""
    found = {}
    for entry in PROC.iterdir():
        if entry.name.isdigit():
            up = parent(int(entry.name))
            if up:
                found[int(entry.name)] = up
    return found


def shells(table: dict[int, int] | None = None) -> list[int]:
    """Every Cursor terminal's top process, in every running Cursor."""
    table = _table() if table is None else table
    hosts = {up for up in set(table.values()) if is_pty_host(up)}
    return sorted(pid for pid, up in table.items() if up in hosts and _in_vscode_terminal(pid))


# The bridges

def version(window: dict) -> tuple[int, ...]:
    """A bridge note's version; (0,) for one that doesn't say (bridges before 0.2.0)."""
    found = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(window.get("version", "")))
    return tuple(int(n) for n in found.groups()) if found else (0,)


def helper_of(note: Path) -> Path:
    """The reload helper's socket in the same window as that bridge note (both run in its extension host)."""
    return note.with_suffix(".reload")


def _listed(note: Path, socket_path: Path | None = None, wait: float = 3.0) -> list[int] | None:
    """The terminal pids one window lists (its bridge, or its reload helper); None when it didn't answer a list."""
    from voice.desktop.cursor_bridge import ask_note
    said = ask_note(note, {"terminals": True}, socket_path=socket_path, wait=wait)
    if not said or not said.get("ok") or not isinstance(said.get("terminals"), list):
        return None
    return [t["pid"] for t in said["terminals"] if isinstance(t, dict) and isinstance(t.get("pid"), int)]


def _same_cursor(window: dict, shell: int) -> bool:
    """That bridge's window belongs to the Cursor whose pty host runs the shell (both are children of its main
    process). Unknown counts as the same: then it is asked too."""
    pid = window.get("pid")
    mine, theirs = parent(parent(shell)), parent(pid) if isinstance(pid, int) else 0
    return not mine or not theirs or mine == theirs


def owner(tab: Tab) -> tuple[Path | None, tuple[Path, dict] | None, str]:
    """(the note of the one window whose bridge can type into that terminal, else the one window running an older
    bridge whose reload helper lists that terminal, why neither). Every window of that Cursor is asked; more than
    one claiming it: none."""
    from voice.desktop.cursor_bridge import live_notes
    shell, started = parse(tab)
    if not shell or born(shell) != started:
        return None, None, "that terminal is gone"
    mine = [(note, window) for note, window in live_notes() if _same_cursor(window, shell)]
    new, old = [], []
    for note, window in mine:
        if version(window) >= NEEDED:
            if shell in (_listed(note) or []):
                new.append(note)
        elif helper_of(note).exists() and shell in (_listed(note, helper_of(note)) or []):
            old.append((note, window))
    if len(new) + len(old) > 1:
        return None, None, "two Cursor windows say that terminal is theirs"
    if new:
        return new[0], None, ""
    if old:
        return None, old[0], "that Cursor window runs an older Henchline bridge"
    if any(version(window) < NEEDED for _, window in mine):
        return None, None, ("its Cursor window may run an older Henchline bridge without the reload helper: run "
                            "./install.sh agents, or Reload Window in it")
    return None, None, ("no Cursor window lists that terminal (is the Henchline bridge installed? "
                        "./install.sh agents)")


BRIDGE_ID = "henchline.henchline-cursor-bridge"


def installed_version() -> tuple[int, ...]:
    """The bridge a reloaded window loads: the newest one Cursor's own list of extensions has that it hasn't marked
    obsolete. (0,) when that list can't be read."""
    folder = Path.home() / ".cursor/extensions"
    try:
        listed = json.loads((folder / "extensions.json").read_text())
    except (OSError, ValueError):
        return (0,)
    try:
        gone = json.loads((folder / ".obsolete").read_text())
    except FileNotFoundError:
        gone = {}
    except (OSError, ValueError):
        return (0,)
    best: tuple[int, ...] = (0,)
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict) or (entry.get("identifier") or {}).get("id") != BRIDGE_ID:
            continue
        if isinstance(gone, dict) and gone.get(entry.get("relativeLocation")):
            continue
        best = max(best, version(entry))
    return best


def _reloads() -> dict:
    """What happened at each Cursor's last reload: {main pid: {state, at, why}}."""
    from voice.desktop.cursor_bridge import DIR
    try:
        found = json.loads((DIR / RELOADS).read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _set_reload(cursor: int, state: str, why: str = "", only_if: str = "") -> None:
    """Records a Cursor's reload state; atomically, under its own small lock (the call server, hands-free and a
    background reload all write it). only_if: changed only while the record still says that state. OSError when
    it can't be written."""
    from voice.desktop.cursor_bridge import DIR
    DIR.mkdir(parents=True, exist_ok=True)
    with open(DIR / "reloads.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now, every = time.time(), _reloads()
        if only_if and (every.get(str(cursor)) or {}).get("state") != only_if:
            return
        done = {k: v for k, v in every.items() if isinstance(v, dict) and now - float(v.get("at") or 0) < RELOAD_AGAIN}
        if state:
            done[str(cursor)] = {"state": state, "at": now, "why": why}
        else:
            done.pop(str(cursor), None)
        path = DIR / f".{RELOADS}.{os.getpid()}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(done))
        os.replace(path, DIR / RELOADS)


@contextlib.contextmanager
def _reload_lock(wait: float = 5.0):
    """One reload at a time, across every process. Yields whether it was taken."""
    from voice.desktop.cursor_bridge import DIR
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        handle = open(DIR / "reload.lock", "w")
    except OSError:
        yield False
        return
    with handle:
        until = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > until:
                    yield False
                    return
                time.sleep(0.3)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _cursor_of(window: dict) -> int:
    pid = window.get("pid")
    return parent(pid) if isinstance(pid, int) else 0


REPO = Path(__file__).resolve().parents[2]


def _start_reload(tab: Tab) -> None:
    """The reload, in a process of its own: its checks and the window coming back take a while, and whoever is
    delivering (hands-free hearing the user, above all) must not wait for them."""
    import subprocess
    import sys
    subprocess.Popen([sys.executable, "-m", "voice.desktop.cursor_term", "reload", tab.address], cwd=str(REPO),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def reach(tab: Tab, reload: bool = True) -> tuple[Path | None, str]:
    """The note of the window whose bridge can type into that terminal; else None and why not, at once. An older
    bridge in the one window that holds it (its reload helper says so): that window is reloaded in the background
    (reload), and only while nothing in it is working; meanwhile the answer is to try again in a moment."""
    note, old, why = owner(tab)
    if note or old is None or not reload:
        return note, why
    if installed_version() < NEEDED:
        return None, "Cursor has an older Henchline bridge installed: run ./install.sh agents, then try again"
    cursor = _cursor_of(old[1])
    last = _reloads().get(str(cursor)) or {}
    age = time.time() - float(last.get("at") or 0) if isinstance(last, dict) else RELOAD_AGAIN
    state = last.get("state") if isinstance(last, dict) else ""
    soon = "that Cursor window is reloading to load the new Henchline bridge: say it again in a moment"
    if state == "checking" and age < CHECKING_FOR:
        return None, soon
    if state == "reloaded" and age < RELOAD_AGAIN:
        return None, soon if age < RELOAD_WAIT else \
            "that Cursor window was reloaded a few minutes ago and its bridge is still the older one"
    if state == "busy" and age < BUSY_AGAIN:
        return None, f"that Cursor window runs an older Henchline bridge, and I won't reload it while {last.get('why')}"
    try:
        _set_reload(cursor, "checking")
        _start_reload(tab)
    except OSError as e:
        with contextlib.suppress(OSError):
            _set_reload(cursor, "", only_if="checking")
        return None, f"that Cursor window runs an older Henchline bridge, and its reload couldn't start ({e.strerror})"
    return None, ("that Cursor window runs an older Henchline bridge: I'm reloading it if nothing in it is working, "
                  "so say it again in a moment")


def reload_now(address: str) -> int:
    """The background half of reach(): checks the window that holds that terminal and reloads it when idle."""
    from voice.desktop.cursor_bridge import ask_note
    tab = Tab(OWNER, address)
    cursor, owned = 0, False
    try:
        with _reload_lock() as mine:
            if not mine:
                return 1  # another one is at it: its record stands, and the finally below leaves it
            owned = True
            note, old, _ = owner(tab)
            if note or old is None:
                return 0
            old_note, window = old
            cursor = _cursor_of(window)
            last = _reloads().get(str(cursor)) or {}
            if isinstance(last, dict) and last.get("state") == "reloaded" and \
                    time.time() - float(last.get("at") or 0) < RELOAD_WAIT:
                return 0  # just reloaded by another: its new bridge is still coming up
            busy = window_busy(window)
            if busy:
                _set_reload(cursor, "busy", busy)
                return 1
            said = ask_note(old_note, {"reload": True}, socket_path=helper_of(old_note))
            _set_reload(cursor, "reloaded" if said and said.get("ok") else "failed")
            return 0 if said and said.get("ok") else 1
    finally:
        # Any way out without an outcome: nobody is told "reloading" for nothing (only by the one doing the check)
        with contextlib.suppress(OSError):
            for main in {cursor, *_checking_for(address)} if owned else ():
                _set_reload(main, "", only_if="checking")


def _checking_for(address: str) -> set[int]:
    """The Cursor reach() marked "checking" for that terminal (its pty host's parent), when it can be told."""
    shell, _ = parse(Tab(OWNER, address))
    main = parent(parent(shell)) if shell else 0
    return {main} if main else set()


def _check(tab: Tab, front: int, agent: int) -> dict:
    """What the bridge checks right before each write: the shell, the program in front and the agent, with their
    start times."""
    shell, started = parse(tab)
    return {"shell": shell, "shell_born": started, "front": front, "front_born": born(front),
            "agent": agent, "agent_born": born(agent)}


def reading(tab: Tab, front: int, agent: int) -> bool:
    """Another program in the agent's process group reads the terminal (an editor it opened, say): words typed
    now would go to that program. Unsure: yes."""
    shell, _ = parse(tab)
    try:
        line = os.readlink(PROC / str(shell) / "fd" / "0")
    except OSError:
        return True
    for entry in PROC.iterdir():
        if not entry.name.isdigit() or int(entry.name) in (front, agent) or _field(int(entry.name), 2) != front:
            continue
        with contextlib.suppress(OSError):
            if os.readlink(entry / "fd" / "0") == line:
                return True
    return False


def _workspace(note: Path) -> str:
    """A bridge note's window, by its workspace's name, for the user."""
    try:
        name = str(json.loads(note.read_text()).get("workspace") or "")
    except (OSError, ValueError, AttributeError):
        name = ""
    return f"“{name}”" if name else "(unnamed)"


def _note(tab: Tab, why: str) -> None:
    """Kept per thread: two deliveries to one terminal at once each read back their own."""
    _why[(threading.get_ident(), tab.address)] = why


def _refused(tab: Tab, why: str) -> bool:
    _note(tab, why)
    return False


def why_not(tab: Tab) -> str:
    """Why the last typing into that tab was refused ("" when it wasn't, or nothing was said)."""
    return _why.get((threading.get_ident(), tab.address), "") if tab else ""


def type_line(tab: Tab, text: str, again: bool = False, taken=None) -> bool:
    """The words, then Enter, through the bridge of the window that shows that terminal. again: Enter again a few
    times after (the Cursor CLI's steer), until `taken` says its log has the words, each Enter checked the same way."""
    _why.pop((threading.get_ident(), tab.address), None)
    front, agent = front_of(tab)
    if not agent:
        return _refused(tab, "no agent is in front in that terminal")
    if reading(tab, front, agent):
        return _refused(tab, "another program the agent started is reading that terminal")
    note, why = reach(tab)
    if note is None:
        return _refused(tab, why)
    from voice.desktop.cursor_bridge import ask_note
    check = _check(tab, front, agent)
    request = {"type": {**check, "text": typeable(text), "enter": True}}
    if len(json.dumps(request).encode()) + 1 > MOST_LINE:
        return _refused(tab, "that message is too long to type into a terminal")
    said = ask_note(note, request, wait=15)
    if said and said.get("unknown"):
        # Sent, but no answer came back: it may well have been typed, and typing it again could send it twice
        _note(tab, f"Cursor's window {_workspace(note)} got the words but didn't say whether it typed them")
        return True
    if not said or not said.get("ok"):
        return _refused(tab, str((said or {}).get("why") or "the Henchline bridge in Cursor didn't answer"))
    if again:
        from voice.desktop.tmux import steer_after

        def press() -> bool:
            answer = ask_note(note, {"enter": check})
            return bool(answer and answer.get("ok"))
        steer_after(press, taken=taken, limit=10 if taken is not None else 1, pause=1.0 if taken is not None else 0.4)
    return True


def interrupt(tab: Tab) -> bool:
    """Escape, as typed in that terminal (the phone's stop), with the same checks. Never reloads a window: an agent
    being stopped is working, so its window wouldn't be reloaded anyway."""
    front, agent = front_of(tab)
    if not agent or reading(tab, front, agent):
        return False
    note, _ = reach(tab, reload=False)
    if note is None:
        return False
    from voice.desktop.cursor_bridge import ask_note
    said = ask_note(note, {"escape": _check(tab, front, agent)})
    return bool(said and said.get("ok"))


def _claimed() -> dict[int, int]:
    """How many windows list each terminal pid (bridges 0.2.0 on, and older windows' reload helpers); kept
    CLAIMS_FOR seconds."""
    global _claims
    at, found = _claims
    if time.monotonic() - at < CLAIMS_FOR:
        return found
    from voice.desktop.cursor_bridge import live_notes
    found = {}
    for note, window in live_notes():
        if version(window) >= NEEDED:
            listed = _listed(note, wait=2.0)
        elif helper_of(note).exists():
            listed = _listed(note, helper_of(note), wait=2.0)
        else:
            continue
        for pid in listed or []:
            found[pid] = found.get(pid, 0) + 1
    _claims = (time.monotonic(), found)
    return found


def agent_tabs() -> list[tuple[Tab, int]]:
    """Every Cursor terminal with an agent in front that words can reach: listed by exactly one window (its bridge,
    or, for a window with an older bridge, reloaded when it is idle, its reload helper)."""
    every = shells()
    if not every:
        return []
    claimed, found = _claimed(), []
    for shell in every:
        tab = tab_of_shell(shell)
        agent = agent_pid(tab)
        if agent and claimed.get(shell) == 1:
            found.append((tab, agent))
    return found


# Is anything in a Cursor window working? (before it is reloaded: unsure is yes)

def window_busy(window: dict) -> str:
    """Why that Cursor window must not be reloaded now ("" when nothing in it works): its terminals would close, a
    Cursor chat is generating or was just written to, or an agent in a terminal of that Cursor is in the middle of
    a turn. Anything unreadable is busy."""
    try:
        return _terminals_close(window) or _chats_busy(window) or _terminals_busy(window)
    except Exception as e:  # noqa: BLE001 - fail closed
        return f"I couldn't check what it is doing ({type(e).__name__})"


USER = Path.home() / ".config/Cursor/User"
PERSISTENT = re.compile(r'"terminal\.integrated\.enablePersistentSessions"\s*:\s*false')


def _workspace_file(label: str) -> Path | None:
    """The .code-workspace file of a window titled "<name> (Workspace)", from Cursor's own records; None when not
    exactly one matches."""
    name = label.removesuffix(" (Workspace)")
    found = set()
    for record in (USER / "workspaceStorage").glob("*/workspace.json"):
        try:
            uri = str(json.loads(record.read_text()).get("workspace") or "")
        except (OSError, ValueError, AttributeError):
            continue
        if uri.startswith("file://"):
            path = Path(urllib.parse.unquote(uri.removeprefix("file://")))
            if path.stem == name:
                found.add(path)
    return found.pop() if len(found) == 1 else None


def _terminals_close(window: dict) -> str:
    """A reload keeps a window's terminals and what runs in them (Cursor's persistent sessions, on by default);
    with that setting off, it would end every agent in them. Read from every place it can be set; a place that
    can't be told (a profile, an unknown workspace file) is unsure."""
    profiles = USER / "profiles"
    if profiles.is_dir() and any(profiles.iterdir()):
        return "Cursor has profiles, and I can't tell whether its terminals would survive a reload"
    files = [USER / "settings.json"]
    files += [Path(f) / ".vscode/settings.json" for f in window.get("folders") or [] if isinstance(f, str)]
    label = str(window.get("workspace") or "")
    if label.endswith(" (Workspace)"):
        workspace = _workspace_file(label)
        if workspace is None:
            return "I can't find its workspace file to tell whether its terminals would survive a reload"
        files.append(workspace)
    for settings in files:
        try:
            text = settings.read_text()
        except FileNotFoundError:
            continue
        except OSError:
            return "its settings can't be read"
        if PERSISTENT.search(text):
            return "its terminals would close (terminal.integrated.enablePersistentSessions is off)"
    return ""


def _chats_busy(window: dict) -> str:
    from voice.desktop import cursor_cdp
    from voice.desktop.cursor_bridge import STATE_DB, project_of
    workspace = str(window.get("workspace") or "")
    page = cursor_cdp.generating(workspace) if workspace else None
    if page is None:
        return "I can't see its Cursor chats (Cursor's debugging port isn't open for it)"
    if page:
        return "a Cursor chat in it is working"
    folders = [f for f in window.get("folders") or [] if isinstance(f, str)]
    every = [Path.home() / ".cursor/projects" / project_of(f) / "agent-transcripts" for f in folders]
    if not any(t.is_dir() for t in every):
        return "I can't tell which Cursor chats are in it"
    now, recent = time.time(), []
    for transcripts in (t for t in every if t.is_dir()):
        for chat in transcripts.iterdir():
            try:
                changed = max(p.stat().st_mtime for p in (chat, *chat.glob("*.jsonl")))
            except (OSError, ValueError):
                return "a Cursor chat's transcript can't be read"
            if now - changed < CHAT_QUIET:
                return "a Cursor chat in it was working a moment ago"
            if now - changed < CHAT_RECENT:
                recent.append(chat.name)
    if not recent:
        return ""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=2)) as db:
            marks = ",".join("?" * len(recent))
            rows = db.execute(f"select value from cursorDiskKV where key in ({marks})",
                              [f"composerData:{c}" for c in recent]).fetchall()
    except sqlite3.Error:
        return "Cursor's store can't be read"
    for (value,) in rows:
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            return "Cursor's store can't be read"
        if isinstance(data, dict) and data.get("status") == "generating":
            return "a Cursor chat in it is working"
    return ""


def _terminals_busy(window: dict) -> str:
    """Every agent under that Cursor's terminals (which window shows which can't be told before the reload)."""
    table = _table()
    pid = window.get("pid")
    cursor = table.get(pid, 0) if isinstance(pid, int) else 0
    hosts = {up for up in set(table.values()) if is_pty_host(up) and (not cursor or table.get(up) == cursor)}
    below: dict[int, list[int]] = {}
    for child, up in table.items():
        below.setdefault(up, []).append(child)
    todo = [c for h in hosts for c in below.get(h, [])]
    seen = set()
    while todo:
        here = todo.pop()
        if here in seen:
            continue
        seen.add(here)
        todo.extend(below.get(here, []))
        agent = _agent(here)
        if agent:
            why = agent_busy(here, agent)
            if why:
                return why
    return ""


def agent_busy(pid: int, agent: str) -> str:
    """Why that agent counts as working ("" when its own record says its turn is over)."""
    if agent == "claude":
        return _claude_busy(pid)
    if agent == "codex":
        return _codex_busy(pid)
    if agent == "cursor":
        return _cursor_cli_busy(pid)
    return f"{agent} runs in one of its terminals, and I can't tell whether it is working"


def _quiet(path: Path, seconds: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime >= seconds
    except OSError:
        return True  # not there (yet): nothing written


def _claude_busy(pid: int) -> str:
    from voice import pickup, resume
    chat = resume.claude_record(pid)
    if not chat:
        return "a Claude in one of its terminals can't be read"
    logs = list((Path.home() / ".claude/projects").glob(f"*/{chat}.jsonl"))
    if not logs:
        return ""  # a new session that hasn't been sent anything
    if len(logs) > 1:
        return "a Claude in one of its terminals can't be read"
    if not _quiet(logs[0], AGENT_QUIET):
        return "a Claude in one of its terminals is working"
    for entry in pickup._backwards(logs[0], most=16 * 1024 * 1024):
        if entry.get("isSidechain"):
            continue
        kind, message = entry.get("type"), entry.get("message")
        if kind == "system" and entry.get("subtype") in ("turn_duration", "stop_hook_summary"):
            return ""
        if kind == "assistant" and isinstance(message, dict) and message.get("stop_reason"):
            return "" if message.get("stop_reason") != "tool_use" else "a Claude in one of its terminals is working"
        if kind == "user" and isinstance(message, dict) and not entry.get("isMeta") \
                and not entry.get("isCompactSummary"):
            content = message.get("content")
            text = content if isinstance(content, str) else " ".join(
                str(p.get("text", "")) for p in content or [] if isinstance(p, dict) and p.get("type") == "text")
            if "[Request interrupted by user" in text:
                return ""
            if not text.lstrip().startswith(("<local-command", "<command-name>", "<bash-")):
                return "a Claude in one of its terminals is working"
    return ""


def _open_files(pid: int) -> list[str]:
    try:
        links = list((PROC / str(pid) / "fd").iterdir())
    except OSError:
        return []
    found = []
    for link in links:
        with contextlib.suppress(OSError):
            found.append(os.readlink(link))
    return found


def _codex_busy(pid: int) -> str:
    from voice import pickup
    rolls = [Path(f) for f in _open_files(pid) if re.search(r"/rollout-[^/]*\.jsonl$", f)]
    if not rolls:
        try:
            folder = os.readlink(PROC / str(pid) / "cwd")
        except OSError:
            return "a Codex in one of its terminals can't be read"
        newest = sorted((Path.home() / ".codex/sessions").glob("*/*/*/rollout-*.jsonl"),
                        key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)[:30]
        for roll in newest:
            try:
                with roll.open("rb") as f:
                    meta = json.loads(f.readline(1024 * 1024) or b"{}")
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and str((meta.get("payload") or {}).get("cwd", "")) == folder:
                rolls = [roll]
                break
    if len(rolls) != 1:
        return "a Codex in one of its terminals can't be read"
    if not _quiet(rolls[0], AGENT_QUIET):
        return "a Codex in one of its terminals is working"
    for entry in pickup._backwards(rolls[0]):
        event = (entry.get("payload") or {}).get("type") if entry.get("type") == "event_msg" else None
        if event == "task_started":
            return "a Codex in one of its terminals is working"
        if event in ("task_complete", "turn_aborted"):
            return ""
    return ""


def _cpu(pid: int) -> int:
    return _field(pid, 11) + _field(pid, 12)  # utime + stime, in clock ticks


def _cursor_cli_busy(pid: int) -> str:
    """The Cursor CLI's store says nothing readable about a turn: it must have been quiet a while, and the CLI
    itself nearly idle (it redraws its spinner while it works)."""
    stores = [Path(f) for f in _open_files(pid) if f.endswith("/store.db")]
    if len(stores) != 1:
        return "a Cursor CLI in one of its terminals can't be read"
    store = stores[0]
    if not all(_quiet(Path(f"{store}{end}"), CURSOR_CLI_QUIET) for end in ("", "-wal", "-journal")):
        return "a Cursor CLI in one of its terminals is working"
    before = _cpu(pid)
    time.sleep(CPU_SAMPLE)
    used = (_cpu(pid) - before) / os.sysconf("SC_CLK_TCK")
    if used / CPU_SAMPLE >= CPU_BUSY:
        return "a Cursor CLI in one of its terminals is working"
    return ""


class CursorTerminals(Terminals):
    """Cursor's terminal panel in front of the desktop's own terminals: its tabs here, every other tab there."""

    def __init__(self, inner: Terminals) -> None:
        self.inner = inner

    def tab_of_caller(self) -> Tab:
        try:
            mine = tab_of_caller()
        except Exception:  # noqa: BLE001 - this terminal unknown: the desktop's own are asked
            mine = None
        return self.inner.tab_of_caller() if mine is None else mine

    def agent_running(self, tab: Tab) -> bool:
        return agent_pid(tab) > 0 if tab.owner == OWNER else self.inner.agent_running(tab)

    def type_line(self, tab: Tab, text: str, again: bool = False, taken=None) -> bool:
        if tab.owner == OWNER:
            return type_line(tab, text, again=again, taken=taken)
        return self.inner.type_line(tab, text, again=again, taken=taken)

    def only_a_shell(self, window: Window) -> bool:
        return self.inner.only_a_shell(window)

    def interrupt(self, tab: Tab) -> bool:
        return interrupt(tab) if tab.owner == OWNER else self.inner.interrupt(tab)

    def agent_pid(self, tab: Tab) -> int:
        return agent_pid(tab) if tab.owner == OWNER else self.inner.agent_pid(tab)

    def agent_tabs(self) -> list[tuple[Tab, int]]:
        try:
            mine = agent_tabs()
        except Exception:  # noqa: BLE001 - Cursor's terminals unknown: the others still count
            mine = []
        return [*self.inner.agent_tabs(), *mine]


if __name__ == "__main__":  # `python -m voice.desktop.cursor_term reload ADDRESS`: reach()'s background reload
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "reload":
        sys.exit(reload_now(sys.argv[2]))
    sys.exit(2)
