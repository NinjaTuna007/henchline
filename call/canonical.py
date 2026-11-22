"""Canonical bytes shared by Henchline host clients and servers.

The request form is intentionally small and fully specified.  Keep this file
dependency-free: the same fixtures are consumed by Android's implementation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any, Iterable
from urllib.parse import quote_from_bytes, unquote_to_bytes


PREFIX = "HENCHLINE-SIG-V1"
UNRESERVED = "-._~"
MAX_SAFE_INTEGER = 2 ** 53 - 1
B64URL = re.compile(r"[A-Za-z0-9_-]+")
BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


RFC3339 = "%Y-%m-%dT%H:%M:%SZ"


def rfc3339(at: float | None = None) -> str:
    """A moment (seconds since the epoch; now when None) as the one timestamp form descriptors, pairing codes and
    events carry: RFC 3339 UTC to the second, "2026-11-21T12:00:00Z" (fractions dropped). It goes into signed
    bytes: never change its spelling."""
    return time.strftime(RFC3339, time.gmtime(time.time() if at is None else at))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(value: str) -> bytes:
    """Strict unpadded base64url: one spelling per byte string, nothing skipped."""
    if not value or not B64URL.fullmatch(value):
        raise ValueError("invalid base64url")
    try:
        data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid base64url") from exc
    if b64url(data) != value:  # stray trailing bits, or a length no encoder produces
        raise ValueError("invalid base64url")
    return data


def _component(value: str) -> str:
    """Percent-normalize one path segment or query component.

    Decoding and re-encoding bytes uppercases escapes and prevents equivalent
    spellings from producing different signatures. Literal ``+`` remains a
    plus byte (and is encoded as ``%2B``); it is never form-decoded to space.
    """
    if BAD_ESCAPE.search(value):  # urllib would keep "%zz" literally; that is a second spelling, so refuse it
        raise ValueError("invalid percent encoding")
    return quote_from_bytes(unquote_to_bytes(value), safe=UNRESERVED)


def canonical_path(path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("path must be absolute")
    # Split only literal slashes. An encoded slash remains inside its segment
    # and is emitted as %2F rather than being confused with a path separator.
    return "/".join(_component(segment) for segment in path.split("/"))


def canonical_query(query: str | Iterable[tuple[str, str]]) -> str:
    if isinstance(query, str):
        pairs: list[tuple[str, str]] = []
        if query:
            for item in query.split("&"):
                if not item:  # "a=1&&b=2" and a trailing "&" add nothing
                    continue
                key, sep, value = item.partition("=")
                pairs.append((_component(key), _component(value if sep else "")))
    else:
        pairs = [(_component(str(key)), _component(str(value))) for key, value in query]
    pairs.sort()
    return "&".join(f"{key}={value}" for key, value in pairs)


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def request_bytes(
    *,
    api_version: str,
    authority_id: str,
    scheme: str,
    authority: str,
    method: str,
    path: str,
    query: str | Iterable[tuple[str, str]],
    body: bytes,
    device_id: str,
    timestamp: int | str,
    nonce: str,
    request_id: str,
    body_sha256: str | None = None,
) -> bytes:
    """body_sha256: the body's digest when the body itself isn't at hand yet (an upload read as it streams in;
    the receiver checks the bytes against it before keeping them)."""
    fields = (
        PREFIX,
        api_version,
        authority_id.lower(),
        scheme.lower(),
        authority.lower(),
        method.upper(),
        canonical_path(path),
        canonical_query(query),
        body_digest(body) if body_sha256 is None else body_sha256,
        device_id,
        str(timestamp),
        nonce,
        request_id.lower(),
    )
    if any("\n" in field or "\r" in field for field in fields):
        raise ValueError("canonical field contains a newline")
    return ("\n".join(fields) + "\n").encode("utf-8")


def request_fingerprint(method: str, path: str, query: str, body: bytes) -> str:
    material = "\n".join((method.upper(), canonical_path(path), canonical_query(query), body_digest(body)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> bytes:
    """RFC 8785 (JCS) bytes for a signed descriptor.

    Descriptors hold strings, integers, booleans, null, lists and objects.
    Floats are refused so this encoder cannot disagree with JCS number
    formatting; integers must be exactly representable as a double. Object
    members are ordered by their UTF-16 code units, as JCS requires (code point
    order differs once a key holds a character outside the BMP).
    """
    def encode(item: Any) -> str:
        if item is None or isinstance(item, bool):
            return json.dumps(item)
        if isinstance(item, int):
            if abs(item) > MAX_SAFE_INTEGER:
                raise ValueError("integers in signed descriptors must fit in 53 bits")
            return str(item)
        if isinstance(item, float):
            raise ValueError("floating point values are not allowed in signed descriptors")
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(child) for child in item) + "]"
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("descriptor keys must be strings")
            keys = sorted(item, key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(f"{encode(key)}:{encode(item[key])}" for key in keys) + "}"
        raise ValueError(f"cannot sign a {type(item).__name__}")

    return encode(value).encode("utf-8")


PAIR_PREFIX = "HENCHLINE-PAIR-V1"


def pairing_bytes(*, host_id: str, nonce: str, algorithm: str, public_key: str) -> bytes:
    """What a phone signs with its new key when it claims a pairing code: proof it holds that key.

    public_key is the base64url text exactly as sent in the claim.
    """
    fields = (PAIR_PREFIX, host_id.lower(), nonce, algorithm, public_key)
    if any("\n" in field or "\r" in field for field in fields):
        raise ValueError("pairing field contains a newline")
    return ("\n".join(fields) + "\n").encode("utf-8")


CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O or U to misread


def crockford(data: bytes, length: int) -> str:
    bits = int.from_bytes(data, "big")
    total = len(data) * 8
    return "".join(CODE_ALPHABET[(bits >> (total - 5 * (i + 1))) & 31] for i in range(length))


def fingerprint(public_key: bytes) -> str:
    """What both screens show before pairing, for the user to compare: 80 bits of the key's SHA-256 as
    XXXX-XXXX-XXXX-XXXX in Crockford base32."""
    text = crockford(hashlib.sha256(public_key).digest()[:10], 16)
    return "-".join(text[i:i + 4] for i in range(0, 16, 4))


def pairing_code(text: str) -> str:
    """A typed one-time code as the host stores it: case, spaces and dashes dropped, O read as 0, I and L as 1."""
    cleaned = text.upper().replace("-", "").replace(" ", "").replace("O", "0").replace("I", "1").replace("L", "1")
    if not cleaned or any(ch not in CODE_ALPHABET for ch in cleaned):
        raise ValueError("a pairing code is letters and digits")
    return cleaned
