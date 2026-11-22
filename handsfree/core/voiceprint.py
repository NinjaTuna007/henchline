"""Is it the user's voice? A soft signal for hands-free, never a gate.

A speaker-embedding model (TitaNet-small, through sherpa-onnx, on the CPU)
turns 1.5 s of speech into a voiceprint; its cosine against the user's own,
enrolled from their recordings, is the score. Each input path has its own
centroid and threshold: the echo canceller changes how the user sounds
(scored against a raw-mic voiceprint the user's median fell from 0.41 to 0.27).

Hands-free only ever uses a score to lean: a voice that matches can stop a
clip whose words were too few to tell (talkover.py), never one whose words were
the clip's own; a voice that doesn't match makes a wake word need a stricter
check, never locks the user out. Without the model, the enrolment or the
setting, everything works as before. Scores are logged as numbers; no audio
or voiceprint is ever written outside VOICE_ID.

    handsfree voiceprint enrol    build the voiceprint from the recordings in VOICE_ID:
                                  laptop-*.wav, phone-*.wav, room*-raw-*.wav (raw mic and phone)
                                  and room*-ec-*.wav (the echo-cancelled mic), 16 kHz mono
    handsfree voiceprint status   what is enrolled, and whether the model loads
"""

from __future__ import annotations

import json
import os
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from voice.state import VOICE_ID, VOICEPRINT, VOICEPRINT_MODEL

RATE = 16000
WINDOW = 1.5  # seconds of speech per voiceprint: shorter ones scattered the user's scores
MIN_SECONDS = 0.6  # less speech than this: no score
WAKE_MIN_SECONDS = 0.4  # a wake word is short
FRAME = 480  # 30 ms
SELECT_DB = 15.0  # frames within this of the loudest one count as the voice; quieter ones are gaps and room
QUIET_DB = -60.0
LOCK_WAIT = 0.05  # seconds a check waits for the model (another check, or it is still loading): then no score
RETRY_LOAD = 300.0  # seconds before a model that failed to load is tried again
THREADS = 2  # a 1.5 s check: 16 ms median, 21 ms p95 on this laptop's CPU (2026-11-23); loading 0.2 s

RAW, EC = "raw", "ec"  # the plain mic (laptop or phone), and the laptop mic after the echo canceller
# Thresholds per path (2026-11-23; enrolment of half the room-distance take, scored on the other half; impostors: the 28
# English Kokoro voices synthesized, clean and through an 8 kHz phone line; real TV talk-show speech; our own af_heart
# through the speakers and canceller). Each must sit above every talkback voice the agents can speak in (voices.py:
# af_heart, af_bella, af_sky, bf_alice, bm_george), whose worst is bm_george, Codex's voice:
#   raw (laptop + phone + room-raw centroid): user p5 0.36, median 0.50, 88 % >= 0.40; bm_george max 0.36 (phone line),
#     bm_daniel 0.36, real TV max 0.26, af_heart through the room 0.26
#   ec (room-ec centroid alone; adding the raw takes pulled the user's median from 0.60 to 0.44 and raised bm_george
#     from 0.35 to 0.37): user p5 0.47, median 0.60, 96 % >= 0.42 (same-day take: expect less on other days, which
#     only means words alone); bm_george max 0.35 (0.38 in 0.75 s slices), TV and af_heart residuals max 0.32
THRESHOLDS = {RAW: 0.40, EC: 0.42}
# Below this a wake word needs a stricter check. In 0.75 s slices (a wake word's length) the user's p1 was 0.20 on both
# paths (p5 0.28 raw, 0.32 ec); real TV speech p95 0.21. About 1 in 100 of the user's wake words gets the second look
WAKE_LOW = {RAW: 0.20, EC: 0.20}
# Which recordings feed which centroid, by file name
ENROL = {RAW: ("laptop-", "phone-", "room*-raw-"), EC: ("room*-ec-",)}


@dataclass(frozen=True)
class Check:
    score: float
    path: str

    @property
    def user(self) -> bool:
        """Sounds like the user, past every talkback voice in use."""
        return self.score >= THRESHOLDS[self.path]

    @property
    def unlike(self) -> bool:
        """Low enough that a wake word should be checked harder."""
        return self.score < WAKE_LOW[self.path]

    def said(self) -> str:
        return f"voiceprint: {self.score:.2f} ({self.path})"


def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def voiced_part(audio: np.ndarray, seconds: float = WINDOW, min_seconds: float = MIN_SECONDS) -> np.ndarray | None:
    """The newest `seconds` of what sounds like the voice in audio (float, 16 kHz): frames within SELECT_DB of the
    loudest, so gaps between scraps don't dilute it. None with less than MIN_SECONDS."""
    audio = np.asarray(audio)
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768
    n = len(audio) // FRAME
    if n == 0:
        return None
    frames = audio[:n * FRAME].astype(np.float32).reshape(n, FRAME)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-9)
    keep = frames[(db >= db.max() - SELECT_DB) & (db > QUIET_DB)]
    if len(keep) * FRAME < min_seconds * RATE:
        return None
    return keep[-round(seconds * RATE / FRAME):].reshape(-1)


