"""Host signing identity and local pairing-code command."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from call.canonical import b64url, canonical_json, fingerprint, rfc3339
from call.host_config import DEFAULT_CONFIG, HostConfig
from call.host_store import PAIRING_LIFETIME, HostStore


class HostSigner:
    def __init__(self, key):
        self.key = key

    @classmethod
    def load_or_create(cls, path: Path) -> "HostSigner":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            if path.stat().st_mode & 0o077:
                raise ValueError(f"{path} must be mode 0600")
            raw = path.read_bytes()
            if len(raw) != 32:
                raise ValueError(f"{path} is not a raw Ed25519 private key")
            return cls(Ed25519PrivateKey.from_private_bytes(raw))
        key = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization
        raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
        return cls(key)

    def public_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization
        return self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, data: bytes) -> bytes:
        return self.key.sign(data)

    def published(self, config: HostConfig, store: HostStore, capabilities: dict[str, bool] | None = None) -> dict:
        """The descriptor this host advertises: content-addressed in its store, so any change of content gets a
        higher descriptor_seq and a fresh updated_at by itself (host.json's sequence is the floor).

        Restoring host.sqlite3 from an older backup rewinds the published sequence: raise descriptor_seq in
        host.json above the highest one phones and the directory have seen first, or they refuse the next
        descriptor as stale."""
        draft = self.descriptor(config, capabilities)
        content = {k: v for k, v in draft.items() if k not in ("descriptor_seq", "updated_at", "signature")}
        digest = hashlib.sha256(canonical_json(content)).hexdigest()
        now = rfc3339()
        seq, updated_at = store.publish(digest, config.descriptor_seq, now)
        return self.descriptor(config, capabilities, seq=seq, updated_at=updated_at)

    def descriptor(self, config: HostConfig, capabilities: dict[str, bool] | None = None, *,
                   seq: int | None = None, updated_at: str | None = None) -> dict:
        """The signed descriptor; capabilities narrows what config enables to what is actually served."""
        descriptor = {
            "schema": "henchline.host/v1",
            "host_id": config.host_id,
            "label": config.label,
            "base_url": config.base_url,
            "tls_authority": config.tls_authority,
            "api_version": config.api_version,
            "descriptor_seq": config.descriptor_seq if seq is None else seq,
            "updated_at": config.updated_at if updated_at is None else updated_at,
            "host_signing_key": b64url(self.public_bytes()),
            "host_key_algorithm": "ed25519",
            "min_app_version": config.min_app_version,
            "capabilities": config.capabilities if capabilities is None else capabilities,
        }
        descriptor["signature"] = b64url(self.sign(canonical_json(descriptor)))
        return descriptor


def devices_main(argv: list[str]) -> int:
    """`voice-call devices list` and `voice-call devices revoke DEVICE_ID`: the host owner's side of
    removal, for a phone that is lost or was paired by someone else."""
    parser = argparse.ArgumentParser(prog="voice-call devices")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="action", required=True)
    config = argparse.ArgumentParser(add_help=False)  # --config may also follow the action
    config.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    sub.add_parser("list", parents=[config], help="every phone ever paired, revoked ones too")
    revoke = sub.add_parser("revoke", parents=[config], help="that phone's key stops working at once")
    revoke.add_argument("device_id")
    args = parser.parse_args(argv)
    store = HostStore(HostConfig.load(args.config).database)
    try:
        if args.action == "list":
            for device in store.devices():
                state = "revoked" if device["revoked_at"] else "active"
                print(f"{device['device_id']}  {state:7}  {device['algorithm']:7}  {device['label']}")
            return 0
        if not store.revoke(args.device_id.lower()):
            print(f"no device {args.device_id}", file=sys.stderr)
            return 1
        print(f"revoked {args.device_id}")
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice-call pair", description="Show a five-minute code that pairs a phone")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--json", action="store_true", help="the code as one JSON line (for a QR code or a script)")
    args = parser.parse_args(argv)
    config = HostConfig.load(args.config)
    config.validate_private_paths()
    signer = HostSigner.load_or_create(config.key_file)
    store = HostStore(config.database)
    now = time.time()
    nonce = store.new_pairing(now)
    store.close()
    expires = dt.datetime.fromtimestamp(int(now + PAIRING_LIFETIME), dt.timezone.utc)
    key = b64url(signer.public_bytes())
    if args.json:
        print(json.dumps({
            "schema": "henchline.pair/v1",
            "host_id": config.host_id,
            "label": config.label,
            "base_url": config.base_url,
            "tls_authority": config.tls_authority,
            "host_signing_key": key,
            "host_key_algorithm": "ed25519",
            "fingerprint": fingerprint(signer.public_bytes()),
            "nonce": nonce,
            "expires_at": rfc3339(expires.timestamp()),
        }, separators=(",", ":")))
        return 0
    print(f"Pair a phone with {config.label}: in Henchline, Settings, Computers, Pair a computer.\n"
          f"  Address:      {config.base_url}\n"
          f"  Fingerprint:  {fingerprint(signer.public_bytes())}   (the phone shows it too: they must match)\n"
          f"  Code:         {nonce[:5]}-{nonce[5:]}   (one use, until {expires.astimezone().strftime('%H:%M')})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
