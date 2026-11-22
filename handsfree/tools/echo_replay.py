#!/usr/bin/env -S sh -c 'exec "$HOME/.local/share/henchline/venv/bin/python3" -E -s "$0" "$@"'
"""Replay an echo_collect.py session through the live talk-over code, offline.

    echo_replay.py SESSION [SUSTAIN] [GAP]

Runs the same VoiceGate and TalkOver (with the word check against the clip's
text) the listener runs, on recorded frames with the recording's own clock,
so thresholds are tuned against recordings before the user has to fight them.
Per take: whether the clip would have been stopped, when (relative to the
chime), and whether that was false (no chime yet, or a quiet take).
"""

from __future__ import annotations

import collections
import json
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "core"), str(HERE.parents[1])]  # the tools, hands-free's modules; voice/
from listener import BARGE_GRACE, FLOOR_SPAN, PREROLL, floor_of  # noqa: E402
from sensing import FRAME, LEVEL_SPAN, RATE, level  # noqa: E402
from talkover import GAP, SUSTAIN, TalkOver, Verdict  # noqa: E402
from voice.speech import whisper  # noqa: E402
from voicegate import VoiceGate  # noqa: E402

NO_LOG = Path("/dev/null")  # a replay must not write into hands-free's own log


def load(folder: Path, name: str) -> np.ndarray:
    with wave.open(str(folder / name)) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
        return audio.reshape(-1, w.getnchannels())[:, 0]


def replay(folder: Path, take: dict, transcriber: whisper.Transcriber, sustain: float,
           gap: float) -> tuple[float | None, int]:
    """(when the clip would have stopped, how often its echo was caught by its words)."""
    ec, play = load(folder, f"{take['take']}-ec.wav"), load(folder, f"{take['take']}-play.wav")
    clock = [0.0]
    over = TalkOver(transcriber.quick_text, clock=lambda: clock[0], sustain=sustain, gap=gap, log_to=NO_LOG)
    gate = VoiceGate()
    echo_floor = collections.deque(maxlen=FLOOR_SPAN)
    plays = collections.deque(maxlen=LEVEL_SPAN)
    preroll = collections.deque(maxlen=PREROLL)
    echoes = 0
    for i in range(min(len(ec), len(play)) // FRAME):
        clock[0] = i * FRAME / RATE
        frame = ec[i * FRAME:(i + 1) * FRAME]
        db = level(frame)
        plays.append(level(play[i * FRAME:(i + 1) * FRAME]))
        over.track_onset(max(plays))
        if not take["clip_start"] < clock[0] < take["clip_end"]:
            continue
        floor = floor_of(echo_floor, db)
        preroll.append(frame)
        gate.add(frame, db)
        speech = gate.talking(frame, db, floor, True, max(plays),
                              clock[0] - take["clip_start"] > BARGE_GRACE, False)
        if not over.over:
            echo_floor.append(db)
        verdict = over.feed(frame, speech or gate.still_speaking(), list(preroll), "replay", take["text"],
                            strong=speech, margin=gate.margin if speech else None)
        if verdict is Verdict.ECHO:
            echoes += 1
        elif verdict is Verdict.USER:
            return clock[0], echoes
    return None, echoes


def main() -> int:
    folder = Path(sys.argv[1])
    sustain = float(sys.argv[2]) if len(sys.argv) > 2 else SUSTAIN
    gap = float(sys.argv[3]) if len(sys.argv) > 3 else GAP
    cfg = whisper.Config.load(NO_LOG)
    transcriber = whisper.Transcriber(cfg, NO_LOG)
    false = miss = caught = 0
    lags = []
    for take in json.loads((folder / "trials.json").read_text()):
        hit, echoes = replay(folder, take, transcriber, sustain, gap)
        caught += echoes
        cue = take["cue"]
        if hit is not None and (cue is None or hit < cue):
            false += 1
            verdict = f"FALSE stop at {hit:.1f}s"
        elif cue is not None and hit is None:
            miss += 1
            verdict = "missed"
        elif hit is not None:
            lags.append(round(hit - cue, 2))
            verdict = f"stopped {hit - cue:.2f}s after the chime"
        else:
            verdict = "kept playing (right)"
        print(f"  {take['take']:18s} {verdict}" + (f", echo caught by its words {echoes}x" if echoes else ""))
    print(f"SUSTAIN {sustain} GAP {gap}: false {false}, missed {miss}, "
          f"echo caught {caught}x, delays after the chime {lags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
