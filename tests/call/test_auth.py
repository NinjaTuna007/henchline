"""call/auth.py: only a fresh, correctly signed request from an enrolled phone, for this host, gets through."""

import tempfile
import time
import unittest
from pathlib import Path

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

from call import auth
from call.host_store import HostStore
from tests.call import hostkit


class Authenticate(AioHTTPTestCase):
    async def get_application(self):
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.config = hostkit.config(folder)
        self.store = HostStore(self.config.database)
        self.key = hostkit.ed25519_key()
        self.device = self.store.claim_pairing(self.store.new_pairing(), "ed25519",
                                               hostkit.public_bytes(self.key)).device_id

        async def echo(request):
            try:
                signed = await auth.authenticate(request, self.config, self.store)
            except auth.ApiError as error:
                return error.response()
            return web.json_response({"device": signed.device.device_id, "request_id": signed.request_id,
                                      "fingerprint": signed.fingerprint, "body": signed.body.decode()})

        app = web.Application()
        app.router.add_route("*", "/api/v1/{tail:.*}", echo)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.store.close()

    async def send(self, method="POST", path="/api/v1/chats/claude-1/messages", body=b'{"a":1}', *,
                   key=None, device=None, headers=None, **sign):
        signed = hostkit.signed_headers(key or self.key, device or self.device, method, path, body, **sign)
        signed.update(headers or {})
        answer = await self.client.request(method, path, data=body, headers=signed)
        return answer.status, await answer.json()

    async def test_a_signed_request_passes(self):
        status, body = await self.send()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["device"], self.device)
        self.assertEqual(body["body"], '{"a":1}')

    async def test_query_and_encoded_paths_verify(self):
        status, body = await self.send("GET", "/api/v1/chats/a%2fb/events?limit=5&cursor=x%20y&cursor=a", b"")
        self.assertEqual(status, 200, body)

    async def test_a_tampered_body_fails(self):
        signed = hostkit.signed_headers(self.key, self.device, "POST", "/api/v1/x", b'{"a":1}')
        answer = await self.client.post("/api/v1/x", data=b'{"a":2}', headers=signed)
        self.assertEqual(answer.status, 401)
        self.assertEqual((await answer.json())["error"]["code"], "bad_signature")

    async def test_a_tampered_query_fails(self):
        signed = hostkit.signed_headers(self.key, self.device, "GET", "/api/v1/chats?limit=5")
        answer = await self.client.get("/api/v1/chats?limit=6", headers=signed)
        self.assertEqual(answer.status, 401)

    async def test_the_clock_window_is_120_seconds_each_way(self):
        now = int(time.time())
        for offset, ok in ((-110, True), (110, True), (-130, False), (130, False)):
            with self.subTest(offset=offset):
                status, body = await self.send(timestamp=now + offset)
                self.assertEqual(status == 200, ok, body)
                if not ok:
                    self.assertEqual(body["error"]["code"], "timestamp_out_of_window")

    async def test_odd_timestamp_spellings_fail(self):
        for text in ("+1795000000", "01795000000", "1795000000.0", "-1"):
            with self.subTest(text=text):
                status, body = await self.send(headers={"X-Henchline-Timestamp": text})
                self.assertEqual(body["error"]["code"], "timestamp_out_of_window")

    async def test_a_replayed_request_fails(self):
        signed = hostkit.signed_headers(self.key, self.device, "POST", "/api/v1/x", b"{}")
        first = await self.client.post("/api/v1/x", data=b"{}", headers=signed)
        again = await self.client.post("/api/v1/x", data=b"{}", headers=signed)
        self.assertEqual((first.status, again.status), (200, 401))
        self.assertEqual((await again.json())["error"]["code"], "bad_signature")

    async def test_a_bad_signature_does_not_burn_the_nonce(self):
        nonce = "AAECAwQFBgcICQoLDA0ODw"
        status, _ = await self.send(nonce=nonce, key=hostkit.ed25519_key())
        self.assertEqual(status, 401)
        status, body = await self.send(nonce=nonce)
        self.assertEqual(status, 200, body)

    async def test_a_request_for_another_host_fails(self):
        status, body = await self.send(host_id=hostkit.OTHER_HOST_ID)
        self.assertEqual((status, body["error"]["code"]), (401, "bad_signature"))

    async def test_a_signature_for_another_authority_fails(self):
        # Signed for host B's name but sent here with this host's ID: the authority is bound in.
        status, body = await self.send(authority="host-b.example.ts.net")
        self.assertEqual((status, body["error"]["code"]), (401, "bad_signature"))

    async def test_another_api_version_fails(self):
        status, body = await self.send(api_version="2.0")
        self.assertEqual((status, body["error"]["code"]), (412, "api_version_unsupported"))

    async def test_an_unknown_device_fails(self):
        status, body = await self.send(device="99999999-9999-4999-8999-999999999999")
        self.assertEqual(status, 401)

    async def test_another_devices_key_fails(self):
        status, body = await self.send(key=hostkit.ed25519_key())
        self.assertEqual(status, 401)

    async def test_a_revoked_device_fails(self):
        self.store.revoke(self.device)
        status, body = await self.send()
        self.assertEqual((status, body["error"]["code"]), (403, "device_revoked"))

    async def test_missing_headers_and_bad_ids_fail(self):
        status, body = await self.send(headers={"X-Henchline-Request-Id": "not-a-uuid"})
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))
        status, body = await self.send(headers={"X-Henchline-Nonce": "AAAA"})
        self.assertEqual((status, body["error"]["code"]), (401, "bad_signature"))
        answer = await self.client.post("/api/v1/x", data=b"{}")
        self.assertEqual(answer.status, 401)

    async def test_the_error_envelope(self):
        status, body = await self.send(api_version="2.0")
        self.assertEqual(set(body["error"]), {"code", "message", "retryable", "request_id"})
        self.assertFalse(body["error"]["retryable"])

    async def test_a_p256_phone_key_works(self):
        key = hostkit.p256_key()
        device = self.store.claim_pairing(self.store.new_pairing(), "p256", hostkit.public_bytes(key)).device_id
        status, body = await self.send(key=key, device=device)
        self.assertEqual(status, 200, body)

    async def test_the_fingerprint_ignores_signing_details(self):
        _, first = await self.send(body=b'{"a":1}')
        _, second = await self.send(body=b'{"a":1}')
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        _, third = await self.send(body=b'{"a":2}')
        self.assertNotEqual(first["fingerprint"], third["fingerprint"])


class JsonBody(unittest.TestCase):
    def signed(self, body):
        return auth.SignedRequest(device=None, request_id="r", fingerprint="f", body=body)

    def test_only_an_object_is_accepted(self):
        self.assertEqual(auth.json_body(self.signed(b'{"a":1}')), {"a": 1})
        for body in (b"[1]", b"nope", b"\xff"):
            with self.subTest(body=body), self.assertRaises(auth.ApiError):
                auth.json_body(self.signed(body))


class PublicKeys(unittest.TestCase):
    def test_wrong_curves_and_garbage_are_refused(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        p384 = ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        for algorithm, data in (("p256", p384), ("ed25519", b"short"), ("p256", b"junk"), ("rsa", b"x")):
            with self.subTest(algorithm=algorithm), self.assertRaises(ValueError):
                auth.load_public_key(algorithm, data)


if __name__ == "__main__":
    unittest.main()
