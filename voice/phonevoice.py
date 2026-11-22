"""Talkback rendered on the phone: the laptop sends a clip's words and their phonemes, the phone speaks them itself.

Over a call the laptop normally streams its own Kokoro audio. When the phone has the model and the user turned
"Speak on the phone" on, only text crosses the network (a dropped line in a lift loses no words), and the phone
renders it with its own voice: Piper's British English "Jenny" (a VITS model, one speaker, 22.05 kHz). Kokoro
rendered too slowly there (1.12-1.34 s per second of speech on the cores a locked call gets, Jenny 0.14, measured
2026-11-22), so every agent's talkback is Jenny's voice on the phone.

The pieces travel through files, so the talkback worker and the call server stay separate processes:
  CALL_TTS                 the phone's offer for this call: {"engine", "model", "fallback": reason or ""}; the user
                           switching to the laptop mid-call is a fallback too (CHOSE_LAPTOP)
  PHONE_SAY/<id>.json      a clip for the phone, written by the worker, sent by the call server: {"id", "pieces",
                           "ph" (each piece's phonemes, spoken in place of its words), "joined", "sentence_map", …}
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
import threading
import time
from pathlib import Path

from voice.state import STATE, read_json, write_json

# The model's name is the format's version: a phone holding another model (Kokoro, before 2026-11-22) isn't offered
# this one's files under its old folder, it downloads these afresh, and the laptop takes only this model's offers
VERSION = "vits-piper-en_GB-jenny_dioco-medium"
ENGINE = "sherpa-vits"
ARCHIVE_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{VERSION}.tar.bz2"
ARCHIVE_SHA256 = "a0888024569bafbefc05a4b48ddf8419d8dbbf3205f4af37cf7c6f1a87cc20c5"
ROOT = Path.home() / ".local/share/henchline/phone-voice"
MODEL_DIR = ROOT / VERSION
ONNX = "en_GB-jenny_dioco-medium.onnx"
ESPEAK_VOICE = "en-gb-x-rp"  # the espeak-ng voice Jenny was trained on (espeak.voice in the model's .onnx.json)
# espeak-ng's English data, still served (the phone requires it before it counts as ready) though sherpa-onnx 2.0
# ignores it: the phone speaks the phonemes sent with each piece (phonemes()). Only these exact relative paths are
# ever served, never anything else
ESPEAK = ("espeak-ng-data/phontab", "espeak-ng-data/phonindex", "espeak-ng-data/phondata", "espeak-ng-data/intonations",
          "espeak-ng-data/en_dict", "espeak-ng-data/lang/gmw/en", "espeak-ng-data/lang/gmw/en-US",
          "espeak-ng-data/lang/gmw/en-GB-x-rp")
FILES = (ONNX, "tokens.txt", "MODEL_CARD") + ESPEAK  # all the phone loads (MODEL_CARD: the voice's source and terms)
SPEAKER = 0  # Jenny is the model's only speaker

CALL_TTS = STATE / "call.tts"
PHONE_SAY = STATE / "phone-say"
PHONE_RTF = 0.5  # until the phone reports its own: Jenny rendered at 0.14 on the phone's little cores, 2026-11-22
PLAN_RTF_MIN = 0.3  # never planned faster than this: a render also waits for the call's own threads
ID = re.compile(r"[A-Za-z0-9._-]{1,120}")


# --- the model files the phone downloads ---------------------------------------------------------------------

def manifest(root: Path = MODEL_DIR) -> dict | None:
    """{"version", "files": [{name, size, sha256}]}, or None while the files aren't there. Hashes are kept beside
    the files and worked out again only when a file's size or time changed (the model is 63 MB)."""
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
    """One of the files the phone may fetch, never anything else: an exact name from FILES (the espeak data's fixed
    relative paths included), nothing with "..", no other names."""
    if name not in FILES or ".." in name.split("/"):
        return None
    return root / name if (root / name).is_file() else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def prepare(root: Path = ROOT) -> Path:
    """Fetch sherpa-onnx's Jenny archive (once, checked against its pinned hash) and keep only FILES. An earlier
    model's folder beside it (Kokoro's, 325 MB) is left for the user to delete: python -m voice.phonevoice clean."""
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


def voice_id(name: str) -> int | None:
    """A voice's id on the phone: Jenny is its one speaker, whichever agent's voice the laptop would use."""
    return SPEAKER


