"""``handsfree deploy``, ``report`` and ``end``: a boss starts a child, and the child reports back.

Laptop only. The boss is whoever is running the command (``handsfree.caller``).
A child is started with ``tmux.start`` (never resume), only in a folder that agent
already trusts (a deploy never answers a trust question), and registered in the same spawned list as a new chat. The ask
is typed in with a note to report back. The chain log stores the pane and pid;
the session id is bound later, when the child is known.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from voice import chains
from voice.desktop import Tab
from voice.state import HANDSFREE_LOG, SPEAKERS, Speaker, log, recent_speakers

AGENTS = ("claude", "cursor", "codex")
ASK_MARK = "[henchline · deploy]"
ASK_PREFIX = '[henchline · deploy] You were started by another agent. Report back with `handsfree report "…"`.\n\n'


def _server():
    """The call server's folder check, spawned list and phone notice. Imported here so a test can stand in."""
    import call.server as server
    return server


def _routing():
    core = str(Path(__file__).resolve().parents[1] / "handsfree" / "core")
    if core not in sys.path:
        sys.path.insert(0, core)
    import routing
    return routing


def _deploy_args(args: list[str]) -> tuple[str, str, str]:
    agent, folder = "", ""
    ask: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == "--agent" and index + 1 < len(args):
            agent = args[index + 1]
            index += 2
        elif args[index] == "--folder" and index + 1 < len(args):
            folder = args[index + 1]
            index += 2
        elif args[index] == "--ask":
            ask = args[index + 1:]
            break
        else:
            index += 1
    return agent, folder, " ".join(ask).strip()


def _folder(given: str, known: list[Path]):
    """The trusted folder the boss named, or None. Only folders the child agent already trusts: a deploy never
    answers a trust question, so a prompt-injected boss can't start a child in a folder whose own settings (hooks,
    MCP servers) would then run, and leave it trusted for good."""
    raw = Path(given).expanduser()
    try:
        resolved = raw.resolve()
    except OSError:
        resolved = raw
    for folder in known:
        try:
            same = Path(folder).resolve() == resolved
        except OSError:
            same = False
        if same or str(folder) == given:
            return Path(folder)
    return None


def _codex_trusted() -> list[Path]:
    """The folders Codex itself trusts: `trust_level = "trusted"` under [projects."<path>"] in its config."""
    import tomllib
    try:
        projects = tomllib.loads((Path.home() / ".codex/config.toml").read_text()).get("projects", {})
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(projects, dict):
        return []
    return [Path(p) for p, v in projects.items() if isinstance(v, dict) and v.get("trust_level") == "trusted"]


def _trusted(agent: str, server) -> list[Path]:
    """Where a child of that agent may start. Codex keeps its own list; Claude's list (the one the phone's new
    chat offers) stands for Claude and Cursor."""
    if agent == "codex":
        return _codex_trusted()
    return [Path(p) for p in server.trusted_folders(limit=None)]


def _trust_question_up(tab: Tab, agent: str, wait: float) -> bool:
    """The agent's own trust question is on screen (looked for up to `wait` seconds): the folder wasn't trusted
    after all, and the ask must not be typed into it (Codex's default answer is yes)."""
    import time
    until = time.monotonic() + wait
    while True:
        if _still_asking(tab, agent):
            return True
        if time.monotonic() >= until:
            return False
        time.sleep(0.5)


def _pane(speaker: Speaker) -> str:
    """The caller's pane. Its own tab wins: the environment is this process, which may not be the caller under test."""
    if speaker.tab:
        return speaker.tab.address
    return os.environ.get("TMUX_PANE", "")


