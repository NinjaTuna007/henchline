"""Photos taken with the call screen's camera button, waiting to go with the turn being spoken.

The call server (which receives the picture) and hands-free (which delivers the turn) are different processes, so the
waiting list is one small JSON file under a lock. Whoever comes first takes them: the next turn delivered to an
agent, or the call server's own timer when no turn comes (then the photo goes alone). A photo not taken within
MAX_AGE is let go (the file stays in Downloads/Henchline): it must not join a turn of some later call.
"""

from __future__ import annotations

import time
from pathlib import Path

from voice import files
from voice.state import CALL_SNAPS, file_lock, read_json, write_json

WAIT = 45.0  # seconds a photo waits for a spoken turn before it is sent on its own
TRIES = 3  # failed deliveries on their own before a photo is let go
MAX_AGE = 600.0  # seconds after which it is no longer "just taken": dropped from the list
LOCK = CALL_SNAPS.with_suffix(".lock")


def _locked():
    return file_lock(LOCK)


def _read(now: float) -> list[dict]:
    listed = read_json(CALL_SNAPS).get("snaps", [])
    return [s for s in listed if isinstance(s, dict) and now - float(s.get("at", 0)) < MAX_AGE] \
        if isinstance(listed, list) else []


def add(paths: list[Path], session: str, now: float | None = None, at: list[float] | None = None,
        tries: list[int] | None = None) -> None:
    """Photos that wait for the next turn (to that agent's chat, `session`). at: when each was first taken, for
    photos put back after a turn that could not be delivered (so they keep their place and their age)."""
    now = time.time() if now is None else now
    with _locked():
        waiting = _read(now) + [{"path": str(p), "session": session, "at": at[i] if at else now,
                                 "tries": tries[i] if tries else 0}
                                for i, p in enumerate(paths)]
        write_json(CALL_SNAPS, {"snaps": sorted(waiting, key=lambda s: s["at"])}, private=True)


def take(session: str = "", now: float | None = None, older_than: float = 0.0) -> list[dict]:
    """Every photo waiting for that agent (or for any, with no session), removed from the list, as {path, session, at, tries};
    those gone from the disk are dropped. older_than: only those that have waited at least that long (the timer)."""
    now = time.time() if now is None else now
    taken, kept = [], []
    with _locked():
        listed = _read(now)
        for snap in listed:
            mine = not session or not snap.get("session") or snap["session"] == session
            (taken if mine and now - float(snap.get("at", 0)) >= older_than else kept).append(snap)
        if taken or len(listed) != len(read_json(CALL_SNAPS).get("snaps", [])):
            write_json(CALL_SNAPS, {"snaps": kept}, private=True)
    return [s for s in taken if Path(s["path"]).is_file()]


def waiting() -> bool:
    """Is anything still on the list (not yet expired)?"""
    with _locked():
        return bool(_read(time.time()))


def put_back(taken: list[dict], tried: bool = False) -> list[dict]:
    """Photos a turn took but could not carry: back on the list, keeping their age. tried: it was a delivery on
    their own that failed; after TRIES of those they are let go (the file stays). Returns those let go."""
    gone = [s for s in taken if tried and int(s.get("tries", 0)) + 1 >= TRIES]
    for session in {s["session"] for s in taken}:
        mine = [s for s in taken if s["session"] == session and s not in gone]
        if mine:
            add([Path(s["path"]) for s in mine], session, at=[float(s["at"]) for s in mine],
                tries=[int(s.get("tries", 0)) + (1 if tried else 0) for s in mine])
    return gone


def clear() -> None:
    """A call starts: nothing from before it goes with its turns."""
    with _locked():
        CALL_SNAPS.unlink(missing_ok=True)


def paths(taken: list[dict]) -> list[Path]:
    return [Path(s["path"]) for s in taken]


def words(taken: list[dict]) -> str:
    """What the agent is told about them, to go after the spoken words (one line: it is typed into a prompt)."""
    if not taken:
        return ""
    parts = "; ".join(f"{p} ({files.kind(p)}, {files.size_text(p.stat().st_size)})" for p in paths(taken))
    noun = "a photo" if len(taken) == 1 else f"{len(taken)} photos"
    return f" (With this the user took {noun} with their phone's camera: {parts}.)"
