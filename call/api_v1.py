"""The versioned, signed host API (/api/v1), mounted only when the server is started with --api-v1.

Every route but health, the descriptor and the pairing claim needs a request signed by an enrolled phone
(call/auth.py). Every mutation is idempotent: the phone's request ID is reserved in host.sqlite3 before
anything happens, and a retry gets the stored answer back (with "replayed": true) however late it comes.
Delivery itself stays where it was: the server hands this module a LocalHost whose methods call its own
to_agent and agent listing, so nothing here knows about terminals or audio.

A capability is advertised only when the host's config enables it and this module implements it: today that
is "chat" (list chats, read this API's message events, send a message) and "legacy_routes" (the server's own
routes take the same signed requests: guard_legacy). The rest of the contract's routes answer 404 until their
capability is built.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import unquote

from aiohttp import web

from call import auth
from call.auth import ApiError, SignedRequest
from call.canonical import b64url, pairing_bytes, unb64url
from call.host_config import HostConfig
from call.host_store import Device, HostStore

IMPLEMENTED = {"chat", "legacy_routes"}
LOG = logging.getLogger("henchline.api_v1")
BUILD_ID = os.environ.get("HENCHLINE_BUILD_ID", "dev")
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
MAX_TEXT = 64 * 1024  # characters in one message
UNSIGNED_PER_MINUTE = 60  # health, descriptor and pairing claims together: they are reachable before pairing
SESSION_ID = re.compile(r"[^\x00-\x1f\x7f]{1,256}")
TEXT_ID = re.compile(r"[0-9a-f]{16}")
INGEST_EVERY = 2.0  # seconds between looks for agents' new texts
PRUNE_EVERY = 3600.0  # seconds between sweeps of events older than the host's ledger_days


class LocalHost(Protocol):
    """What this machine does, as the server provides it (blocking calls: they run in an executor)."""

    def deliver(self, text: str, session_id: str) -> tuple[str, str]:
        """Type text into that agent's chat: (who got it, "") or ("", why nobody did)."""

    def chats(self) -> list[dict[str, Any]]:
        """The agents reachable now: [{session_id, name, ...}]."""

    def announce(self, words: str) -> None:
        """Tell the user something they must know about (a phone was enrolled)."""

    def texts(self) -> list[dict[str, Any]]:
        """The agents' texts to the phone this host still keeps: [{id, agent, session, words, at, status}]."""

    def mark_text(self, text_id: str, status: str) -> bool:
        """A text delivered or read on a phone: moved forward only (never back). Whether it moved."""


@dataclass
class Api:
    config: HostConfig
    store: HostStore
    host: LocalHost
    signer: Any  # call.pair.HostSigner
    capabilities: dict[str, bool]
    descriptor: dict[str, Any]
    unsigned_calls: list[float]
    in_flight: set[tuple[str, str]]  # (device, request id) being carried out by this process now


API_KEY = web.AppKey("henchline_api_v1", Api)


def effective_capabilities(config: HostConfig) -> dict[str, bool]:
    return {name: enabled and name in IMPLEMENTED for name, enabled in config.capabilities.items()}


def setup(app: web.Application, config: HostConfig, store: HostStore, host: LocalHost, signer: Any) -> Api:
    capabilities = effective_capabilities(config)
    descriptor = signer.published(config, store, capabilities)
    api = Api(config, store, host, signer, capabilities, descriptor, [], set())
    app[API_KEY] = api
    r = app.router
    r.add_get("/.well-known/henchline/v1/host", host_descriptor)
    r.add_get("/api/v1/health", health)
    r.add_post("/api/v1/pairing/claim", pairing_claim)
    r.add_get("/api/v1/capabilities", signed(capabilities_route))
    r.add_post("/api/v1/requests/{request_id}/ack", signed(acknowledge))
    r.add_delete("/api/v1/devices/{device_id}", signed(revoke_device))
    if capabilities.get("chat"):
        r.add_get("/api/v1/chats", signed(chats))
        r.add_get("/api/v1/chats/{session_id}/events", signed(chat_events))
        r.add_post("/api/v1/chats/{session_id}/messages", idempotent(send_message))
        r.add_post("/api/v1/chats/{session_id}/receipts", idempotent(receipts))
        app.on_startup.append(start_ledger)
        app.on_cleanup.append(stop_ledger)
    r.add_route("*", "/api/v1/{tail:.*}", not_found)  # last: everything else answers in the error envelope
    return api