def stale(root: Path = ROOT) -> list[Path]:
    """Folders of models the phone no longer uses (Kokoro's), left by an earlier prepare."""
    return [p for p in root.iterdir() if p.is_dir() and p.name != VERSION] if root.is_dir() else []


# --- the call: the phone's offer, and its fallback ------------------------------------------------------------

CHOSE_LAPTOP = "the user chose the laptop"  # a fallback the user picked mid-call, not one the phone needed
SAID_PHONE = "Speaking from your phone now."  # said in the new voice when the user switches mid-call
SAID_LAPTOP = "Back on the laptop."


def offer(message: dict) -> None:
    """The phone said what it can render ({"tts": {"engine", "model"}}; "chosen": true when the user switched to it
    mid-call), that it can't keep up any more ({"tts": "fallback", "reason"}), or that the user chose the laptop's
    voice mid-call ({"tts": "laptop"}): after either of the last two, this call streams the laptop's audio again."""
    tts = message.get("tts")
    if tts == "laptop":  # kept as a fallback with its own reason: the phone's measured speed stays for a switch back
        state = read_json(CALL_TTS) if CALL_TTS.exists() else {}
        if state:
            state["fallback"] = CHOSE_LAPTOP
            write_json(CALL_TTS, state)
        return
    if isinstance(tts, dict):
        if tts.get("model") == VERSION and tts.get("engine") == ENGINE:
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
            heard = message.get("heard")
            _progress(clip, fallback=state.get("fallback") or "the phone asked",
                      heard=heard if isinstance(heard, int) and not isinstance(heard, bool) and heard >= -1 else None)


def chosen(message: dict) -> bool | None:
    """The user switched the voice mid-call: True to the phone, False to the laptop; None for anything else (an
    offer at the call's start, a fallback the phone needed)."""
    tts = message.get("tts")
    if tts == "laptop":
        return False
    if isinstance(tts, dict) and message.get("chosen") is True:
        return True
    return None


def confirmation(message: dict) -> str | None:
    """The words that confirm a switch the user made, in the voice they chose: None when it wasn't a switch, or the
    switch to the phone didn't take (another model: the laptop keeps speaking, and says nothing of it)."""
    to_phone = chosen(message)
    if to_phone is None:
        return None
    if to_phone:
        return SAID_PHONE if phone_renders() else None
    return SAID_LAPTOP


def _rtf(value) -> float | None:
    """A render speed the phone reported (render s per audio s), or None when it isn't a sane number."""
    return float(value) if isinstance(value, (int, float)) and 0.05 <= value <= 10 else None


def plan_rtf() -> float:
    """The speed the laptop cuts the phone's pieces for: the phone's own measured speed with a margin
    (max(1.2 x measured, PLAN_RTF_MIN)), else PHONE_RTF."""
    state = read_json(CALL_TTS) if CALL_TTS.exists() else {}
    measured = _rtf(state.get("rtf"))
    return max(1.2 * measured, PLAN_RTF_MIN) if measured else PHONE_RTF


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

_backend = None
_symbols: set[str] | None = None
_backend_lock = threading.Lock()
# Punctuation espeak passes through that Jenny has no symbol for, as the pause a reader makes there
DASH = re.compile(r"\s*[\u2013\u2014]\s*")
ELLIPSIS = re.compile(r"\s*\u2026")


def symbols(root: Path = MODEL_DIR) -> set[str] | None:
    """The phonemes Jenny knows (its tokens.txt: a symbol and its id per line), or None before prepare."""
    try:
        lines = (root / "tokens.txt").read_text(encoding="utf-8").split("\n")
    except OSError:
        return None
    return {line.rsplit(" ", 1)[0] for line in lines if " " in line} or None


