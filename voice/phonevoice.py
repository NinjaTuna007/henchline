"""Talkback rendered on the phone: the laptop sends a clip's words, the phone speaks them with Kokoro itself.

Over a call the laptop normally streams its own Kokoro audio. When the phone has the model and the user turned
"Speak on the phone" on, only text crosses the network (a dropped line in a lift loses no words), and the phone
renders the same voice: sherpa-onnx's Kokoro v1.0 export carries the very same voices file (checked 2026-11-22:
af_heart, af_bella, bf_alice, af_sky, bm_george equal to the laptop's to the last float), a voice's id is its place
in the sorted list of names.

The pieces travel through files, so the talkback worker and the call server stay separate processes:
  CALL_TTS                 the phone's offer for this call: {"engine", "model", "fallback": reason or ""}
  PHONE_SAY/<id>.json      a clip for the phone, written by the worker, sent by the call server
  PHONE_SAY/<id>.sent      the server sent it
  PHONE_SAY/<id>.progress  the phone's acks: {"spoken": pieces started, "done": bool, "fallback": reason}
  PHONE_SAY/<id>.stop      the worker cut the clip (the user talked over it): the server tells the phone

The model files the phone downloads are served by the laptop (MODEL_DIR), so the phone needs no internet and gets
the same bytes the laptop checked: `python -m voice.phonevoice prepare` fetches sherpa-onnx's archive once
(pinned SHA-256) and keeps only what the phone uses.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import time
from pathlib import Path

from voice.state import STATE, read_json, write_json

VERSION = "kokoro-multi-lang-v1_0"
ARCHIVE_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{VERSION}.tar.bz2"
ARCHIVE_SHA256 = "60703707bc05ccd48d60e1eb723f38b07d6e474795a0b6a588d7220400e0f7a4"
ROOT = Path.home() / ".local/share/henchline/phone-voice"
MODEL_DIR = ROOT / VERSION
FILES = ("model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt", "LICENSE")  # all the phone loads
LAPTOP_VOICES = Path.home() / ".local/share/talkback/voices-v1.0.bin"

CALL_TTS = STATE / "call.tts"
PHONE_SAY = STATE / "phone-say"
PHONE_RTF = 1.0  # until the phone reports its own: in a call it rendered at 0.94-1.55 (adb shell said 0.68), 2026-11-22
ID = re.compile(r"[A-Za-z0-9._-]{1,120}")


# --- the model files the phone downloads ---------------------------------------------------------------------

def manifest(root: Path = MODEL_DIR) -> dict | None:
    """{"version", "files": [{name, size, sha256}]}, or None while the files aren't there. Hashes are kept beside
    the files and worked out again only when a file's size or time changed (the model is 325 MB)."""
    cache_path = root / ".manifest.json"
    cache = read_json(cache_path) if cache_path.exists() else {}
    files, changed = [], False
    for name in FILES:
        path = root / name
        if not path.is_file():
            return None
        stat = path.stat()
        known = cache.get(name) or {}
        if known.get("size") != stat.st_size or known.get("mtime") != stat.st_mtime:
            known = {"size": stat.st_size, "mtime": stat.st_mtime, "sha256": _sha256(path)}
            cache[name], changed = known, True
        files.append({"name": name, "size": known["size"], "sha256": known["sha256"]})
    if changed:
        write_json(cache_path, cache)
    return {"version": VERSION, "files": files}


