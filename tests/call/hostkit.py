"""Shared pieces for the host API tests: test-only keys, a host config, and a signed-request builder."""

from __future__ import annotations

import json
import secrets
import tempfile
import time
import uuid
from pathlib import Path

from tests import sandbox

from call import canonical
from call.host_config import HostConfig

FIXTURES = sandbox.REPO / "tests/fixtures"
VECTORS = json.loads((FIXTURES / "host-signatures.json").read_text())
HOST_ID = "00000000-0000-4000-8000-000000000001"
OTHER_HOST_ID = "00000000-0000-4000-8000-000000000002"
AUTHORITY = "host-a.example.ts.net"


def config(folder: Path | None = None, **changes) -> HostConfig:
    folder = Path(folder or tempfile.mkdtemp(dir=sandbox.HOME))
    raw = {
        "host_id": HOST_ID, "label": "Host A", "base_url": f"https://{AUTHORITY}/",
        "tls_authority": AUTHORITY, "descriptor_seq": 3, "updated_at": "2026-11-21T12:00:00Z",
        "capabilities": {"chat": True, "calls": False},
        "database": str(folder / "host.sqlite3"), "key_file": str(folder / "keys/host.ed25519"),
    }
    raw.update(changes)
    return HostConfig.from_dict(raw)


def ed25519_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.generate()


def p256_key():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1())


def public_bytes(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if isinstance(key, Ed25519PrivateKey):
        return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def sign(key, message: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if isinstance(key, Ed25519PrivateKey):
        return key.sign(message)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
    return key.sign(message, ECDSA(hashes.SHA256()))


def signed_headers(key, device_id: str, method: str, path_qs: str, body: bytes = b"", *,
                   host_id: str = HOST_ID, authority: str = AUTHORITY, api_version: str = "1.0",
                   timestamp: int | None = None, nonce: str | None = None,
                   request_id: str | None = None) -> dict[str, str]:
    path, _, query = path_qs.partition("?")
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = nonce or canonical.b64url(secrets.token_bytes(16))
    request_id = request_id or str(uuid.uuid4())
    message = canonical.request_bytes(
        api_version=api_version, authority_id=host_id, scheme="https", authority=authority,
        method=method, path=path, query=query, body=body, device_id=device_id,
        timestamp=timestamp, nonce=nonce, request_id=request_id)
    return {
        "X-Henchline-Api-Version": api_version,
        "X-Henchline-Host": host_id,
        "X-Henchline-Device": device_id,
        "X-Henchline-Timestamp": str(timestamp),
        "X-Henchline-Nonce": nonce,
        "X-Henchline-Request-Id": request_id,
        "X-Henchline-Signature": canonical.b64url(sign(key, message)),
    }
