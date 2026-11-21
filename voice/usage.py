"""How much each agent has used, for the phone's /usage (the user's wish, 2026-11-17: what Claude's /usage and
/context show in its terminal, as bars on the phone): its plan's limits and its context, from what each agent
already writes down, never from its screen.

- Claude: its status line's report (agents/claude-status.py): the 5-hour and weekly limits, the context, the cost.
- Codex: the last token count in its session log: the 5-hour and weekly limits, the context.
- Cursor: its own record of the chat: the context, and what fills it. Its plan's limits aren't kept on the laptop,
  so the Cursor CLI is asked: started out of sight in a folder of its own, sent /usage, read, closed (cursor_plan).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from voice.state import STATE

CLAUDE_STATUS = Path.home() / ".local/state/ptt/claude-status"
CODEX_SESSIONS = Path.home() / ".codex/sessions"


def _limit(label: str, percent, resets) -> dict | None:
    if not isinstance(percent, (int, float)):
        return None
    resets = int(resets) if isinstance(resets, (int, float)) else 0
    if resets and resets < time.time():  # read before the window ended: it has started over since
        percent, resets = 0, 0
    return {"label": label, "percent": round(float(percent), 1), "resets": resets}


def _window_name(minutes) -> str:
    if minutes == 300:
        return "5-hour session"
    if minutes == 10080:
        return "Week"
    if isinstance(minutes, (int, float)) and minutes:
        return f"{int(minutes) // 60} hours" if minutes < 2880 else f"{int(minutes) // 1440} days"
    return "Limit"


CLAUDE_SESSION = "Current session"  # Claude's own /usage wording, so the phone's card reads like its panel
CLAUDE_WEEK = "Current week (all models)"


def claude(session: str, status_dir: Path = CLAUDE_STATUS) -> dict:
    uuid = session.removeprefix("claude-")
    if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
        return {}
    try:
        status = json.loads((status_dir / f"{uuid}.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(status, dict):
        return {}
    limits = status.get("rate_limits") or {}
    window = status.get("context_window") or {}
    size, used = window.get("context_window_size"), window.get("used_percentage")
    current = window.get("current_usage") or {}
    tokens = sum(v for k, v in current.items() if k.endswith("input_tokens") and isinstance(v, int)) if isinstance(current, dict) else 0
    cost = (status.get("cost") or {}).get("total_cost_usd")
    return {
        "model": str((status.get("model") or {}).get("display_name", "")),
        "limits": [x for x in (_limit(CLAUDE_SESSION, (limits.get("five_hour") or {}).get("used_percentage"),
                                      (limits.get("five_hour") or {}).get("resets_at")),
                               _limit(CLAUDE_WEEK, (limits.get("seven_day") or {}).get("used_percentage"),
                                      (limits.get("seven_day") or {}).get("resets_at"))) if x],
        "context": {"percent": float(used), "size": int(size or 0), "used": tokens, "parts": []}
                   if isinstance(used, (int, float)) else None,
        "notes": [f"This session has cost ${cost:,.2f} at API prices"] if isinstance(cost, (int, float)) else [],
    }


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # gone since it was listed
        return 0.0


def _codex_log(folder_key: str, root: Path) -> Path | None:
    """The newest Codex session log in that folder (its key: the folder's path with odd characters as _)."""
    for log in sorted(root.glob("*/*/*/rollout-*.jsonl"), key=_mtime, reverse=True)[:30]:
        try:
            with log.open("rb") as f:
                meta = json.loads(f.readline(1024 * 1024) or b"{}")
        except (OSError, ValueError):
            continue
        cwd = str((meta.get("payload") or {}).get("cwd", "")) if isinstance(meta, dict) else ""
        if cwd and re.sub(r"[^A-Za-z0-9_.-]+", "_", cwd)[-120:] == folder_key[-120:]:
            return log
    return None


def codex_name(session: str, root: Path = CODEX_SESSIONS, index: Path | None = None) -> str:
    """The name a Codex conversation was given (/rename, or Codex's own title), from Codex's session index: the
    newest log in that folder names its thread id, the index its newest name. "" when it has none."""
    log = _codex_log(session.removeprefix("codex-"), root)
    if not log:
        return ""
    try:
        with log.open("rb") as f:
            thread = str((json.loads(f.readline(1024 * 1024) or b"{}").get("payload") or {}).get("id", ""))
    except (OSError, ValueError, AttributeError):
        return ""
    name = ""
    try:
        with (index or root.parent / "session_index.jsonl").open("rb") as f:
            for raw in f:
                if thread.encode() in raw:
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    if entry.get("id") == thread and entry.get("thread_name"):
                        name = str(entry["thread_name"]).strip()  # the last one wins: renamed again later
    except OSError:
        return ""
    return name


def codex(session: str, root: Path = CODEX_SESSIONS) -> dict:
    log = _codex_log(session.removeprefix("codex-"), root)
    if not log:
        return {}
    last = None
    try:
        with log.open("rb") as f:
            for raw in f:
                if b'"token_count"' in raw:
                    last = raw
    except OSError:
        return {}
    try:
        payload = (json.loads(last) or {}).get("payload") or {} if last else {}
    except ValueError:
        payload = {}
    limits = payload.get("rate_limits") or {}
    info = payload.get("info") or {}
    size = info.get("model_context_window")
    used = ((info.get("last_token_usage") or {}).get("input_tokens")) if isinstance(info, dict) else None
    found = []
    for key in ("primary", "secondary"):
        window = limits.get(key) or {}
        if isinstance(window, dict):
            item = _limit(_window_name(window.get("window_minutes")), window.get("used_percent"), window.get("resets_at"))
            if item:
                found.append(item)
    context = None
    if isinstance(size, int) and size and isinstance(used, int):
        context = {"percent": round(100 * used / size, 1), "size": size, "used": used, "parts": []}
    plan = limits.get("plan_type")
    notes = [f"Plan: {plan}"] if isinstance(plan, str) and plan else []
    if _no_credits(limits):  # as on the account card: no stale plan windows next to "out of credits"
        found = [{"label": "Out of credits", "percent": 100.0, "resets": 0}]
        notes.insert(0, OUT_OF_CREDITS)
    return {"model": "", "limits": found, "context": context, "notes": notes}


OUT_OF_CREDITS = "Out of credits: Codex can't run until the workspace is refilled"


def _rate_lines(log: Path) -> list[dict]:
    """Every rate_limits report in a Codex session log, oldest first, with the line's own time (0 when unknown)."""
    found = []
    try:
        with log.open("rb") as f:
            for raw in f:
                if b'"rate_limits"' not in raw:
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                payload = line.get("payload") if isinstance(line, dict) else None
                limits = payload.get("rate_limits") if isinstance(payload, dict) else None
                if isinstance(limits, dict):
                    found.append({"limits": limits, "at": _stamp(line.get("timestamp"))})
    except OSError:
        pass
    return found


def _stamp(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def _windows(limits: dict, now: float | None = None) -> list[dict]:
    """A report's windows; one whose reset time has passed since says so ("reset since") rather than a bare 0%."""
    now = time.time() if now is None else now
    items = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if isinstance(window, dict):
            name = _window_name(window.get("window_minutes"))
            resets = window.get("resets_at")
            if isinstance(resets, (int, float)) and 0 < resets < now:  # read before that window ended: it's new now
                if isinstance(window.get("used_percent"), (int, float)):
                    items.append({"label": name + " (reset since)", "percent": 0.0, "resets": 0})
                continue
            item = _limit(name, window.get("used_percent"), resets)
            if item:
                items.append(item)
    return items


def _no_credits(limits: dict) -> bool:
    credits = limits.get("credits")
    return isinstance(credits, dict) and credits.get("has_credits") is False and not credits.get("unlimited")


def codex_account(root: Path = CODEX_SESSIONS, now: float | None = None) -> dict:
    """The Codex card for the phone's Usage tab, from the newest session logs: {limits, notes}, or {} with no report.
    The newest report decides credits and plan; the windows come from the newest report that has them (a workspace
    out of credits reports none), marked with their age when that isn't the newest."""
    now = time.time() if now is None else now
    newest = None
    windows, windows_at = [], 0.0
    for log in sorted(root.glob("*/*/*/rollout-*.jsonl"), key=_mtime, reverse=True)[:10]:
        lines = _rate_lines(log)
        if not lines:
            continue
        if newest is None:
            newest = lines[-1]
        for line in reversed(lines):
            got = _windows(line["limits"], now)
            if got:
                windows, windows_at = got, line["at"] or _mtime(log)
                break
        if windows:
            break
    if newest is None:
        return {}
    limits, notes = [], []
    if _no_credits(newest["limits"]):
        # Out of credits: Codex can't run, so the plan's windows mean nothing now. Not drawn as bars (the user read a
        # stale "Week 37%" as current, 2026-11-22); at most one quiet line of what they were before.
        limits.append({"label": "Out of credits", "percent": 100.0, "resets": 0})
        notes.append(OUT_OF_CREDITS)
        week = next((w for w in windows if w["label"].startswith("Week")), None)
        if week and windows_at:
            notes.append(f"Weekly was {week['percent']:.0f}% used (last reported {_ago(now - windows_at)} ago)")
    else:
        limits += windows
        if windows and windows_at and newest["at"] and windows_at < newest["at"] - 60:
            notes.append(f"Limits as last reported, {_ago(now - windows_at)} ago")
    plan = newest["limits"].get("plan_type")
    if isinstance(plan, str) and plan:
        notes.append(f"Plan: {plan}")
    return {"limits": limits, "notes": notes}


CURSOR_ROOM = STATE / "cursor-usage"  # the hidden CLI's own folder: trusted once (--trust), nothing else in it
_plan: tuple[float, list[dict], list[str]] = (0.0, [], [])  # when read, limits, notes
PLAN_FOR = 600  # seconds the plan is kept before the CLI is asked again
FAILED_FOR = 120  # and after a failed read, before trying again (each try starts a CLI, up to 20 s)
PROMPT = "Plan, search, build anything"  # the empty input box of a new CLI chat, and nothing else on screen says it
_plan_lock = threading.Lock()  # one hidden CLI at a time: two would share (and kill) the same tmux server


def parse_cursor_usage(screen: str, today: datetime | None = None) -> tuple[list[dict], list[str]]:
    """The Cursor CLI's /usage panel, as text: (limits, notes). "Included 44% used" rows, "Resets Nov 30"."""
    today = today or datetime.now()
    resets = 0
    found = re.search(r"Resets\s+([A-Z][a-z]{2})\s+(\d{1,2})", screen)
    if found:
        try:
            day = datetime.strptime(f"{found.group(1)} {found.group(2)} {today.year}", "%b %d %Y")
            if day < today.replace(hour=0, minute=0, second=0, microsecond=0):
                day = day.replace(year=today.year + 1)
            resets = int(day.timestamp())
        except ValueError:
            pass
    limits = []
    for row in re.finditer(r"^\s*(Included|Auto|API|[A-Z][\w-]+)\s+(\d+(?:\.\d+)?)%\s+used", screen, re.M):
        label = {"Included": "Month: included", "Auto": "Month: Auto model", "API": "Month: other models"}.get(
            row.group(1), f"Month: {row.group(1)}")
        limits.append({"label": label, "percent": float(row.group(2)), "resets": resets})
    notes = []
    plan = re.search(r"Usage\s*•\s*(\S[^\n]*?)\s{2,}", screen)
    if plan:
        notes.append(f"Plan: {plan.group(1).strip()}")
    if re.search(r"On-demand usage is off", screen):
        notes.append("On-demand usage is off")
    return limits, notes


def cursor_plan(wait: float = 20.0, fresh: bool = False) -> tuple[list[dict], list[str]]:
    """Cursor's plan usage, as its CLI shows it under /usage: asked of a CLI started in its own tmux server, out of
    sight, and closed again; kept ten minutes. ([], []) when the CLI isn't there or didn't show it."""
    with _plan_lock:  # a second caller waits, then gets the first one's answer
        # fresh (a pull-down on the Usage tab): asked again, unless another caller just did
        if time.time() - _plan[0] < (5 if fresh else PLAN_FOR if _plan[1] else FAILED_FOR):
            return _plan[1], _plan[2]
        limits, notes = _ask_cursor(wait)
        _plan_set(limits, notes)
        return limits, notes


def _plan_set(limits: list[dict], notes: list[str]) -> None:
    global _plan
    _plan = (time.time(), limits, notes)


def _ask_cursor(wait: float) -> tuple[list[dict], list[str]]:
    """The hidden CLI's /usage. Nothing is typed until its empty input box is on screen, and nothing but /usage:
    a key typed into a question answers it (an "a" once trusted a folder, 2026-11-17)."""
    if not shutil.which("agent") or not shutil.which("tmux"):
        return [], []
    CURSOR_ROOM.mkdir(parents=True, exist_ok=True)
    tmux = ["tmux", "-L", "henchline-usage"]

    def screen() -> str:
        done = subprocess.run([*tmux, "capture-pane", "-p", "-t", "u"], capture_output=True, text=True, timeout=5)
        return done.stdout

    try:
        subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)
        subprocess.run([*tmux, "new-session", "-d", "-s", "u", "-x", "160", "-y", "40", "-c", str(CURSOR_ROOM),
                        "agent --trust"], capture_output=True, timeout=10, check=True)
        until = time.monotonic() + wait
        while PROMPT not in screen() and time.monotonic() < until:
            time.sleep(0.5)
        if PROMPT not in screen():  # a question, a login, an update: nothing is typed into it
            return [], []
        subprocess.run([*tmux, "send-keys", "-t", "u", "-l", "/usage"], timeout=5)
        time.sleep(0.5)
        if "/usage" not in screen():  # the words didn't land in its box: no Enter
            return [], []
        subprocess.run([*tmux, "send-keys", "-t", "u", "Enter"], timeout=5)
        shown = ""
        while time.monotonic() < until:
            shown = screen()
            if "% used" in shown or "Disabled" in shown:
                time.sleep(0.5)
                shown = screen()
                break
            time.sleep(0.5)
        limits, notes = parse_cursor_usage(shown)
    except (OSError, subprocess.SubprocessError):
        limits, notes = [], []
    finally:
        subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)
    return limits, notes


