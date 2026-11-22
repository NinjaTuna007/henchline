"""Agents in tmux: the ones Henchline starts from the phone (a new chat), and any the user runs in tmux.

A tmux pane is a tab whose owner is "tmux" and whose address is the pane id (%12). Typing into it needs no
window, no focus and no desktop: `tmux send-keys`. So an agent started from the phone works on any Linux
(the user's point, 2026-11-16: not tied to KDE), and the user can watch it with `tmux attach -t henchline`.
WithTmux puts this in front of the desktop's own terminals: tmux panes here, every other tab there.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from voice.agents import process
from voice.desktop import Tab, Terminals, Window, typeable

OWNER = "tmux"
SESSION = "henchline"  # where the phone's new chats run: `tmux attach -t henchline`
TYPE_PAUSE = 0.15  # seconds between the text and its Enter, as for Konsole
# What a new chat starts, per agent, never stopping to ask for a keypress the phone can't give (the user's point,
# 2026-11-16): Claude in auto mode (a classifier decides; what it refuses is refused, not asked), the Cursor CLI on
# Run Everything; Codex sends its approvals to its own reviewer (approvals_reviewer = "auto_review" in its config)
COMMANDS = {"claude": ["claude", "--permission-mode", "auto"], "codex": ["codex"],
            "cursor": ["cursor-agent", "--force"]}


def tmux(*args: str, own_scope: bool = False, given: str | None = None) -> subprocess.CompletedProcess | None:
    """own_scope: the tmux server started in a systemd scope of its own, so restarting the service that asked
    (the call server) doesn't take every agent started from the phone down with its cgroup."""
    scope = ["systemd-run", "--user", "--scope", "--quiet", "--collect"] \
        if own_scope and shutil.which("systemd-run") else []
    try:
        return subprocess.run([*scope, "tmux", *args], input=given, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None


def out(*args: str) -> str:
    done = tmux(*args)
    return done.stdout.strip() if done and done.returncode == 0 else ""


def agent_in(pane_pid: int) -> int:
    """The agent the pane runs: the pane's own process, or one of its children (a shell that started it)."""
    if (p := process(pane_pid)) and p.agent:
        return pane_pid
    try:
        children = Path(f"/proc/{pane_pid}/task/{pane_pid}/children").read_text().split()
    except OSError:
        return 0
    return next((int(c) for c in children if (p := process(int(c))) and p.agent), 0)


# Claude Code's aliases for the newest of each model (`claude --model`, `/model`)
CLAUDE_MODELS = [("opus", "Opus"), ("sonnet", "Sonnet"), ("fable", "Fable"), ("haiku", "Haiku")]
MODEL_FLAG = {"claude": "--model", "codex": "-m", "cursor": "--model"}
_cursor_models: tuple[float, list[dict]] = (0.0, [])


def models(agent: str) -> list[dict]:
    """The models a new chat (or a switch) can pick for that agent: [{id, name}], as each CLI lists them."""
    global _cursor_models
    if agent == "claude":
        return [{"id": i, "name": n} for i, n in CLAUDE_MODELS]
    if agent == "codex":  # Codex's own cache of the account's models; the hidden ones (its reviewer) left out
        try:
            listed = json.loads((Path.home() / ".codex/models_cache.json").read_text()).get("models", [])
        except (OSError, ValueError, AttributeError):
            return []
        return [{"id": m["slug"], "name": m.get("display_name", m["slug"])} for m in listed
                if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"]
    if agent == "cursor":  # `cursor-agent models`: "slug - Name" lines; asked at most every ten minutes
        at, found = _cursor_models
        if time.monotonic() - at > 600 or not found:
            try:
                listing = subprocess.run([shutil.which("cursor-agent") or "cursor-agent", "models"],
                                         capture_output=True, text=True, timeout=20).stdout
            except (OSError, subprocess.TimeoutExpired):
                listing = ""
            lines = (line.partition(" - ") for line in listing.splitlines())
            found = [{"id": slug.strip(), "name": " ".join(name.split())}
                     for slug, _, name in lines if name and " " not in slug.strip()]
            _cursor_models = (time.monotonic(), found)
        return found
    return []


# Claude's models by their exact names (not only the family alias), each with whether it has the 1M context
CLAUDE_MENU = [("claude-opus-5-5", "Opus 5.5", True), ("claude-sonnet-5-5", "Sonnet 5.5", True),
               ("claude-fable-5-1", "Fable 5.1", True), ("claude-haiku-5-5", "Haiku 5.5", False)]
CLAUDE_EFFORTS = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("xhigh", "Extra High"), ("max", "Max")]


def menu(agent: str) -> list[dict]:
    """The model menu for an agent, as Cursor's has it (the user's wish, 2026-11-16: the same across the board):
    [{id, name, params: [{id, name, kind, values: [{value, name}]}]}]. Claude: exact model, effort, context; Codex:
    model, effort. [] for an agent with only a flat list (the Cursor CLI)."""
    def enum(pid: str, name: str, values: list[tuple[str, str]]) -> dict:
        return {"id": pid, "name": name, "kind": "enum", "values": [{"value": v, "name": n} for v, n in values]}
    if agent == "claude":
        return [{"id": mid, "name": name,
                 "params": [enum("effort", "Effort", CLAUDE_EFFORTS)]
                 + ([enum("context", "Context", [("standard", "Standard"), ("1m", "1M")])] if long else [])}
                for mid, name, long in CLAUDE_MENU]
    if agent == "codex":
        try:
            listed = json.loads((Path.home() / ".codex/models_cache.json").read_text()).get("models", [])
        except (OSError, ValueError, AttributeError):
            return []
        return [{"id": m["slug"], "name": m.get("display_name", m["slug"]),
                 "params": [enum("effort", "Effort", [(lv["effort"], lv["effort"].capitalize())
                                                      for lv in m.get("supported_reasoning_levels") or []
                                                      if isinstance(lv, dict) and lv.get("effort")])]}
                for m in listed if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"]
    return []


def checked(agent: str, model: str, params: dict) -> dict | None:
    """The settings, if that model in the agent's menu has each of them; None when one isn't offered."""
    entry = next((m for m in menu(agent) if m["id"] == model), None)
    if entry is None:
        return None
    allowed = {p["id"]: {v["value"] for v in p["values"]} for p in entry["params"]}
    return dict(params) if all(v in allowed.get(k, set()) for k, v in params.items()) else None


def model_args(agent: str, model: str, params: dict) -> list[str]:
    """The command-line words that start an agent on a model with its settings (all checked by the caller)."""
    if agent == "claude":
        name = model + ("[1m]" if params.get("context") == "1m" else "")
        return ["--model", name] + (["--effort", params["effort"]] if params.get("effort") else [])
    if agent == "codex":
        return ["-m", model] + (["-c", f'model_reasoning_effort="{params["effort"]}"'] if params.get("effort") else [])
    return [MODEL_FLAG[agent], model] if model and agent in MODEL_FLAG else []


def available() -> list[str]:
    """The agents a new chat can start: those installed, with tmux."""
    if not shutil.which("tmux"):
        return []
    return [agent for agent, command in COMMANDS.items() if shutil.which(command[0])]


# Another agent's marks: a tmux server first started by an agent (a Cursor chat's terminal) hands them to every new
# window, and a Claude started there introduced itself as that Cursor chat's workspace (one project for another, the
# user's catch, 2026-11-16)
INHERITED = re.compile(r"^(CURSOR_(?!API_KEY$)|__CURSOR|AGENT_TRANSCRIPTS$|CLAUDECODE$|CLAUDE_CODE_ENTRYPOINT$|"
                       r"CLAUDE_CODE_SESSION_ID$|CODEX_THREAD_ID$|CODEX_SANDBOX|TALKBACK_AGENT$)")
# A tmux server started from an agent's own shell can keep that shell's "no color" settings and give them to
# every pane. The Cursor CLI then draws no cursor block, after it has hidden the terminal's own cursor.
# NO_COLOR turns color off whenever it is set. FORCE_COLOR turns it off only for 0 and false; 1, 2, 3, true,
# and an empty value turn color on, and those are left as the server set them.
_FORCE_COLOR_OFF = {"0", "false"}


def _color_off(line: str) -> str:
    """The color variable to drop from a `show-environment -g` line, or "" when the line keeps its color."""
    name, sep, value = line.partition("=")
    if name.startswith("-") or not sep:
        return ""
    if name == "NO_COLOR":
        return name
    if name == "FORCE_COLOR" and value.lower() in _FORCE_COLOR_OFF:
        return name
    return ""


def clean_env() -> list[str]:
    """`env -u NAME …` for each agent's mark in tmux's own environment or ours: the new agent starts as itself."""
    names = {name for name in os.environ if INHERITED.match(name)}
    for line in out("show-environment", "-g").splitlines():
        bare = line.split("=", 1)[0].lstrip("-")
        if INHERITED.match(bare):
            names.add(bare)
        if off := _color_off(line):
            names.add(off)
    env = shutil.which("env")
    return [env, *(f"-u{name}" for name in sorted(names))] if env and names else []


def _window(folder: Path, name: str, argv: list[str]) -> Tab:
    """A tmux window running `argv` in that folder. The pane, or empty when it couldn't start."""
    program = shutil.which(argv[0]) if argv else ""
    if not program:
        return Tab()
    where = ["-c", str(folder), "-n", name, "-P", "-F", "#{pane_id}", *clean_env(), program, *argv[1:]]
    running = tmux("has-session", "-t", SESSION)
    if running and running.returncode == 0:
        pane = out("new-window", "-d", "-t", f"{SESSION}:", *where)
    else:
        started = tmux("new-session", "-d", "-s", SESSION, *where, own_scope=True)
        pane = started.stdout.strip() if started and started.returncode == 0 else ""
    return Tab(OWNER, pane) if pane.startswith("%") else Tab()


def start(agent: str, folder: Path, model: str = "", params: dict | None = None,
          flags: list[str] | None = None) -> Tab:
    """A new agent in its own tmux window in that folder; its tab, or an empty one when it couldn't start.
    flags: its permission flags instead of the new-chat ones (a deployed child gets no more than its boss)."""
    command = COMMANDS.get(agent, [""]) if flags is None else [COMMANDS.get(agent, [""])[0], *flags]
    chosen = model_args(agent, model, params or {}) if model else []  # checked by the caller
    return _window(folder, f"{agent}:{folder.name}", [*command, *chosen])


def resume(agent: str, folder: Path, chat: str) -> Tab:
    """That chat id, continued, in its own tmux window. Empty when this agent has no resume, or it didn't start.
    It comes back in the permission mode its own record says it last had, ask mode when that can't be read: a
    message to a chat the user closed must not reopen it running commands without asking."""
    from voice.permissions import resume_flags, resume_refusal
    if resume_refusal(agent, chat):
        return Tab()  # it would come back with more than it had: place() says why
    flags = resume_flags(agent, chat)
    argv = {"claude": ["claude", *flags, "--resume", chat],
            "cursor": ["cursor-agent", *flags, "--resume", chat],
            "codex": ["codex", "resume", *flags, chat]}.get(agent)
    return _window(folder, f"{agent}:{chat[:8]}", argv) if argv else Tab()


def _composer(lines: list[str]) -> list[str] | None:
    """Claude's prompt box: the lines between the last two rules drawn across the pane, the last rule near the
    bottom (only its footer below). None when the pane doesn't look like that, so the box can't be told apart
    from the transcript."""
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    rules = [i for i, line in enumerate(lines) if len(line.strip()) >= 20 and set(line.strip()) <= set("─━")]
    if len(rules) < 2 or len(lines) - 1 - rules[-1] > 3:
        return None
    return lines[rules[-2] + 1:rules[-1]]


DIALOG = re.compile(r"❯\s*\d+\.|do you want", re.I)  # a numbered choice: Enter would pick an answer


def _dialog(lines: list[str]) -> bool:
    """A question waiting for an answer below the transcript: after the last rule, or anywhere on a screen that
    isn't the usual box (a dialog replaces it). The transcript above, the user's own echoed words, doesn't count."""
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if _composer(lines) is not None:
        rules = [i for i, line in enumerate(lines) if len(line.strip()) >= 20 and set(line.strip()) <= set("─━")]
        lines = lines[rules[-1] + 1:]  # the footer: Claude's own questions are drawn there, not in the transcript
    return any(DIALOG.search(line) for line in lines)


def _pending(box: list[str], marker: str) -> bool:
    """The words are still in the box: its first line is the prompt mark and the marker, or a paste placeholder."""
    first = next((line.strip() for line in box if line.strip()), "")
    if first.startswith("❯") and first.removeprefix("❯").lstrip().startswith(marker):
        return True
    return any("[Pasted text" in line for line in box)


def composer_ready(tab: Tab, wait: float = 15.0) -> bool:
    """True once the agent has drawn its prompt box: text pasted while it is still starting can sit there unsent."""
    until = time.monotonic() + wait
    while time.monotonic() < until:
        box = _composer(out("capture-pane", "-p", "-t", tab.address).splitlines())
        if box is not None and any(line.lstrip().startswith(("❯", ">", "›")) for line in box):
            return True
        time.sleep(0.3)
    return False


def submit_pending(tab: Tab, marker: str, tries: int = 3, pause: float = 1.0) -> bool:
    """After typing, the words must have left the prompt box. A multi-line paste can swallow the Enter that
    follows it (a deploy's ask sat unsent, 2026-11-21), so Enter is pressed again while the box still holds the
    marker. Claude's layout only. True when the box no longer holds it, or the box can't be found (nothing to
    check against). Also True, with no press, when a question is waiting below the transcript: Enter would answer
    it, and what happened to the ask is unknown, so the pane is left for the user rather than ended."""
    for attempt in range(tries + 1):
        time.sleep(pause)
        lines = out("capture-pane", "-p", "-t", tab.address).splitlines()
        if _dialog(lines):
            return True
        box = _composer(lines)
        if box is None or not _pending(box, marker):
            return True
        if attempt < tries:
            tmux("send-keys", "-t", tab.address, "Enter")
    return False


def pane_pid(tab: Tab) -> int:
    """The pane's first process: with the pane id, says it is still the same pane (tmux reuses ids after a
    restart). 0 when it is gone, or when tmux could not be asked: callers that must not treat those the same
    use pane_there."""
    panes = pane_pids() if tab and tab.owner == OWNER else None
    return (panes or {}).get(tab.address if tab else "", 0)


def pane_there(tab: Tab) -> bool | None:
    """True when that tmux pane exists, False when tmux says it does not, None when tmux could not be asked.
    None is not a dead pane: a failed query must not start a second resume. Read from the list of panes: asked
    about one pane that isn't there, `display-message -t` answers nothing and still succeeds (tmux 3.4)."""
    if not tab or tab.owner != OWNER:
        return False
    panes = pane_pids()
    return None if panes is None else tab.address in panes


def server_started() -> float | None:
    """When the running tmux server started: pane ids count from %0 again on a new one. 0 when none runs, None
    when tmux couldn't be asked."""
    when = out("display-message", "-p", "#{start_time}")
    if when.isdigit():
        return float(when)
    return 0.0 if pane_pids() == {} else None


def pane_pids() -> dict[str, int] | None:
    """Every pane with its first process, in one call; {} when no tmux server runs (no panes), None when tmux
    couldn't be asked (slow, missing): then nothing is known gone."""
    done = tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_pid}")
    if done is None:
        return None
    if done.returncode != 0:
        # No panes only when there is no server at all: none running, or its socket isn't there. Any other failure
        # (permission denied on the socket, say) is unknown, and then nothing may be taken as gone
        err = done.stderr.lower()
        no_socket = "error connecting" in err and "no such file or directory" in err
        return {} if "no server running" in err or no_socket else None
    pairs = (line.split(" ", 1) for line in done.stdout.splitlines() if " " in line)
    return {pane: int(pid) for pane, pid in pairs if pid.isdigit()}


# Each agent's own question about a folder it hasn't trusted, and the answer that says yes: (words of the question,
# the cursor mark, the chosen line that is yes). Claude Code's default answer is to exit; Codex's is yes.
TRUST_QUESTIONS = {
    "claude": (("trust this folder", "quick safety check"), "❯", "❯ yes, i trust this folder"),
    "codex": (("do you trust the contents of this directory",), "›", "› 1. yes, continue"),
}


def accept_trust(tab: Tab, folder: Path, agent: str = "claude", wait: float = 12.0) -> bool:
    """An agent started in a folder it hasn't trusted yet asks first. Answered yes only when that very question is
    on screen: its wording and the folder's own path, in the pane's last lines, and the highlighted answer (moved
    down to it if need be) being the yes. Anything else is left for the user (`tmux attach -t henchline`). Only for
    a folder the user picked or typed on the phone: their first message must not be the answer (it was, in a
    test, 2026-11-16, and the agent exited)."""
    if agent not in TRUST_QUESTIONS:
        return False
    words, mark, yes = TRUST_QUESTIONS[agent]
    shown = str(folder).replace(str(Path.home()), "~", 1)

    def screen() -> list[str]:  # the bottom of the pane: where the question and its answers are
        return out("capture-pane", "-p", "-t", tab.address).splitlines()[-30:]

    def asking(lines: list[str]) -> bool:
        text = " ".join(lines).lower()
        return any(w in text for w in words) and any(shown in line or str(folder) in line for line in lines)

    def chosen(lines: list[str]) -> str:
        return next((line.strip().lower() for line in lines if line.lstrip().startswith(mark)), "")

    until = time.monotonic() + wait
    while time.monotonic() < until:
        lines = screen()
        if asking(lines) and chosen(lines):
            for _ in range(3):  # down the answers until the yes is the chosen one
                lines = screen()
                if not asking(lines):
                    return False
                if chosen(lines).startswith(yes):
                    done = tmux("send-keys", "-t", tab.address, "Enter")
                    return bool(done and done.returncode == 0)
                tmux("send-keys", "-t", tab.address, "Down")
                time.sleep(0.3)
            return False
        time.sleep(0.5)
    return False


def steer_after(press, taken=None, watch=None, limit: int = 8, pause: float = 0.45) -> None:
    """More Enters after the line is in. On a side thread: the "I got it" chime plays when the first Enter
    lands, and must not wait while these catch the line up."""
    def run() -> None:
        for _ in range(limit):
            time.sleep(pause)
            if taken is not None and taken():
                return
            if watch is not None and watch():
                return
            if not press():
                return
    threading.Thread(target=run, name="steer", daemon=True).start()


def end(tab: Tab) -> bool:
    return tab.owner == OWNER and (done := tmux("kill-pane", "-t", tab.address)) is not None and done.returncode == 0


class WithTmux(Terminals):
    def __init__(self, inner: Terminals) -> None:
        self.inner = inner

    def tab_of_caller(self) -> Tab:
        pane = os.environ.get("TMUX_PANE", "")
        if pane and os.environ.get("TMUX"):  # inside tmux, even in a Konsole tab: the pane is the precise place
            return Tab(OWNER, pane)
        return self.inner.tab_of_caller()

    def agent_running(self, tab: Tab) -> bool:
        if tab.owner != OWNER:
            return self.inner.agent_running(tab)
        pid = pane_pid(tab)
        return pid > 0 and agent_in(pid) > 0

    def type_line(self, tab: Tab, text: str, again: bool = False, taken=None) -> bool:
        if tab.owner != OWNER:
            return self.inner.type_line(tab, text, again=again, taken=taken)
        # Through a paste buffer from stdin: the words are never tmux arguments, so a leading "-" or a trailing ";"
        # (tmux's command separator) can't be read as tmux's own, and a newline doesn't send early
        # Control characters gone first: an ESC[201~ in the words would end the paste early and type the rest as keys
        buffer = f"henchline-{os.getpid()}-{time.monotonic_ns()}"
        loaded = tmux("load-buffer", "-b", buffer, "-", given=typeable(text))
        pasted = loaded and loaded.returncode == 0 and tmux("paste-buffer", "-d", "-p", "-b", buffer, "-t", tab.address)
        if not pasted or pasted.returncode != 0:
            tmux("delete-buffer", "-b", buffer)
            return False
        time.sleep(TYPE_PAUSE)
        entered = tmux("send-keys", "-t", tab.address, "Enter")
        if not (entered and entered.returncode == 0):
            return False
        if again:
            def press() -> bool:
                done = tmux("send-keys", "-t", tab.address, "Enter")
                return bool(done and done.returncode == 0)

            def clear() -> bool:
                shown = out("capture-pane", "-p", "-t", tab.address).lower()
                return "follow-ups" not in shown and "enter steer" not in shown

            if taken is not None:
                steer_after(press, taken=taken, limit=10, pause=1.0)
            else:
                steer_after(press, watch=clear)
        return True

    def only_a_shell(self, window: Window) -> bool:
        return self.inner.only_a_shell(window)

    def interrupt(self, tab: Tab) -> bool:
        if tab.owner != OWNER:
            return self.inner.interrupt(tab)
        done = tmux("send-keys", "-t", tab.address, "Escape")
        return bool(done and done.returncode == 0)

    def agent_pid(self, tab: Tab) -> int:
        if tab.owner != OWNER:
            return self.inner.agent_pid(tab)
        pid = pane_pid(tab)
        return agent_in(pid) if pid else 0

    def agent_tabs(self) -> list[tuple[Tab, int]]:
        found = list(self.inner.agent_tabs())
        for line in out("list-panes", "-a", "-F", "#{pane_id} #{pane_pid}").splitlines():
            pane, _, pid = line.partition(" ")
            if pid.isdigit() and (agent := agent_in(int(pid))):
                found.append((Tab(OWNER, pane), agent))
        return found