def guard_legacy(app: web.Application, streamed: frozenset[str] = frozenset()) -> None:
    """The server's own routes (everything outside /api/v1) take a request signed exactly as /api/v1 wants it,
    when the host offers "legacy_routes". It is authenticated here, before the route runs, and kept on the
    request under auth.SIGNED: the route's Origin check (server.trusted) then lets it through as that phone.
    A request without a signature is left as it was (the route checks its Origin). One with a bad, replayed,
    revoked or unknown signature is answered here in the error envelope and never falls back to the Origin.
    streamed: routes that read their body as a stream (uploads, up to gigabytes): the body is left unread, the
    signature covers the digest the phone declares (X-Henchline-Content-SHA256), and the route keeps the upload
    only when what arrived has that digest (SignedRequest.body_matches).
    No idempotency here (unlike /api/v1's mutations): a nonce is never accepted twice, but a retry signed
    afresh runs the route again, exactly as an unsigned retry always has."""

    @web.middleware
    async def signed_legacy(request: web.Request, handler: Any) -> web.StreamResponse:
        if "X-Henchline-Signature" not in request.headers or request.path.startswith(("/api/v1/", "/.well-known/")):
            return await handler(request)
        api = request.app[API_KEY]
        try:
            if not api.capabilities.get("legacy_routes"):
                raise ApiError(404, "capability_unavailable", "this host takes signed requests only under /api/v1")
            resource = request.match_info.route.resource
            request[auth.SIGNED] = await auth.authenticate(
                request, api.config, api.store, streamed=resource is not None and resource.canonical in streamed)
        except ApiError as error:
            return error.response()
        return await handler(request)

    app.middlewares.append(signed_legacy)


# ---- plumbing -------------------------------------------------------------------------------------------

def ok(signed_request: SignedRequest | None, body: dict[str, Any], status: int = 200) -> web.Response:
    if signed_request is not None:
        body = {**body, "request_id": signed_request.request_id}
    return web.json_response(body, status=status)


def throttle(api: Api, request_id: str = "") -> None:
    now = time.monotonic()
    api.unsigned_calls[:] = [at for at in api.unsigned_calls if now - at < 60]
    if len(api.unsigned_calls) >= UNSIGNED_PER_MINUTE:
        raise ApiError(429, "rate_limited", "too many unauthenticated requests; try again in a minute",
                       retryable=True, request_id=request_id)
    api.unsigned_calls.append(now)


Handler = Callable[[web.Request, Api, SignedRequest], Awaitable[web.Response]]


def signed(handler: Handler) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def route(request: web.Request) -> web.Response:
        api = request.app[API_KEY]
        try:
            signed_request = await auth.authenticate(request, api.config, api.store)
            return await handler(request, api, signed_request)
        except ApiError as error:
            return error.response()
    return route