class Model:
    """TitaNet-small, loaded on first use (or by warm()), one check at a time."""

    def __init__(self, path: Path = VOICEPRINT_MODEL) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.extractor = None
        self.failed_at = 0.0
        self.error = ""

    def _load(self) -> bool:
        if self.extractor is not None:
            return True
        if self.failed_at and time.monotonic() - self.failed_at < RETRY_LOAD:
            return False
        try:
            import sherpa_onnx

            if not self.path.exists():
                raise FileNotFoundError(str(self.path))
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self.path), num_threads=THREADS)
            self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            return True
        except Exception as exc:  # fail open: no scores
            self.failed_at, self.error = time.monotonic(), f"{type(exc).__name__}: {exc}"
            return False

    def warm(self) -> bool:
        with self.lock:
            return self._load()

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        """A unit voiceprint of float 16 kHz audio; None when the model is busy, missing or failing."""
        if not self.lock.acquire(timeout=LOCK_WAIT):
            return None
        try:
            if not self._load():
                return None
            stream = self.extractor.create_stream()
            stream.accept_waveform(RATE, np.ascontiguousarray(audio, dtype=np.float32))
            stream.input_finished()
            return unit(np.array(self.extractor.compute(stream)))
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return None
        finally:
            self.lock.release()


class Voiceprint:
    """The user's centroids per path, and the scoring. check() never raises: None means "no opinion"."""

    def __init__(self, centroids: dict[str, np.ndarray], embed: Callable[[np.ndarray], np.ndarray | None]) -> None:
        self.centroids = {path: unit(c) for path, c in centroids.items()}
        self.embed = embed
        self.took = 0.0  # seconds the last check took, for measuring

    @classmethod
    def load(cls, path: Path = VOICEPRINT, model: Model | None = None) -> Voiceprint | None:
        """The enrolled voiceprint, or None without one (hands-free then works as before)."""
        try:
            data = json.loads(path.read_text())
            centroids = {k: np.array(v, dtype=np.float32) for k, v in data["centroids"].items() if k in THRESHOLDS}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        if not centroids:
            return None
        return cls(centroids, (model or Model()).embed)

    def check(self, audio: np.ndarray, path: str, seconds: float = WINDOW,
              min_seconds: float = MIN_SECONDS) -> Check | None:
        began = time.perf_counter()
        try:
            centroid = self.centroids.get(path)
            if centroid is None:
                return None
            part = voiced_part(audio, seconds, min_seconds)
            if part is None:
                return None
            e = self.embed(part)
            if e is None:
                return None
            score = float(np.dot(unit(e), centroid))
            return Check(score, path) if np.isfinite(score) else None
        except Exception:
            return None
        finally:
            self.took = time.perf_counter() - began

    def score(self, samples: np.ndarray, path: str) -> float | None:
        check = self.check(samples, path)
        return check.score if check else None


# --- enrolment -------------------------------------------------------------

def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: needs 16 kHz 16-bit")
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        return audio.reshape(-1, w.getnchannels()).mean(1)


def speech_only(audio: np.ndarray) -> np.ndarray:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    spans = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300, speech_pad_ms=100))
    return np.concatenate([audio[s["start"]:s["end"]] for s in spans]) if spans else audio[:0]


def windows(audio: np.ndarray, seconds: float = WINDOW, hop: float = WINDOW / 2) -> list[np.ndarray]:
    w, h = round(seconds * RATE), round(hop * RATE)
    return [audio[i:i + w] for i in range(0, len(audio) - w + 1, h)]


def recordings(folder: Path = VOICE_ID) -> dict[str, list[Path]]:
    """The recordings for each path. A cancelled take identical to its raw twin (the canceller wasn't running when it
    was recorded) is left out of both."""
    found = {path: sorted({f for pattern in patterns for f in folder.glob(pattern + "*.wav")})
             for path, patterns in ENROL.items()}
    for ec in list(found[EC]):
        twin = ec.with_name(ec.name.replace("-ec-", "-raw-"))
        if twin.exists() and twin.read_bytes() == ec.read_bytes():
            found[EC].remove(ec)
            found[RAW] = [f for f in found[RAW] if f != twin]
    return found


def enrol(folder: Path = VOICE_ID, out: Path = VOICEPRINT, model: Model | None = None,
          say: Callable[[str], None] = print) -> int:
    model = model or Model()
    if not model.warm():
        say(f"the voiceprint model didn't load ({model.error})")
        return 1
    centroids, counts = {}, {}
    for path, files in recordings(folder).items():
        prints = []
        for f in files:
            took = [e for e in (model.embed(w) for w in windows(speech_only(read_wav(f)))) if e is not None]
            say(f"{path}: {f.name}, {len(took)} windows")
            prints += took
        if prints:
            centroids[path] = unit(np.mean(prints, axis=0)).round(6).tolist()
            counts[path] = len(prints)
    if not centroids:
        say(f"no recordings in {folder}")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        json.dump({"model": model.path.name, "made": time.strftime("%Y-%m-%d %H:%M"), "windows": counts,
                   "centroids": centroids}, fh)
    os.chmod(tmp, 0o600)
    tmp.replace(out)
    say(f"voiceprint saved: {', '.join(f'{k} {n} windows' for k, n in counts.items())} -> {out}")
    return 0


def status(say: Callable[[str], None] = print) -> int:
    try:
        data = json.loads(VOICEPRINT.read_text())
        say(f"enrolled {data.get('made', '?')}: " + ", ".join(f"{k} {n} windows" for k, n in data["windows"].items()))
    except (OSError, ValueError, KeyError):
        say("not enrolled: hands-free works without it")
    model = Model()
    say("model loads" if model.warm() else f"model doesn't load ({model.error})")
    return 0


def main(args: list[str]) -> int:
    if args[:1] == ["enrol"]:
        return enrol()
    if args[:1] in (["status"], []):
        return status()
    print(__doc__)
    return 1
