"""Talkback's queue: one clip per file, played oldest first, held per session.

A clip is what one agent asked to say. When the user talks and the words go
to an agent, every session's waiting clips are held until that session
speaks again (stale: dropped), releases them, or HOLD runs out.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from voice.desktop import Tab
from voice.state import HOLDS, SPOOL, TALKBACK_LOG, log, read_json, tab_from, write_json

HOLD = 90  # seconds a held clip waits for its session before it is dropped
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Clip:
    agent: str
    session: str
    sentences: list[str]
    voice: str = ""  # a Kokoro voice name; empty: the agent's own
    where: str = ""  # the agent's workspace folder name
    tab: Tab = field(default_factory=Tab)  # where hands-free types replies to this agent
    queued: float = field(default_factory=time.time)
    # Said to the phone: queued while a call was up. Once the call is over it isn't said aloud at the laptop (maybe
    # in an office, the user's worry, 2026-11-17): it goes to the phone as a text instead
    for_call: bool = False
    # Cut by a dropped call line: said again from the interrupted sentence when the phone is back (logged then)
    replay: bool = False

    @classmethod
    def of_text(cls, text: str, **fields) -> Clip:
        return cls(sentences=[s for s in SENTENCE_END.split(text) if s.strip()], **fields)

    @classmethod
    def load(cls, path: Path) -> Clip | None:
        data = read_json(path)
        if not data.get("sentences"):
            return None
        data["tab"] = tab_from(data)
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    def save(self, path: Path) -> None:
        write_json(path, {**asdict(self), "tab": [self.tab.owner, self.tab.address]})


def add(clip: Clip, at_ns: int | None = None) -> Path:
    """Queue the clip; `at_ns` places it among the others, default now."""
    path = SPOOL / f"{at_ns or time.time_ns()}-{os.getpid()}.json"
    clip.save(path)
    return path


def queued() -> list[Path]:
    return sorted(SPOOL.glob("*.json"))


def of_session(session: str) -> list[Path]:
    return [path for path in queued() if read_json(path).get("session") == session]


def queued_sessions(skip_agents: set[str] = frozenset()) -> set[str]:
    """Sessions with clips waiting, leaving out those of the given agents."""
    clips = [read_json(path) for path in queued()]
    return {c.get("session") for c in clips if c.get("agent") not in skip_agents} - {None}


def is_held(session: str) -> bool:
    return (HOLDS / session).exists()


def any_held() -> bool:
    return any(HOLDS.glob("[!.]*"))


def hold(sessions: set[str]) -> None:
    for session in sessions:
        write_json(HOLDS / session, {"until": time.time() + HOLD})
    if sessions:
        log(TALKBACK_LOG, f"held {', '.join(sorted(sessions))} for {HOLD}s")


def release(session: str) -> None:
    (HOLDS / session).unlink(missing_ok=True)


def drop(session: str) -> None:
    for path in of_session(session):
        path.unlink(missing_ok=True)
    release(session)


def expire() -> None:
    for path in HOLDS.glob("[!.]*"):
        if read_json(path).get("until", 0) < time.time():
            drop(path.name)
            log(TALKBACK_LOG, f"dropped {path.name}, nobody decided in time")


def clear() -> None:
    for path in [*queued(), *HOLDS.iterdir()]:
        path.unlink(missing_ok=True)