def idempotent(handler: Handler) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Signed, and done at most once per (device, request ID), forever.

    An identical retry gets the first answer back with "replayed": true; the same ID with other content is
    409 idempotency_mismatch; a retry while the first is still running is a retryable 409 state_conflict.
    A retryable error means nothing was done, so the ID is freed for the retry. Any other answer, errors
    included, is kept. A request left pending by a server that stopped mid-way (not running here now) may or
    may not have happened: it is settled once as a non-retryable 409 outcome_unknown, kept like any answer,
    so the phone can tell the user instead of retrying forever. Never done twice.
    """
    async def route(request: web.Request) -> web.Response:
        api = request.app[API_KEY]
        try:
            signed_request = await auth.authenticate(request, api.config, api.store)
        except ApiError as error:
            return error.response()
        device, rid = signed_request.device.device_id, signed_request.request_id
        key, run = (device, rid), asyncio.get_running_loop().run_in_executor
        # Claimed before the ledger is read (one event loop: check-and-add is atomic). A duplicate arriving
        # while this one runs is turned away here, so a pending row not claimed here really was left by a
        # server that stopped mid-way.
        if key in api.in_flight:
            return ApiError(409, "state_conflict", "this request is still being carried out",
                            retryable=True, request_id=rid).response()
        api.in_flight.add(key)
        try:
            held = await run(None, api.store.reserve, device, rid, signed_request.fingerprint)
            if held.state == "replay":
                return replayed(held.status, held.content_type, held.body)
            if held.state == "mismatch":
                return ApiError(409, "idempotency_mismatch", "request id was already used for different content",
                                request_id=rid).response()
            if held.state == "pending":
                unknown = ApiError(409, "outcome_unknown", "the host stopped while carrying this out; it may or "
                                   "may not have happened, so it will not be tried again under this request id",
                                   request_id=rid).response()
                await run(None, api.store.complete, device, rid, unknown.status, unknown.content_type, unknown.body)
                return unknown
            try:
                answer = await handler(request, api, signed_request)
            except ApiError as error:
                if error.retryable:
                    await run(None, api.store.abandon, device, rid)
                    return error.response()
                answer = error.response()
            except Exception:
                answer = ApiError(500, "internal_error", "the host failed while carrying this out; it may or may "
                                  "not have happened, and will not be tried again under this request id",
                                  request_id=rid).response()
                LOG.exception("api v1: %s %s failed", request.method, request.path)  # no body: it may be private
            await run(None, api.store.complete, device, rid, answer.status, answer.content_type, answer.body)
            return answer
        finally:
            api.in_flight.discard(key)
    return route


def replayed(status: int, content_type: str, body: bytes) -> web.Response:
    if content_type == "application/json":
        try:
            value = json.loads(body)
            if isinstance(value, dict) and "error" not in value:
                value["replayed"] = True
            return web.json_response(value, status=status)
        except ValueError:
            pass
    return web.Response(status=status, body=body, content_type=content_type)


def limit_of(request: web.Request, rid: str) -> int:
    text = request.query.get("limit", str(DEFAULT_LIMIT))
    if not text.isdigit() or not 1 <= int(text) <= MAX_LIMIT:
        raise ApiError(400, "invalid_request", f"limit must be 1..{MAX_LIMIT}", request_id=rid)
    return int(text)


def session_of(request: web.Request, rid: str) -> str:
    # aiohttp's match_info has %2F decoded already; the raw segment keeps a session id's own slashes apart
    raw = request.raw_path.split("?", 1)[0].split("/")
    session_id = unquote(raw[4]) if len(raw) > 4 else ""
    if not SESSION_ID.fullmatch(session_id):
        raise ApiError(400, "invalid_request", "invalid session id", request_id=rid)
    return session_id


def cursor_after(text: str, rid: str) -> tuple[float, str] | None:
    if not text:
        return None
    try:
        at, event_id = json.loads(unb64url(text))
        return float(at), str(event_id)
    except (ValueError, TypeError):
        raise ApiError(400, "invalid_request", "invalid cursor", request_id=rid) from None


def cursor_of(row: Any) -> str:
    return b64url(json.dumps([row["created_at"], row["event_id"]]).encode())


# ---- unsigned -------------------------------------------------------------------------------------------

async def health(request: web.Request) -> web.Response:
    api = request.app[API_KEY]
    try:
        throttle(api)
    except ApiError as error:
        return error.response()
    return web.json_response({"status": "ok", "api_version": api.config.api_version, "build_id": BUILD_ID,
                              "server_time": int(time.time())})


async def host_descriptor(request: web.Request) -> web.Response:
    api = request.app[API_KEY]
    try:
        throttle(api)
    except ApiError as error:
        return error.response()
    etag = f'"{api.descriptor["descriptor_seq"]}-{api.descriptor["signature"][:16]}"'
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers={"ETag": etag})
    return web.json_response({"descriptor": api.descriptor,
                              "pairing": {"supported": True, "device_key_algorithms": ["ed25519", "p256"]}},
                             headers={"ETag": etag, "Cache-Control": "no-cache"})


async def pairing_claim(request: web.Request) -> web.Response:
    """{nonce, algorithm: ed25519|p256, public_key: base64url (raw Ed25519 or P-256 SubjectPublicKeyInfo DER),
    proof: base64url signature by that key over canonical.pairing_bytes, label}: one enrolled phone key and its
    new device ID, with the signed descriptor to check against the QR. The user is told of every enrolment."""
    api = request.app[API_KEY]
    try:
        throttle(api)
        try:
            body = await request.json()
            nonce, algorithm = str(body["nonce"]), str(body["algorithm"])
            encoded, proof = str(body["public_key"]), str(body["proof"])
            label = " ".join(str(body.get("label", "")).split())[:80]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ApiError(400, "invalid_request", "expected {nonce, algorithm, public_key, proof, label}") from None
        run = asyncio.get_running_loop().run_in_executor
        try:
            public_key = unb64url(encoded)
            auth.verify_signature(
                Device("", algorithm, public_key, None), unb64url(proof),
                pairing_bytes(host_id=api.config.host_id, nonce=nonce, algorithm=algorithm, public_key=encoded))
        except (ValueError, ApiError):  # checked before the nonce is spent: a bad claim costs one attempt
            await run(None, api.store.pairing_failure, nonce)
            raise ApiError(422, "validation_failed", "public key or its proof of possession is invalid") from None
        try:
            device = await run(None, lambda: api.store.claim_pairing(nonce, algorithm, public_key, label))
        except (LookupError, TimeoutError, PermissionError):  # unknown, used, expired or locked: all the same
            raise ApiError(410, "pairing_expired", "pairing code is not valid any more; show a new one") from None
    except ApiError as error:
        return error.response()
    # Told after the answer is on its way, not before: a slow text must not time the phone out into a retry
    # that finds its code spent and never learns its device ID.
    words = (f"A phone was paired with this computer for Henchline: {label or 'no name'}, device "
             f"{device.device_id[:8]}. If that wasn't you: voice-call devices revoke {device.device_id}")
    task = asyncio.get_running_loop().run_in_executor(None, api.host.announce, words)
    task.add_done_callback(lambda done: done.exception() and LOG.warning("enrolment not announced: %s",
                                                                         done.exception()))
    return web.json_response({"host_id": api.config.host_id, "device_id": device.device_id,
                              "descriptor": api.descriptor}, status=201)


async def not_found(request: web.Request) -> web.Response:
    return ApiError(404, "not_found", "no such route in this host's API").response()


# ---- signed ---------------------------------------------------------------------------------------------

async def capabilities_route(request: web.Request, api: Api, signed_request: SignedRequest) -> web.Response:
    return ok(signed_request, {"host_id": api.config.host_id, "descriptor": api.descriptor})


async def acknowledge(request: web.Request, api: Api, signed_request: SignedRequest) -> web.Response:
    """The phone has its answer: the stored result may be compacted later, but never executed again."""
    target = request.match_info["request_id"].lower()
    if not await asyncio.get_running_loop().run_in_executor(
            None, api.store.acknowledge, signed_request.device.device_id, target):
        raise ApiError(404, "not_found", "no finished request with that id from this device",
                       request_id=signed_request.request_id)
    return ok(signed_request, {"host_id": api.config.host_id, "acknowledged": target})


async def revoke_device(request: web.Request, api: Api, signed_request: SignedRequest) -> web.Response:
    """A phone revokes itself before forgetting this host. Another device's ID is refused: that is the
    host owner's `voice-call devices revoke`."""
    target = request.match_info["device_id"].lower()
    if target != signed_request.device.device_id:
        raise ApiError(403, "capability_denied", "a device may revoke only itself",
                       request_id=signed_request.request_id)
    await asyncio.get_running_loop().run_in_executor(None, api.store.revoke, target)
    return ok(signed_request, {"host_id": api.config.host_id, "device_id": target, "revoked": True})