def deploy(args: list[str], caller) -> int:
    """Start a child in a new tmux pane and type the ask. The caller is the boss."""
    agent, folder, ask = _deploy_args(args)
    if agent not in AGENTS or not folder or not ask:
        print('usage: handsfree deploy --agent claude|cursor|codex --folder PATH --ask "words"')
        return 1
    boss = caller() if caller else None
    if boss is None or not boss.session:
        print("couldn't tell which agent is deploying")
        return 1
    chains.bind(boss.session, _pane(boss), int(boss.pid or 0))
    depth = 2 if chains.parent_of(boss.session) else 1
    chain = chains.open(boss.session)
    from voice.desktop import tmux
    # A child that exited by itself (/exit, a crash) has no ended line: its pane is gone, so its slot is freed here.
    # The slot is held from this check on: the launch line is only written once the ask is typed, seconds later
    refused, slot = chains.reserve(chain, boss.session, depth, alive=tmux.pane_pids())
    if refused:
        print(refused)
        return 1
    launched = False
    try:
        launched = _launch(chain, slot, boss, depth, agent, folder, ask) == 0
    finally:
        if not launched:
            chains.release(chain, slot)
    return 0 if launched else 1


def _launch(chain: str, slot: str, boss: Speaker, depth: int, agent: str, folder: str, ask: str) -> int:
    """Start the child in its reserved slot; 0 once its launch line (which takes over the slot) is written."""
    from voice.desktop import tmux
    server = _server()
    match = _folder(folder, _trusted(agent, server))
    if match is None:
        print(f"not started: {agent} doesn't trust {folder} yet, and a deploy never says yes to that question. "
              f"Start {agent} there yourself once (or from the phone's new chat) and answer it, then deploy again.")
        return 1
    from voice import permissions, resume
    from voice.desktop import current as desktop
    # No more than the boss may do itself, from the boss's own record; ask mode when that can't be read
    boss_chat = resume.chat_id(boss.session)
    boss_level = permissions.level(boss.agent, boss_chat, bool(boss.in_editor))
    # A Codex boss's own -a and -s go to a Codex child, so it never gets more than its boss runs with
    boss_codex = permissions.codex_turn(boss_chat) if boss.agent == "codex" else None
    flags = permissions.child_flags(agent, boss_level, boss_codex)
    if flags is None:
        print(f"not started: Cursor's settings run every command without asking, and this agent asks first, so a "
              f"{agent} helper would get more than you have. Deploy claude or codex instead.")
        return 1
    tab = tmux.start(agent, match, flags=flags)
    if not tab:
        print(f"{agent} didn't start")
        return 1
    # Never answered here: a folder the agent trusts asks nothing. If its question shows anyway, the ask would be
    # typed into it (Codex's default answer is yes), so the child is closed instead. Claude's is looked for once
    # its box is drawn; Codex's may take a moment to come.
    if agent in ("claude", "cursor"):  # the layouts read here; Codex would only wait out the timeout
        tmux.composer_ready(tab)
    if _trust_question_up(tab, agent, 4.0 if agent == "codex" else 0.0):
        tmux.end(tab)
        print(f"not started: {agent} asked whether to trust {match.name}, and a deploy never answers that")
        return 1
    # The box was drawn first, and the ask must actually leave it: a multi-line paste can keep its Enter
    typed = desktop().terminals.type_line(tab, ASK_PREFIX + ask) and (
        agent not in ("claude", "cursor") or tmux.submit_pending(tab, ASK_MARK))
    if not typed:
        tmux.end(tab)
        print(f"{agent} started in {match.name} (tmux {tab.address}), but the ask was not typed")
        return 1
    pid = tmux.pane_pid(tab)
    server.spawned(lambda alive: [*alive[-49:], {"pane": tab.address, "pid": pid}])
    # Folder name only: the notice goes to the phone, and the chain file keeps the pane, not the path
    server.notice(f"{agent.capitalize()} started in {match.name}",
                  f"From a deploy, in {match.name}. {permissions.child_runs(agent, boss_level)} "
                  "`tmux attach -t henchline` to watch or answer.")
    # The boss's own pane, from the tree: a Cursor CLI's chat can only be told by its pane (agents.vouched), and a
    # helper's pane must never pass for it
    from voice.agents import tree_pane
    boss_pane, boss_pid = tree_pane()
    chains.append(chain, "launch", parent=boss.session, pane=tab.address, pid=pid, depth=depth + 1,
                  agent=agent, where=match.name, ask=ask, reservation=slot, parent_pane=boss_pane, parent_pid=boss_pid)
    log(HANDSFREE_LOG, f"deployed {agent} in {match.name} (tmux {tab.address})")
    print(f"deployed {agent} in {match.name} (tmux {tab.address})")
    return 0


