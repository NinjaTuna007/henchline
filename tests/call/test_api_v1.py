"""call/api_v1.py: pairing, signed reads, and messages that are delivered at most once however often retried."""

import asyncio
import json
import tempfile
import threading
import time
import uuid
from pathlib import Path

from tests import sandbox

import unittest

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

from call import api_v1, canonical
from call.host_store import HostStore
from call.pair import HostSigner
from tests.call import hostkit


class FakeHost:
    def __init__(self):
        self.delivered, self.announced, self.marked = [], [], []
        self.texts_kept = []
        self.reachable = {"claude-1", "a/b", "żółw"}
        self.fail_with = None

    def deliver(self, text, session_id):
        if self.fail_with:
            raise self.fail_with
        if session_id not in self.reachable:
            return "", "That agent isn't reachable."
        self.delivered.append((text, session_id))
        return "Claude", ""

    def chats(self):
        return [{"session_id": f"claude-{i}", "name": f"Claude {i}"} for i in range(5)]

    def announce(self, words):
        self.announced.append(words)

    texts_kept: list = []

    def texts(self):
        return list(self.texts_kept)

    def mark_text(self, text_id, status):
        self.marked.append((text_id, status))
        return True


class Base(AioHTTPTestCase):
    capabilities = {"chat": True, "calls": True}

    async def get_application(self):
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.config = hostkit.config(folder, capabilities=self.capabilities)
        self.store = HostStore(self.config.database)
        self.signer = HostSigner.load_or_create(self.config.key_file)
        self.host = FakeHost()
        app = web.Application()
        self.api = api_v1.setup(app, self.config, self.store, self.host, self.signer)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.store.close()

    async def pair(self, key=None, algorithm="ed25519", label="Test phone"):
        key = key or hostkit.ed25519_key()
        nonce = self.store.new_pairing()
        public = canonical.b64url(hostkit.public_bytes(key))
        proof = hostkit.sign(key, canonical.pairing_bytes(host_id=self.config.host_id, nonce=nonce,
                                                          algorithm=algorithm, public_key=public))
        answer = await self.client.post("/api/v1/pairing/claim", json={
            "nonce": nonce, "algorithm": algorithm, "public_key": public, "proof": canonical.b64url(proof),
            "label": label})
        return key, answer

    async def enrolled(self):
        key, answer = await self.pair()
        self.assertEqual(answer.status, 201)
        return key, (await answer.json())["device_id"]

    async def call(self, key, device, method, path, body=None, request_id=None, **sign):
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        headers = hostkit.signed_headers(key, device, method, path, data, request_id=request_id, **sign)
        answer = await self.client.request(method, path, data=data, headers=headers)
        return answer.status, await answer.json()

    def message(self, session="claude-1", text="hello", **changes):
        return {"host_id": self.config.host_id, "session_id": session,
                "client_message_id": str(uuid.uuid4()), "content": {"type": "text", "text": text}, **changes}


class Unsigned(Base):
    async def test_health_says_little_and_tells_the_time(self):
        answer = await self.client.get("/api/v1/health")
        body = await answer.json()
        self.assertEqual(set(body), {"status", "api_version", "build_id", "server_time"})
        self.assertAlmostEqual(body["server_time"], time.time(), delta=5)

    async def test_the_descriptor_is_signed_and_advertises_only_what_is_served(self):
        answer = await self.client.get("/.well-known/henchline/v1/host")
        body = await answer.json()
        descriptor = dict(body["descriptor"])
        signature = canonical.unb64url(descriptor.pop("signature"))
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(self.signer.public_bytes()).verify(
            signature, canonical.canonical_json(descriptor))
        self.assertTrue(descriptor["capabilities"]["chat"])
        self.assertFalse(descriptor["capabilities"]["calls"])  # enabled in host.json, not built yet
        again = await self.client.get("/.well-known/henchline/v1/host",
                                      headers={"If-None-Match": answer.headers["ETag"]})
        self.assertEqual(again.status, 304)

    async def test_unsigned_requests_are_rate_limited(self):
        for _ in range(api_v1.UNSIGNED_PER_MINUTE):
            await self.client.get("/api/v1/health")
        answer = await self.client.get("/api/v1/health")
        body = await answer.json()
        self.assertEqual((answer.status, body["error"]["code"], body["error"]["retryable"]),
                         (429, "rate_limited", True))

    async def test_unknown_routes_answer_in_the_envelope(self):
        answer = await self.client.post("/api/v1/calls")
        self.assertEqual((answer.status, (await answer.json())["error"]["code"]), (404, "not_found"))

    async def test_signed_routes_refuse_an_unsigned_request(self):
        answer = await self.client.get("/api/v1/chats")
        self.assertEqual(answer.status, 401)


