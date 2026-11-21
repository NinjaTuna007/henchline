"""The directory's HTTP API (/v1/...), on loopback behind a gateway that adds the public prefix.

Standard library HTTP server: the directory answers a handful of small JSON requests, so the release needs
no web framework, only `cryptography`. Signatures follow the host API's rules (canonical.request_bytes),
with this directory's ID in the authority slot and its public base URL's scheme, authority and path prefix:
  phone:  X-Henchline-Directory + X-Henchline-Device (the device slot: its directory device ID)
  host:   X-Henchline-Directory + X-Henchline-Host   (the device slot: its host ID; its pinned host key signs)
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from henchline_directory.identity import DirectoryConfig, Identity, verify
from henchline_directory.shared import canonical
from henchline_directory.store import DirectoryStore

API_VERSION = "1.0"
WINDOW = 120
MAX_BODY = 64 * 1024
UNSIGNED_PER_MINUTE = 60
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TIMESTAMP = re.compile(r"[1-9][0-9]{0,11}")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable, self.extra = status, code, message, retryable, extra


@dataclass
class Directory:
    config: DirectoryConfig
    store: DirectoryStore
    identity: Identity
    build_id: str
    unsigned_calls: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def throttle(self) -> None:
        now = time.monotonic()
        with self.lock:
            self.unsigned_calls[:] = [at for at in self.unsigned_calls if now - at < 60]
            if len(self.unsigned_calls) >= UNSIGNED_PER_MINUTE:
                raise ApiError(429, "rate_limited", "too many unauthenticated requests", retryable=True)
            self.unsigned_calls.append(now)

    def public(self) -> tuple[str, str, str]:
        """(scheme, authority, path prefix) of the public base URL, or 503 when none is configured."""
        if not self.config.base_url:
            raise ApiError(503, "host_unavailable", "this directory has no base_url configured")
        parts = urlsplit(self.config.base_url)
        return parts.scheme, parts.netloc.lower(), parts.path.rstrip("/")


def _header(headers, name: str) -> str:
    value = (headers.get(name) or "").strip()
    if not value:
        raise ApiError(401, "bad_signature", f"missing {name}")
    return value


def authenticate(directory: Directory, kind: str, method: str, raw_path: str, headers, body: bytes,
                 now: float | None = None) -> str:
    """The signer's ID (device or host), or ApiError."""
    clock = time.time() if now is None else now
    scheme, authority, prefix = directory.public()
    request_id = _header(headers, "X-Henchline-Request-Id")
    if not UUID_TEXT.fullmatch(request_id):
        raise ApiError(400, "invalid_request", "request id must be a lowercase hyphenated UUID")
    version = _header(headers, "X-Henchline-Api-Version")
    if version != API_VERSION:
        raise ApiError(412, "api_version_unsupported", "directory does not support that API version")
    if _header(headers, "X-Henchline-Directory").lower() != directory.store.directory_id:
        raise ApiError(401, "bad_signature", "request names a different directory")
    signer_id = _header(headers, "X-Henchline-Device" if kind == "phone" else "X-Henchline-Host").lower()
    signer = directory.store.signer(kind, signer_id)
    if signer is None:
        raise ApiError(401, "bad_signature", f"unknown {kind}")
    if signer.revoked:
        raise ApiError(403, "device_revoked", "device has been revoked")
    stamp = _header(headers, "X-Henchline-Timestamp")
    if not TIMESTAMP.fullmatch(stamp) or abs(clock - int(stamp)) > WINDOW:
        raise ApiError(401, "timestamp_out_of_window", "request timestamp is outside the allowed window",
                       server_time=int(clock))
    nonce = _header(headers, "X-Henchline-Nonce")
    try:
        if len(canonical.unb64url(nonce)) != 16:
            raise ValueError
        signature = canonical.unb64url(_header(headers, "X-Henchline-Signature"))
    except ValueError:
        raise ApiError(401, "bad_signature", "nonce or signature is not valid base64url") from None
    path, _, query = raw_path.partition("?")
    try:
        message = canonical.request_bytes(
            api_version=version, authority_id=directory.store.directory_id, scheme=scheme, authority=authority,
            method=method, path=prefix + path, query=query, body=body, device_id=signer_id, timestamp=stamp,
            nonce=nonce, request_id=request_id)
    except ValueError as exc:
        raise ApiError(400, "invalid_request", f"request cannot be canonicalized: {exc}") from None
    if not verify(signer.algorithm, signer.public_key, signature, message):
        raise ApiError(401, "bad_signature", "request signature did not verify")
    if not directory.store.use_nonce(f"{kind}:{signer_id}", nonce, now=clock):
        raise ApiError(401, "bad_signature", "request nonce was already used")
    return signer_id


