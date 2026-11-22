"""Whisper, the chime, and the settings both push-to-talk and hands-free read
from ~/.config/ptt/config.json."""

from __future__ import annotations

import gc
import json
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from voice.state import in_call, log

CONFIG = Path.home() / ".config/ptt/config.json"
AC_ONLINE = Path("/sys/class/power_supply/AC/online")
# Names Whisper otherwise hears as "cloud" and the like. Keep it short: a long
# list (and hyphenated words) made Whisper hyphenate and drop ordinary words.
DEFAULT_HOTWORDS = ["Claude", "Cursor", "Codex", "Kokoro"]


@dataclass
class Config:
    model: str = "small"
    device: str = "auto"  # auto: GPU on AC, CPU on battery; or cpu / cuda
    language: str = "en"  # "auto" lets Whisper detect it
    tone: bool = True  # tone marks for agent windows
    # Hands-free turns heard while the user talks (voice/speech/nemotron.py): the words are ready a moment after
    # they stop, on the CPU; Whisper still confirms wake words and transcribes when the model isn't installed
    streaming: bool = True
    # With those words: a turn whose words so far end a sentence ends after a shorter pause (turn.py, EARLY_*)
    early_end: bool = True
    hotwords: list[str] = field(default_factory=lambda: list(DEFAULT_HOTWORDS))
    chime: bool = True
    # Hands-free leans on the user's voiceprint when enrolled (handsfree/core/voiceprint.py); false: words alone
    voiceprint: bool = True
    chime_sound: str = "/usr/share/sounds/freedesktop/stereo/complete.oga"
    keys: list[str] = field(default_factory=lambda: ["F23"])  # the laptop Copilot key
    chords: list[list[str]] = field(default_factory=lambda: [["RIGHTCTRL", "RIGHTSHIFT"]])

    @classmethod
    def load(cls, log_to: Path) -> Config:
        """The file's settings over the defaults; unknown keys are ignored."""
        try:
            data = json.loads(CONFIG.read_text())
        except FileNotFoundError:
            data = {}
        except ValueError as exc:
            log(log_to, f"bad config: {exc}")
            data = {}
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def save(self) -> None:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(asdict(self), indent=2) + "\n")

    def want_cuda(self) -> bool:
        if self.device in ("cpu", "cuda"):
            return self.device == "cuda"
        try:
            return AC_ONLINE.read_text().strip() != "0"
        except OSError:
            return True


# On the CPU an 8 s turn took 1.8 s with 4 threads, 1.4 s with 16, against 0.2 s on the GPU (2026-11-15)
CPU_THREADS = max(4, (os.cpu_count() or 8) // 2)
GPU_NEEDS_MB = 1500  # free GPU memory before Whisper moves back to it (the small model takes ~900 MB)
RETRY_GPU = 60.0  # seconds between looks at whether the GPU has room again


def load_model(cfg: Config, cuda: bool, log_to: Path):
    from faster_whisper import WhisperModel

    if cuda:
        try:
            model = WhisperModel(cfg.model, device="cuda", compute_type="float16")
            log(log_to, f"model {cfg.model} on cuda")
            return model
        except Exception as exc:
            log(log_to, f"cuda model failed ({exc}); using cpu")
    model = WhisperModel(cfg.model, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)
    log(log_to, f"model {cfg.model} on cpu ({CPU_THREADS} threads)")
    return model


def gpu_free_mb() -> int | None:
    """Free memory on the first NVIDIA GPU, or None if it cannot be asked."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        return int(out[0]) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def transcribe(model, path: Path, cfg: Config, log_to: Path) -> tuple[str, str]:
    """The plain transcript, and the one with tone marks for agent windows."""
    segments, _info = model.transcribe(
        str(path), language=None if cfg.language in ("", "auto") else cfg.language, vad_filter=True,
        beam_size=1, word_timestamps=cfg.tone, hotwords=", ".join(cfg.hotwords) or None,
    )
    segments = list(segments)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    rich = text
    if cfg.tone and text:
        try:
            from voice.speech import tone

            words = [(w.start, w.end, w.word) for seg in segments for w in seg.words or []]
            rich = tone.annotate(path, words, text)
        except Exception as exc:  # tone is extra; the words must still get through
            log(log_to, f"tone failed: {exc}")
    return text, rich


def ding(cfg: Config) -> None:
    # Not on a phone call: the app shows the same state, and the laptop may be chiming to an empty office
    if cfg.chime and os.path.exists(cfg.chime_sound) and not in_call():
        subprocess.Popen(["pw-play", cfg.chime_sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


class Transcriber:
    """Whisper on the GPU when it can be, the CPU when it must.

    Out of GPU memory (a training run), it moves to the CPU and carries on;
    settle(), called when idle, moves it back once the GPU has room again, and
    follows the power source (GPU on mains, CPU on battery). Before, one
    out-of-memory left hands-free on the CPU until it was restarted, and
    push-to-talk lost the words.
    """

    def __init__(self, cfg: Config, log_to: Path) -> None:
        self.cfg, self.log_to = cfg, log_to
        self.on_cpu = not cfg.want_cuda()
        self.model = load_model(cfg, not self.on_cpu, log_to)
        self.on_cpu = self.model.model.device == "cpu"
        self.looked = 0.0  # when settle() last looked at the GPU
        self.lock = threading.RLock()  # one transcription at a time: hands-free's turns and voice messages share it

    def settle(self) -> None:
        """Between uses: back to the GPU if wanted and it has room; to the CPU on battery."""
        with self.lock:
            self._settle()

    def _settle(self) -> None:
        if time.monotonic() - self.looked < RETRY_GPU:
            return
        self.looked = time.monotonic()
        wanted = self.cfg.want_cuda()
        if wanted == (not self.on_cpu):
            return
        if wanted and (gpu_free_mb() or 0) < GPU_NEEDS_MB:
            return  # still full: stay on the CPU
        log(self.log_to, "moving Whisper " + ("back to the GPU" if wanted else "to the CPU (battery)"))
        self.model = None
        gc.collect()
        self.model = load_model(self.cfg, wanted, self.log_to)
        self.on_cpu = self.model.model.device == "cpu"

    def run(self, use):
        """use(model); if the GPU is full, move Whisper to the CPU and try again."""
        with self.lock:
            return self._run(use)

    def _run(self, use):
        try:
            return use(self.model)
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower() or self.on_cpu:
                raise
            log(self.log_to, "GPU out of memory: Whisper moves to the CPU")
            self.model, self.on_cpu = load_model(self.cfg, False, self.log_to), True
            self.looked = time.monotonic()  # give the GPU a while before trying it again
            return use(self.model)

    def quick_text(self, audio) -> str:
        """A fast, plain transcript of a little audio (16 kHz floats): no tone, no voice detection."""
        return self.run(lambda model: " ".join(
            seg.text for seg in model.transcribe(audio, language="en", beam_size=1, vad_filter=False)[0]))

    def file(self, path: Path) -> tuple[str, str]:
        """(plain text, text with tone marks) of a recording."""
        return self.run(lambda model: transcribe(model, path, self.cfg, self.log_to))