class Pairing(Base):
    async def test_a_claim_enrolls_and_tells_the_user(self):
        key, answer = await self.pair(label="Pixel\nphone")
        body = await answer.json()
        for _ in range(50):  # told in the background, after the answer
            if self.host.announced:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(answer.status, 201)
        self.assertEqual(body["host_id"], self.config.host_id)
        self.assertEqual(body["descriptor"]["host_signing_key"], canonical.b64url(self.signer.public_bytes()))
        self.assertEqual(self.store.device(body["device_id"]).public_key, hostkit.public_bytes(key))
        self.assertEqual(len(self.host.announced), 1)
        self.assertIn("Pixel phone", self.host.announced[0])
        self.assertIn(f"voice-call devices revoke {body['device_id']}", self.host.announced[0])

    async def test_a_slow_announcement_does_not_hold_the_answer(self):
        release = threading.Event()
        self.host.announce = lambda words: release.wait(5)
        _, answer = await asyncio.wait_for(self.pair(), 2)
        self.assertEqual(answer.status, 201)
        release.set()

    async def test_a_p256_phone_can_pair_and_sign(self):
        key, answer = await self.pair(hostkit.p256_key(), "p256")
        self.assertEqual(answer.status, 201)
        status, _ = await self.call(key, (await answer.json())["device_id"], "GET", "/api/v1/capabilities")
        self.assertEqual(status, 200)

    async def test_a_proof_from_another_key_fails_and_costs_an_attempt(self):
        nonce = self.store.new_pairing()
        key, other = hostkit.ed25519_key(), hostkit.ed25519_key()
        public = canonical.b64url(hostkit.public_bytes(key))
        proof = canonical.b64url(other.sign(canonical.pairing_bytes(
            host_id=self.config.host_id, nonce=nonce, algorithm="ed25519", public_key=public)))
        for _ in range(5):
            answer = await self.client.post("/api/v1/pairing/claim", json={
                "nonce": nonce, "algorithm": "ed25519", "public_key": public, "proof": proof})
            self.assertEqual((await answer.json())["error"]["code"], "validation_failed")
        with self.assertRaises(PermissionError):
            self.store.claim_pairing(nonce, "ed25519", hostkit.public_bytes(key))
        self.assertEqual(self.host.announced, [])

    async def test_a_proof_for_another_host_fails(self):
        nonce = self.store.new_pairing()
        key = hostkit.ed25519_key()
        public = canonical.b64url(hostkit.public_bytes(key))
        proof = canonical.b64url(key.sign(canonical.pairing_bytes(
            host_id=hostkit.OTHER_HOST_ID, nonce=nonce, algorithm="ed25519", public_key=public)))
        answer = await self.client.post("/api/v1/pairing/claim", json={
            "nonce": nonce, "algorithm": "ed25519", "public_key": public, "proof": proof})
        self.assertEqual(answer.status, 422)

    async def test_a_used_or_made_up_code_is_expired(self):
        key = hostkit.ed25519_key()
        public = canonical.b64url(hostkit.public_bytes(key))
        proof = canonical.b64url(key.sign(canonical.pairing_bytes(
            host_id=self.config.host_id, nonce="made-up", algorithm="ed25519", public_key=public)))
        answer = await self.client.post("/api/v1/pairing/claim", json={
            "nonce": "made-up", "algorithm": "ed25519", "public_key": public, "proof": proof})
        self.assertEqual((answer.status, (await answer.json())["error"]["code"]), (410, "pairing_expired"))

    async def test_a_claim_without_proof_is_malformed(self):
        answer = await self.client.post("/api/v1/pairing/claim", json={
            "nonce": self.store.new_pairing(), "algorithm": "ed25519", "public_key": "AA"})
        self.assertEqual(answer.status, 400)


