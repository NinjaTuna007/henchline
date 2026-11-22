"""Files between the user's phone (Henchline) and the agents, both ways.

From an agent: `handsfree send FILE … [WORDS]` puts a copy in OUTBOX/<secret>/, with meta.json saying who sent
it and when. The call server serves it at /files/<secret>/<name> (only your own devices reach the server, and
only who has the secret finds the file), shows it in the call's chat while a call is up, and otherwise
Henchline is pushed a notification for it. Copies go after KEEP_DAYS.

From the phone: Henchline's share target uploads each file to the call server, which saves it under
INBOX/<date>/ and then tells the agent the user is talking to where the files are.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import shutil
import time
import urllib.parse
from pathlib import Path

from voice.state import INBOX, OUTBOX, read_json

KEEP_DAYS = 7  # an agent's file stays downloadable this long
SECRET = re.compile(r"[A-Za-z0-9_-]{16,64}")  # an outbox folder's name: token_urlsafe


def safe_name(name: str) -> str:
    """A file name as given, minus anything that could leave its folder or upset a terminal."""
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", Path(name.strip()).name).lstrip(".")
    return name[:180] or "file"


def unused(folder: Path, name: str) -> Path:
    """`name` in `folder`, or "name (2).ext" and so on when that is taken: nothing is overwritten."""
    path, stem, suffix = folder / name, Path(name).stem, Path(name).suffix
    n = 2
    while path.exists():
        path, n = folder / f"{stem} ({n}){suffix}", n + 1
    return path


def kind(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def size_text(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


# --- from an agent to the phone ---------------------------------------------------------------------------

def post(path: Path, words: str, agent: str, session: str) -> dict:
    """A copy of the file in the outbox (a hard link when it can be: same disk, no space), and its meta."""
    clean()
    secret = secrets.token_urlsafe(18)
    folder = OUTBOX / secret
    folder.mkdir(parents=True, mode=0o700)
    copy = folder / safe_name(path.name)
    try:
        os.link(path, copy)
    except OSError:  # another disk, or a file system without links
        shutil.copyfile(path, copy)
    meta = {"secret": secret, "name": copy.name, "type": kind(copy), "size": copy.stat().st_size,
            "text": words, "agent": agent, "session": session, "at": time.time(), "delivered": False}
    (folder / "meta.json").write_text(json.dumps(meta))
    return meta


def link(meta: dict) -> str:
    """Where the call server serves it, relative to the call page's address."""
    return f"/files/{meta['secret']}/{meta['name']}"


def undelivered() -> list[dict]:
    """The files agents sent that the phone hasn't been shown yet (no call was up, and the push didn't reach
    it), oldest first: the next call's chat shows them."""
    metas = [read_json(meta) for meta in OUTBOX.glob("*/meta.json")]
    return sorted((m for m in metas if m and not m.get("delivered")), key=lambda m: m["at"])


def delivered(meta: dict) -> None:
    meta["delivered"] = True
    (OUTBOX / meta["secret"] / "meta.json").write_text(json.dumps(meta))


# Where credentials live: an agent steered by something it read must not be able to put these on the phone
# Henchline's own: the host's signing key and pairing database (~/.local/share/henchline/keys, host.sqlite3), and
# ~/.config/henchline (host.json, and signing.properties with the release keystore's passwords)
PRIVATE = (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store", ".config/notify-user", ".config/ptt",
           ".local/share/keyrings", ".mozilla", ".config/google-chrome", ".config/chromium", ".netrc", ".pgpass",
           ".config/henchline", ".local/share/henchline/keys", ".local/share/henchline/host.sqlite3")
# Keys and keystores by their names: the host's and the directory's keys (*.ed25519, wherever their configs put
# them), an Android release keystore (*.jks, *.keystore)
KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".ed25519", ".jks", ".keystore")


def configured_secrets() -> list[Path]:
    """The host key and database where ~/.config/henchline/host.json moved them, if it did."""
    try:
        raw = read_json(Path.home() / ".config/henchline/host.json")
    except OSError:
        return []
    if not isinstance(raw, dict):
        return []
    return [Path(raw[k]).expanduser().resolve() for k in ("key_file", "database") if isinstance(raw.get(k), str) and raw[k]]


def private(path: Path) -> bool:
    """A file under one of PRIVATE, a key or certificate, or one only its owner may read in a folder of secrets."""
    path, home = path.resolve(), Path.home().resolve()
    if any(path == home / p or home / p in path.parents for p in PRIVATE):
        return True
    if path in configured_secrets():
        return True
    return path.suffix in KEY_SUFFIXES or path.name.startswith("id_")


def find(secret: str, name: str) -> Path | None:
    """The file behind /files/<secret>/<name>, or None: a wrong secret finds nothing, and nothing outside."""
    if not SECRET.fullmatch(secret) or safe_name(name) != name:
        return None
    path = OUTBOX / secret / name
    return path if name != "meta.json" and path.is_file() else None


# Shown in the browser only these: anything else (HTML, SVG, XML, scripts…) would run on the call page's own
# address, where it could open the call's connection and type to the agents. They download instead
INLINE = re.compile(r"(image/(png|jpeg|gif|webp|heic|avif|bmp)|audio/[\w.+-]+|video/[\w.+-]+)")


def headers(path: Path) -> dict[str, str]:
    """How a sent file is served: pictures, audio and video shown, sandboxed; everything else a download."""
    quoted = urllib.parse.quote(path.name)
    safe = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"}
    if INLINE.fullmatch(kind(path)):
        return {**safe, "Content-Type": kind(path), "Content-Disposition": f"inline; filename*=UTF-8''{quoted}"}
    return {**safe, "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"}


def clean() -> None:
    """Agents' files older than KEEP_DAYS go."""
    for folder in OUTBOX.glob("*"):
        try:
            if time.time() - folder.stat().st_mtime > KEEP_DAYS * 86400:
                shutil.rmtree(folder)
        except OSError:
            pass


# --- from the phone to the agents -------------------------------------------------------------------------

def inbox_today() -> Path:
    folder = INBOX / time.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def received(name: str) -> Path | None:
    """A file the phone uploaded, by the name the server gave it (today's folder, or yesterday's after
    midnight); None for anything else, so the phone can only point an agent at its own uploads."""
    if safe_name(name) != name:
        return None
    for day in (time.time(), time.time() - 86400):
        path = INBOX / time.strftime("%Y-%m-%d", time.localtime(day)) / name
        if path.is_file():
            return path
    return None


def message(paths: list[Path], text: str, note: str) -> str:
    """What the agent is told: where each file is, its type and size, and anything the user wrote."""
    parts = [f"{p} ({kind(p)}, {size_text(p.stat().st_size)})" for p in paths]
    said = " ".join(" ".join(w.split()) for w in (note, text) if w.strip())  # one line: it is typed into a prompt
    if not parts:
        return f"The user shared this from their phone: {said}"
    files = parts[0] if len(parts) == 1 else "; ".join(parts)
    noun = "a file" if len(parts) == 1 else f"{len(parts)} files"
    return f"The user shared {noun} from their phone: {files}." + (f" They wrote: {said}" if said else "")
