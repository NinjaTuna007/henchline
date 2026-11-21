"""The directory's private state: its identity, paired phones, pinned hosts and their last descriptors."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from henchline_directory.shared import canonical

SCHEMA_VERSION = 1
PAIRING_LIFETIME = 5 * 60
PAIRING_ATTEMPTS = 5
NONCE_LIFETIME = 10 * 60
HEARTBEAT_EVERY = 60
STALE_AFTER = 3 * HEARTBEAT_EVERY  # three missed heartbeats


@dataclass(frozen=True)
class Signer:
    """A key that may sign requests here: a paired phone, or an enrolled host."""
    kind: str  # phone | host
    signer_id: str  # device_id or host_id
    algorithm: str
    public_key: bytes
    revoked: bool


class DirectoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        os.chmod(self.path, 0o600)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        except Exception:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(
                    schema_version INTEGER NOT NULL,
                    directory_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS phones(
                    device_id TEXT PRIMARY KEY,
                    algorithm TEXT NOT NULL CHECK(algorithm IN ('ed25519', 'p256')),
                    public_key BLOB NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    enrolled_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS pairing_nonces(
                    digest BLOB PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('phone', 'host')),
                    expires_at REAL NOT NULL,
                    attempts_left INTEGER NOT NULL,
                    consumed_at REAL
                );
                CREATE TABLE IF NOT EXISTS hosts(
                    host_id TEXT PRIMARY KEY,
                    public_key BLOB NOT NULL,
                    enrolled_at REAL NOT NULL,
                    descriptor BLOB NOT NULL,
                    descriptor_seq INTEGER NOT NULL,
                    observed_at REAL NOT NULL,
                    withdrawn_at REAL,
                    revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS replay_nonces(
                    signer TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    seen_at REAL NOT NULL,
                    PRIMARY KEY(signer, nonce)
                );
            """)
            columns = {r["name"] for r in self._db.execute("PRAGMA table_info(hosts)")}
            if "revoked_at" not in columns:  # a database from the first build of this schema
                try:
                    self._db.execute("ALTER TABLE hosts ADD COLUMN revoked_at REAL")
                except sqlite3.OperationalError as exc:  # another process opening it added it first
                    if "duplicate column" not in str(exc):
                        raise
            row = self._db.execute("SELECT * FROM meta").fetchone()
            if row is None:
                self._db.execute("INSERT INTO meta VALUES (?,?,?)", (SCHEMA_VERSION, str(uuid.uuid4()), time.time()))
            elif int(row["schema_version"]) != SCHEMA_VERSION:
                raise RuntimeError(f"unsupported directory database schema {row['schema_version']}")

    @property
    def directory_id(self) -> str:
        with self._lock:
            return self._db.execute("SELECT directory_id FROM meta").fetchone()[0]

    # ---- one-time codes: a phone pairing, or a host enrolment --------------------------------------------

    @staticmethod
    def _digest(kind: str, nonce: str) -> bytes:
        return hashlib.sha256(f"henchline-directory-{kind}-v1\0{nonce}".encode()).digest()

    def new_code(self, kind: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        nonce = canonical.b64url(secrets.token_bytes(24))
        with self._lock, self._db:
            self._db.execute("DELETE FROM pairing_nonces WHERE expires_at < ? OR consumed_at IS NOT NULL", (now,))
            self._db.execute("INSERT INTO pairing_nonces(digest,kind,expires_at,attempts_left) VALUES (?,?,?,?)",
                             (self._digest(kind, nonce), kind, now + PAIRING_LIFETIME, PAIRING_ATTEMPTS))
        return nonce

    def code_failure(self, kind: str, nonce: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE pairing_nonces SET attempts_left=MAX(0, attempts_left-1) "
                             "WHERE digest=? AND consumed_at IS NULL", (self._digest(kind, nonce),))

    def _consume(self, kind: str, nonce: str, now: float) -> None:
        """Inside a write transaction: the code is live, or LookupError / TimeoutError / PermissionError."""
        digest = self._digest(kind, nonce)
        row = self._db.execute("SELECT * FROM pairing_nonces WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise LookupError("code_invalid")
        if row["consumed_at"] is not None or row["expires_at"] < now:
            raise TimeoutError("code_expired")
        if row["attempts_left"] <= 0:
            raise PermissionError("code_locked")
        self._db.execute("UPDATE pairing_nonces SET consumed_at=? WHERE digest=?", (now, digest))

    def claim_phone(self, nonce: str, algorithm: str, public_key: bytes, label: str = "",
                    now: float | None = None) -> str:
        now = time.time() if now is None else now
        device_id = str(uuid.uuid4())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._consume("phone", nonce, now)
                self._db.execute("INSERT INTO phones(device_id,algorithm,public_key,label,enrolled_at) "
                                 "VALUES (?,?,?,?,?)", (device_id, algorithm, public_key, label[:80], now))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return device_id

    def enroll_host(self, nonce: str, host_id: str, public_key: bytes, descriptor: bytes, seq: int,
                    now: float | None = None) -> None:
        """Pin (host_id, key) once. A host already pinned is refused (the owner's `hosts forget` first). The
        code is checked first: without a live code nobody learns which host IDs are enrolled."""
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._consume("host", nonce, now)  # rolled back below if the host turns out to be pinned
                if self._db.execute("SELECT 1 FROM hosts WHERE host_id=?", (host_id,)).fetchone():
                    raise FileExistsError("host_already_enrolled")
                self._db.execute("INSERT INTO hosts(host_id,public_key,enrolled_at,descriptor,descriptor_seq,"
                                 "observed_at) VALUES (?,?,?,?,?,?)",
                                 (host_id, public_key, now, descriptor, seq, now))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    # ---- signers and replay -------------------------------------------------------------------------------

    def signer(self, kind: str, signer_id: str) -> Signer | None:
        with self._lock:
            if kind == "phone":
                row = self._db.execute("SELECT * FROM phones WHERE device_id=?", (signer_id,)).fetchone()
                return row and Signer("phone", signer_id, row["algorithm"], bytes(row["public_key"]),
                                      row["revoked_at"] is not None)
            row = self._db.execute("SELECT * FROM hosts WHERE host_id=?", (signer_id,)).fetchone()
            return row and Signer("host", signer_id, "ed25519", bytes(row["public_key"]),
                                  row["revoked_at"] is not None)

    def use_nonce(self, signer: str, nonce: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock, self._db:
            self._db.execute("DELETE FROM replay_nonces WHERE seen_at < ?", (now - NONCE_LIFETIME,))
            try:
                self._db.execute("INSERT INTO replay_nonces VALUES (?,?,?)", (signer, nonce, now))
                return True
            except sqlite3.IntegrityError:
                return False

    def revoke_phone(self, device_id: str, now: float | None = None) -> bool:
        with self._lock, self._db:
            return bool(self._db.execute("UPDATE phones SET revoked_at=COALESCE(revoked_at, ?) WHERE device_id=?",
                                         (time.time() if now is None else now, device_id)).rowcount)

    def phones(self) -> list[dict]:
        with self._lock:
            return [dict(row) | {"public_key": canonical.b64url(bytes(row["public_key"]))}
                    for row in self._db.execute("SELECT * FROM phones ORDER BY enrolled_at")]

    # ---- host descriptors --------------------------------------------------------------------------------

    def heartbeat(self, host_id: str, descriptor: bytes, seq: int, now: float | None = None) -> str:
        """"stored" (a higher sequence), "seen" (the same descriptor again), or ValueError: a lower sequence,
        or another descriptor under the same sequence."""
        now = time.time() if now is None else now
        with self._lock, self._db:
            row = self._db.execute("SELECT descriptor_seq, descriptor FROM hosts WHERE host_id=?",
                                   (host_id,)).fetchone()
            if row is None:
                raise LookupError("host_not_enrolled")
            if seq < row["descriptor_seq"] or (seq == row["descriptor_seq"] and bytes(row["descriptor"]) != descriptor):
                raise ValueError("descriptor_stale")
            self._db.execute("UPDATE hosts SET descriptor=?, descriptor_seq=?, observed_at=?, withdrawn_at=NULL "
                             "WHERE host_id=?", (descriptor, seq, now, host_id))
            return "stored" if seq > row["descriptor_seq"] else "seen"

    def revoke_host(self, host_id: str, now: float | None = None) -> bool:
        """The owner's revocation: the host's key stops working here and it is not listed. Unlike a host's own
        withdrawal, no heartbeat undoes it; only `hosts forget` and a fresh enrolment do."""
        with self._lock, self._db:
            return bool(self._db.execute("UPDATE hosts SET revoked_at=COALESCE(revoked_at, ?) WHERE host_id=?",
                                         (time.time() if now is None else now, host_id)).rowcount)

    def forget_host(self, host_id: str) -> bool:
        """The owner's removal of a pin, so the host can enrol again (with a new key, say)."""
        with self._lock, self._db:
            return bool(self._db.execute("DELETE FROM hosts WHERE host_id=?", (host_id,)).rowcount)

    def host_rows(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT host_id, enrolled_at, descriptor_seq, observed_at, withdrawn_at, revoked_at FROM hosts "
                "ORDER BY enrolled_at")]

    def withdraw(self, host_id: str, now: float | None = None) -> bool:
        with self._lock, self._db:
            return bool(self._db.execute("UPDATE hosts SET withdrawn_at=? WHERE host_id=?",
                                         (time.time() if now is None else now, host_id)).rowcount)

    def hosts(self, offset: int, limit: int, now: float | None = None) -> tuple[list[dict], bool]:
        now = time.time() if now is None else now
        with self._lock:
            rows = list(self._db.execute(
                "SELECT * FROM hosts WHERE withdrawn_at IS NULL AND revoked_at IS NULL "
                "ORDER BY enrolled_at, host_id LIMIT ? OFFSET ?",
                (limit + 1, offset)))
        listed = [{"host_id": row["host_id"], "descriptor": json.loads(row["descriptor"]),
                   "observed_at": utc(row["observed_at"]), "stale": now - row["observed_at"] > STALE_AFTER}
                  for row in rows[:limit]]
        return listed, len(rows) > limit


def utc(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