class Reads(Base):
    async def test_chats_are_paged_and_host_qualified(self):
        key, device = await self.enrolled()
        status, body = await self.call(key, device, "GET", "/api/v1/chats?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual([c["session_id"] for c in body["chats"]], ["claude-0", "claude-1"])
        self.assertTrue(all(c["host_id"] == self.config.host_id for c in body["chats"]))
        self.assertEqual(body["host_id"], self.config.host_id)
        self.assertIn("request_id", body)
        status, body = await self.call(key, device, "GET", f"/api/v1/chats?limit=2&cursor={body['next_cursor']}")
        self.assertEqual([c["session_id"] for c in body["chats"]], ["claude-2", "claude-3"])
        status, body = await self.call(key, device, "GET", "/api/v1/chats?limit=2&cursor=4")
        self.assertIsNone(body["next_cursor"])

    async def test_bad_limits_and_cursors(self):
        key, device = await self.enrolled()
        for query in ("limit=0", "limit=201", "limit=x", "cursor=-1"):
            with self.subTest(query=query):
                status, body = await self.call(key, device, "GET", f"/api/v1/chats?{query}")
                self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))

    async def test_a_skewed_clock_learns_the_hosts_time(self):
        key, device = await self.enrolled()
        status, body = await self.call(key, device, "GET", "/api/v1/chats", timestamp=int(time.time()) - 600)
        self.assertEqual(status, 401)
        self.assertAlmostEqual(body["error"]["server_time"], time.time(), delta=5)