def model_file(name: str, root: Path = MODEL_DIR) -> Path | None:
    """One of the files the phone may fetch, never anything else (no paths, no other names)."""
    return root / name if name in FILES and (root / name).is_file() else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def prepare(root: Path = ROOT) -> Path:
    """Fetch sherpa-onnx's Kokoro archive (once, checked against its pinned hash) and keep only FILES."""
    import urllib.request
    target = root / VERSION
    if all((target / name).is_file() for name in FILES):
        return target
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"{VERSION}.tar.bz2"
    if not archive.exists() or _sha256(archive) != ARCHIVE_SHA256:
        partial = archive.with_suffix(".part")
        urllib.request.urlretrieve(ARCHIVE_URL, partial)
        if _sha256(partial) != ARCHIVE_SHA256:
            partial.unlink(missing_ok=True)
            raise RuntimeError("the downloaded archive doesn't match its pinned hash")
        partial.replace(archive)
    with tarfile.open(archive, "r:bz2") as tar:
        wanted = [tar.getmember(f"{VERSION}/{name}") for name in FILES]
        tar.extractall(root, members=wanted, filter="data")
    return target


def voice_id(name: str, voices: Path = LAPTOP_VOICES) -> int | None:
    """A voice's id on the phone: its place among the sorted names of the shared voices file."""
    try:
        import numpy as np
        names = sorted(np.load(voices).files)
    except (OSError, ValueError, ImportError):
        return None
    return names.index(name) if name in names else None


# --- the call: the phone's offer, and its fallback ------------------------------------------------------------

def offer(message: dict) -> None:
    """The phone said what it can render ({"tts": {"engine", "model"}}), or that it can't keep up any more
    ({"tts": "fallback", "reason"}): from then on, this call streams the laptop's audio again."""
    tts = message.get("tts")
    if isinstance(tts, dict):
        if tts.get("model") == VERSION and tts.get("engine") == "sherpa-kokoro":
            state = {"engine": tts["engine"], "model": tts["model"], "fallback": ""}
            if _rtf(tts.get("rtf")):
                state["rtf"] = _rtf(tts.get("rtf"))
            write_json(CALL_TTS, state)
        else:
            CALL_TTS.unlink(missing_ok=True)
    elif tts == "fallback":
        state = read_json(CALL_TTS) if CALL_TTS.exists() else {}
        if state:
            state["fallback"] = str(message.get("reason") or "the phone asked")[:200]
            write_json(CALL_TTS, state)
        clip = str(message.get("id") or "")
        if ID.fullmatch(clip):
            _progress(clip, fallback=state.get("fallback") or "the phone asked")


def _rtf(value) -> float | None:
    """A render speed the phone reported (render s per audio s), or None when it isn't a sane number."""
    return float(value) if isinstance(value, (int, float)) and 0.05 <= value <= 10 else None


def plan_rtf() -> float:
    """The speed the laptop cuts the phone's pieces for: the phone's own measured speed with a margin
    (max(1.2 x measured, 0.9)), else PHONE_RTF."""
    state = read_json(CALL_TTS) if CALL_TTS.exists() else {}
    measured = _rtf(state.get("rtf"))
    return max(1.2 * measured, 0.9) if measured else PHONE_RTF


def phone_renders() -> bool:
    """This call's talkback goes to the phone as words."""
    state = read_json(CALL_TTS) if CALL_TTS.exists() else {}
    return bool(state) and not state.get("fallback")


def call_over() -> None:
    """The call ended: the offer goes, and so do clips nobody will speak (the worker texts or keeps its own)."""
    CALL_TTS.unlink(missing_ok=True)
    if PHONE_SAY.is_dir():
        for path in PHONE_SAY.iterdir():
            path.unlink(missing_ok=True)


# --- the worker's side -----------------------------------------------------------------------------------------

def send(clip: str, agent: str, voice: str, sid: int, pieces: list[str], joined: set[int],
         sentence_map: list[int]) -> None:
    """A clip for the phone: its pieces as the laptop cut them, which join the one before, which sentence each is."""
    PHONE_SAY.mkdir(parents=True, exist_ok=True)
    for suffix in (".sent", ".progress", ".stop"):
        (PHONE_SAY / f"{clip}{suffix}").unlink(missing_ok=True)
    write_json(PHONE_SAY / f"{clip}.json", {"id": clip, "agent": agent, "voice": voice, "sid": sid,
                                             "pieces": pieces, "joined": sorted(joined),
                                             "sentence_map": sentence_map, "at": time.time()}, private=True)


