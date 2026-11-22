"""The directory's config, its own Ed25519 identity, and signature checks."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from henchline_directory.shared import canonical

API_VERSION = "1.0"


@dataclass(frozen=True)
class DirectoryConfig:
    """henchline.directory-config/v1: where the state lives, and the public URL phones and hosts sign for."""
    listen: str
    database: Path
    signing_key: Path
    api_version: str
    expected_build_id: str
    generate_identity_if_absent: bool
    base_url: str  # https://<name>/.well-known/henchline/directory/ ; "" until configured

    @classmethod
    def load(cls, path: Path) -> "DirectoryConfig":
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read directory config {path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != "henchline.directory-config/v1":
            raise ValueError("directory config must be a henchline.directory-config/v1 object")
        if raw.get("rotate_identity", False):
            raise ValueError("rotate_identity is not supported: the directory's key is never replaced silently")
        if str(raw.get("api_version", API_VERSION)) != API_VERSION:
            raise ValueError(f"api_version must be {API_VERSION}")
        base_url = str(raw.get("base_url", "") or "")
        if base_url:
            parts = urlsplit(base_url)
            if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or parts.username \
                    or not parts.path.endswith("/"):
                raise ValueError("base_url must be https://name/prefix/ (ending in a slash)")
        try:
            return cls(listen=str(raw.get("listen", "127.0.0.1:8791")), database=Path(raw["database"]),
                       signing_key=Path(raw["signing_key"]), api_version=API_VERSION,
                       expected_build_id=str(raw.get("expected_build_id", "")),
                       generate_identity_if_absent=bool(raw.get("generate_identity_if_absent", False)),
                       base_url=base_url)
        except KeyError as exc:
            raise ValueError(f"directory config lacks {exc}") from exc


def verify(algorithm: str, public_key: bytes, signature: bytes, message: bytes) -> bool:
    """Ed25519 (raw 32-byte key) or P-256 ECDSA/SHA-256 (SubjectPublicKeyInfo DER, DER signature)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        if algorithm == "ed25519":
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
            return True
        if algorithm == "p256":
            key = serialization.load_der_public_key(public_key)
            if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
                return False
            key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
            return True
    except (InvalidSignature, ValueError, TypeError):
        return False
    return False


class Identity:
    """The directory's signing key (created once, never replaced) and its signed descriptor."""

    def __init__(self, key: Any, directory_id: str, config: DirectoryConfig):
        from cryptography.hazmat.primitives import serialization
        self.key = key
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        parts = urlsplit(config.base_url) if config.base_url else None
        unsigned = {
            "schema": "henchline.directory/v1",
            "directory_id": directory_id,
            "base_url": config.base_url or None,
            "tls_authority": parts.netloc.lower() if parts else None,
            "api_version": API_VERSION,
            "descriptor_seq": 1,
            "directory_signing_key": canonical.b64url(public),
            "directory_key_algorithm": "ed25519",
        }
        self.descriptor = {**unsigned, "signature": canonical.b64url(key.sign(canonical.canonical_json(unsigned)))}
        self.public_key = public

    @classmethod
    def load(cls, config: DirectoryConfig, directory_id: str) -> "Identity":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        path = config.signing_key
        if path.exists():
            if path.stat().st_mode & 0o077:
                raise ValueError(f"{path} must be mode 0600")
            raw = path.read_bytes()
            if len(raw) != 32:
                raise ValueError(f"{path} is not a raw Ed25519 private key")
            return cls(Ed25519PrivateKey.from_private_bytes(raw), directory_id, config)
        if not config.generate_identity_if_absent:
            raise ValueError(f"no directory key at {path} and generate_identity_if_absent is false")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = Ed25519PrivateKey.generate()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                           serialization.NoEncryption()))
            output.flush()
            os.fsync(output.fileno())
        return cls(key, directory_id, config)


def expires(lifetime: float) -> str:
    return canonical.rfc3339(time.time() + lifetime)