class Messages(Base):
    async def test_a_message_is_delivered_and_recorded(self):
        key, device = await self.enrolled()
        message = self.message(text="  hello\r\n there \n")
        status, body = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message)
        self.assertEqual(status, 201, body)
        self.assertEqual(self.host.delivered, [("hello\n there", "claude-1")])
        self.assertEqual((body["host_id"], body["session_id"], body["client_message_id"], body["state"]),
                         (self.config.host_id, "claude-1", message["client_message_id"], "delivered"))
        status, events = await self.call(key, device, "GET", "/api/v1/chats/claude-1/events")
        self.assertEqual([e["event_id"] for e in events["events"]], [body["event_id"]])
        self.assertEqual(events["events"][0]["content"]["text"], "hello\n there")

    async def test_a_retry_is_answered_from_the_ledger_not_delivered_again(self):
        key, device = await self.enrolled()
        message, rid = json.dumps(self.message()).encode(), str(uuid.uuid4())
        _, first = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message, rid)
        status, again = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message, rid)
        self.assertEqual(status, 201)
        self.assertTrue(again.pop("replayed"))
        self.assertEqual(again, first)
        self.assertEqual(len(self.host.delivered), 1)

    async def test_a_retry_after_a_restart_is_still_not_delivered_again(self):
        key, device = await self.enrolled()
        message, rid = json.dumps(self.message()).encode(), str(uuid.uuid4())
        await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message, rid)
        self.store.close()
        self.api.store = self.store = HostStore(self.config.database)
        status, again = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message, rid)
        self.assertEqual((status, again["replayed"], len(self.host.delivered)), (201, True, 1))

    async def test_other_content_under_the_same_request_id_conflicts(self):
        key, device = await self.enrolled()
        rid = str(uuid.uuid4())
        await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", self.message(), rid)
        status, body = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages",
                                       self.message(text="other"), rid)
        self.assertEqual((status, body["error"]["code"]), (409, "idempotency_mismatch"))
        self.assertEqual(len(self.host.delivered), 1)

    async def test_an_unreachable_agent_is_a_recorded_failure(self):
        key, device = await self.enrolled()
        status, body = await self.call(key, device, "POST", "/api/v1/chats/nobody/messages",
                                       self.message(session="nobody"))
        self.assertEqual((status, body["state"]), (200, "failed"))
        self.assertIn("reachable", body["note"])

    async def test_the_body_must_name_this_host_and_the_paths_chat(self):
        key, device = await self.enrolled()
        for message in (self.message(host_id=hostkit.OTHER_HOST_ID), self.message(session="claude-2"),
                        self.message(client_message_id="x"), self.message(content={"type": "shell", "text": "rm"}),
                        self.message(text="   ")):
            with self.subTest(message=message):
                status, body = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message)
                self.assertEqual((status, body["error"]["code"]), (422, "validation_failed"))
        self.assertEqual(self.host.delivered, [])

    async def test_session_ids_keep_their_encoded_slashes_and_unicode(self):
        key, device = await self.enrolled()
        for path, session in (("a%2Fb", "a/b"), ("%C5%BC%C3%B3%C5%82w", "żółw")):
            with self.subTest(session=session):
                status, body = await self.call(key, device, "POST", f"/api/v1/chats/{path}/messages",
                                               self.message(session=session))
                self.assertEqual((status, body["session_id"]), (201, session), body)
        self.assertEqual([s for _, s in self.host.delivered], ["a/b", "żółw"])

    async def test_a_crash_mid_delivery_is_never_retried(self):
        key, device = await self.enrolled()
        message, rid = json.dumps(self.message()).encode(), str(uuid.uuid4())
        self.host.fail_with = RuntimeError("tmux vanished")
        with self.assertLogs("henchline.api_v1", "ERROR"):
            status, body = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message, rid)
        self.assertEqual((status, body["error"]["code"]), (500, "internal_error"))
        self.host.fail_with = None
        status, body = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message, rid)
        self.assertEqual((status, body["error"]["code"]), (500, "internal_error"))
        self.assertEqual(self.host.delivered, [])

    async def test_a_request_left_pending_by_a_stopped_server_is_settled_as_unknown(self):
        key, device = await self.enrolled()
        message, rid = json.dumps(self.message()).encode(), str(uuid.uuid4())
        path = "/api/v1/chats/claude-1/messages"
        self.store.reserve(device, rid, canonical.request_fingerprint("POST", path, "", message))
        for _ in range(2):
            status, body = await self.call(key, device, "POST", path, message, rid)
            self.assertEqual((status, body["error"]["code"], body["error"]["retryable"]),
                             (409, "outcome_unknown", False))
        self.assertEqual(self.host.delivered, [])

    async def test_a_request_in_flight_here_is_a_retryable_conflict(self):
        key, device = await self.enrolled()
        message, rid = json.dumps(self.message()).encode(), str(uuid.uuid4())
        path = "/api/v1/chats/claude-1/messages"
        self.store.reserve(device, rid, canonical.request_fingerprint("POST", path, "", message))
        self.api.in_flight.add((device, rid))
        status, body = await self.call(key, device, "POST", path, message, rid)
        self.assertEqual((status, body["error"]["code"], body["error"]["retryable"]), (409, "state_conflict", True))

    async def test_a_duplicate_during_delivery_waits_and_the_first_completes(self):
        key, device = await self.enrolled()
        message, rid = json.dumps(self.message()).encode(), str(uuid.uuid4())
        path = "/api/v1/chats/claude-1/messages"
        started, release = threading.Event(), threading.Event()
        deliver = self.host.deliver

        def slow(text, session_id):
            started.set()
            release.wait(5)
            return deliver(text, session_id)
        self.host.deliver = slow
        first = asyncio.ensure_future(self.call(key, device, "POST", path, message, rid))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        status, body = await self.call(key, device, "POST", path, message, rid)
        self.assertEqual((status, body["error"]["code"], body["error"]["retryable"]), (409, "state_conflict", True))
        release.set()
        status, body = await first
        self.assertEqual((status, body["state"]), (201, "delivered"))
        status, again = await self.call(key, device, "POST", path, message, rid)
        self.assertEqual((status, again["replayed"], len(self.host.delivered)), (201, True, 1))

    async def test_events_page_with_a_cursor(self):
        key, device = await self.enrolled()
        for i in range(3):
            await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", self.message(text=f"m{i}"))
        _, page = await self.call(key, device, "GET", "/api/v1/chats/claude-1/events?limit=2")
        self.assertEqual([e["content"]["text"] for e in page["events"]], ["m0", "m1"])
        _, rest = await self.call(key, device, "GET",
                                  f"/api/v1/chats/claude-1/events?limit=2&cursor={page['next_cursor']}")
        self.assertEqual([e["content"]["text"] for e in rest["events"]], ["m2"])
        self.assertIsNone(rest["next_cursor"])
        # A part page still says where each event is: polling resumes after the last one held, sees only what's new
        _, none_new = await self.call(key, device, "GET",
                                      f"/api/v1/chats/claude-1/events?cursor={rest['events'][-1]['cursor']}")
        self.assertEqual(none_new["events"], [])
        await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", self.message(text="m3"))
        _, new = await self.call(key, device, "GET",
                                 f"/api/v1/chats/claude-1/events?cursor={rest['events'][-1]['cursor']}")
        self.assertEqual([e["content"]["text"] for e in new["events"]], ["m3"])
        self.assertEqual(page["events"][-1]["cursor"], page["next_cursor"])
        status, _ = await self.call(key, device, "GET", "/api/v1/chats/claude-1/events?cursor=bogus")
        self.assertEqual(status, 400)


