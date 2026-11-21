"""Host signing identity and local pairing-code command."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path

from call.canonical import b64url, canonical_json
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

    def descriptor(self, config: HostConfig, capabilities: dict[str, bool] | None = None) -> dict:
        """The signed descriptor; capabilities narrows what config enables to what is actually served."""
        descriptor = {
            "schema": "henchline.host/v1",
            "host_id": config.host_id,
            "label": config.label,
            "base_url": config.base_url,
            "tls_authority": config.tls_authority,
            "api_version": config.api_version,
            "descriptor_seq": config.descriptor_seq,
            "updated_at": config.updated_at,
            "host_signing_key": b64url(self.public_bytes()),
            "host_key_algorithm": "ed25519",
            "min_app_version": config.min_app_version,
            "capabilities": config.capabilities if capabilities is None else capabilities,
        }
        descriptor["signature"] = b64url(self.sign(canonical_json(descriptor)))
        return descriptor


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a short-lived Henchline phone pairing code")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    config = HostConfig.load(args.config)
    signer = HostSigner.load_or_create(config.key_file)
    store = HostStore(config.database)
    now = time.time()
    nonce = store.new_pairing(now)
    print(json.dumps({
        "schema": "henchline.pair/v1",
        "host_id": config.host_id,
        "label": config.label,
        "base_url": config.base_url,
        "tls_authority": config.tls_authority,
        "host_signing_key": b64url(signer.public_bytes()),
        "host_key_algorithm": "ed25519",
        "nonce": nonce,
        "expires_at": dt.datetime.fromtimestamp(int(now + PAIRING_LIFETIME), dt.timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
