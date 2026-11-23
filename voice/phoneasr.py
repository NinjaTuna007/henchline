"""Speech-to-text on the phone: the model files the phone downloads to hear a call itself when the line is bad.

The laptop stays the call's main listener. When the link degrades the phone listens with the same streaming model
hands-free uses here (Nemotron int8, sherpa-onnx's export) and sends text instead of audio. Its files are the
laptop's own copies (`install.sh handsfree` fetched them for hands-free), so the phone needs no internet and gets
the bytes the laptop already runs with:
  encoder/decoder/joiner (int8), tokens.txt   the transducer
  bpe.vocab                                   word pieces, so the hint words can be boosted (as nemotron.py does)
  silero_vad.onnx                             opens and closes the phone's own turns
  MODEL_CARD                                  where the files come from and their terms

`python -m voice.phoneasr prepare` links (or copies) them into MODEL_DIR, checked against pinned SHA-256 hashes, so
a different file under the same name is never offered under this version. The hint words travel in the manifest,
read fresh from ~/.config/ptt/config.json each time it is asked for.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from voice.state import HANDSFREE_DATA, SHARE, read_json, write_json

# The model's name is the format's version: a phone holding another version downloads this one into its own folder
VERSION = "nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
ENGINE = "sherpa-transducer"
ROOT = SHARE / "henchline/phone-asr"
MODEL_DIR = ROOT / VERSION
# Small files first: a cut download has the quick ones whole, and resumes inside the large one
FILES = ("tokens.txt", "bpe.vocab", "MODEL_CARD", "silero_vad.onnx", "joiner.int8.onnx", "decoder.int8.onnx",
         "encoder.int8.onnx")
# The bytes hands-free runs with on the laptop (measured 2026-11-23; the copy pushed to the phone for its bench is
# the same). MODEL_CARD is written here, so it has no pinned hash
PINNED = {
    "tokens.txt": "dc0b4584ab2e4ddbf888425c076c61b736e7356a015250db7d307e6f1a8188ff",
    "bpe.vocab": "d21a867c6cbf85d06e84b4179745c51e0b1e046599bcb7e2a60f1d6a4660a649",
    "silero_vad.onnx": "a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28",
    "joiner.int8.onnx": "a35eac38a22ebceb04d230ed7afe0d68f446ba6914a036b97f14fece95967e23",
    "decoder.int8.onnx": "0be9702c2f427a2b6bb241d298e0d3836a558de1f5b9fd3018f1cce6e2b3fa98",
    "encoder.int8.onnx": "7d932213491ad355c6e5576705dc3494731a52af87d7a1b954559340147909d8",
}
CARD = f"""# {VERSION}

Speech-to-text for Henchline calls, run on the phone when the line to the computer is bad.

* Transducer (encoder, decoder, joiner, tokens.txt): NVIDIA Nemotron Speech Streaming English 0.6B, 560 ms chunks,
  int8 export by sherpa-onnx: https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
  Original model: https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b
  Licence: NVIDIA Open Model License Agreement (as the original model's card states),
  https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/
* bpe.vocab: built by Henchline from the original model's tokenizer.json (word pieces for hint words).
* silero_vad.onnx: Silero VAD, https://github.com/snakers4/silero-vad (MIT licence).
"""
MAX_HINTS = 100  # hint words sent at most; a longer list only slows decoding


def sources(data: Path = HANDSFREE_DATA) -> dict[str, list[Path]]:
    """Where the laptop already keeps each file, in order of preference."""
    found = {name: [data / "nemotron" / name] for name in PINNED if name != "silero_vad.onnx"}
    vad = [data / "silero_vad.onnx"]
    try:
        import openwakeword  # ships the same Silero file
        vad.append(Path(openwakeword.__file__).parent / "resources/models/silero_vad.onnx")
    except ImportError:
        pass
    found["silero_vad.onnx"] = vad
    return found


def prepare(root: Path = ROOT, data: Path = HANDSFREE_DATA) -> Path:
    """MODEL_DIR filled from the laptop's own copies: hard links where the disk allows (no second 650 MB), else
    copies, each checked against PINNED before it is put in place. Raises with what is missing."""
    target = root / VERSION
    target.mkdir(parents=True, exist_ok=True)
    for name, wanted in PINNED.items():
        path = target / name
        if path.is_file() and _sha256(path) == wanted:
            continue
        source = next((p for p in sources(data)[name] if p.is_file() and _sha256(p) == wanted), None)
        if source is None:
            raise RuntimeError(f"no {name} matching the pinned hash: run ./install.sh handsfree first")
        partial = target / f"{name}.part"
        partial.unlink(missing_ok=True)
        try:
            os.link(source, partial)
        except OSError:
            shutil.copyfile(source, partial)
        partial.replace(path)
    card = target / "MODEL_CARD"
    if not card.is_file() or card.read_text() != CARD:
        card.write_text(CARD)
    return target


def manifest(root: Path | None = None, hints: list[str] | None = None) -> dict | None:
    """{"version", "engine", "size", "files": [{name, size, sha256}], "hints": [words]}, or None while the files
    aren't all there or one isn't the pinned file (prepare puts it right). Hashes are kept beside the files and
    worked out again only when a file's size or time changed (the encoder is 650 MB)."""
    root = root or MODEL_DIR
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
    if any(PINNED.get(f["name"], f["sha256"]) != f["sha256"] for f in files):
        return None  # never offered under this version: another file in its place
    return {"version": VERSION, "engine": ENGINE, "size": sum(f["size"] for f in files), "files": files,
            "hints": hint_words() if hints is None else clean_hints(hints)}


def model_file(name: str, root: Path | None = None) -> Path | None:
    """One of the files the phone may fetch, never anything else: an exact name from FILES."""
    root = root or MODEL_DIR
    if name not in FILES:
        return None
    return root / name if (root / name).is_file() else None


def clean_hints(words) -> list[str]:
    """Hint words as the phone may take them: strings, one line each, no blanks or repeats, at most MAX_HINTS."""
    out: list[str] = []
    for word in words if isinstance(words, list) else []:
        if isinstance(word, str):
            word = " ".join(word.split())[:60]
            if word and word not in out:
                out.append(word)
    return out[:MAX_HINTS]


def hint_words() -> list[str]:
    """The hint words hands-free decodes with here (~/.config/ptt/config.json, else its defaults)."""
    from voice.speech import whisper
    try:
        data = json.loads(whisper.CONFIG.read_text())
    except (OSError, ValueError):
        data = {}
    words = data.get("hotwords") if isinstance(data, dict) else None
    return clean_hints(words if isinstance(words, list) else list(whisper.DEFAULT_HOTWORDS))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["prepare"]:
        print(prepare())
        print(json.dumps(manifest(), indent=1))
    else:
        print("usage: python -m voice.phoneasr prepare", file=sys.stderr)
        sys.exit(2)
