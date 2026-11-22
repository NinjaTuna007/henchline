#!/usr/bin/env -S sh -c 'exec "$HOME/.local/share/henchline/venv/bin/python3" -E -s "$0" "$@"'
"""Speak text with the local Kokoro voice on the default output.

    talkback [--wait] [--voice V] [TEXT ...]   text from the arguments, or stdin
    talkback --release [TEXT]     play this session's held clips (TEXT first, if given)
    talkback --drop               throw away this session's held and queued clips
    talkback --stop               cut off everything and empty the queue

Each call queues its text (spool.py) and returns at once; one detached
worker (worker.py) speaks the queue, one clip after another, in the calling
agent's own voice. A different speaker introduces itself first. While the
user talks the worker goes quiet; when the words went to an agent, every
session's waiting clips are held, and speaking again drops a session's stale
ones. The voice is Kokoro on the CPU, so the dGPU can sleep.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # the repository, for voice/
sys.path.insert(0, str(HERE))

from voice import spool  # noqa: E402
import worker  # noqa: E402
from voice.agents import agent_where, identity  # noqa: E402
from voice.desktop import current as desktop  # noqa: E402
from voice.state import AWAY, HOLDS, PLAYING, SPOOL, TALKBACK_LOG, WORKER_PID, Playing, in_call, log  # noqa: E402


def spawn_worker() -> None:
    subprocess.Popen([str(HERE / "say.py"), "--worker"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=open(TALKBACK_LOG, "a"), start_new_session=True)


def stop() -> None:
    spool.clear()
    try:
        os.killpg(int(WORKER_PID.read_text().strip()), signal.SIGTERM)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass
    WORKER_PID.unlink(missing_ok=True)
    PLAYING.unlink(missing_ok=True)


def new_clip(agent: str, session: str, text: str, voice: str) -> spool.Clip:
    return spool.Clip.of_text(text, agent=agent, session=session, voice=voice, where=agent_where(),
                              tab=desktop().terminals.tab_of_caller(), for_call=in_call())


def text_from(words: list[str]) -> str:
    return " ".join(words).strip() or ("" if sys.stdin.isatty() else sys.stdin.read().strip())


def main() -> int:
    parser = argparse.ArgumentParser(prog="talkback", add_help=True, allow_abbrev=False,
                                     description="Speak text aloud in the calling agent's own voice (queued; returns at once).")
    mode = parser.add_mutually_exclusive_group()
    for flag, about in (("--stop", "cut off everything and empty the queue"),
                        ("--worker", "run the worker that speaks the queue (started by itself)"),
                        ("--release", "play this session's held clips (the text first, if given)"),
                        ("--drop", "throw away this session's held and queued clips"),
                        ("--wait", "return once this text has been spoken")):
        mode.add_argument(flag, action="store_true", help=about)
    parser.add_argument("--agent", default="", help="speak as this agent (default: the one calling)")
    parser.add_argument("--session", default="", help="speak as this agent session (default: the calling one)")
    parser.add_argument("--voice", default=os.environ.get("TALKBACK_VOICE", ""),
                        help="a Kokoro voice instead of the agent's own (or $TALKBACK_VOICE)")
    parser.add_argument("words", nargs="*", help="what to say (default: stdin)")
    args, extra = parser.parse_known_args()  # text that happens to start with "-" is still text
    args.words += extra
    SPOOL.mkdir(parents=True, exist_ok=True)
    HOLDS.mkdir(parents=True, exist_ok=True)
    if args.stop:
        stop()
        return 0
    if args.worker:
        return worker.work()
    agent, session = identity(args.agent, args.session)
    if args.release:
        # With text: say it first ("no worries, as I was saying"), then the held
        # clips carry on from the start of the sentence that was cut
        lead, mine = text_from(args.words), spool.of_session(session)
        if lead:
            first = int(mine[0].name.split("-")[0]) - 1 if mine else None
            spool.add(new_clip(agent, session, lead, args.voice), at_ns=first)
        spool.release(session)
        spawn_worker()
        return 0
    if args.drop:
        spool.drop(session)
        if (playing := Playing.now()) and playing.session == session:
            try:
                os.kill(int(WORKER_PID.read_text().strip()), signal.SIGUSR1)
            except (FileNotFoundError, ValueError, ProcessLookupError):
                pass
        return 0
    text = text_from(args.words)
    if not text:
        return 0
    if AWAY.exists() and not in_call() and agent not in ("handsfree",):  # the agent learns it, and chooses a text
        print("not said: the user is away and can't hear the laptop. Text them instead: handsfree text \"…\"")
        log(TALKBACK_LOG, f"{agent}: not said, the user is away")
        return 0
    if spool.is_held(session):
        # Speaking again means the agent has moved on: what it had not said
        # before the user cut in is stale, and holding the new reply behind it
        # left the user in silence whenever an agent forgot --release/--drop
        spool.drop(session)
        log(TALKBACK_LOG, f"{agent} spoke again; dropped its older held clips")
    path = spool.add(new_clip(agent, session, text, args.voice))
    spawn_worker()
    while args.wait and (path.exists() or ((playing := Playing.now()) and playing.clip == path.name)):
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
