"""One append-only log per job a boss starts.

The boss is depth 1 and is the only session that may deploy. A child is depth 2,
stored with may_deploy false, and cannot raise the chain's knobs. The default
knobs are max_depth 2 and slots 8 (live children, so an ended one frees a slot).

The directory is voice.state.CHAINS, one ``<id>.jsonl`` per job. The first line
is the chain (boss and knobs). After that the lines are launch, bound,
correction, report, end_requested, end_kept, ended and replaced, and reserved/unreserved: a slot held while a
child starts. A launch may only know the pane and its pid; a bound line attaches the session once it exists. No topic, token or folder
path is written. Corrections and reports are the words themselves. A launch (and the slot reserved for it) carries the
helper's name, unique among its boss's live helpers, so the boss can address it before it has a session.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from voice.state import CHAINS, file_lock, write_json

KINDS = ("launch", "bound", "correction", "report", "end_requested", "end_chosen", "end_kept", "ended", "replaced",
         "reserved", "unreserved")
DEFAULT_DEPTH = 2  # boss is 1, so a child is as deep as a chain allows
DEFAULT_SLOTS = 8
REPORT = "[henchline · report]"  # an internal report, not a correction from the user
_NAMED_REPORT = "[henchline · report · from "


def is_report(text: object) -> bool:
    """Whether the words are a helper's report: the bare tag, or one naming the helper."""
    words = str(text or "").lstrip()
    return words.startswith(REPORT) or words.startswith(_NAMED_REPORT)


def report_tag(name: str) -> str:
    """A report's tag, naming the helper when it has a name (brackets taken out, so the tag stays one tag)."""
    name = " ".join(str(name or "").replace("[", "").replace("]", "").split())[:40]
    return f"[henchline · report · from {name}]" if name else REPORT
_LOCK = CHAINS.parent / "chains.lock"
PHONE_VIEWS = CHAINS.parent / "chain-views.json"
REAP_AFTER = 30.0  # seconds: a launch this young is never taken for gone
# A slot held by reserve() while its child starts (tmux, a trust answer, the ask typed: ~12 s, more on a slow laptop).
# A deploy that died before its launch or release line no longer holds it after this long
RESERVE_FOR = 120.0
_DROP = {"topic", "token", "secret", "password", "folder", "path"}
_LOOK = object()  # look the panes up here (one tmux call)
NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")  # a helper's name: short, and never a tmux pane id (%15)


@dataclass
class _Chain:
    id: str
    boss: str
    max_depth: int
    slots: int
    events: list[dict] = field(default_factory=list)
    closed: bool = False


def _locked():
    return file_lock(_LOCK)


def _load_all() -> list[_Chain]:
    if not CHAINS.is_dir():
        return []
    found = []
    for path in sorted(CHAINS.glob("*.jsonl")):
        chain = _load(path)
        if chain:
            found.append(chain)
    return found


def _load(path) -> _Chain | None:
    header = None
    events = []
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "chain" and header is None:
            header = item
        elif item.get("kind") in KINDS:
            events.append(item)
    if not header or not header.get("id") or not header.get("boss"):
        return None
    boss = str(header["boss"])
    boss_names = _renamed_as(boss)
    closed = any(e.get("kind") == "ended" and str(e.get("session") or "") in boss_names for e in events)
    try:
        depth, slots = int(header.get("max_depth")), int(header.get("slots"))
    except (TypeError, ValueError):
        depth, slots = DEFAULT_DEPTH, DEFAULT_SLOTS
    return _Chain(str(header["id"]), boss, depth, slots, events, closed)


def _one(chain_id: str) -> _Chain | None:
    if not chain_id:
        return None
    path = CHAINS / f"{chain_id}.jsonl"
    return _load(path) if path.is_file() else None


def _parents(chain: _Chain) -> dict[str, str]:
    """Session → the boss that started it. An ended child still counts: ending one pane doesn't drop a sibling."""
    parents: dict[str, str] = {}
    pane_parent: dict[str, str] = {}
    for event in chain.events:
        kind = event.get("kind")
        pane = str(event.get("pane") or "")
        session = str(event.get("session") or "")
        if kind == "launch":
            parent = str(event.get("parent") or chain.boss)
            if pane:
                pane_parent[pane] = parent
            if session and session != chain.boss:
                parents[session] = parent
        elif kind == "bound":
            parent = pane_parent.get(pane, str(event.get("parent") or chain.boss))
            if session and session != chain.boss:
                parents[session] = parent
                if pane:
                    pane_parent[pane] = parent
        elif kind == "replaced":
            parents.pop(session, None)
            new = str(event.get("by") or "")
            if new and new != chain.boss:
                parents[new] = pane_parent.get(pane, chain.boss)
    return parents