async def chats(request: web.Request, api: Api, signed_request: SignedRequest) -> web.Response:
    rid = signed_request.request_id
    limit = limit_of(request, rid)
    start = request.query.get("cursor", "")
    if start and not start.isdigit():
        raise ApiError(400, "invalid_request", "invalid cursor", request_id=rid)
    listed = await asyncio.get_running_loop().run_in_executor(None, api.host.chats)
    offset = int(start or 0)
    page = listed[offset:offset + limit]
    more = offset + limit < len(listed)
    return ok(signed_request, {"host_id": api.config.host_id,
                               "chats": [{**chat, "host_id": api.config.host_id} for chat in page],
                               "next_cursor": str(offset + limit) if more else None})


async def chat_events(request: web.Request, api: Api, signed_request: SignedRequest) -> web.Response:
    rid = signed_request.request_id
    session_id = session_of(request, rid)
    limit = limit_of(request, rid)
    # Call lines only for a phone that asks for them (with=call_lines): an older one would draw an unknown kind as
    # a message of its own
    wanted = set(filter(None, request.query.get("with", "").split(",")))
    if wanted - {"call_lines"}:
        raise ApiError(400, "invalid_request", "with: call_lines is the only option", request_id=rid)
    without = () if "call_lines" in wanted else ("call_line",)
    rows = await asyncio.get_running_loop().run_in_executor(
        None, api.store.events, session_id, limit, cursor_after(request.query.get("cursor", ""), rid), without)
    # Each event's own cursor: a phone polling for what's new resumes after the last one it has (next_cursor is
    # only there when the page was full), a little earlier for a text that is filed late in its place
    events = [{"host_id": api.config.host_id, "session_id": session_id, "event_id": row["event_id"],
               "kind": row["kind"], **json.loads(row["body"]), "cursor": cursor_of(row)} for row in rows]
    return ok(signed_request, {"host_id": api.config.host_id, "session_id": session_id, "events": events,
                               "next_cursor": cursor_of(rows[-1]) if len(rows) == limit else None})