# Claude's per-model weekly limits (Fable, ...) aren't in its status line: its /usage panel shows them, so a hidden
# Claude CLI is asked, the way Cursor's is: in its own tmux server, nothing typed until its empty input box is on
# screen, /usage read, closed. It sends nothing to the model (the panel shows $0 and no tokens). It runs in
# Henchline's own folder, which Claude already trusts there; anywhere else its trust question appears and nothing is
# typed. HENCHLINE_USAGE_PROBE keeps it out of Henchline's own records (claude-status.py, the call mirror hook).
CLAUDE_ROOM = Path(__file__).resolve().parents[1]
_claude_plan: tuple[float, list[dict]] = (0.0, [])
_claude_lock = threading.Lock()
TRUST_QUESTION = re.compile(r"trust this folder|Quick safety check", re.I)
CLAUDE_BOX = re.compile(r"^❯ ?\S?", re.M)


def parse_claude_usage(screen: str, now: datetime | None = None) -> list[dict]:
    """Claude's /usage panel, as text: every "Current session" / "Current week" row, with its model in brackets when
    it has one ("Current week (Fable)"), in the panel's order, labelled as the panel labels it."""
    now = now or datetime.now()
    found = []
    for block in re.finditer(r"^\s*(Current (?:session|week)(?: \([^)\n]+\))?)\s*\n(?:[^\n]*\n)??[^\n]*?"
                             r"(\d+(?:\.\d+)?)% used[^\n]*\n\s*Resets ([^\n(]+)", screen, re.M):
        label = block.group(1).strip()
        if label == "Current week":
            label = CLAUDE_WEEK
        found.append({"label": label, "percent": float(block.group(2)), "resets": _claude_reset(block.group(3), now)})
    return found


