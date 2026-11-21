"""Private durable state for authenticated Henchline host APIs."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from call.canonical import b64url, crockford
from call.sqlite_backup import backup  # noqa: F401  (voice-call backup)


SCHEMA_VERSION = 2  # 2: host_events.source, so an ingested record is kept once
PAIRING_LIFETIME = 5 * 60
NONCE_LIFETIME = 10 * 60
PAIRING_ATTEMPTS = 5


@dataclass(frozen=True)
class Device:
    device_id: str
    algorithm: str
    public_key: bytes
    revoked_at: float | None


@dataclass(frozen=True)
class Reservation:
    state: str  # new, replay, pending, mismatch
    status: int = 0
    content_type: str = "application/json"
    body: bytes = b""


class HostStore:
    """SQLite operations whose transaction boundaries are part of the protocol."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Private from the first byte: SQLite gives its -wal and -shm files the database's own mode.
        os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        os.chmod(self.path, 0o600)  # an older file made under a looser umask
        try:
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
                CREATE TABLE IF NOT EXISTS schema_meta(
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices(
                    device_id TEXT PRIMARY KEY,
                    algorithm TEXT NOT NULL CHECK(algorithm IN ('ed25519', 'p256')),
                    public_key BLOB NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    enrolled_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS pairing_nonces(
                    digest BLOB PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    attempts_left INTEGER NOT NULL,
                    consumed_at REAL
                );
                CREATE TABLE IF NOT EXISTS replay_nonces(
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    nonce TEXT NOT NULL,
                    seen_at REAL NOT NULL,
                    PRIMARY KEY(device_id, nonce)
                );
                CREATE TABLE IF NOT EXISTS idempotency(
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'complete')),
                    status INTEGER,
                    content_type TEXT,
                    response BLOB,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    acknowledged_at REAL,
                    PRIMARY KEY(device_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS host_events(
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body BLOB NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS host_events_session_time
                    ON host_events(session_id, created_at, event_id);
                CREATE TABLE IF NOT EXISTS published_descriptor(
                    one INTEGER PRIMARY KEY CHECK(one = 1),
                    seq INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            row = self._db.execute("SELECT version FROM schema_meta").fetchone()
            version = SCHEMA_VERSION if row is None else int(row[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"unsupported host database schema {version}")
            # 1 -> 2: where an event came from ("text:<id>"), unique, so ingesting again never duplicates it
            columns = {r[1] for r in self._db.execute("PRAGMA table_info(host_events)")}
            if "source" not in columns:
                self._db.execute("ALTER TABLE host_events ADD COLUMN source TEXT")
            self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS host_events_source ON host_events(source) "
                             "WHERE source IS NOT NULL")
            if row is None:
                self._db.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif version != SCHEMA_VERSION:
                self._db.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))

    @staticmethod
    def _pair_digest(nonce: str) -> bytes:
        return hashlib.sha256(("henchline-pair-v1\0" + nonce).encode()).digest()

    def new_pairing(self, now: float | None = None) -> str:
        """A one-time code short enough to type: 10 Crockford base32 characters (50 bits). Guessing is bounded by
        the unauthenticated rate limit and the five-minute life: about 300 tries at 2^50 each."""
        now = time.time() if now is None else now
        nonce = crockford(secrets.token_bytes(7), 10)
        with self._lock, self._db:
            self._db.execute("DELETE FROM pairing_nonces WHERE expires_at < ? OR consumed_at IS NOT NULL", (now,))
            self._db.execute(
                "INSERT INTO pairing_nonces(digest, expires_at, attempts_left) VALUES (?,?,?)",
                (self._pair_digest(nonce), now + PAIRING_LIFETIME, PAIRING_ATTEMPTS),
            )
        return nonce

    def claim_pairing(self, nonce: str, algorithm: str, public_key: bytes, label: str = "",
                      now: float | None = None) -> Device:
        now = time.time() if now is None else now
        if algorithm not in {"ed25519", "p256"}:
            raise ValueError("unsupported device key algorithm")
        if not public_key or len(public_key) > 2048:
            raise ValueError("invalid public key")
        digest = self._pair_digest(nonce)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM pairing_nonces WHERE digest=?", (digest,)).fetchone()
                if row is None:
                    raise LookupError("pairing_invalid")
                if row["consumed_at"] is not None or row["expires_at"] < now:
                    raise TimeoutError("pairing_expired")
                if row["attempts_left"] <= 0:
                    raise PermissionError("pairing_locked")
                # The nonce proves presence at the host. Invalid key material consumes an
                # attempt, while a valid claim consumes it permanently.
                device_id = str(uuid.uuid4())
                self._db.execute(
                    "INSERT INTO devices(device_id,algorithm,public_key,label,enrolled_at) VALUES (?,?,?,?,?)",
                    (device_id, algorithm, public_key, label[:80], now),
                )
                self._db.execute("UPDATE pairing_nonces SET consumed_at=? WHERE digest=?", (now, digest))
                self._db.commit()
            except Exception:
                self._db.rollback()
                # A wrong nonce cannot be matched to a row without storing it in clear;
                # a matched but malformed claim is charged by the API before this call.
                raise
        return Device(device_id, algorithm, public_key, None)

    def pairing_failure(self, nonce: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE pairing_nonces SET attempts_left=MAX(0, attempts_left-1) "
                "WHERE digest=? AND consumed_at IS NULL",
                (self._pair_digest(nonce),),
            )

    def devices(self) -> list[dict]:
        with self._lock:
            return [dict(row) | {"public_key": b64url(bytes(row["public_key"]))} for row in self._db.execute(
                "SELECT device_id,algorithm,public_key,label,enrolled_at,revoked_at FROM devices ORDER BY enrolled_at")]

    def device(self, device_id: str) -> Device | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        return (Device(row["device_id"], row["algorithm"], bytes(row["public_key"]), row["revoked_at"])
                if row is not None else None)

    def revoke(self, device_id: str, now: float | None = None) -> bool:
        with self._lock, self._db:
            changed = self._db.execute(
                "UPDATE devices SET revoked_at=COALESCE(revoked_at, ?) WHERE device_id=?",
                (time.time() if now is None else now, device_id),
            ).rowcount
        return bool(changed)

    def use_nonce(self, device_id: str, nonce: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock, self._db:
            self._db.execute("DELETE FROM replay_nonces WHERE seen_at < ?", (now - NONCE_LIFETIME,))
            try:
                self._db.execute(
                    "INSERT INTO replay_nonces(device_id,nonce,seen_at) VALUES (?,?,?)",
                    (device_id, nonce, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def reserve(self, device_id: str, request_id: str, fingerprint: str,
                now: float | None = None) -> Reservation:
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM idempotency WHERE device_id=? AND request_id=?",
                    (device_id, request_id),
                ).fetchone()
                if row is not None:
                    self._db.commit()
                    if row["fingerprint"] != fingerprint:
                        return Reservation("mismatch")
                    if row["state"] == "pending":
                        return Reservation("pending")
                    return Reservation("replay", int(row["status"]), row["content_type"], bytes(row["response"]))
                self._db.execute(
                    "INSERT INTO idempotency(device_id,request_id,fingerprint,state,created_at) "
                    "VALUES (?,?,?,'pending',?)",
                    (device_id, request_id, fingerprint, now),
                )
                self._db.commit()
                return Reservation("new")
            except Exception:
                self._db.rollback()
                raise

    def complete(self, device_id: str, request_id: str, status: int, content_type: str, body: bytes,
                 now: float | None = None) -> None:
        with self._lock, self._db:
            changed = self._db.execute(
                "UPDATE idempotency SET state='complete',status=?,content_type=?,response=?,completed_at=? "
                "WHERE device_id=? AND request_id=? AND state='pending'",
                (status, content_type, body, time.time() if now is None else now, device_id, request_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("idempotency reservation was lost")

    def abandon(self, device_id: str, request_id: str) -> None:
        """Remove only an unperformed operation; completed actions are never forgotten."""
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM idempotency WHERE device_id=? AND request_id=? AND state='pending'",
                (device_id, request_id),
            )

    def acknowledge(self, device_id: str, request_id: str, now: float | None = None) -> bool:
        # Results intentionally stay durable. Acknowledgement is a compaction
        # opportunity for a later schema, never permission to execute again.
        with self._lock, self._db:
            changed = self._db.execute(
                "UPDATE idempotency SET acknowledged_at=COALESCE(acknowledged_at, ?) "
                "WHERE device_id=? AND request_id=? AND state='complete'",
                (time.time() if now is None else now, device_id, request_id),
            ).rowcount
        return bool(changed)

    def publish(self, content_sha256: str, floor_seq: int, now_utc: str) -> tuple[int, str]:
        """(descriptor_seq, updated_at) for descriptor content with this digest. Same content: what was published
        before. New content (capabilities, URL, label, key...): the next sequence, never below host.json's,
        stamped now, so a phone or directory never sees two descriptors under one sequence."""
        with self._lock:
            # The write lock before the read: the server and `voice-call directory` may publish at once, and a
            # read-then-upgrade in WAL mode would fail with a busy snapshot instead of waiting its turn.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM published_descriptor").fetchone()
                if row is not None and row["content_sha256"] == content_sha256 and row["seq"] >= floor_seq:
                    self._db.commit()
                    return int(row["seq"]), row["updated_at"]
                seq = max(floor_seq, int(row["seq"]) + 1 if row is not None else floor_seq)
                self._db.execute("INSERT OR REPLACE INTO published_descriptor VALUES (1,?,?,?)",
                                 (seq, content_sha256, now_utc))
                self._db.commit()
                return seq, now_utc
            except Exception:
                self._db.rollback()
                raise

    def append_event(self, session_id: str, kind: str, body: bytes, now: float | None = None,
                     source: str | None = None) -> str | None:
        """The new event's id; None when source names an event already kept (ingested before)."""
        event_id = str(uuid.uuid4())
        with self._lock, self._db:
            added = self._db.execute(
                "INSERT OR IGNORE INTO host_events(event_id,session_id,kind,body,created_at,source) VALUES (?,?,?,?,?,?)",
                (event_id, session_id, kind, body, time.time() if now is None else now, source),
            ).rowcount
        return event_id if added else None

    def event(self, event_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute("SELECT * FROM host_events WHERE event_id=?", (event_id,)).fetchone()

    def prune_events(self, before: float) -> int:
        """Events older than that go: the ledger is not a second, unbounded copy of every message."""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM host_events WHERE created_at < ?", (before,)).rowcount

    def events(self, session_id: str, limit: int = 50,
               after: tuple[float, str] | None = None) -> list[sqlite3.Row]:
        """A chat's events, oldest first; after=(created_at, event_id) of the last one already seen."""
        limit = max(1, min(200, int(limit)))
        since, last = after if after is not None else (-1.0, "")
        with self._lock:
            return list(self._db.execute(
                "SELECT * FROM host_events WHERE session_id=? AND (created_at>? OR (created_at=? AND event_id>?)) "
                "ORDER BY created_at,event_id LIMIT ?",
                (session_id, since, since, last, limit),
            ))