async def send_message(request: web.Request, api: Api, signed_request: SignedRequest) -> web.Response:
    rid = signed_request.request_id
    session_id = session_of(request, rid)
    body = auth.json_body(signed_request)
    if str(body.get("host_id", "")).lower() != api.config.host_id:
        raise ApiError(422, "validation_failed", "host_id names another host", request_id=rid)
    if body.get("session_id") != session_id:
        raise ApiError(422, "validation_failed", "session_id differs from the path", request_id=rid)
    client_message_id = str(body.get("client_message_id", ""))
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", client_message_id):
        raise ApiError(422, "validation_failed", "client_message_id must be a lowercase UUID", request_id=rid)
    content = body.get("content")
    if not isinstance(content, dict) or content.get("type") != "text" or not isinstance(content.get("text"), str):
        raise ApiError(422, "validation_failed", 'content must be {"type": "text", "text": ...}', request_id=rid)
    text = content["text"].replace("\r\n", "\n").replace("\r", "\n").strip()  # its own line breaks kept
    if not text or len(text) > MAX_TEXT:
        raise ApiError(422, "validation_failed", f"text must be 1..{MAX_TEXT} characters", request_id=rid)
    got, why = await asyncio.get_running_loop().run_in_executor(None, api.host.deliver, text, session_id)
    now = time.time()
    event = {"client_message_id": client_message_id, "state": "delivered" if got else "failed",
             "content": {"type": "text", "text": text}, "created_at": utc(now),
             **({"delivered_to": got} if got else {"note": why})}
    event_id = await asyncio.get_running_loop().run_in_executor(
        None, lambda: api.store.append_event(session_id, "message", json.dumps(event).encode(), now=now))
    return ok(signed_request, {"host_id": api.config.host_id, "session_id": session_id, "event_id": event_id,
                               **event}, status=201 if got else 200)


def utc(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))



# ---- the reply ledger: agents' texts kept as chat events -----------------------------------------------------

MAX_FROM = 64  # characters of a call line's speaker


def record_call_line(api: Api, chat: dict[str, Any], now: float | None = None) -> str | None:
    """A line said on a call (your words as heard, an agent's talkback, a line typed in its terminal), kept in its
    chat's events as kind call_line ({from, text, created_at}), so a paired phone can read the call afterwards.
    Not an agent's text (tid: ingested from texts/ already), a file (its link is a secret) or a placeholder with no
    words. A phone message sent over this API never comes here: it is delivered as a text (Heard.TEXTED), which the
    call's chat doesn't repeat. A talkback clip is kept once however often it is sent (source clip:<name>). The new
    event's id, or None when the line isn't kept. Pruned with the rest after ledger_days."""
    session, text = str(chat.get("session", "")), chat.get("text")
    if (chat.get("tid") or chat.get("file") or chat.get("placeholder") or not SESSION_ID.fullmatch(session)
            or not isinstance(text, str) or not text.strip()):
        return None
    at = time.time() if now is None else now
    body = {"from": str(chat.get("from", ""))[:MAX_FROM], "text": text[:MAX_TEXT], "created_at": utc(at)}
    clip = chat.get("clip")
    source = f"clip:{clip}" if isinstance(clip, str) and clip else None
    return api.store.append_event(session, "call_line", json.dumps(body).encode(), now=at, source=source)