def _still_asking(tab: Tab, agent: str) -> bool:
    """The trust question is still the thing on screen, so the ask must not be typed into it."""
    from voice.desktop import tmux
    words = {"claude": "trust this folder", "codex": "do you trust the contents of this directory"}.get(agent, "")
    if not words:
        return False
    shown = tmux.out("capture-pane", "-p", "-t", tab.address).lower()
    return words in shown


def _speaker(session: str) -> Speaker | None:
    names = chains.aliases(session)
    preferred = chains.canonical(session)
    found = Speaker.load(SPEAKERS / f"{preferred}.json")
    if found:
        return found
    # A discovered folder key is replaced by the real chat id once that boss speaks. Its old speaker file is
    # deliberately removed, so reports must follow the same rename as the phone's chat. The newest live alias is
    # the canonical one when a short rename transition leaves both files present.
    live = [speaker for speaker in recent_speakers() if speaker.session in names]
    if live:
        return max(live, key=lambda speaker: speaker.at)
    for name in names:
        found = Speaker.load(SPEAKERS / f"{name}.json")
        if found:
            return found
    return None


def _boss(session: str) -> Speaker | None:
    return _speaker(session)


def report(args: list[str], caller) -> int:
    """The caller is the child. Corrections since its last report are delivered to the boss first.

    Whose call is pinned is left as it is: this types to the boss and does not switch.
    """
    words = " ".join(args).strip()
    if not words:
        print("usage: handsfree report WORDS")
        return 1
    child = caller() if caller else None
    if child is None or not child.session:
        print("couldn't tell which agent is reporting")
        return 1
    chains.bind(child.session, _pane(child), int(child.pid or 0))
    boss = chains.parent_of(child.session)
    if not boss:
        print("this agent has no boss to report to")
        return 1
    target = _boss(boss)
    if target is None:
        print("the boss isn't a known agent yet, so the report was not typed")
        return 1
    prior = chains.pending_corrections(child.session)
    body = words if not prior else "Corrections since the last report:\n" + \
        "\n".join(item["text"] for item in prior) + "\n\n" + words
    routing = _routing()
    # SCHEDULED so a failure isn't spoken into the room, and so this isn't the user answering the boss.
    # The text itself starts with the report marker, so it is typed as a report and not kept as a correction.
    delivery = routing.deliver(f"{chains.REPORT} {body}", routing.Heard.SCHEDULED, to=target)
    if not delivery:
        print(delivery.why or "couldn't deliver the report")
        return 1
    chain = chains.chain_of(child.session)
    if chain:
        chains.append(chain, "report", session=child.session, text=words, exact=True)
    chains.mark_reported(child.session, prior)
    print("reported")
    return 0


def _hand_up(session: str) -> bool:
    """Deliver unreported corrections to the boss before the pane is closed. False when they could not be delivered."""
    for _ in range(3):
        prior = chains.pending_corrections(session)
        if not prior:
            return True
        target = _boss(chains.parent_of(session))
        if target is None:
            return False
        body = "Corrections since the last report:\n" + "\n".join(item["text"] for item in prior)
        routing = _routing()
        if not routing.deliver(f"{chains.REPORT} {body}", routing.Heard.SCHEDULED, to=target):
            return False
        chains.mark_reported(session, prior)
    return not chains.pending_corrections(session)