class Devices(Base):
    async def test_a_phone_revokes_itself_and_is_then_refused(self):
        key, device = await self.enrolled()
        status, body = await self.call(key, device, "DELETE", f"/api/v1/devices/{device}")
        self.assertEqual((status, body["revoked"]), (200, True))
        status, body = await self.call(key, device, "GET", "/api/v1/capabilities")
        self.assertEqual((status, body["error"]["code"]), (403, "device_revoked"))

    async def test_a_phone_cannot_revoke_another(self):
        key, device = await self.enrolled()
        _, other = await self.enrolled()
        status, body = await self.call(key, device, "DELETE", f"/api/v1/devices/{other}")
        self.assertEqual((status, body["error"]["code"]), (403, "capability_denied"))
        self.assertIsNone(self.store.device(other).revoked_at)

    async def test_acknowledging_keeps_the_answer(self):
        key, device = await self.enrolled()
        message, rid = json.dumps(self.message()).encode(), str(uuid.uuid4())
        await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message, rid)
        status, body = await self.call(key, device, "POST", f"/api/v1/requests/{rid}/ack")
        self.assertEqual((status, body["acknowledged"]), (200, rid))
        status, again = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", message, rid)
        self.assertTrue(again["replayed"])
        status, _ = await self.call(key, device, "POST", f"/api/v1/requests/{uuid.uuid4()}/ack")
        self.assertEqual(status, 404)


def text(tid, session="claude-1", words="Tests pass.", at=1795000000.0, agent="claude"):
    return {"id": tid, "agent": agent, "session": session, "words": words, "at": at, "status": "sent"}