def ingest(api: Api, now: float | None = None) -> int:
    """Each agent text this host keeps, added once to its chat's events (kind agent_text). Safe to run again and
    again, and after a restart: the text's id is the event's source, kept unique. Texts older than ledger_days are
    left out, so pruning and ingesting never take turns over the same old text. How many were new."""
    added = 0
    cutoff = (time.time() if now is None else now) - api.config.ledger_days * 86400
    for record in api.host.texts():
        tid, words, session = str(record.get("id", "")), record.get("words"), str(record.get("session", ""))
        at = record.get("at")
        if (not TEXT_ID.fullmatch(tid) or not isinstance(words, str) or not words.strip()
                or not SESSION_ID.fullmatch(session) or not isinstance(at, (int, float)) or isinstance(at, bool)
                or at < cutoff):
            continue
        body = {"agent": str(record.get("agent", "")), "text": words, "text_id": tid, "created_at": utc(at)}
        if api.store.append_event(session, "agent_text", json.dumps(body).encode(), now=float(at),
                                  source=f"text:{tid}") is not None:
            added += 1
    return added


async def keep_ledger(api: Api) -> None:
    loop = asyncio.get_running_loop()
    pruned_at = 0.0
    while True:
        try:
            await loop.run_in_executor(None, ingest, api)
            if time.monotonic() - pruned_at > PRUNE_EVERY:
                await loop.run_in_executor(None, api.store.prune_events, time.time() - api.config.ledger_days * 86400)
                pruned_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:  # the loop outlives any one bad record or a busy database
            LOG.exception("api v1: ledger")
        await asyncio.sleep(INGEST_EVERY)


LEDGER_KEY = web.AppKey("henchline_api_v1_ledger", asyncio.Task)


async def start_ledger(app: web.Application) -> None:
    app[LEDGER_KEY] = asyncio.create_task(keep_ledger(app[API_KEY]))


async def stop_ledger(app: web.Application) -> None:
    app[LEDGER_KEY].cancel()


async def receipts(request: web.Request, api: Api, signed_request: SignedRequest) -> web.Response:
    """{host_id, session_id, event_ids: [...], state: delivered | read}: the agents' texts among those events,
    moved forward on this host as the phone's notification would (a read text stops an until-read ring)."""
    rid = signed_request.request_id
    session_id = session_of(request, rid)
    body = auth.json_body(signed_request)
    if str(body.get("host_id", "")).lower() != api.config.host_id:
        raise ApiError(422, "validation_failed", "host_id names another host", request_id=rid)
    if body.get("session_id") != session_id:
        raise ApiError(422, "validation_failed", "session_id differs from the path", request_id=rid)
    state, ids = body.get("state"), body.get("event_ids")
    if state not in ("delivered", "read"):
        raise ApiError(422, "validation_failed", "state is delivered or read", request_id=rid)
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_LIMIT or not all(isinstance(i, str) for i in ids):
        raise ApiError(422, "validation_failed", f"event_ids is a list of 1..{MAX_LIMIT} ids", request_id=rid)
    loop = asyncio.get_running_loop()
    marked, unknown = [], []
    for event_id in dict.fromkeys(ids):
        row = await loop.run_in_executor(None, api.store.event, event_id)
        if row is None or row["session_id"] != session_id or row["kind"] != "agent_text":
            unknown.append(event_id)
            continue
        tid = json.loads(row["body"]).get("text_id", "")
        await loop.run_in_executor(None, api.host.mark_text, tid, state)
        marked.append(event_id)
    return ok(signed_request, {"host_id": api.config.host_id, "session_id": session_id, "state": state,
                               "marked": marked, "unknown": unknown})
