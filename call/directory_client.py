"""This host's side of the optional directory: enrol once with an admin code, then signed heartbeats.

    voice-call directory enroll [--code JSON]   the code `henchline-directory enroll-host` printed (or stdin)
    voice-call directory status                 which directory, if any
    voice-call directory heartbeat              one heartbeat now (the server sends them with --directory-heartbeat)
    voice-call directory withdraw               stop being listed until the next heartbeat

A running server keeps the descriptor it published at startup. `voice-call directory heartbeat` publishes the
current one, so after a change of content (host.json, a new capability) the server's own heartbeats send the
older, lower descriptor and are refused as stale until the server is restarted: restart it after such a change.
Restoring host.sqlite3 from an old backup needs host.json's descriptor_seq raised (see HostSigner.published).

The directory never carries traffic: it lists this host's own signed descriptor for paired phones. The
enrolment pins the directory's key from the code and checks the directory's signed descriptor against it,
so a heartbeat only ever goes to the directory the user enrolled with. Stored in
~/.config/henchline/directory.json (0600; public data only: no key, no nonce).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from call import canonical
from call.host_config import DEFAULT_CONFIG, HostConfig

DIRECTORY_FILE = Path.home() / ".config/henchline/directory.json"
HEARTBEAT_EVERY = 60
API_VERSION = "1.0"

# (method, url, body, headers) -> (status, JSON answer); the default speaks HTTPS with certificate checks
Transport = Callable[[str, str, bytes, dict[str, str]], tuple[int, dict]]


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """A signed request goes to exactly the URL it was signed for: a 30x is an error, never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl} refused", headers, fp)


OPENER = urllib.request.build_opener(NoRedirects)