class Ledger(Base):
    async def events(self, key, device, session="claude-1"):
        status, body = await self.call(key, device, "GET", f"/api/v1/chats/{session}/events")
        self.assertEqual(status, 200, body)
        return body["events"]

    async def test_texts_are_kept_once_however_often_ingested(self):
        key, device = await self.enrolled()
        self.host.texts_kept = [text("a" * 16, at=1795000010.0), text("b" * 16, words="Second.", at=1795000020.0)]
        self.assertEqual(api_v1.ingest(self.api), 2)
        self.assertEqual(api_v1.ingest(self.api), 0)  # a rescan
        self.store.close()
        self.api.store = self.store = HostStore(self.config.database)  # a restart
        self.assertEqual(api_v1.ingest(self.api), 0)
        events = [e for e in await self.events(key, device) if e["kind"] == "agent_text"]
        self.assertEqual([(e["text"], e["text_id"], e["agent"]) for e in events],
                         [("Tests pass.", "a" * 16, "claude"), ("Second.", "b" * 16, "claude")])
        self.assertEqual(events[0]["created_at"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1795000010)))

    async def test_a_late_older_text_takes_its_place(self):
        key, device = await self.enrolled()
        self.host.texts_kept = [text("b" * 16, words="Later.", at=1795000020.0)]
        api_v1.ingest(self.api)
        self.host.texts_kept.append(text("a" * 16, words="Earlier.", at=1795000010.0))
        api_v1.ingest(self.api)
        self.assertEqual([e["text"] for e in await self.events(key, device)], ["Earlier.", "Later."])

    async def test_records_that_are_not_texts_are_skipped(self):
        self.host.texts_kept = [text("short"), text("c" * 16, words=""), text("d" * 16, session="bad\nid"),
                                {**text("e" * 16), "at": "yesterday"}, {**text("f" * 16), "words": None},
                                text("0" * 16, session="other")]
        self.assertEqual(api_v1.ingest(self.api), 1)
        self.assertEqual([r["session_id"] for r in self.store.events("other")], ["other"])

    async def test_the_loop_ingests_as_the_server_runs(self):
        key, device = await self.enrolled()
        self.host.texts_kept = [text("a" * 16)]
        for _ in range(50):  # the first pass runs at once, then every INGEST_EVERY seconds
            if self.store.events("claude-1"):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(len(await self.events(key, device)), 1)

    async def test_read_receipts_move_texts_forward_once(self):
        key, device = await self.enrolled()
        self.host.texts_kept = [text("a" * 16)]
        api_v1.ingest(self.api)
        status, sent = await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", self.message())
        [agent_event] = [e for e in await self.events(key, device) if e["kind"] == "agent_text"]
        body = {"host_id": self.config.host_id, "session_id": "claude-1", "state": "read",
                "event_ids": [agent_event["event_id"], sent["event_id"], "no-such-event"]}
        rid = str(uuid.uuid4())
        status, answer = await self.call(key, device, "POST", "/api/v1/chats/claude-1/receipts", body, rid)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["marked"], [agent_event["event_id"]])
        self.assertEqual(answer["unknown"], [sent["event_id"], "no-such-event"])  # a message isn't a text
        self.assertEqual(self.host.marked, [("a" * 16, "read")])
        status, again = await self.call(key, device, "POST", "/api/v1/chats/claude-1/receipts", body, rid)
        self.assertTrue(again["replayed"])
        self.assertEqual(len(self.host.marked), 1)

    async def test_receipts_are_for_this_chat_and_host_only(self):
        key, device = await self.enrolled()
        self.host.texts_kept = [text("a" * 16, session="claude-2")]
        api_v1.ingest(self.api)
        [event] = self.store.events("claude-2")
        good = {"host_id": self.config.host_id, "session_id": "claude-1", "state": "read", "event_ids": [event["event_id"]]}
        status, answer = await self.call(key, device, "POST", "/api/v1/chats/claude-1/receipts", good)
        self.assertEqual((status, answer["marked"]), (200, []))  # another chat's text
        for bad in ({**good, "state": "seen"}, {**good, "event_ids": []}, {**good, "host_id": hostkit.OTHER_HOST_ID},
                    {**good, "session_id": "claude-2"}):
            status, answer = await self.call(key, device, "POST", "/api/v1/chats/claude-1/receipts", bad)
            self.assertEqual((status, answer["error"]["code"]), (422, "validation_failed"))
        self.assertEqual(self.host.marked, [])

    async def test_old_texts_are_neither_ingested_nor_kept(self):
        now = time.time()
        self.host.texts_kept = [text("a" * 16, at=now - 40 * 86400), text("b" * 16, at=now - 29 * 86400)]
        self.assertEqual(api_v1.ingest(self.api, now=now), 1)  # older than ledger_days (30): left out
        self.assertEqual(self.store.prune_events(now + 2 * 86400 - self.config.ledger_days * 86400), 1)  # two days on
        self.assertEqual(self.store.events("claude-1"), [])
        self.assertEqual(api_v1.ingest(self.api, now=now + 2 * 86400), 0)  # no churn: pruned stays out


