"""The server's own routes taking requests signed by a paired phone (api_v1.guard_legacy, server.trusted)."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

from call import api_v1, canonical
from call.host_store import HostStore
from call.pair import HostSigner
from tests.call import hostkit
from tests.call.test_api_v1 import FakeHost

spec = importlib.util.spec_from_file_location("call_server_signed", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


async def socket(request: web.Request) -> web.WebSocketResponse:
    """As /call begins: the Origin check, then the upgrade."""
    server.trusted(request)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_str("hello")
    await ws.close()
    return ws


async def files(request: web.Request) -> web.Response:
    """As /files/{secret}/{name}: a capability link, no Origin check."""
    return web.Response(text="file")


class Base(AioHTTPTestCase):
    capabilities = {"chat": True, "legacy_routes": True}
    signed_only = False

    async def get_application(self):
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.config = hostkit.config(folder, capabilities=self.capabilities)
        self.store = HostStore(self.config.database)
        app = web.Application()
        app.router.add_post("/text", server.texted)
        app.router.add_get("/chat/minions", server.chat_minions)
        app.router.add_get("/call", socket)
        app.router.add_post("/share/file", server.share_file)
        app.router.add_get("/files/{secret}/{name}", files)
        self.api = api_v1.setup(app, self.config, self.store, FakeHost(), HostSigner.load_or_create(self.config.key_file))
        api_v1.guard_legacy(app, server.STREAMED)
        patches = [mock.patch.object(server, "SIGNED_ORIGIN", f"https://{hostkit.AUTHORITY}"),
                   mock.patch.object(server, "SIGNED_ONLY", self.signed_only),
                   mock.patch.object(server, "to_agent", return_value=("Claude", ""))]
        for p in patches:
            self.to_agent = p.start()
            self.addCleanup(p.stop)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.store.close()

    async def enrolled(self, key=None):
        key = key or hostkit.ed25519_key()
        nonce = self.store.new_pairing()
        public = canonical.b64url(hostkit.public_bytes(key))
        proof = hostkit.sign(key, canonical.pairing_bytes(host_id=self.config.host_id, nonce=nonce,
                                                          algorithm="ed25519", public_key=public))
        answer = await self.client.post("/api/v1/pairing/claim", json={
            "nonce": nonce, "algorithm": "ed25519", "public_key": public, "proof": canonical.b64url(proof)})
        self.assertEqual(answer.status, 201)
        return key, (await answer.json())["device_id"]

    def origin(self):
        return {"Origin": f"https://{self.server.host}:{self.server.port}"}

    async def signed(self, key, device, method, path, body=None, **sign):
        data = b"" if body is None else json.dumps(body).encode()
        headers = hostkit.signed_headers(key, device, method, path, data, **sign)
        return await self.client.request(method, path, data=data, headers=headers), headers


class SignedRoutes(Base):
    async def test_a_signed_post_reaches_the_route_without_an_origin(self):
        key, device = await self.enrolled()
        answer, _ = await self.signed(key, device, "POST", "/text", {"text": "hi", "session": "claude-1"})
        self.assertEqual((answer.status, await answer.json()), (200, {"to": "Claude", "queued": False}))
        self.to_agent.assert_called_once_with("hi", "claude-1", server.Heard.TEXTED, False, False)
        self.assertEqual(server.CALL_ORIGIN.read_text(), f"https://{hostkit.AUTHORITY}")  # the host's own address

    async def test_a_signed_get_and_a_signed_websocket(self):
        key, device = await self.enrolled()
        with mock.patch.object(server.chains, "view", return_value={}), \
                mock.patch.object(server.subagents, "helpers", return_value=[]):
            answer, _ = await self.signed(key, device, "GET", "/chat/minions?session=claude-x")
        self.assertEqual(answer.status, 200)
        headers = hostkit.signed_headers(key, device, "GET", "/call?resume=1")
        async with self.client.ws_connect("/call?resume=1", headers=headers) as ws:
            self.assertEqual(await ws.receive_str(), "hello")

    async def test_the_origin_still_works_unsigned_when_not_signed_only(self):
        answer = await self.client.post("/text", json={"text": "hi", "session": "claude-1"}, headers=self.origin())
        self.assertEqual(answer.status, 200)
        answer = await self.client.post("/text", json={"text": "hi", "session": "claude-1"},
                                        headers={"Origin": "https://evil.example"})
        self.assertEqual(answer.status, 403)

    async def test_a_bad_signature_never_falls_back_to_the_origin(self):
        key, device = await self.enrolled()
        body = {"text": "hi", "session": "claude-1"}
        headers = hostkit.signed_headers(key, device, "POST", "/text", json.dumps(body).encode())
        headers.update(self.origin())
        answer = await self.client.post("/text", data=json.dumps({"text": "other", "session": "claude-1"}),
                                        headers=headers)  # signed one body, sent another
        self.assertEqual((answer.status, (await answer.json())["error"]["code"]), (401, "bad_signature"))
        self.to_agent.assert_not_called()

    async def test_a_replayed_request_is_refused(self):
        key, device = await self.enrolled()
        data = json.dumps({"text": "hi", "session": "claude-1"}).encode()
        headers = hostkit.signed_headers(key, device, "POST", "/text", data)
        first = await self.client.post("/text", data=data, headers=headers)
        again = await self.client.post("/text", data=data, headers=headers)
        self.assertEqual((first.status, again.status), (200, 401))
        self.assertEqual(self.to_agent.call_count, 1)

    async def test_a_revoked_or_unknown_phone_is_refused(self):
        key, device = await self.enrolled()
        self.store.revoke(device)
        answer, _ = await self.signed(key, device, "POST", "/text", {"text": "hi", "session": "claude-1"})
        self.assertEqual((answer.status, (await answer.json())["error"]["code"]), (403, "device_revoked"))
        answer, _ = await self.signed(hostkit.ed25519_key(), "nobody", "POST", "/text", {"text": "hi", "session": "c"})
        self.assertEqual(answer.status, 401)
        self.to_agent.assert_not_called()

    async def test_streamed_uploads_refuse_signed_requests_for_now(self):
        key, device = await self.enrolled()
        answer, _ = await self.signed(key, device, "POST", "/share/file?name=a.txt")
        self.assertEqual((answer.status, (await answer.json())["error"]["code"]), (400, "unsupported"))

    async def test_api_v1_routes_are_not_checked_twice(self):
        key, device = await self.enrolled()
        answer, _ = await self.signed(key, device, "GET", "/api/v1/chats")
        self.assertEqual(answer.status, 200)  # one nonce use: a second check would call it replayed

    async def test_the_descriptor_offers_it(self):
        answer = await self.client.get("/.well-known/henchline/v1/host")
        self.assertTrue((await answer.json())["descriptor"]["capabilities"]["legacy_routes"])


class SignedOnly(Base):
    signed_only = True

    async def test_unsigned_requests_are_refused_even_with_the_origin(self):
        answer = await self.client.post("/text", json={"text": "hi", "session": "claude-1"}, headers=self.origin())
        self.assertEqual((answer.status, (await answer.json())["error"]["code"]), (401, "signature_required"))
        with self.assertRaises(Exception):
            async with self.client.ws_connect("/call", headers=self.origin()):
                pass
        self.to_agent.assert_not_called()

    async def test_signed_requests_still_get_in(self):
        key, device = await self.enrolled()
        answer, _ = await self.signed(key, device, "POST", "/text", {"text": "hi", "session": "claude-1"})
        self.assertEqual(answer.status, 200)

    async def test_capability_links_pairing_and_health_stay_open(self):
        self.assertEqual((await self.client.get("/files/abc/a.txt")).status, 200)
        self.assertEqual((await self.client.get("/api/v1/health")).status, 200)
        self.assertEqual((await self.client.get("/.well-known/henchline/v1/host")).status, 200)


class NotOffered(Base):
    capabilities = {"chat": True}

    async def test_a_signed_request_is_refused_and_unsigned_ones_are_unchanged(self):
        key, device = await self.enrolled()
        answer, _ = await self.signed(key, device, "POST", "/text", {"text": "hi", "session": "claude-1"})
        self.assertEqual((answer.status, (await answer.json())["error"]["code"]), (404, "capability_unavailable"))
        answer = await self.client.post("/text", json={"text": "hi", "session": "claude-1"}, headers=self.origin())
        self.assertEqual(answer.status, 200)
        self.assertFalse((await (await self.client.get("/.well-known/henchline/v1/host")).json())
                         ["descriptor"]["capabilities"]["legacy_routes"])


class Mounting(unittest.TestCase):
    def test_signed_only_needs_the_capability(self):
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        config = hostkit.config(folder, capabilities={"chat": True})
        with mock.patch("call.host_config.HostConfig.load", return_value=config):
            why = server.mount_api_v1(web.Application(), signed_only=True)
        self.assertIn("legacy_routes", why)
        self.assertFalse(server.SIGNED_ONLY)


if __name__ == "__main__":
    unittest.main()