def phonemes(texts: list[str], dropped: set[str] | None = None) -> list[str] | None:
    """Each text as Jenny's phonemes: espeak-ng's IPA in the voice she was trained on (en-gb-x-rp), stress marks
    and punctuation kept, language-switch flags removed, as Piper makes them; sherpa-onnx adds the start, end and
    padding marks itself. None when espeak isn't available here. The phone speaks these, not the words: its engine
    (sherpa-onnx 2.0) has no text front end for this model. A symbol Jenny lacks is left out (the phone's engine
    would skip it anyway) and added to `dropped`, so the caller can log it."""
    global _backend, _symbols
    try:
        with _backend_lock:
            if _backend is None:
                from kokoro_onnx.tokenizer import Tokenizer
                Tokenizer()  # points phonemizer at the espeak-ng library and data the laptop's talkback uses
                from phonemizer.backend import EspeakBackend
                _backend = EspeakBackend(ESPEAK_VOICE, preserve_punctuation=True, with_stress=True,
                                         language_switch="remove-flags")
                _symbols = symbols()
            try:
                from kokoro_onnx.tokenizer import _espeak_lock as lock  # espeak's state is the process's
            except ImportError:
                lock = threading.Lock()
            out = []
            for text in texts:
                if not text.strip():
                    out.append("")
                    continue
                with lock:
                    ph = _backend.phonemize([text], strip=True)[0]
                ph = ELLIPSIS.sub(".", DASH.sub(", ", ph)).strip()
                if _symbols is not None:
                    lost = {c for c in ph if c not in _symbols}
                    if lost:
                        ph = "".join(c for c in ph if c in _symbols)
                        if dropped is not None:
                            dropped |= lost
                out.append(ph)
            return out
    except Exception:  # no espeak or no phonemizer: the phone can't speak these, the laptop says them
        return None


def send(clip: str, agent: str, voice: str, sid: int, pieces: list[str], joined: set[int],
         sentence_map: list[int], ph: list[str] | None = None) -> None:
    """A clip for the phone: its pieces as the laptop cut them, which join the one before, which sentence each is,
    and, when given (one per piece), each piece's phonemes ("ph"), which the phone speaks in place of the words (the
    words stay for its log); an older phone ignores them."""
    PHONE_SAY.mkdir(parents=True, exist_ok=True)
    for suffix in (".sent", ".progress", ".stop"):
        (PHONE_SAY / f"{clip}{suffix}").unlink(missing_ok=True)
    say = {"id": clip, "agent": agent, "voice": voice, "sid": sid, "pieces": pieces, "joined": sorted(joined),
           "sentence_map": sentence_map, "at": time.time()}
    if ph is not None and len(ph) == len(pieces):
        say["ph"] = ph
    write_json(PHONE_SAY / f"{clip}.json", say, private=True)


def progress(clip: str) -> dict:
    """{"spoken", "done", "fallback", "heard"} as the phone last said: pieces it has started, and on a fallback the
    last piece whose audio had all played (None when the phone didn't say)."""
    path = PHONE_SAY / f"{clip}.progress"
    found = read_json(path) if path.exists() else {}
    heard = found.get("heard")
    return {"spoken": int(found.get("spoken") or 0), "done": bool(found.get("done")),
            "fallback": str(found.get("fallback") or ""),
            "heard": heard if isinstance(heard, int) and not isinstance(heard, bool) and heard >= -1 else None}


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


def _progress(clip: str, spoken: int | None = None, done: bool = False, fallback: str = "",
              heard: int | None = None) -> None:
    if not (PHONE_SAY / f"{clip}.json").exists():
        return  # a clip already over (or another call's): nothing to record
    now = progress(clip)
    write_json(PHONE_SAY / f"{clip}.progress", {
        "spoken": max(now["spoken"], spoken or 0), "done": now["done"] or done, "fallback": fallback or now["fallback"],
        "heard": heard if heard is not None else now["heard"]})


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["prepare"]:
        print(prepare())
        print(json.dumps(manifest(), indent=1))
        for old in stale():
            print(f"no longer used: {old} (python -m voice.phonevoice clean)")
    elif sys.argv[1:2] == ["phonemes"] and len(sys.argv) > 2:  # what the phone is sent for a text, for its bench
        print(phonemes([" ".join(sys.argv[2:])])[0])
    elif sys.argv[1:] == ["clean"]:
        import shutil
        for old in stale():
            shutil.rmtree(old)
            print(f"deleted {old}")
        for archive in ROOT.glob("*.tar.bz2") if ROOT.is_dir() else []:
            if archive.name != f"{VERSION}.tar.bz2":
                archive.unlink()
                print(f"deleted {archive}")
    else:
        print("usage: python -m voice.phonevoice prepare|clean|phonemes TEXT", file=sys.stderr)
        sys.exit(2)
    os._exit(0)