def _renamed_as(session: str) -> set[str]:
    """This session and the keys it was renamed to or from. Not another session that shares a pane."""
    from voice.state import RENAMED, read_json
    try:
        data = read_json(RENAMED)
    except OSError:
        data = {}
    names = {session}
    changed = True
    while changed and isinstance(data, dict):
        changed = False
        for old, new in data.items():
            old, new = str(old), str(new)
            if new in names and old not in names:
                names.add(old)
                changed = True
            if old in names and new not in names:
                names.add(new)
                changed = True
    return names


def _canonical(session: str) -> str:
    """Follow forward renames to the key the phone and live speaker now use."""
    from voice.state import RENAMED, read_json
    try:
        data = read_json(RENAMED)
    except OSError:
        data = {}
    current = session
    seen = set()
    while isinstance(data, dict) and current not in seen:
        seen.add(current)
        following = str(data.get(current) or "")
        if not following or following == current:
            break
        current = following
    return current


def _known_as(session: str) -> set[str]:
    """This session, the keys it was renamed to or from, and any key already written on the same live pane.

    A child is often filed first under a folder placeholder, then under its chat id. A correction on one and a
    report on the other are the same child. A pane id is only shared when the pid matches, because tmux reuses
    pane ids.
    """
    names = _renamed_as(session)
    panes: set[tuple[str, int]] = set()
    for chain in _load_all():
        for event in chain.events:
            if str(event.get("session") or "") in names and event.get("pane"):
                panes.add((str(event["pane"]), int(event.get("pid") or 0)))
    if not panes:
        return names
    for chain in _load_all():
        for event in chain.events:
            who = str(event.get("session") or "")
            pane = str(event.get("pane") or "")
            if not who or not pane:
                continue
            recorded = int(event.get("pid") or 0)
            for known_pane, known_pid in panes:
                if pane == known_pane and (not known_pid or not recorded or known_pid == recorded):
                    names.add(who)
    return names


def _parent_of(session: str) -> str:
    if not session:
        return ""
    names = _known_as(session)
    for chain in _load_all():
        parents = _parents(chain)
        for name in names:
            parent = parents.get(name, "")
            if parent and parent != session:
                return parent
    return ""


def _chain_of(session: str) -> _Chain | None:
    """The open job this session belongs to: as a child first, else as its boss."""
    names = _known_as(session) if session else set()
    as_boss = None
    for chain in _load_all():
        parents = _parents(chain)
        if any(name in parents for name in names):
            return chain
        if chain.boss in names and not chain.closed:
            as_boss = chain
    return as_boss


def _same_pid(left: int, right: int) -> bool:
    """Both name one process, or both are unknown. A missing pid is not a known one."""
    if left and right:
        return left == right
    return not left and not right


def _closes(later: dict, launch: dict) -> bool:
    """An ended or replaced line closes this launch only.

    The same session closes it. A shared pane id closes it only when the pids
    are the same number, or both lines have no pid. Ending pid 7 does not close
    pid 9, and a line with no pid does not close a launch that has one.
    """
    if later.get("kind") not in ("ended", "replaced"):
        return False
    launched = str(launch.get("session") or "")
    if launched and str(later.get("session") or "") in _renamed_as(launched):
        return True
    if not launch.get("pane") or str(later.get("pane") or "") != str(launch.get("pane") or ""):
        return False
    return _same_pid(int(launch.get("pid") or 0), int(later.get("pid") or 0))


def _active(events: list[dict], now: float | None = None) -> int:
    """Launches with no later ended or replaced line for that same process, and slots reserved for a child still
    starting (no launch or unreserved line with that reservation yet, and younger than RESERVE_FOR)."""
    now = time.time() if now is None else now
    held: dict[str, float] = {}
    for event in events:
        token = str(event.get("reservation") or "")
        if not token:
            continue
        if event.get("kind") == "reserved":
            held[token] = float(event.get("at") or 0)
        elif event.get("kind") in ("launch", "unreserved"):
            held.pop(token, None)
    reserved = sum(1 for at in held.values() if now - at < RESERVE_FOR)
    live: dict[str, dict] = {}
    for index, event in enumerate(events):
        if event.get("kind") == "launch":
            pane = str(event.get("pane") or "")
            pid = int(event.get("pid") or 0)
            key = f"{pane}#{pid}" if pane else f"at:{event.get('at')}:{index}"
            live[key] = event
        else:
            for key, launch in list(live.items()):
                if _closes(event, launch):
                    del live[key]
    return len(live) + reserved


def _write(chain_id: str, item: dict) -> None:
    CHAINS.mkdir(parents=True, exist_ok=True)
    os.chmod(CHAINS, 0o700)
    path = CHAINS / f"{chain_id}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(json.dumps(item) + "\n")


def _knob_pair(max_depth: int, slots: int) -> tuple[int, int]:
    try:
        max_depth = int(max_depth)
    except (TypeError, ValueError):
        max_depth = DEFAULT_DEPTH
    try:
        slots = int(slots)
    except (TypeError, ValueError):
        slots = DEFAULT_SLOTS
    if max_depth < 1:
        max_depth = DEFAULT_DEPTH
    if slots < 1:
        slots = DEFAULT_SLOTS
    return max_depth, slots


