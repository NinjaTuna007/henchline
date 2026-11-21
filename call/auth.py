"""Application authentication for the versioned Henchline host API."""

from __future__ import annotations

import json
import re
import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from call.canonical import request_bytes, request_fingerprint, unb64url
from call.host_config import HostConfig
from call.host_store import Device, HostStore


WINDOW = 120
TIMESTAMP = re.compile(r"[1-9][0-9]{0,11}")
REQUEST_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False,
                 request_id: str = "", **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        self.extra = extra  # more members of the error object, e.g. server_time

    def response(self) -> web.Response:
        return web.json_response({"error": {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": self.request_id,
            **self.extra,
        }}, status=self.status)


@dataclass(frozen=True)
class SignedRequest:
    device: Device
    request_id: str
    fingerprint: str
    body: bytes


def _header(request: web.Request, name: str) -> str:
    value = request.headers.get(name, "").strip()
    if not value:
        raise ApiError(401, "bad_signature", f"missing {name}")
    return value


def load_public_key(algorithm: str, encoded: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        if algorithm == "ed25519":
            if len(encoded) == 32:
                return Ed25519PublicKey.from_public_bytes(encoded)
            key = serialization.load_der_public_key(encoded)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError("not an Ed25519 key")
            return key
        if algorithm == "p256":
            from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey, SECP256R1
            key = serialization.load_der_public_key(encoded)
            if not isinstance(key, EllipticCurvePublicKey) or not isinstance(key.curve, SECP256R1):
                raise ValueError("not a P-256 key")
            return key
    except (ImportError, ValueError, TypeError) as exc:
        raise ValueError(f"invalid {algorithm} public key") from exc
    raise ValueError("unsupported key algorithm")


def verify_signature(device: Device, signature: bytes, message: bytes) -> None:
    key = load_public_key(device.algorithm, device.public_key)
    try:
        if device.algorithm == "ed25519":
            key.verify(signature, message)
        else:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
            key.verify(signature, message, ECDSA(hashes.SHA256()))
    except Exception as exc:
        raise ApiError(401, "bad_signature", "request signature did not verify") from exc


async def authenticate(request: web.Request, config: HostConfig, store: HostStore,
                       *, now: float | None = None) -> SignedRequest:
    request_id = _header(request, "X-Henchline-Request-Id")
    if not REQUEST_ID.fullmatch(request_id):  # exactly one spelling: what the signature and the ledger hold
        raise ApiError(400, "invalid_request", "request id must be a lowercase hyphenated UUID",
                       request_id=request_id[:64])
    version = _header(request, "X-Henchline-Api-Version")
    if version != config.api_version:
        raise ApiError(412, "api_version_unsupported", "host does not support that API version",
                       request_id=request_id)
    if _header(request, "X-Henchline-Host").lower() != config.host_id:
        raise ApiError(401, "bad_signature", "request names a different host", request_id=request_id)
    device_id = _header(request, "X-Henchline-Device")
    loop = asyncio.get_running_loop()  # SQLite may wait on its lock: never on the event loop
    device = await loop.run_in_executor(None, store.device, device_id)
    if device is None:
        # reason: a phone removing this host may then forget it (nothing here trusts its key)
        raise ApiError(401, "bad_signature", "unknown device", request_id=request_id, reason="unknown_device")
    if device.revoked_at is not None:
        raise ApiError(403, "device_revoked", "device has been revoked", request_id=request_id)
    timestamp_text = _header(request, "X-Henchline-Timestamp")
    clock = time.time() if now is None else now
    if not TIMESTAMP.fullmatch(timestamp_text):  # one spelling per second: no sign, space or leading zero
        raise ApiError(401, "timestamp_out_of_window", "invalid timestamp", request_id=request_id,
                       server_time=int(clock))
    timestamp = int(timestamp_text)
    if abs(clock - timestamp) > WINDOW:  # the host's clock in the answer: a phone that is off can tell by how much
        raise ApiError(401, "timestamp_out_of_window", "request timestamp is outside the allowed window",
                       request_id=request_id, server_time=int(clock))
    nonce = _header(request, "X-Henchline-Nonce")
    try:
        if len(unb64url(nonce)) != 16:
            raise ValueError
    except ValueError as exc:
        raise ApiError(401, "bad_signature", "nonce must encode 16 bytes", request_id=request_id) from exc
    try:
        signature = unb64url(_header(request, "X-Henchline-Signature"))
    except ValueError as exc:
        raise ApiError(401, "bad_signature", "invalid signature encoding", request_id=request_id) from exc
    body = await request.read()
    raw_path, _, raw_query = request.raw_path.partition("?")
    base = urlsplit(config.base_url)
    try:
        message = request_bytes(
            api_version=version,
            authority_id=config.host_id,
            scheme=base.scheme,
            authority=config.tls_authority,
            method=request.method,
            path=raw_path,
            query=raw_query,
            body=body,
            device_id=device_id,
            timestamp=timestamp_text,
            nonce=nonce,
            request_id=request_id,
        )
    except ValueError as exc:  # a malformed escape, a newline: no signature can cover it
        raise ApiError(400, "invalid_request", f"request cannot be canonicalized: {exc}",
                       request_id=request_id) from exc
    verify_signature(device, signature, message)
    if not await loop.run_in_executor(None, lambda: store.use_nonce(device_id, nonce, now=clock)):
        raise ApiError(401, "bad_signature", "request nonce was already used", request_id=request_id)
    return SignedRequest(
        device=device,
        request_id=request_id,
        fingerprint=request_fingerprint(request.method, raw_path, raw_query, body),
        body=body,
    )


def json_body(signed: SignedRequest) -> dict[str, Any]:
    try:
        value = json.loads(signed.body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(400, "invalid_request", "expected a JSON object", request_id=signed.request_id) from exc
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_request", "expected a JSON object", request_id=signed.request_id)
    return value
