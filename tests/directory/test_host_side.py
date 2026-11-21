"""call/directory_client.py against the real directory service: enrol with a code, heartbeat, withdraw."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests import sandbox
from tests.directory.test_directory import BASE, Live

from call import directory_client  # noqa: E402
from tests.call import hostkit  # noqa: E402


class HostSide(Live):
    def setUp(self):
        super().setUp()
        self.host_folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.host_config = self.host_folder / "host.json"
        self.write_host(seq=1)
        self.directory_file = self.host_folder / "directory.json"

    def write_host(self, **changes):
        raw = {"host_id": hostkit.HOST_ID, "label": "Host A", "base_url": f"https://{hostkit.AUTHORITY}/",
               "tls_authority": hostkit.AUTHORITY, "descriptor_seq": 1, "updated_at": "2026-11-21T12:00:00Z",
               "capabilities": {"chat": True, "calls": True},
               "database": str(self.host_folder / "host.sqlite3"), "key_file": str(self.host_folder / "keys/host.ed25519")}
        raw.update({"descriptor_seq": changes.pop("seq", 1)}, **changes)
        self.host_config.write_text(json.dumps(raw))

    def transport(self, method, url, body, headers):
        """https://directory.example.ts.net/.well-known/henchline/directory/v1/... -> this loopback server."""
        self.assertTrue(url.startswith(BASE), url)
        return self.http(method, "/" + url[len(BASE):], body if method != "GET" else None, headers)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = directory_client.main(["--config", str(self.host_config), *argv], self.transport,
                                         self.directory_file)
        return code, out.getvalue(), err.getvalue()

    def code(self, **changes):
        from henchline_directory import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["enroll-host", "--config", str(self.config_path)])
        return json.dumps({**json.loads(out.getvalue()), **changes})

    def listed(self):
        key, device = getattr(self, "_phone", None) or self.pair_phone()
        self._phone = (key, device)
        return self.signed(key, "phone", device, "GET", "/v1/hosts")[1]["hosts"]

    def test_enrol_then_heartbeat_then_withdraw(self):
        code, out, err = self.cli("enroll", "--code", self.code())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.directory_file.stat().st_mode & 0o777, 0o600)
        saved = json.loads(self.directory_file.read_text())
        self.assertEqual((saved["directory_id"], saved["base_url"]), (self.store.directory_id, BASE))
        self.assertNotIn("nonce", saved)
        [entry] = self.listed()
        self.assertEqual(entry["host_id"], hostkit.HOST_ID)
        self.assertFalse(entry["descriptor"]["capabilities"]["calls"])  # only what the host really serves
        self.assertEqual(self.cli("heartbeat")[1].strip(), "seen")
        self.write_host(label="Host A, renamed")  # host.json's sequence not touched: it rises by itself
        self.assertEqual(self.cli("heartbeat")[1].strip(), "stored")
        entry = self.listed()[0]["descriptor"]
        self.assertEqual((entry["label"], entry["descriptor_seq"]), ("Host A, renamed", 2))
        self.write_host(seq=7, label="Host A, renamed")  # a raised floor wins
        self.assertEqual(self.cli("heartbeat")[1].strip(), "stored")
        self.assertEqual(self.listed()[0]["descriptor"]["descriptor_seq"], 7)
        self.assertEqual(self.cli("withdraw")[0], 0)
        self.assertEqual(self.listed(), [])
        self.assertIn(self.store.directory_id, self.cli("status")[1])

    def test_a_second_enrolment_is_refused_locally(self):
        self.cli("enroll", "--code", self.code())
        code, _, err = self.cli("enroll", "--code", self.code())
        self.assertEqual(code, 1)
        self.assertIn("already enrolled", err)

    def test_a_code_with_another_key_is_refused_and_nothing_is_saved(self):
        other = hostkit.ed25519_key()
        from call import canonical
        code, _, err = self.cli("enroll", "--code", self.code(
            directory_signing_key=canonical.b64url(hostkit.public_bytes(other))))
        self.assertEqual(code, 1)
        self.assertIn("not signed by the key in the code", err)
        self.assertFalse(self.directory_file.exists())
        self.assertEqual(self.listed(), [])

    def test_a_plain_http_code_is_refused(self):
        code, _, err = self.cli("enroll", "--code", self.code(base_url="http://directory.example.ts.net/x/"))
        self.assertEqual(code, 1)
        self.assertIn("https", err)

    def test_the_default_transport_refuses_http(self):
        with self.assertRaises(ValueError):
            directory_client.https("GET", "http://directory.example/v1/info", b"", {})

    def test_heartbeat_without_enrolment(self):
        code, _, err = self.cli("heartbeat")
        self.assertEqual(code, 1)
        self.assertIn("not enrolled", err)

    def test_the_server_loop_logs_only_changes(self):
        from call.pair import HostSigner
        from call.host_config import HostConfig
        config = HostConfig.load(self.host_config)
        signer = HostSigner.load_or_create(config.key_file)
        _, _, descriptor = directory_client._host(self.host_config)  # what the server would advertise
        logged = []

        async def run(ticks):
            task = asyncio.ensure_future(directory_client.keep_listed(
                descriptor, signer, logged.append, self.directory_file, self.transport, every=0.01))
            await asyncio.sleep(ticks)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run(0.05))
        self.assertEqual(len(logged), 1)
        self.assertIn("not enrolled", logged[0])
        self.cli("enroll", "--code", self.code())
        asyncio.run(run(0.1))
        self.assertEqual(logged[1], f"directory heartbeats reach {BASE}")
        self.assertEqual(len(logged), 2)


class ServerFlag(unittest.TestCase):
    def test_heartbeats_need_the_api(self):
        from tests.call.test_loopback_mode import Startup
        started, _, code, err = Startup.start(Startup(), "--behind-tailscale-serve", "--directory-heartbeat")
        self.assertEqual((started, code), (None, 2))
        self.assertIn("needs --api-v1", err)


if __name__ == "__main__":
    unittest.main()