def knobs(chain: str) -> dict:
    """``{"max_depth", "slots"}`` for that job, or empty when it isn't one."""
    with _locked():
        found = _one(chain)
    if not found:
        return {}
    return {"max_depth": found.max_depth, "slots": found.slots}


def history(chain: str) -> list[dict]:
    """The job's lines, oldest first, without the opening knob line."""
    with _locked():
        found = _one(chain)
    return list(found.events) if found else []


def open(boss_session: str, *, max_depth: int = DEFAULT_DEPTH, slots: int = DEFAULT_SLOTS) -> str:
    """The boss's open job, reused when one exists and has not ended.

    A session that is already a child gets that same job back. Higher knobs on
    the call are ignored: a child cannot raise them, and reopening doesn't either.
    """
    if not boss_session:
        raise ValueError("a chain needs a boss")
    max_depth, slots = _knob_pair(max_depth, slots)
    with _locked():
        parent = _parent_of(boss_session)
        existing = _chain_of(boss_session)
        if parent and existing:
            return existing.id
        if existing and existing.boss in _known_as(boss_session):
            return existing.id
        chain_id = secrets.token_hex(4)
        while (CHAINS / f"{chain_id}.jsonl").exists():
            chain_id = secrets.token_hex(4)
        _write(chain_id, {"at": time.time(), "kind": "chain", "id": chain_id, "boss": boss_session,
                          "max_depth": max_depth, "slots": slots})
        return chain_id


def append(chain: str, kind: str, **fields) -> None:
    """Append one line. A launch is stored with may_deploy false; knob fields are dropped."""
    if kind not in KINDS:
        raise ValueError(f"not a chain line: {kind}")
    clean = {}
    for key, value in fields.items():
        if key in _DROP or key == "kind":
            continue
        if isinstance(value, str) and key == "where" and "/" in value:
            value = value.rstrip("/").rsplit("/", 1)[-1]
        clean[key] = value
    with _locked():
        found = _one(chain)
        if found is None:
            raise ValueError(f"no such chain: {chain}")
        if kind == "launch":
            clean["may_deploy"] = False  # every child: only the boss deploys, and a child cannot raise this
            try:
                depth = int(clean.get("depth") or DEFAULT_DEPTH)
            except (TypeError, ValueError):
                depth = DEFAULT_DEPTH
            clean["depth"] = min(max(depth, 2), found.max_depth)
            clean.pop("max_depth", None)
            clean.pop("slots", None)
        else:
            clean.pop("max_depth", None)
            clean.pop("slots", None)
            clean.pop("may_deploy", None)
        if kind == "correction" and not clean.get("correction"):
            clean["correction"] = secrets.token_hex(8)
        if "pid" in clean:
            try:
                clean["pid"] = int(clean["pid"])
            except (TypeError, ValueError):
                clean["pid"] = 0
        _write(chain, {"at": time.time(), "kind": kind, **clean})


def _gone(launch: dict, alive: dict[str, int]) -> bool:
    """That launch's process is no longer in its pane: the pane is gone, or holds another process now."""
    pane = str(launch.get("pane") or "")
    pid = int(launch.get("pid") or 0)
    if not pane:
        return False  # nothing to look at: not known gone
    if pane not in alive:
        return True
    return bool(pid) and alive[pane] != pid


def _reap(found: _Chain, alive: dict[str, int] | None) -> None:
    """An ended line for each open launch whose process is gone (a child that exited by itself, or crashed, never
    wrote one), so it stops holding a slot. ``alive`` is every tmux pane with its first process; None when tmux
    couldn't be asked, and then nothing is known gone."""
    if alive is None:
        return
    settled = time.time() - REAP_AFTER  # a launch newer than the pane list may not be in it yet
    for index, event in enumerate(list(found.events)):
        if event.get("kind") != "launch" or float(event.get("at") or 0) > settled:
            continue
        if not _launch_open(found.events, index) or not _gone(event, alive):
            continue
        line = {"at": time.time(), "kind": "ended", "session": str(event.get("session") or ""),
                "pane": str(event.get("pane") or ""), "pid": int(event.get("pid") or 0), "reason": "gone"}
        _write(found.id, line)
        found.events.append(line)


def allow(chain: str, caller_session: str, depth: int, alive: dict[str, int] | None = None) -> str:
    """``""`` when ``caller_session``, working at ``depth``, may start a child. Otherwise a short reason.

    The boss is depth 1. A child is depth 2. With max_depth 2 there is no room
    under a child, and a child is not the boss, so it may not deploy. ``alive`` (tmux panes and their first
    process) frees the slots of children that are gone before they are counted.
    """
    return _allow(chain, caller_session, depth, alive, reserve=False)[0]