def _claude_reset(text: str, now: datetime) -> int:
    """'Nov 28, 5am' or 'Nov 28, 5:30pm' (local time) to a timestamp; 0 when unreadable."""
    hit = re.match(r"\s*([A-Z][a-z]{2})\s+(\d{1,2}),?\s*(\d{1,2})(?::(\d{2}))?\s*([ap]m)", text.strip(), re.I)
    if not hit:
        alone = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*$", text.strip(), re.I)  # "4:50am": the next one
        if not alone:
            return 0
        at = now.replace(hour=int(alone.group(1)) % 12 + (12 if alone.group(3).lower() == "pm" else 0),
                         minute=int(alone.group(2) or 0), second=0, microsecond=0)
        if at <= now:
            at = at.fromtimestamp(at.timestamp() + 86400)
        return int(at.timestamp())
    try:
        hour = int(hit.group(3)) % 12 + (12 if hit.group(5).lower() == "pm" else 0)
        day = datetime.strptime(f"{hit.group(1)} {hit.group(2)} {now.year}", "%b %d %Y").replace(
            hour=hour, minute=int(hit.group(4) or 0))
        if day < now.replace(hour=0, minute=0, second=0, microsecond=0):
            day = day.replace(year=now.year + 1)
        return int(day.timestamp())
    except ValueError:
        return 0


