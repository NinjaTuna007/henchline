"""A consistent private copy of a live SQLite database: `voice-call backup` and `henchline-directory backup`.

Standard library only: the directory's release carries this file as it is.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
def backup(source: Path, output: Path) -> dict:
    """A consistent copy of a live SQLite database (its WAL included), private, written then linked into place.

    Never overwrites: an existing output is an error, so a backup cannot clobber an earlier one. The output's
    folder must not be writable by group or others (someone could swap the copy). Returns what a manifest
    records: the path, its SHA-256 (of the bytes written, before they are linked) and size.
    """
    source, output = Path(source).resolve(), Path(output)
    if not source.is_file():
        raise FileNotFoundError(f"no database at {source}")
    if output.exists():
        raise FileExistsError(f"{output} already exists")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.parent.stat().st_mode & 0o022:
        raise PermissionError(f"{output.parent} is writable by group or others")
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    os.close(os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        live = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
        copy = sqlite3.connect(partial)
        try:
            live.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal beside it
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("the copy failed its integrity check")
        finally:
            copy.close()
            live.close()
        digest = hashlib.sha256()
        with open(partial, "rb") as written:
            for chunk in iter(lambda: written.read(1 << 20), b""):
                digest.update(chunk)
            os.fsync(written.fileno())
        size = partial.stat().st_size
        os.link(partial, output)  # fails rather than replaces, should the output appear meanwhile
    finally:
        partial.unlink(missing_ok=True)
    folder = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(folder)  # the new name (and the partial's removal) on disk too
    finally:
        os.close(folder)
    return {"path": str(output), "sha256": digest.hexdigest(), "bytes": size}