def reserve(chain: str, caller_session: str, depth: int, alive: dict[str, int] | None = None) -> tuple[str, str]:
    """allow(), and when allowed a slot held under the same lock: ``("", reservation)``, else ``(reason, "")``.

    The child takes a while to start, and its launch line is only written once its ask is typed: without this, two
    deploys from one boss could both pass with one slot left. Hand the reservation to the launch line
    (``append(chain, "launch", reservation=...)``) or give it back with release(); one left behind by a deploy that
    died stops counting after RESERVE_FOR.
    """
    return _allow(chain, caller_session, depth, alive, reserve=True)[:2]


def reserve_named(chain: str, caller_session: str, depth: int, agent: str, name: str = "",
                  alive: dict[str, int] | None = None) -> tuple[str, str, str]:
    """reserve(), with the helper's name held by the same slot: ``("", reservation, name)``, else
    ``(reason, "", "")``. A name a live helper of this boss (or one still starting) has is refused; without one, the
    first ``<agent>-<n>`` this boss hasn't given in this job. The caller checks the name's form (NAME)."""
    return _allow(chain, caller_session, depth, alive, reserve=True, name=name, agent=agent or "helper")


def release(chain: str, reservation: str) -> None:
    """Give back a slot reserve() held, for a child that did not start."""
    if reservation:
        append(chain, "unreserved", reservation=reservation)


def _names(found: _Chain, boss: str, now: float) -> tuple[set[str], set[str]]:
    """(the names of this boss's live helpers and of the slots it holds for ones starting, every name it has given
    in this job), lower-cased. The caller holds the lock."""
    bosses = _renamed_as(boss)
    live: set[str] = set()
    ever: set[str] = set()
    held: dict[str, tuple[float, str]] = {}
    for index, event in enumerate(found.events):
        kind = event.get("kind")
        label = str(event.get("name") or "").lower()
        token = str(event.get("reservation") or "")
        if kind == "reserved" and token and label and str(event.get("parent") or "") in bosses:
            held[token] = (float(event.get("at") or 0), label)
        elif kind in ("launch", "unreserved") and token:
            held.pop(token, None)
        if kind == "launch" and label and str(event.get("parent") or found.boss) in bosses:
            ever.add(label)
            if _launch_open(found.events, index):
                live.add(label)
    for at, label in held.values():
        if now - at < RESERVE_FOR:
            live.add(label)
            ever.add(label)
    return live, ever


def _allow(chain: str, caller_session: str, depth, alive, reserve: bool, name: str = "",
           agent: str = "") -> tuple[str, str, str]:
    with _locked():
        found = _one(chain)
        if found is None:
            return "no such chain", "", ""
        _reap(found, alive)
        if found.closed:
            return "that chain has ended", "", ""
        if found.boss not in _renamed_as(caller_session):
            return "only the boss may deploy", "", ""
        try:
            here = int(depth)
        except (TypeError, ValueError):
            here = found.max_depth
        if here >= found.max_depth:
            return "too deep", "", ""
        if _active(found.events) >= found.slots:
            return "no slots left", "", ""
        if not reserve:
            return "", "", ""
        now = time.time()
        line = {"at": now, "kind": "reserved", "parent": caller_session}
        if agent:  # a named slot
            live, ever = _names(found, caller_session, now)
            if name and name.lower() in live:
                return (f'you already have a helper called "{name}": pick another name, or end that one first '
                        f"(handsfree end {name})"), "", ""
            if not name:
                name = next(f"{agent}-{n}" for n in range(1, len(ever) + 2) if f"{agent}-{n}" not in ever)
            line["name"] = name
        token = secrets.token_hex(8)
        _write(found.id, {**line, "reservation": token})
        return "", token, name if agent else ""


def parent_of(session: str) -> str:
    """The boss that started this session, or ``""`` when it isn't a child."""
    with _locked():
        return _parent_of(session)


def aliases(session: str) -> set[str]:
    """This session and every key renamed to or from it."""
    with _locked():
        return _renamed_as(session)


def canonical(session: str) -> str:
    """The current forward-renamed key for this session."""
    with _locked():
        return _canonical(session)


def chain_of(session: str) -> str:
    """The job id this session belongs to, or ``""``."""
    with _locked():
        found = _chain_of(session)
    return found.id if found else ""


def pane_of(session: str) -> str:
    """The tmux pane last recorded for this session, or ``""``."""
    found = ""
    with _locked():
        names = _renamed_as(session)
        for chain in _load_all():
            for event in chain.events:
                if str(event.get("session") or "") in names and event.get("pane") and event.get("kind") in (
                        "launch", "bound", "ended"):
                    found = str(event["pane"])
    return found