def https(method: str, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    if urlsplit(url).scheme != "https":
        raise ValueError("the directory is only reached over https")
    request = urllib.request.Request(url, data=body if method != "GET" else None, method=method,
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with OPENER.open(request, timeout=15) as answer:
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}


@dataclass(frozen=True)
class Enrolment:
    directory_id: str
    base_url: str  # https://name/.well-known/henchline/directory/
    tls_authority: str
    directory_signing_key: str  # base64url Ed25519, pinned from the enrolment code
    enrolled_at: str

    def save(self, path: Path = DIRECTORY_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = path.with_name(f".{path.name}.partial")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(asdict(self), output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(partial, path)

    @classmethod
    def load(cls, path: Path = DIRECTORY_FILE) -> "Enrolment | None":
        try:
            raw = json.loads(path.read_text())
            return cls(**{k: str(raw[k]) for k in cls.__dataclass_fields__})
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc


class DirectoryError(Exception):
    pass


def _verify_descriptor(descriptor: Any, key_b64: str, directory_id: str) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(descriptor, dict) or descriptor.get("schema") != "henchline.directory/v1":
        raise DirectoryError("the directory did not send a henchline.directory/v1 descriptor")
    unsigned = {k: v for k, v in descriptor.items() if k != "signature"}
    try:
        Ed25519PublicKey.from_public_bytes(canonical.unb64url(key_b64)).verify(
            canonical.unb64url(str(descriptor.get("signature", ""))), canonical.canonical_json(unsigned))
    except Exception as exc:
        raise DirectoryError("the directory's descriptor is not signed by the key in the code") from exc
    if descriptor.get("directory_id") != directory_id or descriptor.get("directory_signing_key") != key_b64:
        raise DirectoryError("the directory's descriptor names another directory or key than the code")


def enroll(code: dict, descriptor: dict, transport: Transport = https) -> Enrolment:
    """Pin the directory from the code, check its signed descriptor, and claim the code with ours."""
    if code.get("schema") != "henchline.directory-enroll/v1":
        raise DirectoryError("that is not a henchline-directory enroll-host code")
    try:
        directory_id = str(uuid.UUID(str(code["directory_id"])))
        base_url, key, nonce = str(code["base_url"]), str(code["directory_signing_key"]), str(code["nonce"])
    except (KeyError, ValueError) as exc:
        raise DirectoryError(f"the code is incomplete: {exc}") from exc
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or not parts.path.endswith("/"):
        raise DirectoryError("the code's base_url must be https://name/prefix/")
    status, info = transport("GET", base_url + "v1/info", b"", {})
    if status != 200:
        raise DirectoryError(f"the directory answered {status} for its info")
    _verify_descriptor(info.get("descriptor"), key, directory_id)
    body = json.dumps({"nonce": nonce, "descriptor": descriptor}).encode()
    status, answer = transport("POST", base_url + "v1/hosts/enroll/claim", body, {})
    if status != 201:
        raise DirectoryError(f"enrolment refused ({status}): {answer.get('error', {}).get('message', answer)}")
    if answer.get("directory_id") != directory_id or answer.get("host_id") != descriptor.get("host_id"):
        raise DirectoryError("the directory's answer names another directory or host")
    return Enrolment(directory_id, base_url, parts.netloc.lower(), key,
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def signed_request(enrolment: Enrolment, host_id: str, signer: Any, method: str, route: str,
                   body: bytes, transport: Transport = https) -> tuple[int, dict]:
    """route is relative to /v1/ (hosts/heartbeat); signed for the public path, as the directory checks."""
    url = enrolment.base_url + "v1/" + route
    parts = urlsplit(url)
    stamp, nonce, rid = str(int(time.time())), canonical.b64url(secrets.token_bytes(16)), str(uuid.uuid4())
    message = canonical.request_bytes(
        api_version=API_VERSION, authority_id=enrolment.directory_id, scheme="https",
        authority=enrolment.tls_authority, method=method, path=parts.path, query=parts.query, body=body,
        device_id=host_id, timestamp=stamp, nonce=nonce, request_id=rid)
    headers = {"X-Henchline-Api-Version": API_VERSION, "X-Henchline-Directory": enrolment.directory_id,
               "X-Henchline-Host": host_id, "X-Henchline-Timestamp": stamp, "X-Henchline-Nonce": nonce,
               "X-Henchline-Request-Id": rid, "X-Henchline-Signature": canonical.b64url(signer.sign(message))}
    return transport(method, url, body, headers)


def heartbeat(enrolment: Enrolment, descriptor: dict, signer: Any, transport: Transport = https) -> str:
    """"stored" or "seen", else DirectoryError."""
    status, answer = signed_request(enrolment, descriptor["host_id"], signer, "POST", "hosts/heartbeat",
                                    json.dumps({"descriptor": descriptor}).encode(), transport)
    if status != 200:
        raise DirectoryError(f"heartbeat refused ({status}): {answer.get('error', {}).get('code', answer)}")
    return str(answer.get("outcome"))


def withdraw(enrolment: Enrolment, host_id: str, signer: Any, transport: Transport = https) -> None:
    status, answer = signed_request(enrolment, host_id, signer, "DELETE", f"hosts/{host_id}", b"", transport)
    if status != 200:
        raise DirectoryError(f"withdraw refused ({status}): {answer.get('error', {}).get('code', answer)}")


def _host(config_path: Path) -> tuple[HostConfig, Any, dict]:
    """The same descriptor the server advertises (published through host.sqlite3)."""
    from call import api_v1
    from call.host_store import HostStore
    from call.pair import HostSigner
    config = HostConfig.load(config_path)
    config.validate_private_paths()
    signer = HostSigner.load_or_create(config.key_file)
    store = HostStore(config.database)
    try:
        return config, signer, signer.published(config, store, api_v1.effective_capabilities(config))
    finally:
        store.close()


def main(argv: list[str], transport: Transport = https, directory_file: Path | None = None) -> int:
    directory_file = directory_file or DIRECTORY_FILE
    parser = argparse.ArgumentParser(prog="voice-call directory")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="action", required=True)
    enrol = sub.add_parser("enroll", help="enrol this host with a directory, once")
    enrol.add_argument("--code", help="the JSON line `henchline-directory enroll-host` printed (default: stdin)")
    sub.add_parser("status")
    sub.add_parser("heartbeat")
    sub.add_parser("withdraw")
    args = parser.parse_args(argv)
    try:
        current = Enrolment.load(directory_file)
        if args.action == "status":
            print(json.dumps(asdict(current)) if current else "not enrolled with a directory")
            return 0
        config, signer, descriptor = _host(args.config)
        if args.action == "enroll":
            if current:
                print(f"already enrolled with {current.base_url} ({current.directory_id}); "
                      f"remove {directory_file} first to change", file=sys.stderr)
                return 1
            code = json.loads(args.code if args.code is not None else sys.stdin.read())
            enrolment = enroll(code, descriptor, transport)
            enrolment.save(directory_file)
            print(f"enrolled with {enrolment.base_url} ({enrolment.directory_id})")
            return 0
        if current is None:
            print("not enrolled with a directory: voice-call directory enroll", file=sys.stderr)
            return 1
        if args.action == "heartbeat":
            print(heartbeat(current, descriptor, signer, transport))
        else:
            withdraw(current, config.host_id, signer, transport)
            print("withdrawn until the next heartbeat")
        return 0
    except (DirectoryError, ValueError, OSError) as exc:
        print(f"voice-call directory {args.action}: {exc}", file=sys.stderr)
        return 1


async def keep_listed(descriptor: dict, signer: Any, log: Callable[[str], None],
                      directory_file: Path | None = None, transport: Transport = https,
                      every: float = HEARTBEAT_EVERY) -> None:
    """The server's loop (--directory-heartbeat): one heartbeat at start, then one a minute. Logged only
    when the outcome changes, so a directory that is down for a day writes two lines, not 1440."""
    import asyncio
    loop = asyncio.get_running_loop()
    last = None
    while True:
        try:
            enrolment = Enrolment.load(directory_file or DIRECTORY_FILE)
            if enrolment is None:
                now = "not enrolled with a directory (voice-call directory enroll): no heartbeats"
            else:
                await loop.run_in_executor(None, heartbeat, enrolment, descriptor, signer, transport)
                now = f"directory heartbeats reach {enrolment.base_url}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the loop outlives any one failure; the next minute tries again
            now = f"directory heartbeat failed: {exc}"
        if now != last:
            log(now)
            last = now
        await asyncio.sleep(every)