class NoChat(Base):
    capabilities = {"chat": False}

    async def test_a_disabled_capability_has_no_routes(self):
        key, device = await self.enrolled()
        status, body = await self.call(key, device, "GET", "/api/v1/chats")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))

class CallLines(Base):
    """What is said on a call, kept as its chat's call_line events and served only to a phone that asks."""

    def line(self, text="hello", session="claude-1", **extra):
        return {"from": "Claude", "text": text, "session": session, **extra}

    async def test_lines_are_kept_but_never_texts_files_or_bad_ones(self):
        # Recent times: the app's own ledger loop prunes anything older than ledger_days meanwhile
        at = float(int(time.time()))
        self.assertIsNotNone(api_v1.record_call_line(self.api, self.line("one"), now=at))
        for skipped in (self.line(tid="0123456789abcdef"), self.line(file={"url": "/files/s/x"}), self.line(session=""),
                        self.line(session="a\nb"), self.line(text="  "), self.line(text=None)):
            with self.subTest(line=skipped):
                self.assertIsNone(api_v1.record_call_line(self.api, skipped, now=at + 1))
        rows = self.store.events("claude-1", 50)
        self.assertEqual([(r["kind"], json.loads(r["body"])["text"]) for r in rows], [("call_line", "one")])
        self.assertEqual(json.loads(rows[0]["body"]),
                         {"from": "Claude", "text": "one", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))})

    async def test_served_only_when_asked_for_in_order_and_paged(self):
        key, device = await self.enrolled()
        now = time.time()
        for i in range(3):
            api_v1.record_call_line(self.api, self.line(f"said {i}"), now=now - 10 + i)  # before the messages
        for i in range(2):
            await self.call(key, device, "POST", "/api/v1/chats/claude-1/messages", self.message(text=f"m{i}"))
        _, plain = await self.call(key, device, "GET", "/api/v1/chats/claude-1/events?limit=2")
        # An older phone: as before, and a full page of what it knows (left out in the query, not after it)
        self.assertEqual([e["kind"] for e in plain["events"]], ["message", "message"])
        _, asked = await self.call(key, device, "GET", "/api/v1/chats/claude-1/events?with=call_lines")
        self.assertEqual([e.get("text") or e["content"]["text"] for e in asked["events"]],
                         ["said 0", "said 1", "said 2", "m0", "m1"])
        self.assertEqual(asked["events"][0]["from"], "Claude")
        status, body = await self.call(key, device, "GET", "/api/v1/chats/claude-1/events?with=everything")
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))

    async def test_only_a_paired_phone_reads_them_and_they_are_pruned(self):
        at = time.time() - 3600
        api_v1.record_call_line(self.api, self.line("an hour ago"), now=at)
        answer = await self.client.get("/api/v1/chats/claude-1/events?with=call_lines")
        self.assertEqual(answer.status, 401)
        self.assertEqual(self.store.prune_events(at + 1), 1)  # with the rest of the ledger
        self.assertEqual(self.store.events("claude-1", 50), [])

    async def test_a_clip_is_kept_once_and_a_placeholder_never(self):
        at = time.time()
        talk = self.line("Done, tests pass.", clip="0042-claude.json")
        self.assertIsNotNone(api_v1.record_call_line(self.api, talk, now=at))
        self.assertIsNone(api_v1.record_call_line(self.api, talk, now=at + 3))  # sent again after a resume
        self.assertIsNone(api_v1.record_call_line(self.api, {"from": "you", "text": "(spoken, sent to Claude)",
                                                             "session": "claude-1", "placeholder": True}, now=at))
        self.assertEqual([json.loads(r["body"])["text"] for r in self.store.events("claude-1", 50)], ["Done, tests pass."])


if __name__ == "__main__":
    unittest.main()