def _pid(event: dict, key: str = "pid") -> int:
    try:
        return int(event.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def places(session: str) -> list[tuple[str, int]]:
    """Every (tmux pane, its first process) recorded for this session: launched or bound there, or the pane it
    deployed from. Written only by deploy and bind, after the tree vouched for the session."""
    found = []
    with _locked():
        names = _renamed_as(session)
        for chain in _load_all():
            for event in chain.events:
                kind = event.get("kind")
                if kind in ("launch", "bound") and str(event.get("session") or "") in names and event.get("pane"):
                    found.append((str(event["pane"]), _pid(event)))
                if kind == "launch" and str(event.get("parent") or "") in names and event.get("parent_pane"):
                    found.append((str(event["parent_pane"]), _pid(event, "parent_pid")))
    return found


def launched_at(pane: str, pid: int) -> tuple[set[str], set[str]]:
    """For an open launch in that pane (and process): (its boss and the chain's boss, with their aliases; the
    sessions bound to it). Both empty when no helper was launched there."""
    parents: set[str] = set()
    owners: set[str] = set()
    with _locked():
        for chain in _load_all():
            if chain.closed:
                continue
            for index, event in enumerate(chain.events):
                if event.get("kind") != "launch" or str(event.get("pane") or "") != pane:
                    continue
                if _pid(event) and pid and _pid(event) != pid or not _launch_open(chain.events, index):
                    continue
                for boss in (str(event.get("parent") or ""), chain.boss):
                    if boss:
                        parents |= _renamed_as(boss)
                if event.get("session"):
                    owners.add(str(event["session"]))
                owners |= {str(later.get("session") or "") for later in chain.events[index + 1:]
                           if later.get("kind") == "bound" and _same_launch_line(later, event)} - {""}
    return parents, owners


def helper(boss: str, label: str, alive=_LOOK) -> dict:
    """The boss's own helper called ``label`` (its name, in any case) or launched in that tmux pane (``%15``):
    ``{"name", "agent", "where", "pane", "pid", "session", "live"}``, where session is its chat once it is bound
    (``""`` before: it may only be reachable by its pane). A live one wins over an ended one, then the newest. Empty
    when no helper this boss started matches: a name never reaches outside the boss's own chain."""
    label = str(label or "").strip()
    if not boss or not label:
        return {}
    panes = _panes(alive)
    want = label.lower()
    best = None
    with _locked():
        bosses = _renamed_as(boss)
        for chain in _load_all():
            if not chain.closed:
                _reap(chain, panes)
            for index, event in enumerate(chain.events):
                if event.get("kind") != "launch" or str(event.get("parent") or chain.boss) not in bosses:
                    continue
                if str(event.get("name") or "").lower() != want and str(event.get("pane") or "") != label:
                    continue
                key = (not chain.closed and _launch_open(chain.events, index), float(event.get("at") or 0))
                if best is None or key >= best[0]:
                    best = (key, chain, index, event)
        if best is None:
            return {}
        (live, _), chain, index, launch = best
        child = str(launch.get("session") or "")
        for later in chain.events[index + 1:]:
            if later.get("kind") == "bound" and _same_launch_line(later, launch):
                child = str(later.get("session") or child)
        return {"name": str(launch.get("name") or ""), "agent": str(launch.get("agent") or ""),
                "where": str(launch.get("where") or ""), "pane": str(launch.get("pane") or ""), "pid": _pid(launch),
                "session": _canonical(child) if child else "", "live": live}


def name_of(session: str) -> str:
    """The name a helper was deployed under (``tests``), found by its chat; "" when it was never deployed by name."""
    want = _canonical(session) if session else ""
    if not want:
        return ""
    with _locked():
        for chain in _load_all():
            for index, launch in enumerate(chain.events):
                if launch.get("kind") != "launch":
                    continue
                child = str(launch.get("session") or "")
                for later in chain.events[index + 1:]:
                    if later.get("kind") == "bound" and _same_launch_line(later, launch):
                        child = str(later.get("session") or child)
                if child and _canonical(child) == want:
                    return str(launch.get("name") or "")
    return ""


def _launch_open(events: list[dict], index: int) -> bool:
    launch = events[index]
    return not any(_closes(later, launch) for later in events[index + 1:])


def _same_launch_line(event: dict, launch: dict) -> bool:
    """Whether a later line names this launch by its pane and process."""
    pane = str(launch.get("pane") or "")
    if not pane or str(event.get("pane") or "") != pane:
        return False
    return _same_pid(int(launch.get("pid") or 0), int(event.get("pid") or 0))


def _panes(alive):
    """Every tmux pane with its first process, for reaping: the caller's list, or one looked up now. None (tmux
    couldn't be asked) means nothing is known gone."""
    if alive is not _LOOK:
        return alive
    try:
        from voice.desktop import tmux
        return tmux.pane_pids()
    except Exception:
        return None


def view(session: str, alive=_LOOK) -> dict:
    """One job as the phone may show it, including each child's ordered hand-offs. A child whose pane is gone is
    reaped first, so it isn't shown running (or wearing a hat) after it crashed or exited by itself."""
    panes = _panes(alive)
    with _locked():
        chain = _chain_of(session)
        if not chain:
            return {}
        if not chain.closed:
            _reap(chain, panes)
        events = list(chain.events)
        launches = [(index, event) for index, event in enumerate(events) if event.get("kind") == "launch"]
        minions = []
        timeline = []
        for index, launch in launches:
            later = []
            for later_index, event in enumerate(events[index + 1:], start=index + 1):
                later.append((later_index, event))
                if _closes(event, launch):
                    break
            child = str(launch.get("session") or "")
            for _, event in later:
                if event.get("kind") == "bound" and _same_launch_line(event, launch):
                    child = str(event.get("session") or child)
            child = _canonical(child) if child else ""
            names = _renamed_as(child) if child else set()

            def belongs(event: dict) -> bool:
                who = str(event.get("session") or "")
                return bool(who and who in names) or _same_launch_line(event, launch)

            handoffs = []
            ask = str(launch.get("ask") or "").strip()
            if ask:
                item = {"kind": "ask", "text": ask, "at": launch.get("at", 0)}
                handoffs.append(item)
                timeline.append({**item, "order": index, "session": child,
                                 "agent": str(launch.get("agent") or ""), "where": str(launch.get("where") or "")})
            pending = None
            for later_index, event in later:
                if not belongs(event):
                    continue
                kind = str(event.get("kind") or "")
                if kind in ("correction", "report") and str(event.get("text") or "").strip():
                    item = {"kind": kind, "text": str(event["text"]).strip(), "at": event.get("at", 0)}
                    handoffs.append(item)
                    timeline.append({**item, "order": later_index, "session": child,
                                     "agent": str(launch.get("agent") or ""), "where": str(launch.get("where") or "")})
                if kind == "end_requested":
                    pending = {"at": event.get("at", 0), "reason": str(event.get("reason") or "")}
                elif kind in ("end_chosen", "end_kept", "ended", "replaced"):
                    pending = None
            minions.append({
                "session": child,
                "agent": str(launch.get("agent") or ""),
                "where": str(launch.get("where") or ""),
                "state": "running" if _launch_open(events, index) else "ended",
                "handoffs": handoffs,
                **({"end_waiting": pending} if pending else {}),
            })
        return {
            "id": chain.id,
            "boss": _canonical(chain.boss),
            "is_boss": chain.boss in _renamed_as(session),
            "closed": chain.closed,  # an ended job: no hats, as teams() says
            "used": _active(events),
            "slots": chain.slots,
            "minions": minions,
            "handoffs": [{key: value for key, value in item.items() if key != "order"}
                         for item in sorted(timeline, key=lambda item: item["order"])],
        }


def teams(alive=_LOOK, bosses: dict[str, str] | None = None) -> dict[str, str]:
    """Each chat's place in a crew, for its avatar's hard hat: "manager" (its job is open and a child still runs),
    "crew" (a child that hasn't ended); a chat in neither works solo and isn't listed. A child whose pane is gone
    (it crashed, or typed /exit) is reaped first, so it doesn't keep the hats on. Keyed by each chat's current key
    only: an old folder placeholder is not a chat, and a new chat started in that folder must not inherit a hat.
    `bosses`, when given, is filled with each crew chat's manager (its current key), for the phone's nested list."""
    out: dict[str, str] = {}
    panes = _panes(alive)
    with _locked():
        found_all = _load_all()
        for chain in found_all:
            if not chain.closed:
                _reap(chain, panes)
    for chain in found_all:
        if chain.closed:
            continue
        events = chain.events
        live = []
        for index, launch in enumerate(events):
            if launch.get("kind") != "launch" or not _launch_open(events, index):
                continue
            child = str(launch.get("session") or "")
            for event in events[index + 1:]:
                if event.get("kind") == "bound" and _same_launch_line(event, launch):
                    child = str(event.get("session") or child)
            live.append(child)  # an unbound child still makes its boss a manager; it has no chat yet
        if not live:
            continue
        boss = _canonical(chain.boss)
        out[boss] = "manager"
        for child in live:
            key = _canonical(child) if child else ""
            if key and out.get(key) != "manager":  # a manager stays one, whatever else it is
                out[key] = "crew"
                if bosses is not None:
                    bosses[key] = boss
    if bosses is not None:  # a chat that is also a manager is listed as one, not nested under another
        for key in [k for k in bosses if out.get(k) != "crew"]:
            del bosses[key]
    return out


def ended(within: float = 3 * 86400) -> list[str]:
    """Chats whose work is over, for the phone's list: a child whose launch was ended or replaced (handsfree end, the
    phone's End, or reaped when its pane was gone) and a boss whose job closed, in the last `within` seconds. A chat
    that is live again in another job is left out. Current keys only. Call after teams(), which reaps first."""
    since = time.time() - within
    over: set[str] = set()
    live: set[str] = set()
    for chain in _load_all():
        events = chain.events
        for index, launch in enumerate(events):
            if launch.get("kind") != "launch":
                continue
            child = str(launch.get("session") or "")
            closing = None
            for event in events[index + 1:]:
                if event.get("kind") == "bound" and _same_launch_line(event, launch):
                    child = str(event.get("session") or child)
                elif _closes(event, launch):
                    closing = event
            if not child:
                continue
            key = _canonical(child)
            if _launch_open(events, index):
                live.add(key)
            elif closing is not None and float(closing.get("at") or 0) >= since:
                over.add(key)
        if chain.closed:
            at = max((float(e.get("at") or 0) for e in events if e.get("kind") == "ended"), default=0)
            if at >= since:
                over.add(_canonical(chain.boss))
    # Only keys that name one chat: a folder placeholder (a Codex helper's codex-<folder>) would hide every later
    # chat of that agent in that folder from the phone's offline list
    from voice import resume
    return sorted(key for key in over - live if resume.chat_id(key))


def launched_where() -> dict[str, str]:
    """Each helper's folder name as its launch named it, by its current key: the folder it was started in, which
    names it on the phone after it ended (its own record may say a subfolder it moved into)."""
    out: dict[str, str] = {}
    for chain in _load_all():
        events = chain.events
        for index, launch in enumerate(events):
            if launch.get("kind") != "launch" or not launch.get("where"):
                continue
            child = str(launch.get("session") or "")
            for event in events[index + 1:]:
                if event.get("kind") == "bound" and _same_launch_line(event, launch):
                    child = str(event.get("session") or child)
            if child:
                out[_canonical(child)] = str(launch["where"])
    return out


def pending_end(session: str) -> bool:
    """True when this child has an unanswered End-or-Keep request."""
    shown = view(session)
    names = aliases(session)
    return any(item.get("session") in names and item.get("end_waiting") for item in shown.get("minions", []))


def decide_end(session: str, decision: str) -> bool:
    """Atomically claim one pending end request as End or Keep."""
    if decision not in ("end", "keep"):
        return False
    with _locked():
        found = _chain_of(session)
        if not found:
            return False
        names = _known_as(session)
        pending = False
        for event in found.events:
            if str(event.get("session") or "") not in names:
                continue
            if event.get("kind") == "end_requested":
                pending = True
            elif event.get("kind") in ("end_chosen", "end_kept", "ended", "replaced"):
                pending = False
        if not pending:
            return False
        _write(found.id, {"at": time.time(), "kind": "end_chosen" if decision == "end" else "end_kept",
                          "session": session})
        return True


def restore_end(session: str, reason: str = "failed") -> None:
    """Offer End or Keep again after a claimed End could not close the child safely."""
    chain = chain_of(session)
    if chain:
        append(chain, "end_requested", session=session, reason=reason)


def keep(session: str) -> bool:
    """Compatibility wrapper for answering a pending end request with Keep."""
    return decide_end(session, "keep")


def bind(session: str, pane: str = "", pid: int = 0) -> str:
    """Attach ``session`` to the live launch of that pane (or that pane pid). The boss, or ``""``.

    The child often has no session id at launch. The pane and pid are enough until this line.
    """
    if not session:
        return ""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        pid = 0
    pane = str(pane or "")
    with _locked():
        already = _parent_of(session)
        if already:
            return already
        if not pane and not pid:
            return ""
        match: tuple[_Chain, dict] | None = None
        for chain in _load_all():
            if chain.closed:
                continue
            for index, event in enumerate(chain.events):
                if event.get("kind") != "launch" or not _launch_open(chain.events, index):
                    continue
                recorded = int(event.get("pid") or 0)
                recorded_session = str(event.get("session") or "")
                same_pane = bool(pane) and str(event.get("pane") or "") == pane
                # The same pid, or both unknown. A missing pid may still name an unnamed launch
                # (a report arrives before the pid is known). It must not take a launch that already has a pid.
                if _same_pid(pid, recorded) and (same_pane or (not pane and bool(pid))):
                    match = (chain, event)  # the latest open launch wins
                elif same_pane and not recorded_session and bool(pid) != bool(recorded):
                    match = (chain, event)
        if match is None:
            return ""
        chain, launch = match
        _write(chain.id, {"at": time.time(), "kind": "bound", "session": session,
                          "pane": str(launch.get("pane") or pane), "pid": int(launch.get("pid") or pid or 0)})
        return chain.boss


def _pending_corrections(session: str) -> list[dict]:
    """Unacknowledged corrections. The caller holds the chain lock."""
    pending: list[dict] = []
    names = _known_as(session)
    for chain in _load_all():
        candidates: list[dict] = []
        acknowledged: set[str] = set()
        for index, event in enumerate(chain.events):
            who = str(event.get("session") or "")
            if who not in names:
                continue
            if event.get("kind") == "correction":
                text = str(event.get("text") or "").strip()
                if text:
                    candidates.append({"id": str(event.get("correction") or f"legacy:{index}"), "text": text})
            elif event.get("kind") == "report":
                if event.get("exact"):
                    acknowledged.update(str(item) for item in event.get("corrections") or [])
                else:  # lines written before exact watermarks acknowledged every earlier correction
                    acknowledged.update(item["id"] for item in candidates)
        pending.extend(item for item in candidates if item["id"] not in acknowledged)
    return pending


def pending_corrections(session: str) -> list[dict]:
    """Unacknowledged corrections for this child, with stable ids, oldest first."""
    if not session:
        return []
    with _locked():
        return _pending_corrections(session)


def corrections_since(session: str) -> list[str]:
    """Correction texts for this session that no report has included yet, oldest first."""
    return [item["text"] for item in pending_corrections(session)]


def mark_reported(session: str, corrections: list[dict]) -> None:
    """Acknowledge exactly the correction snapshot that was delivered."""
    ids = [str(item.get("id") or "") for item in corrections if str(item.get("id") or "")]
    if not session or not ids:
        return
    with _locked():
        found = _chain_of(session)
    if found:
        append(found.id, "report", session=session, mark=True, exact=True, corrections=ids)


def note_phone_view(session: str) -> None:
    """Remember briefly that the phone is visibly showing this chat."""
    if not session:
        return
    now = time.time()
    with _locked():
        try:
            shown = json.loads(PHONE_VIEWS.read_text())
        except (OSError, ValueError):
            shown = {}
        if not isinstance(shown, dict):
            shown = {}
        shown = {str(key): float(value) for key, value in shown.items()
                 if isinstance(value, (int, float)) and now - float(value) < 60}
        shown[session] = now
        write_json(PHONE_VIEWS, shown, private=True)


def phone_viewing(session: str, stale: float = 8.0) -> bool:
    """Whether the phone refreshed this chat recently enough to still be looking at it."""
    if not session:
        return False
    with _locked():
        try:
            shown = json.loads(PHONE_VIEWS.read_text())
        except (OSError, ValueError):
            return False
        names = _renamed_as(session)
        now = time.time()
        return isinstance(shown, dict) and any(
            isinstance(shown.get(name), (int, float)) and now - float(shown[name]) <= stale for name in names)


@contextmanager
def delivery_guard(session: str, pane: str = "", pid: int = 0):
    """Keep a child's terminal delivery and its correction line indivisible from closing that child.

    The yielded callback records the words after type_line succeeds. It is None for an ordinary non-child
    delivery. Binding happens first; if closing wins the following race, typing sees the dead pane and fails
    instead of silently accepting words that no report could carry.
    """
    pane = str(pane or "")
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        pid = 0
    if not session or not CHAINS.is_dir():
        yield None
        return
    if pane or pid:
        bind(session, pane, pid)
    if not parent_of(session):
        yield None
        return
    with _locked():
        found = _chain_of(session)
        if not found or not _parent_of(session):
            yield None
            return

        def remember(text: str) -> None:
            words = str(text or "").strip()
            if not words or is_report(words):
                return
            _write(found.id, {"at": time.time(), "kind": "correction", "session": session, "text": words,
                              "pane": pane, "pid": pid, "correction": secrets.token_hex(8)})

        yield remember


@contextmanager
def close_guard(session: str):
    """Hold delivery out while the caller performs the final pending check and closes the pane."""
    with _locked():
        yield not _pending_corrections(session)


def note_correction(session: str, text: str, pane: str = "", pid: int = 0) -> None:
    """Remember the user's words to a child. A line that is itself a report is not one, and nothing is typed on."""
    if not session or not str(text or "").strip() or is_report(text):
        return
    if not CHAINS.is_dir():  # no job has been opened: nothing to attach, and ordinary delivery stays a directory check
        return
    if pane or pid:
        bind(session, pane, pid)
    if not parent_of(session):
        return
    chain = chain_of(session)
    if chain:
        append(chain, "correction", session=session, text=str(text).strip(), pane=str(pane or ""), pid=int(pid or 0))


def close_child(session: str, pane: str = "", pid: int = 0) -> None:
    """Record that this child ended, even when the launch line never stored its session.

    The pane and pid find the launch. A session that was only bound later, or only known to the phone, still
    frees the slot when the pid is the same one. A missing pid does not end a launch that has a pid, and it
    does not bind onto a launch that already has another name.
    """
    pane = str(pane or "")
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        pid = 0
    # Binding first with no pid would claim whatever launch is on that pane and then look like its session.
    if session and pid:
        bind(session, pane, pid)
    with _locked():
        match: tuple[_Chain, dict] | None = None
        for chain in _load_all():
            for index, event in enumerate(chain.events):
                if event.get("kind") != "launch" or not _launch_open(chain.events, index):
                    continue
                recorded = int(event.get("pid") or 0)
                if not _same_pid(pid, recorded):
                    continue
                same_pane = bool(pane) and str(event.get("pane") or "") == pane
                recorded_session = str(event.get("session") or "")
                same_session = bool(session) and recorded_session in _renamed_as(session)
                if same_pane or same_session or (not pane and pid and recorded == pid):
                    match = (chain, event)
        if match is None:
            return
        chain, launch = match
        _write(chain.id, {"at": time.time(), "kind": "ended", "session": session,
                          "pane": str(launch.get("pane") or pane), "pid": int(launch.get("pid") or pid or 0)})