def end_session(session: str, *, force: bool = False) -> int:
    """End that one session's pane, when it is one started here (the same rule as ending a chat from the phone).

    Siblings stay. The old id is not resumed.
    """
    from voice.desktop import tmux
    session = chains.canonical(session)
    names = chains.aliases(session)
    target = _speaker(session)
    if target and target.tab:
        tab = target.tab
    else:
        pane = chains.pane_of(session)
        tab = Tab(tmux.OWNER, pane) if pane else Tab()
    if not tab or tab.owner != tmux.OWNER:
        print("only an agent started from here can be ended, and only that one")
        return 1
    server = _server()
    if not server.owns(tab.address, int(target.pid or 0) if target else 0):
        print("only an agent started from here can be ended, and only that one")
        return 1
    from voice.state import LAST, last_speaker
    # A child with the user in it or unreported words waits for an explicit phone choice.
    current = last_speaker()
    reasons = []
    if (current and current.session in names) or chains.phone_viewing(session):
        reasons.append("open")
    if chains.corrections_since(session):
        reasons.append("words")
    if reasons and not force:
        chain = chains.chain_of(session)
        if not chain:
            print("that is not a delegated chat, so it was not ended")
            return 1
        if not chains.pending_end(session):
            chains.append(chain, "end_requested", session=session, pane=tab.address,
                          pid=int(target.pid or 0) if target else 0, reason=",".join(reasons))
        print("waiting for the phone: End or Keep")
        return 0
    pid = int(target.pid or 0) if target else 0
    # A delivery and its correction journal line hold the same lock as this final check and pane close. If words
    # landed just after a hand-up, release the lock, hand that exact new snapshot up, and try the close again.
    for _ in range(4):
        if not _hand_up(session):
            print("the user's words were not delivered to the boss, so it was not ended")
            return 1
        with chains.close_guard(session) as clear:
            if not clear:
                continue
            if not tmux.end(tab):
                print("couldn't end that pane")
                return 1
            break
    else:
        print("new words kept arriving, so the pane was not ended")
        return 1
    server.spawned(lambda alive: [e for e in alive if e.get("pane") != tab.address])
    chains.close_child(session, tab.address, pid)
    # The next hands-free turn must not resume this id. Follow the boss when we know them.
    followed = last_speaker()
    if followed and followed.session in names:
        boss = _boss(chains.parent_of(session))
        if boss:
            boss.save(LAST)
        else:
            LAST.unlink(missing_ok=True)
    agent = target.agent if target else ""
    for other in (Speaker.load(path) for path in SPEAKERS.glob("*.json")):
        if not other:
            continue
        same = other.session in names or (
            other.tab.address == tab.address and other.tab.owner == tmux.OWNER and (not agent or other.agent == agent))
        if same:
            (SPEAKERS / f"{other.session}.json").unlink(missing_ok=True)
    log(HANDSFREE_LOG, f"ended {session} (tmux {tab.address})")
    print(f"ended {session}")
    return 0


def may_end(caller_session: str, target: str) -> bool:
    """Only the boss that started that child, or the child itself, may end it from the command line. The phone's
    End is the user's own choice and does not come through here. The caller is known from its own environment
    (handsfree.caller), which an agent could fake: this guards against mistakes (a wrong id, a sibling), it is
    not a security boundary."""
    if not caller_session or not target:
        return False
    mine = chains.aliases(caller_session)
    if chains.canonical(target) in mine or target in mine:
        return True
    boss = chains.parent_of(target)
    return bool(boss) and boss in mine


def _bind_child(target: str) -> None:
    """A launch is stored by its pane and pid; the child's session is bound to it later. Bound here from the
    child's own record, the same way a report binds it, so its boss is known before may_end asks."""
    if not target or chains.parent_of(target):
        return
    known = _speaker(chains.canonical(target))
    if known and known.tab and known.pid:
        chains.bind(known.session, known.tab.address, int(known.pid))


def end(args: list[str], caller=None) -> int:
    """Command-line wrapper for ending one delegated session: the caller must be its boss, or the child itself."""
    if len(args) != 1 or not args[0].strip():
        print("usage: handsfree end SESSION")
        return 1
    target = args[0].strip()
    me = caller() if caller else None
    if me is None or not me.session:
        print("couldn't tell which agent is ending it")
        return 1
    _bind_child(target)
    if not may_end(me.session, target):
        print("only the agent that started it, or the agent itself, may end it")
        return 1
    return end_session(target)