def json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        value = None
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_request", "expected a JSON object")
    return value


def host_descriptor(value: Any, *, pinned_key: bytes | None = None) -> tuple[str, bytes, bytes, int]:
    """A host-signed descriptor checked by its own key (or the pinned one): (host_id, key, stored bytes, seq)."""
    if not isinstance(value, dict) or value.get("schema") != "henchline.host/v1":
        raise ApiError(422, "validation_failed", "expected a henchline.host/v1 descriptor")
    unsigned = {k: v for k, v in value.items() if k != "signature"}
    try:
        host_id = str(uuid.UUID(str(value["host_id"])))
        key = canonical.unb64url(str(value["host_signing_key"]))
        signature = canonical.unb64url(str(value["signature"]))
        seq = value["descriptor_seq"]
        signed = canonical.canonical_json(unsigned)
        stored = canonical.canonical_json(value)
    except (KeyError, ValueError):
        raise ApiError(422, "validation_failed", "descriptor is malformed") from None
    if host_id != value["host_id"] or not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ApiError(422, "validation_failed", "descriptor host_id or descriptor_seq is malformed")
    if value.get("host_key_algorithm", "ed25519") != "ed25519" or len(key) != 32:
        raise ApiError(422, "validation_failed", "host keys are Ed25519")
    if pinned_key is not None and key != pinned_key:
        raise ApiError(409, "state_conflict", "host key differs from the pinned one; re-enroll the host")
    if not verify("ed25519", key, signature, signed):
        raise ApiError(422, "validation_failed", "descriptor signature did not verify")
    return host_id, key, stored, seq


def limit_and_offset(query: dict[str, str]) -> tuple[int, int]:
    limit, cursor = query.get("limit", str(DEFAULT_LIMIT)), query.get("cursor", "") or "0"
    if not limit.isdigit() or not 1 <= int(limit) <= MAX_LIMIT or not cursor.isdigit():
        raise ApiError(400, "invalid_request", f"limit must be 1..{MAX_LIMIT}; cursor is opaque")
    return int(limit), int(cursor)