CLAUDE_PROJECTS = Path.home() / ".claude/projects"
PROBE_PARTS = re.compile(r"<command-name>/usage</command-name>|<command-message>usage</command-message>|"
                         r"<command-args>\s*</command-args>|<local-command-std(?:out|err)>[\s\S]*?</local-command-std(?:out|err)>")


def _probe_text(text: str) -> bool:
    """A user turn that is nothing but the /usage command (its tags, or the bare words) and its panel."""
    return PROBE_PARTS.sub("", text).strip() in ("", "/usage") and ("usage" in text)


def drop_probe_log(started: float, projects: Path = CLAUDE_PROJECTS, room: Path = CLAUDE_ROOM) -> str:
    """The hidden Claude's own session log, so probes don't pile up in `claude --resume`. Deleted only when every
    test holds: the newest log in Henchline's folder, made after the probe started, its user turns nothing but the
    /usage command, no model output. Otherwise nothing is deleted. Returns what happened, for the log."""
    folder = projects / re.sub(r"[^A-Za-z0-9]", "-", str(room))
    logs = sorted(folder.glob("*.jsonl"), key=_mtime, reverse=True) if folder.is_dir() else []
    if not logs:
        return "no probe log"
    log = logs[0]
    try:
        with log.open() as f:  # when the log began: its first stamped line (its file times move with every write)
            first = next((json.loads(raw) for raw in f if '"timestamp"' in raw), {})
    except (OSError, ValueError):
        return f"kept {log.name}: unreadable"
    made = _stamp(first.get("timestamp")) if isinstance(first, dict) else 0.0
    if not made or made < started - 1:
        return "no probe log"  # the newest log is someone's chat that began before the probe: the probe left none
    try:
        lines = [json.loads(raw) for raw in log.read_text().splitlines() if raw.strip()]
    except (OSError, ValueError):
        return f"kept {log.name}: unreadable"
    if any(isinstance(line, dict) and (line.get("type") == "assistant"
                                       or (line.get("message") or {}).get("role") == "assistant") for line in lines):
        return f"kept {log.name}: it has model output"
    users = 0
    for line in lines:
        if isinstance(line, dict) and line.get("type") == "user":
            content = (line.get("message") or {}).get("content")
            texts = [content] if isinstance(content, str) else [c.get("text", "") for c in content or []
                                                                  if isinstance(c, dict) and c.get("type") == "text"]
            if not texts or not all(_probe_text(t) for t in texts):
                return f"kept {log.name}: a user turn that isn't /usage"
            users += 1
    if not users:
        return f"kept {log.name}: no /usage turn in it"
    try:
        log.unlink()
    except OSError as e:
        return f"kept {log.name}: {e}"
    return f"dropped {log.name}"


