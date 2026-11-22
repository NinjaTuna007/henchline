"""Scheduled messages: words typed into an agent at a set time (the user's idea, 2026-11-17).

`handsfree remind 08:30 "…"` (or the phone's /later) keeps a reminder here; the call server, always running,
delivers each one when it is due through the same path as a message from the phone (routing.deliver: paste, a
pause, then Enter), so it reaches Claude, Cursor and Codex alike. A one-off timer typing into a tmux pane with its
own Enter failed for Cursor (the wake-up call, 2026-11-17: the line sat unsent for an hour).

A reminder can keep the laptop awake until it is delivered (keep_awake): the server then holds a sleep inhibitor.

A due one is claimed (pending → sending) under the lock before it is typed, so a cancel either lands first (and
nothing is typed) or finds it already on its way; only the claim is then settled or put back for later.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from voice.state import STATE, file_lock, write_json

FILE = STATE / "reminders.json"
LOCK = STATE / "reminders.lock"
RETRY_FOR = 3600  # seconds a due reminder keeps being tried (the agent not reachable yet), then it is given up
KEEP = 7 * 86400  # delivered or given-up reminders stay listed this long
SEND_FOR = 300  # a claim older than this was left by a server that died mid-delivery: due again


class ReminderState(str, Enum):
    PENDING = "pending"
    SENDING = "sending"  # claimed, being typed: no longer cancellable
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Reminder:
    """A reminder as the call server delivers it (the file and the phone keep the plain JSON)."""

    id: str
    at: float
    session: str
    words: str
    by: str = ""  # "phone": the user set it (the phone's /later); "agent": `handsfree remind`, an agent or a script
    sender: str = ""  # the agent that set it ("claude in henchline"), when one did

    @classmethod
    def of(cls, item: dict) -> Reminder:
        return cls(str(item["id"]), float(item.get("at", 0)), str(item.get("session", "")),
                   str(item.get("words", "")), str(item.get("by", "")), str(item.get("sender", "")))

    @property
    def users(self) -> bool:
        """Set by the user on the phone: typed as they wrote it, a slash command included. Anything else (an agent,
        a script, an older record that doesn't say) is never typed as a command."""
        return self.by == "phone"


def _locked():
    return file_lock(LOCK)


def _read() -> list[dict]:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict) and "id" in r] if isinstance(data, list) else []


def _open(item: dict) -> bool:
    """Still to be delivered: pending, or on its way."""
    return item.get("state") in (ReminderState.PENDING, ReminderState.SENDING)


def _write(items: list[dict]) -> None:
    now = time.time()
    write_json(FILE, [r for r in items if _open(r) or now - r.get("at", now) < KEEP])


def when(text: str, now: datetime | None = None) -> float:
    """"08:30" (the next one), "tomorrow 08:30", "2026-11-18 08:30", "in 20m" / "in 2h" → seconds since the epoch."""
    now = now or datetime.now()
    text = text.strip().lower()
    later = re.fullmatch(r"in\s+(\d+)\s*(s|sec|m|min|h|hr|hours?|minutes?|seconds?)", text)
    if later:
        seconds = int(later.group(1)) * {"s": 1, "m": 60, "h": 3600}[later.group(2)[0]]
        return now.timestamp() + seconds  # elapsed time: right across a clock change too
    day = re.fullmatch(r"(?:(tomorrow|today)\s+|(\d{4}-\d{2}-\d{2})\s+)?(\d{1,2}):(\d{2})", text)
    if not day:
        raise ValueError(f"not a time: {text!r} (try 08:30, tomorrow 08:30, 2026-11-18 08:30, in 20m)")
    hour, minute = int(day.group(3)), int(day.group(4))
    if day.group(2):
        base = datetime.strptime(day.group(2), "%Y-%m-%d")
    else:
        base = now + timedelta(days=1) if day.group(1) == "tomorrow" else now
    at = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if not day.group(1) and not day.group(2) and at <= now:  # a bare time already past today: tomorrow's
        at += timedelta(days=1)
    elif at <= now:
        raise ValueError(f"{text!r} has already passed")
    return at.timestamp()


def add(at: float, session: str, words: str, by: str = "", keep_awake: bool = False, sender: str = "") -> dict:
    item = {"id": secrets.token_hex(4), "at": at, "session": session, "words": words, "by": by,
            "sender": sender, "keep_awake": keep_awake, "state": ReminderState.PENDING.value, "made": time.time()}
    with _locked():
        _write(_read() + [item])
    return item


def listed(session: str = "") -> list[dict]:
    return sorted((r for r in _read() if not session or r.get("session") == session), key=lambda r: r.get("at", 0))


def cancel(rid: str) -> bool:
    with _locked():
        items = _read()
        hit = [r for r in items if r.get("id") == rid and r.get("state") == ReminderState.PENDING]
        for r in hit:
            r["state"] = ReminderState.CANCELLED.value
        if hit:
            _write(items)
    return bool(hit)


BACKOFF = [60, 120, 300, 600]  # seconds before the next try when one fails, then every 10 minutes


def due(now: float | None = None) -> list[Reminder]:
    """Pending reminders whose time (and backoff) has come. A claim a dead server left behind counts again."""
    now = now or time.time()
    with _locked():
        items = _read()
        stale = [r for r in items if r.get("state") == ReminderState.SENDING
                 and now - float(r.get("claimed", 0)) > SEND_FOR]
        for r in stale:
            r["state"] = ReminderState.PENDING.value
        if stale:
            _write(items)
    return [Reminder.of(r) for r in items if r.get("state") == ReminderState.PENDING and r.get("at", 0) <= now
            and r.get("next", 0) <= now]


def claim(rid: str) -> Reminder | None:
    """Right before typing it: the reminder, still pending, now marked as on its way (a cancel no longer takes it);
    None when it was cancelled or settled meanwhile, and then nothing may be typed."""
    with _locked():
        items = _read()
        for r in items:
            if r.get("id") == rid and r.get("state") == ReminderState.PENDING:
                r["state"], r["claimed"] = ReminderState.SENDING.value, time.time()
                _write(items)
                return Reminder.of(r)
    return None


def _change(rid: str, change) -> bool:
    """`change` applied to the claimed reminder only: one cancelled or settled meanwhile is left as it is."""
    with _locked():
        items = _read()
        hit = [r for r in items if r.get("id") == rid and r.get("state") == ReminderState.SENDING]
        for r in hit:
            change(r)
        if hit:
            _write(items)
    return bool(hit)


def later(rid: str) -> bool:
    """A claimed try that failed (the agent not reachable yet): pending again, the next try after a backoff, not
    every 10 s."""
    def again(r: dict) -> None:
        tries = r.get("tries", 0)
        r["state"] = ReminderState.PENDING.value
        r["tries"], r["next"] = tries + 1, time.time() + BACKOFF[min(tries, len(BACKOFF) - 1)]
    return _change(rid, again)


def settle(rid: str, state: ReminderState, note: str = "") -> bool:
    """A claimed reminder delivered (DELIVERED), or given up (FAILED, with why)."""
    if state not in (ReminderState.DELIVERED, ReminderState.FAILED):
        raise ValueError(f"not how a reminder ends: {state}")
    return _change(rid, lambda r: r.update(state=ReminderState(state).value, note=note, settled=time.time()))


def keep_awake_until() -> float:
    """The latest due time among open reminders that keep the laptop awake (0: none)."""
    return max((r.get("at", 0) + 300 for r in _read() if _open(r) and r.get("keep_awake")), default=0.0)