def route(directory: Directory, method: str, raw_path: str, headers, body: bytes) -> tuple[int, dict, dict]:
    """(status, JSON body, extra headers) for one request; every error in the one envelope."""
    path = raw_path.partition("?")[0]
    query = dict(item.partition("=")[::2] for item in raw_path.partition("?")[2].split("&") if item)
    store = directory.store

    if method == "GET" and path == "/v1/health":
        directory.throttle()
        return 200, {"status": "ok", "api_version": API_VERSION, "build_id": directory.build_id,
                     "server_time": int(time.time())}, {}
    if method == "GET" and path == "/v1/info":
        directory.throttle()
        directory.public()
        return 200, {"descriptor": directory.identity.descriptor,
                     "pairing": {"supported": True, "device_key_algorithms": ["ed25519", "p256"]}}, {}
    if method == "POST" and path == "/v1/pairing/claim":
        directory.throttle()
        directory.public()
        claim = json_object(body)
        try:
            nonce, algorithm, encoded, proof = (str(claim[k]) for k in ("nonce", "algorithm", "public_key", "proof"))
        except KeyError:
            raise ApiError(400, "invalid_request", "expected {nonce, algorithm, public_key, proof, label}") from None
        try:
            public_key = canonical.unb64url(encoded)
            message = canonical.pairing_bytes(host_id=store.directory_id, nonce=nonce, algorithm=algorithm,
                                              public_key=encoded)
            good = algorithm in ("ed25519", "p256") and verify(algorithm, public_key, canonical.unb64url(proof), message)
        except ValueError:
            good = False
        if not good:
            store.code_failure("phone", nonce)
            raise ApiError(422, "validation_failed", "public key or its proof of possession is invalid")
        label = " ".join(str(claim.get("label", "")).split())[:80]
        try:
            device_id = store.claim_phone(nonce, algorithm, public_key, label)
        except (LookupError, TimeoutError, PermissionError):
            raise ApiError(410, "pairing_expired", "pairing code is not valid any more; show a new one") from None
        return 201, {"directory_id": store.directory_id, "device_id": device_id,
                     "descriptor": directory.identity.descriptor}, {}
    if method == "POST" and path == "/v1/hosts/enroll/claim":
        directory.throttle()
        claim = json_object(body)
        nonce = str(claim.get("nonce", ""))
        try:
            host_id, key, stored, seq = host_descriptor(claim.get("descriptor"))
        except ApiError:
            store.code_failure("host", nonce)
            raise
        try:
            store.enroll_host(nonce, host_id, key, stored, seq)
        except FileExistsError:
            raise ApiError(409, "state_conflict", "host is already enrolled") from None
        except (LookupError, TimeoutError, PermissionError):
            raise ApiError(410, "pairing_expired", "enrolment code is not valid any more") from None
        return 201, {"directory_id": store.directory_id, "host_id": host_id, "descriptor_seq": seq}, {}

    if method == "POST" and path == "/v1/hosts/heartbeat":
        signer = authenticate(directory, "host", method, raw_path, headers, body)
        pinned = store.signer("host", signer)
        host_id, _, stored, seq = host_descriptor(json_object(body).get("descriptor"), pinned_key=pinned.public_key)
        if host_id != signer:
            raise ApiError(422, "validation_failed", "descriptor names another host")
        try:
            outcome = store.heartbeat(host_id, stored, seq)
        except ValueError:
            raise ApiError(412, "descriptor_stale", "descriptor_seq must not go down, nor change without rising") from None
        return 200, {"directory_id": store.directory_id, "host_id": host_id, "descriptor_seq": seq,
                     "outcome": outcome, "request_id": headers.get("X-Henchline-Request-Id")}, {}
    match = re.fullmatch(r"/v1/hosts/([^/]+)", path)
    if method == "DELETE" and match:
        signer = authenticate(directory, "host", method, raw_path, headers, body)
        if unquote(match.group(1)).lower() != signer:
            raise ApiError(403, "capability_denied", "a host may withdraw only itself")
        store.withdraw(signer)
        return 200, {"directory_id": store.directory_id, "host_id": signer, "withdrawn": True,
                     "request_id": headers.get("X-Henchline-Request-Id")}, {}
    if method == "GET" and path == "/v1/hosts":
        authenticate(directory, "phone", method, raw_path, headers, body)
        limit, offset = limit_and_offset(query)
        listed, more = store.hosts(offset, limit)
        return 200, {"directory_id": store.directory_id, "hosts": listed,
                     "next_cursor": str(offset + limit) if more else None,
                     "request_id": headers.get("X-Henchline-Request-Id")}, {}
    match = re.fullmatch(r"/v1/devices/([^/]+)", path)
    if method == "DELETE" and match:
        signer = authenticate(directory, "phone", method, raw_path, headers, body)
        if unquote(match.group(1)).lower() != signer:
            raise ApiError(403, "capability_denied", "a device may revoke only itself")
        store.revoke_phone(signer)
        return 200, {"directory_id": store.directory_id, "device_id": signer, "revoked": True,
                     "request_id": headers.get("X-Henchline-Request-Id")}, {}
    raise ApiError(404, "not_found", "no such route in the directory API")


class Handler(BaseHTTPRequestHandler):
    server_version = "henchline-directory"
    sys_version = ""
    directory: Directory  # set on the subclass made by serve()

    def log_message(self, format: str, *args: Any) -> None:  # method, path and status only: never bodies
        pass

    def _answer(self, method: str) -> None:
        body = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY:
                raise ApiError(413 if length > 0 else 400, "invalid_request", "body too large")
            body = self.rfile.read(length) if length else b""
            status, value, extra = route(self.directory, method, self.path, self.headers, body)
        except ApiError as error:
            status, extra = error.status, {}
            value = {"error": {"code": error.code, "message": error.message, "retryable": error.retryable,
                               "request_id": (self.headers.get("X-Henchline-Request-Id") or "")[:64],
                               **error.extra}}
        except Exception:  # never a traceback to the client
            status, extra = 500, {}
            value = {"error": {"code": "internal_error", "message": "the directory failed", "retryable": True,
                               "request_id": ""}}
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, text in extra.items():
            self.send_header(name, text)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")

    def do_DELETE(self) -> None:
        self._answer("DELETE")


def make_server(directory: Directory, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("DirectoryHandler", (Handler,), {"directory": directory})
    server_class = ThreadingHTTPServer
    if ":" in host:
        import socket
        server_class = type("V6Server", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
    server = server_class((host, port), handler)
    server.daemon_threads = True
    return server
