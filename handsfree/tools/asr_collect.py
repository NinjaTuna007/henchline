#!/usr/bin/env -S sh -c 'exec "$HOME/.local/share/henchline/venv/bin/python3" -E -s "$0" "$@"'
"""A test set for speech-to-text: the user's own voice saying known sentences.

    asr_collect.py [--strict] [POSITION ...]

--strict: word for word, into asr-set/strict/ (the loose first round was
paraphrased, as allowed, so its texts could not score whole sentences).

Repeat after me, hands-free: for each position talkback says where to stand,
then for each sentence it reads the sentence out, chimes, and records the
user saying it back from the same echo-cancelled mic hands-free uses. The
takes and their texts (set.json) land in ~/.local/share/handsfree/asr-set/,
for asr_bench.py to score Whisper models against. Hands-free listening is
paused meanwhile, so nothing is sent to an agent.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "core"), str(Path(__file__).resolve().parents[2])]  # hands-free's modules; voice/
from collect import CHIME, speak as say  # noqa: E402
from sensing import RATE  # noqa: E402
from voice.state import EC_SOURCE, HANDSFREE_DATA, PAUSE  # noqa: E402

SET = HANDSFREE_DATA / "asr-set"
POSITIONS = [
    ("desk", "at your desk, facing me"),
    ("across", "across the room, wherever you usually stand"),
]
# What the user says to the agents: their names, tools, machines, and ordinary talk
SENTENCES = [
    "Claude, can you check the logs and tell me what went wrong?",
    "Switch me over to Cursor in the copilot ptt workspace.",
    "Commit that and push it to GitHub once the review is clean.",
    "What's the GPU memory looking like on Berzelius right now?",
    "Restart the hands-free service and run the echo replay again.",
    "Send me an ntfy notification when the training run finishes.",
    "Open a new Konsole tab and attach to the tmux session.",
    "I think the echo canceller drifted again, take a look.",
    "Hand me back to Codex, I want to finish the sidescan model.",
    "The weather in Stockholm is grey today, as usual.",
    "Never mind, scratch that, let's do the Android app first.",
    "How much battery do I have left, and is the laptop on AC?",
    "Give me a minute, I'm making coffee in the kitchen.",
    "Is the home server still up, and did the welcome home service fire?",
    "Read me the last three commit messages, please.",
]


def record(seconds: float) -> np.ndarray:
    proc = subprocess.run(["timeout", str(seconds), "parec", f"--device={EC_SOURCE}", "--format=s16le",
                           f"--rate={RATE}", "--channels=1", "--raw"], capture_output=True)
    return np.frombuffer(proc.stdout, dtype="<i2")


def save(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(audio.tobytes())


def main() -> int:
    strict = "--strict" in sys.argv
    wanted = {a for a in sys.argv[1:] if a != "--strict"}
    positions = [p for p in POSITIONS if not wanted or p[0] in wanted]
    folder = SET / "strict" if strict else SET
    folder.mkdir(parents=True, exist_ok=True)
    index = folder / "set.json"
    texts = json.loads(index.read_text()) if index.exists() else {}
    PAUSE.touch()
    try:
        say("Speech test. I'll read a sentence, then chime. After the chime, say the same sentence back, "
            + ("word for word, exactly as I said it, in your normal voice and pace." if strict
               else "in your normal voice. It doesn't have to be word perfect."))
        for name, where in positions:
            say(f"Next: {where}. First sentence in ten seconds.")
            time.sleep(10)
            for n, sentence in enumerate(SENTENCES, 1):
                say(sentence)
                subprocess.run(["pw-play", CHIME])
                audio = record(len(sentence.split()) * 0.55 + 3)  # room for the reaction and a slow pace
                take = f"{name}-{n:02d}-{time.strftime('%H%M%S')}.wav"
                save(folder / take, audio)
                texts[take] = sentence
                index.write_text(json.dumps(texts, indent=1))
                print(f"{take}: {len(audio) / RATE:.1f} s", flush=True)
        say("That's all, thank you.")
    finally:
        PAUSE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
