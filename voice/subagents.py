"""A Claude chat's own helpers (the user's wish, 2026-11-21): its subagents and workflow runs, for the phone's
Delegated agents strip, shown as status only (they run inside the chat; there is no chat to open or end).

Read from Claude Code's own records, never the screen:
- <session dir>/subagents/agent-<id>.meta.json: {agentType, description, isFork, finished, ...}; `finished` is
  true once the subagent has stopped (a resumed one runs again until its transcript ends a turn).
- <session dir>/subagents/agent-<id>.jsonl: its transcript. The first timed line is when it started; the last
  line says whether it ended its turn (assistant, stop_reason end_turn) or is mid-work (a tool call or result).
- Workflow runs: voice/workflows.py (one row per run).
Only the head and the tail of a transcript are read, and only when its size or time changed.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from voice import workflows

RECENT = 1800  # finished helpers stay listed this long (seconds after their last line)
STALE = 1800  # an unfinished subagent with no line for this long has stopped (crashed, killed with the session)
RESUMED = 300  # a finished subagent writing mid-work lines this recently was sent another message: running again
MOST = 8
TAIL = 65536  # bytes read from a transcript's end to find its last line
HEAD = 16384  # bytes read from its start to find its first timed line

_cache: dict[str, tuple[tuple[float, int], dict]] = {}


def _stamp(value: object) -> float:
    """An ISO time ("2026-11-21T19:29:53.939Z") as epoch seconds, 0 when absent or odd."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _lines(path: Path, size: int) -> tuple[list[str], list[str]]:
    """Whole lines from the transcript's head and its tail."""
    with open(path, "rb") as handle:
        head = handle.read(HEAD).decode("utf-8", "replace")
        if size > TAIL:
            handle.seek(size - TAIL)
        else:
            handle.seek(0)
        tail = handle.read().decode("utf-8", "replace")
    head_lines = head.splitlines()
    if size > HEAD and head_lines:
        head_lines = head_lines[:-1]  # cut mid-line
    tail_lines = tail.splitlines()
    if size > TAIL and tail_lines:
        tail_lines = tail_lines[1:]  # began mid-line
    return head_lines, tail_lines


def _parse(raw: str) -> dict:
    try:
        entry = json.loads(raw)
    except ValueError:
        return {}
    return entry if isinstance(entry, dict) else {}


HANDBACK = "SubagentHandback"  # the tool a subagent ends with when it reports: done once it is called


def _last_turn(entries: list[dict]) -> dict:
    """The newest assistant or user line (attachments and other notes skipped), or the newest line at all."""
    return next((e for e in reversed(entries) if e.get("type") in ("assistant", "user")), entries[-1] if entries else {})


def _handed_back(entries: list[dict]) -> bool:
    """The newest assistant line calls the hand-back tool: the subagent reported and stopped."""
    for entry in reversed(entries):
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content") if isinstance(entry.get("message"), dict) else None
        return isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "tool_use" and part.get("name") == HANDBACK for part in content)
    return False


def _new_message(entry: dict) -> bool:
    """A user line that is not a tool result: a message sent to the subagent after it stopped (it works again)."""
    if entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content") if isinstance(entry.get("message"), dict) else None
    if isinstance(content, list):
        return not all(isinstance(part, dict) and part.get("type") == "tool_result" for part in content)
    return bool(content)


def _ended_turn(entry: dict) -> bool | None:
    """True when the transcript's last line ends a turn, False when it is mid-work, None when unclear."""
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    if entry.get("type") == "assistant":
        reason = message.get("stop_reason")
        if reason == "end_turn":
            return True
        if reason == "tool_use":
            return False
        return None
    if entry.get("type") == "user":
        return False  # a tool result (or a new message) it has yet to answer
    return None


def _subagent(meta_path: Path, now: float) -> dict | None:
    agent_id = meta_path.name.removeprefix("agent-").removesuffix(".meta.json")
    transcript = meta_path.with_name(f"agent-{agent_id}.jsonl")
    try:
        meta_stat, stat = meta_path.stat(), transcript.stat()
    except OSError:
        return None
    key = (max(meta_stat.st_mtime, stat.st_mtime), stat.st_size)
    cached = _cache.get(str(transcript))
    if cached and cached[0] == key:
        found = dict(cached[1])
    else:
        try:
            meta = json.loads(meta_path.read_text())
            head, tail = _lines(transcript, stat.st_size)
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        started = next((t for t in (_stamp(_parse(raw).get("timestamp")) for raw in head) if t), 0.0)
        entries = [e for e in (_parse(raw) for raw in tail) if e]
        last_entry = _last_turn(entries)
        found = {
            "id": agent_id,
            "description": str(meta.get("description") or meta.get("agentType") or "subagent")[:120],
            "kind": "subagent",
            "type": str(meta.get("agentType") or ""),
            "finished": bool(meta.get("finished")),
            "ended_turn": True if _handed_back(entries) and not _new_message(last_entry) else _ended_turn(last_entry),
            "failed": bool(last_entry.get("isApiErrorMessage") or last_entry.get("error")),
            "started": started or stat.st_mtime,
            "last": _stamp(last_entry.get("timestamp")) or stat.st_mtime,
        }
        _cache[str(transcript)] = (key, dict(found))
    last = found["last"]  # its last line's own time (the file's time only when lines carry none)
    if found.pop("failed"):
        state = "failed"
    elif found["ended_turn"] is False:
        # Mid-work. `finished` can still be true from an earlier run when the subagent was sent another message,
        # so a finished one counts as running again only while its transcript is being written.
        limit = RESUMED if found["finished"] else STALE
        state = "running" if now - last < limit else ("done" if found["finished"] else "stopped")
    elif found["finished"] or found["ended_turn"]:
        state = "done"
    else:
        state = "running" if now - last < STALE else "stopped"
    found.pop("finished"); found.pop("ended_turn")
    return {**found, "state": state, "last": last}


def _session_dir(session: str, root: Path) -> Path | None:
    return workflows._session_dir(session, root)  # noqa: SLF001 (same rule: one folder per Claude session)


def helpers(session: str, root: Path | None = None, now: float | None = None) -> list[dict]:
    """That Claude session's helpers worth showing: every running one and those that finished in the last
    RECENT seconds, running first then newest, at most MOST ([] for any other agent)."""
    root = root or workflows.PROJECTS
    now = time.time() if now is None else now
    sdir = _session_dir(session, root)
    if sdir is None:
        return []
    found: list[dict] = []
    for meta_path in (sdir / "subagents").glob("agent-*.meta.json"):
        item = _subagent(meta_path, now)
        if item:
            found.append(item)
    for run in workflows.runs(session, 3, root):
        state = {"running": "running", "done": "done", "failed": "failed"}.get(run["state"], "stopped")
        changed = float(run.get("changed") or 0)
        found.append({"id": run["id"], "description": run["name"], "kind": "workflow", "type": "workflow",
                      "state": state, "started": changed, "last": changed,
                      "progress": f"{run['done']}/{run['total']}" if run.get("total") else ""})
    shown = [item for item in found if item["state"] == "running" or now - item["last"] < RECENT]
    shown.sort(key=lambda item: (item["state"] != "running", -item["last"]))
    return shown[:MOST]


def reset_cache() -> None:
    _cache.clear()


if __name__ == "__main__":  # a quick look: python3 -m voice.subagents claude-<uuid>
    import sys
    for item in helpers(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CLAUDE_CODE_SESSION_ID", "")):
        print(item["state"].ljust(8), item["kind"].ljust(9), item["description"])