def progress(clip: str) -> dict:
    """{"spoken", "done", "fallback"} as the phone last said (pieces it has started)."""
    path = PHONE_SAY / f"{clip}.progress"
    found = read_json(path) if path.exists() else {}
    return {"spoken": int(found.get("spoken") or 0), "done": bool(found.get("done")),
            "fallback": str(found.get("fallback") or "")}


def stop(clip: str) -> None:
    """The user talked over it: the phone stops at once."""
    if (PHONE_SAY / f"{clip}.json").exists():
        (PHONE_SAY / f"{clip}.stop").touch()


def cancel(clip: str, wait: float = 1.5) -> None:
    """The laptop takes the clip back (the phone never started it, or the user talked over it): the phone is told to
    stop BEFORE the files go, so a slow phone can't speak it alongside the laptop's own audio. Waits for the call
    server to pass the stop on (at most `wait`; with no call to pass it on, there's no phone to speak it either)."""
    if (PHONE_SAY / f"{clip}.json").exists():
        (PHONE_SAY / f"{clip}.stop").touch()
        until = time.time() + wait
        while (PHONE_SAY / f"{clip}.stop").exists() and time.time() < until:
            time.sleep(0.02)
    finished(clip)


def finished(clip: str) -> None:
    for suffix in (".json", ".sent", ".progress", ".stop"):
        (PHONE_SAY / f"{clip}{suffix}").unlink(missing_ok=True)


# --- the call server's side ------------------------------------------------------------------------------------

def outgoing() -> list[dict]:
    """What the server has to tell the phone now: new clips ({"say": …}) and cuts ({"stop": id}), oldest first.
    Each is returned once."""
    if not PHONE_SAY.is_dir():
        return []
    found = []
    for path in sorted(PHONE_SAY.glob("*.json"), key=lambda p: p.stat().st_mtime):
        clip = path.stem
        if not ID.fullmatch(clip):
            continue
        sent = PHONE_SAY / f"{clip}.sent"
        cut = PHONE_SAY / f"{clip}.stop"
        if not sent.exists() and cut.exists():  # taken back before it went out: never sent at all
            sent.touch()
            cut.unlink(missing_ok=True)
            continue
        if not sent.exists():
            say = read_json(path)
            if say:
                found.append({"say": say})
                sent.touch()
        if cut.exists() and sent.exists():
            found.append({"stop": clip})
            cut.unlink(missing_ok=True)
    return found


def said(message: dict) -> None:
    """The phone's ack: {"said": {"id", "piece", "done"}}: piece n has started playing (or the clip is over)."""
    ack = message.get("said")
    if not isinstance(ack, dict) or not ID.fullmatch(str(ack.get("id") or "")):
        return
    rtf = _rtf(ack.get("rtf"))
    if rtf and CALL_TTS.exists():  # the phone's running render speed: the next clip is cut for it
        state = read_json(CALL_TTS)
        if state and state.get("rtf") != rtf:
            state["rtf"] = rtf
            write_json(CALL_TTS, state)
    piece = ack.get("piece")
    _progress(str(ack["id"]), spoken=piece + 1 if isinstance(piece, int) and piece >= 0 else None,
              done=bool(ack.get("done")))


def _progress(clip: str, spoken: int | None = None, done: bool = False, fallback: str = "") -> None:
    if not (PHONE_SAY / f"{clip}.json").exists():
        return  # a clip already over (or another call's): nothing to record
    now = progress(clip)
    write_json(PHONE_SAY / f"{clip}.progress", {
        "spoken": max(now["spoken"], spoken or 0), "done": now["done"] or done, "fallback": fallback or now["fallback"]})


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["prepare"]:
        print(prepare())
        print(json.dumps(manifest(), indent=1))
    else:
        print("usage: python -m voice.phonevoice prepare", file=sys.stderr)
        sys.exit(2)
    os._exit(0)