def claude_rows(status: list[dict], panel: list[dict]) -> list[dict]:
    """The Claude card's rows in /usage's order and wording: the panel's rows, with the session and all-models week
    taken from the status line when it has them (it's live; the panel is read every ten minutes). The status line's
    two rows alone when the panel couldn't be read."""
    live = {row["label"]: row for row in status}
    if not panel:
        return list(status)
    rows = [live.get(row["label"], row) for row in panel]
    shown = {row["label"] for row in rows}
    return [row for row in status if row["label"] not in shown] + rows


def claude_plan(wait: float = 20.0, fresh: bool = False) -> list[dict]:
    """Claude's per-model weekly limits from its /usage panel, kept ten minutes ([] when not shown)."""
    global _claude_plan
    with _claude_lock:
        if time.time() - _claude_plan[0] < (5 if fresh else PLAN_FOR if _claude_plan[1] else FAILED_FOR):
            return _claude_plan[1]
        found = _ask_claude(wait)
        _claude_plan = (time.time(), found)
        return found


def _ask_claude(wait: float) -> list[dict]:
    if not shutil.which("claude") or not shutil.which("tmux"):
        return []
    tmux = ["tmux", "-L", "henchline-claude-usage"]

    def screen() -> str:
        shown = subprocess.run([*tmux, "capture-pane", "-p", "-t", "u"], capture_output=True, text=True, timeout=5).stdout
        return shown.replace("\xa0", " ")  # its box writes "❯" and a no-break space

    def ready(shown: str) -> bool:
        return "Claude Code" in shown and not TRUST_QUESTION.search(shown) and bool(CLAUDE_BOX.search(shown))

    started = 0.0
    try:
        subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)
        from voice.desktop import tmux as desktop_tmux
        started = time.time()
        subprocess.run([*tmux, "new-session", "-d", "-s", "u", "-x", "160", "-y", "60", "-c", str(CLAUDE_ROOM),
                        "-e", "HENCHLINE_USAGE_PROBE=1", *desktop_tmux.clean_env(), "claude"],
                       capture_output=True, timeout=10, check=True)
        until = time.monotonic() + wait
        while not ready(screen()) and time.monotonic() < until:
            time.sleep(0.5)
        if not ready(screen()):  # its trust question, a login, an update: nothing is typed into it
            return []
        for _ in range(4):  # the box is drawn before its input takes keys: typed again only while it's still empty
            time.sleep(1.0)
            subprocess.run([*tmux, "send-keys", "-t", "u", "-l", "/usage"], timeout=5)
            time.sleep(0.7)
            box = [line for line in screen().splitlines() if line.startswith("❯")]
            if box and box[-1].strip() == "❯ /usage":
                break
            if box and not box[-1].startswith("❯ Try"):  # something else in its box: nothing more is typed, no Enter
                return []
        else:
            return []
        subprocess.run([*tmux, "send-keys", "-t", "u", "Enter"], timeout=5)
        shown = ""
        while time.monotonic() < until:
            shown = screen()
            if "Current week" in shown and "Resets" in shown.split("Current week")[-1]:
                time.sleep(0.5)
                shown = screen()
                break
            time.sleep(0.5)
        return parse_claude_usage(shown)
    except (OSError, subprocess.SubprocessError):
        return []
    finally:
        subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)
        if started:
            outcome = drop_probe_log(started)
            if not outcome.startswith(("dropped", "no probe")):
                print(f"usage probe: {outcome}", file=sys.stderr)


def cursor(record: dict) -> dict:
    """From Cursor's own record of the chat (cursor_bridge.composer)."""
    if not record:
        return {}
    used, size, percent = record.get("contextTokensUsed"), record.get("contextTokenLimit"), record.get("contextUsagePercent")
    breakdown = record.get("promptTokenBreakdown") or {}
    parts = [{"label": str(c.get("label") or c.get("id") or "?"), "tokens": int(c.get("estimatedTokens") or 0)}
             for c in (breakdown.get("categories") or []) if isinstance(c, dict) and c.get("estimatedTokens")]
    context = None
    if isinstance(percent, (int, float)):
        context = {"percent": round(float(percent), 1), "size": int(size or 0), "used": int(used or 0), "parts": parts}
    model = (record.get("modelConfig") or {}).get("modelName") if isinstance(record.get("modelConfig"), dict) else ""
    limits, notes = cursor_plan()
    return {"model": str(model or ""), "limits": limits, "context": context, "notes": notes}


def _amount(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def words(found: dict, which: str) -> str:
    """The readout typed into the agent. `usage` is the plan limits; `context` is the window. The bars stay on the phone."""
    if which == "context":
        context = found.get("context") if isinstance(found.get("context"), dict) else None
        if not context or not isinstance(context.get("percent"), (int, float)):
            return "Context hasn't been reported for this chat."
        line = f"Context is {context['percent']:g}% full"
        size, used = int(context.get("size") or 0), int(context.get("used") or 0)
        if size:
            line += f", {_amount(used)} of {_amount(size)}"
        line += "."
        parts = [p for p in (context.get("parts") or []) if isinstance(p, dict)]
        if parts:
            line += " " + ", ".join(f"{p.get('label', '?')} {_amount(int(p.get('tokens') or 0))}" for p in parts[:8]) + "."
        return line
    limits = [item for item in (found.get("limits") or []) if isinstance(item, dict)]
    notes = [str(note) for note in (found.get("notes") or []) if str(note).strip()]
    if not limits and not notes:
        return "Usage hasn't been reported here."
    line = "Usage: " + ", ".join(f"{item.get('label', 'Limit')} {item.get('percent', 0):g}%" for item in limits) + "."
    if notes:
        line += " " + " ".join(notes)
        if not line.endswith("."):
            line += "."
    return line


def overview(fresh: bool = False) -> list[dict]:
    """Every AI on the laptop at once, for the phone's Usage tab: each one's plan limits (the account's, not one
    chat's), from its newest report. [{agent, limits, notes}]."""
    found = []
    newest = sorted(CLAUDE_STATUS.glob("*.json"), key=_mtime, reverse=True)
    if newest:
        got = claude(f"claude-{newest[0].stem}")
        if got.get("limits"):
            found.append({"agent": "claude", "limits": claude_rows(got["limits"], claude_plan(fresh=fresh)),
                          "notes": ["From your newest Claude session"]})
    codex_card = codex_account()
    if codex_card:
        found.append({"agent": "codex", **codex_card})
    limits, notes = cursor_plan(fresh=fresh)
    if limits:
        found.append({"agent": "cursor", "limits": limits, "notes": notes})
    return found
