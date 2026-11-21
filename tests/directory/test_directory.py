"""The directory (directory/henchline_directory): pairing phones, pinning hosts, heartbeats and listings, over
real HTTP on loopback with requests signed as a phone or host would sign them."""

import contextlib
import io
import json
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from tests import sandbox

sys.path.insert(0, str(sandbox.REPO / "directory"))
try:
    import cryptography  # noqa: F401
except ImportError:
    raise unittest.SkipTest("cryptography missing: run with ~/.local/share/henchline/venv/bin/python3")

from call import canonical  # noqa: E402
from call.pair import HostSigner  # noqa: E402
from henchline_directory import cli  # noqa: E402
from henchline_directory.identity import DirectoryConfig, Identity  # noqa: E402
from henchline_directory.service import Directory, make_server  # noqa: E402
from henchline_directory.store import STALE_AFTER, DirectoryStore  # noqa: E402
from tests.call import hostkit  # noqa: E402

BASE = "https://directory.example.ts.net/.well-known/henchline/directory/"
PREFIX = "/.well-known/henchline/directory"
TEMPLATE = {  # the deployment's directory config, paths moved into the sandbox
    "schema": "henchline.directory-config/v1", "listen": "127.0.0.1:8791", "api_version": "1.0",
    "expected_build_id": "@BUILD_ID@", "generate_identity_if_absent": True, "rotate_identity": False}


def write_config(folder: Path, **changes) -> Path:
    path = folder / "config.json"
    path.write_text(json.dumps({**TEMPLATE, "state_dir": str(folder / "state"),
                                "database": str(folder / "state/directory.sqlite3"),
                                "signing_key": str(folder / "state/keys/directory.ed25519"), **changes}))
    return path


class Live(unittest.TestCase):
    base_url = BASE

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.config_path = write_config(self.folder, base_url=self.base_url)
        self.config = DirectoryConfig.load(self.config_path)
        self.store = DirectoryStore(self.config.database)
        self.identity = Identity.load(self.config, self.store.directory_id)
        self.directory = Directory(self.config, self.store, self.identity, "test-build")
        self.server = make_server(self.directory, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.stop)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()

    def http(self, method, path, body=None, headers=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as answer:
                return answer.status, json.loads(answer.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def signed(self, key, kind, signer_id, method, path, body=b"", *, prefix=PREFIX, timestamp=None,
               directory_id=None, nonce=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        p, _, q = path.partition("?")
        stamp = str(timestamp or int(time.time()))
        nonce = nonce or canonical.b64url(secrets.token_bytes(16))
        rid = str(uuid.uuid4())
        directory_id = directory_id or self.store.directory_id
        message = canonical.request_bytes(
            api_version="1.0", authority_id=directory_id, scheme="https", authority="directory.example.ts.net",
            method=method, path=prefix + p, query=q, body=data, device_id=signer_id, timestamp=stamp,
            nonce=nonce, request_id=rid)
        headers = {"X-Henchline-Api-Version": "1.0", "X-Henchline-Directory": directory_id,
                   "X-Henchline-Device" if kind == "phone" else "X-Henchline-Host": signer_id,
                   "X-Henchline-Timestamp": stamp, "X-Henchline-Nonce": nonce, "X-Henchline-Request-Id": rid,
                   "X-Henchline-Signature": canonical.b64url(hostkit.sign(key, message))}
        return self.http(method, path, data if method != "GET" else None, headers)

    def pair_phone(self, key=None, algorithm="ed25519"):
        key = key or hostkit.ed25519_key()
        nonce = self.store.new_code("phone")
        public = canonical.b64url(hostkit.public_bytes(key))
        proof = hostkit.sign(key, canonical.pairing_bytes(host_id=self.store.directory_id, nonce=nonce,
                                                          algorithm=algorithm, public_key=public))
        status, body = self.http("POST", "/v1/pairing/claim", {
            "nonce": nonce, "algorithm": algorithm, "public_key": public, "proof": canonical.b64url(proof),
            "label": "Test phone"})
        self.assertEqual(status, 201, body)
        return key, body["device_id"]

    def host(self, seq=1, host_id=hostkit.HOST_ID):
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        config = hostkit.config(folder, host_id=host_id, descriptor_seq=seq)
        signer = HostSigner.load_or_create(config.key_file)
        return signer, config

    def enroll(self, signer, config):
        status, body = self.http("POST", "/v1/hosts/enroll/claim",
                                 {"nonce": self.store.new_code("host"), "descriptor": signer.descriptor(config)})
        self.assertEqual(status, 201, body)

    def heartbeat(self, signer, config, **sign):
        return self.signed(signer.key, "host", config.host_id, "POST", "/v1/hosts/heartbeat",
                           {"descriptor": signer.descriptor(config)}, **sign)


class Unsigned(Live):
    def test_health(self):
        status, body = self.http("GET", "/v1/health")
        self.assertEqual((status, body["status"], body["build_id"]), (200, "ok", "test-build"))

    def test_info_is_a_signed_descriptor(self):
        status, body = self.http("GET", "/v1/info")
        descriptor = dict(body["descriptor"])
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(canonical.unb64url(descriptor["directory_signing_key"])).verify(
            canonical.unb64url(descriptor.pop("signature")), canonical.canonical_json(descriptor))
        self.assertEqual((descriptor["directory_id"], descriptor["tls_authority"]),
                         (self.store.directory_id, "directory.example.ts.net"))

    def test_unknown_routes_and_big_bodies(self):
        self.assertEqual(self.http("GET", "/")[0], 404)
        self.assertEqual(self.http("POST", "/v1/pairing/claim", b"x" * (65 * 1024))[0], 413)

    def test_a_non_numeric_length_is_refused(self):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        for length in ("abc", "-1", "+5"):
            with self.subTest(length=length):
                connection.putrequest("POST", "/v1/pairing/claim")
                connection.putheader("Content-Length", length)
                connection.endheaders()
                answer = connection.getresponse()
                body = json.loads(answer.read())
                self.assertEqual((answer.status, body["error"]["code"]), (400, "invalid_request"))
        connection.close()

    def test_a_stalled_client_is_dropped(self):
        from henchline_directory.service import Handler
        self.assertEqual(Handler.timeout, 15)

    def test_the_identity_is_private_and_kept(self):
        self.assertEqual(self.config.signing_key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Identity.load(self.config, self.store.directory_id).public_key, self.identity.public_key)


class Phones(Live):
    def test_a_paired_phone_lists_hosts(self):
        key, device = self.pair_phone()
        signer, config = self.host()
        self.enroll(signer, config)
        status, body = self.signed(key, "phone", device, "GET", "/v1/hosts")
        self.assertEqual(status, 200, body)
        self.assertEqual([h["host_id"] for h in body["hosts"]], [config.host_id])
        self.assertEqual(body["hosts"][0]["descriptor"], signer.descriptor(config))  # the host's own blob
        self.assertFalse(body["hosts"][0]["stale"])

    def test_a_p256_phone(self):
        key, device = self.pair_phone(hostkit.p256_key(), "p256")
        self.assertEqual(self.signed(key, "phone", device, "GET", "/v1/hosts")[0], 200)

    def test_the_public_prefix_is_signed(self):
        key, device = self.pair_phone()
        status, body = self.signed(key, "phone", device, "GET", "/v1/hosts", prefix="")
        self.assertEqual((status, body["error"]["code"]), (401, "bad_signature"))

    def test_replays_other_directories_and_old_clocks_fail(self):
        key, device = self.pair_phone()
        nonce = canonical.b64url(secrets.token_bytes(16))
        self.assertEqual(self.signed(key, "phone", device, "GET", "/v1/hosts", nonce=nonce)[0], 200)
        self.assertEqual(self.signed(key, "phone", device, "GET", "/v1/hosts", nonce=nonce)[0], 401)
        status, body = self.signed(key, "phone", device, "GET", "/v1/hosts", timestamp=int(time.time()) - 300)
        self.assertEqual(body["error"]["code"], "timestamp_out_of_window")
        self.assertIn("server_time", body["error"])
        self.assertEqual(self.signed(key, "phone", device, "GET", "/v1/hosts",
                                     directory_id=str(uuid.uuid4()))[0], 401)

    def test_a_host_key_cannot_act_as_a_phone(self):
        signer, config = self.host()
        self.enroll(signer, config)
        self.assertEqual(self.signed(signer.key, "phone", config.host_id, "GET", "/v1/hosts")[0], 401)

    def test_a_bad_proof_costs_an_attempt_and_codes_are_single_use(self):
        key = hostkit.ed25519_key()
        nonce = self.store.new_code("phone")
        public = canonical.b64url(hostkit.public_bytes(key))
        good = canonical.b64url(key.sign(canonical.pairing_bytes(host_id=self.store.directory_id, nonce=nonce,
                                                                 algorithm="ed25519", public_key=public)))
        claim = {"nonce": nonce, "algorithm": "ed25519", "public_key": public}
        self.assertEqual(self.http("POST", "/v1/pairing/claim", {**claim, "proof": "AAAA"})[0], 422)
        self.assertEqual(self.http("POST", "/v1/pairing/claim", {**claim, "proof": good})[0], 201)
        self.assertEqual(self.http("POST", "/v1/pairing/claim", {**claim, "proof": good})[0], 410)

    def test_a_host_code_does_not_pair_a_phone(self):
        key = hostkit.ed25519_key()
        nonce = self.store.new_code("host")
        public = canonical.b64url(hostkit.public_bytes(key))
        proof = canonical.b64url(key.sign(canonical.pairing_bytes(host_id=self.store.directory_id, nonce=nonce,
                                                                  algorithm="ed25519", public_key=public)))
        self.assertEqual(self.http("POST", "/v1/pairing/claim", {
            "nonce": nonce, "algorithm": "ed25519", "public_key": public, "proof": proof})[0], 410)

    def test_a_phone_revokes_itself_only(self):
        key, device = self.pair_phone()
        _, other = self.pair_phone()
        self.assertEqual(self.signed(key, "phone", device, "DELETE", f"/v1/devices/{other}")[0], 403)
        self.assertEqual(self.signed(key, "phone", device, "DELETE", f"/v1/devices/{device}")[0], 200)
        status, body = self.signed(key, "phone", device, "GET", "/v1/hosts")
        self.assertEqual((status, body["error"]["code"]), (403, "device_revoked"))


class Hosts(Live):
    def test_heartbeats_keep_or_raise_the_sequence_never_lower_it(self):
        signer, config = self.host(seq=5)
        self.enroll(signer, config)
        status, body = self.heartbeat(signer, config)
        self.assertEqual((status, body["outcome"]), (200, "seen"))
        status, body = self.heartbeat(signer, hostkit.config(config.database.parent, descriptor_seq=6))
        self.assertEqual((status, body["outcome"]), (200, "stored"))
        status, body = self.heartbeat(signer, config)  # seq 5 again
        self.assertEqual((status, body["error"]["code"]), (412, "descriptor_stale"))
        changed = hostkit.config(config.database.parent, descriptor_seq=6, label="Renamed")
        self.assertEqual(self.heartbeat(signer, changed)[0], 412)  # new content needs a new sequence

    def test_another_key_cannot_heartbeat_for_a_pinned_host(self):
        signer, config = self.host()
        self.enroll(signer, config)
        thief, _ = self.host()
        status, _ = self.heartbeat(thief, config)  # signs with its own key, claims the pinned host id
        self.assertEqual(status, 401)

    def test_a_host_cannot_change_its_pinned_key_by_heartbeat(self):
        signer, config = self.host()
        self.enroll(signer, config)
        other, _ = self.host()
        descriptor = other.descriptor(config)
        status, body = self.signed(signer.key, "host", config.host_id, "POST", "/v1/hosts/heartbeat",
                                   {"descriptor": descriptor})
        self.assertEqual((status, body["error"]["code"]), (409, "state_conflict"))

    def test_enrolment_needs_a_live_code_and_a_valid_signature(self):
        signer, config = self.host()
        descriptor = signer.descriptor(config)
        self.assertEqual(self.http("POST", "/v1/hosts/enroll/claim",
                                   {"nonce": "made-up", "descriptor": descriptor})[0], 410)
        forged = {**descriptor, "label": "Evil"}
        self.assertEqual(self.http("POST", "/v1/hosts/enroll/claim",
                                   {"nonce": self.store.new_code("host"), "descriptor": forged})[0], 422)
        self.enroll(signer, config)
        status, body = self.http("POST", "/v1/hosts/enroll/claim",
                                 {"nonce": self.store.new_code("host"), "descriptor": descriptor})
        self.assertEqual((status, body["error"]["code"]), (409, "state_conflict"))

    def test_a_dead_code_does_not_reveal_whether_a_host_is_enrolled(self):
        signer, config = self.host()
        self.enroll(signer, config)
        status, body = self.http("POST", "/v1/hosts/enroll/claim",
                                 {"nonce": "made-up", "descriptor": signer.descriptor(config)})
        self.assertEqual((status, body["error"]["code"]), (410, "pairing_expired"))
        nonce = self.store.new_code("host")  # a live code refused for a pinned host stays usable
        self.assertEqual(self.http("POST", "/v1/hosts/enroll/claim",
                                   {"nonce": nonce, "descriptor": signer.descriptor(config)})[0], 409)
        other, other_config = self.host(host_id=hostkit.OTHER_HOST_ID)
        self.assertEqual(self.http("POST", "/v1/hosts/enroll/claim",
                                   {"nonce": nonce, "descriptor": other.descriptor(other_config)})[0], 201)

    def test_the_owners_revocation_outlasts_heartbeats_until_forgotten(self):
        key, device = self.pair_phone()
        signer, config = self.host()
        self.enroll(signer, config)
        self.assertTrue(self.store.revoke_host(config.host_id))
        status, body = self.heartbeat(signer, config)
        self.assertEqual((status, body["error"]["code"]), (403, "device_revoked"))
        self.assertEqual(self.signed(signer.key, "host", config.host_id, "DELETE",
                                     f"/v1/hosts/{config.host_id}")[0], 403)
        self.assertEqual(self.signed(key, "phone", device, "GET", "/v1/hosts")[1]["hosts"], [])
        self.assertEqual(self.http("POST", "/v1/hosts/enroll/claim", {
            "nonce": self.store.new_code("host"), "descriptor": signer.descriptor(config)})[0], 409)
        self.assertTrue(self.store.forget_host(config.host_id))
        self.enroll(signer, config)
        self.assertEqual(self.heartbeat(signer, config)[0], 200)

    def test_a_withdrawn_host_is_not_listed_until_it_heartbeats(self):
        key, device = self.pair_phone()
        signer, config = self.host()
        self.enroll(signer, config)
        other, _ = self.host()
        self.assertEqual(self.signed(signer.key, "host", config.host_id, "DELETE",
                                     f"/v1/hosts/{hostkit.OTHER_HOST_ID}")[0], 403)
        self.assertEqual(self.signed(signer.key, "host", config.host_id, "DELETE",
                                     f"/v1/hosts/{config.host_id}")[0], 200)
        self.assertEqual(self.signed(key, "phone", device, "GET", "/v1/hosts")[1]["hosts"], [])
        self.heartbeat(signer, config)
        self.assertEqual(len(self.signed(key, "phone", device, "GET", "/v1/hosts")[1]["hosts"]), 1)

    def test_listing_pages_and_marks_stale_hosts(self):
        key, device = self.pair_phone()
        for i in range(3):
            signer, config = self.host(host_id=f"00000000-0000-4000-8000-00000000001{i}")
            self.enroll(signer, config)
        with self.store._db:
            self.store._db.execute("UPDATE hosts SET observed_at=? WHERE host_id LIKE '%10'",
                                   (time.time() - STALE_AFTER - 1,))
        _, page = self.signed(key, "phone", device, "GET", "/v1/hosts?limit=2")
        self.assertEqual(len(page["hosts"]), 2)
        self.assertTrue(page["hosts"][0]["stale"])
        _, rest = self.signed(key, "phone", device, "GET", f"/v1/hosts?limit=2&cursor={page['next_cursor']}")
        self.assertEqual((len(rest["hosts"]), rest["next_cursor"]), (1, None))

    def test_nothing_private_is_stored(self):
        self.pair_phone()
        signer, config = self.host()
        self.enroll(signer, config)
        nonce = self.store.new_code("phone")
        self.store.close()
        data = b"".join(p.read_bytes() for p in self.config.database.parent.glob("directory.sqlite3*"))
        self.assertNotIn(nonce.encode(), data)
        self.assertNotIn(self.config.signing_key.read_bytes(), data)
        self.store = DirectoryStore(self.config.database)
        self.directory.store = self.store


class NoBaseUrl(Live):
    base_url = ""

    def test_health_works_and_everything_signed_waits_for_a_base_url(self):
        self.assertEqual(self.http("GET", "/v1/health")[0], 200)
        self.assertEqual(self.http("GET", "/v1/info")[0], 503)
        self.assertEqual(self.http("POST", "/v1/pairing/claim", {"nonce": "x"})[0], 503)


class Commands(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.config = write_config(self.folder, base_url=BASE)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        code, out, _ = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn("henchline-directory 1.0.0", out)

    def test_codes_for_phones_and_hosts(self):
        for command, kind in (("pair-phone", "phone"), ("enroll-host", "host")):
            code, out, _ = self.run_cli(command, "--config", str(self.config))
            value = json.loads(out)
            self.assertEqual((code, value["base_url"], value["tls_authority"]),
                             (0, BASE, "directory.example.ts.net"))
            store = DirectoryStore(self.folder / "state/directory.sqlite3")
            self.assertEqual(value["directory_id"], store.directory_id)
            with store._db:
                row = store._db.execute("SELECT kind FROM pairing_nonces ORDER BY expires_at DESC").fetchone()
            self.assertEqual(row[0], kind)
            store.close()

    def test_devices_and_backup(self):
        store = DirectoryStore(self.folder / "state/directory.sqlite3")
        device = store.claim_phone(store.new_code("phone"), "ed25519", b"k" * 32, "Test phone")
        store.close()
        self.assertEqual(self.run_cli("devices", "revoke", device, "--config", str(self.config))[0], 0)
        self.assertIn(f"{device}  revoked", self.run_cli("devices", "--config", str(self.config), "list")[1])
        output = self.folder / "backup/application/directory.sqlite3"
        code, out, _ = self.run_cli("backup", "--config", str(self.config), "--output", str(output))
        self.assertEqual((code, json.loads(out)["path"]), (0, str(output)))
        copy = sqlite3.connect(output)
        self.assertEqual(copy.execute("SELECT COUNT(*) FROM phones").fetchone()[0], 1)
        copy.close()

    def test_hosts_list_revoke_forget(self):
        store = DirectoryStore(self.folder / "state/directory.sqlite3")
        store.enroll_host(store.new_code("host"), hostkit.HOST_ID, b"k" * 32, b"{}", 1)
        store.close()
        self.assertIn(f"{hostkit.HOST_ID}  listed", self.run_cli("hosts", "list", "--config", str(self.config))[1])
        self.assertEqual(self.run_cli("hosts", "revoke", hostkit.HOST_ID, "--config", str(self.config))[0], 0)
        self.assertIn("revoked", self.run_cli("hosts", "--config", str(self.config), "list")[1])
        self.assertEqual(self.run_cli("hosts", "forget", hostkit.HOST_ID, "--config", str(self.config))[0], 0)
        self.assertEqual(self.run_cli("hosts", "list", "--config", str(self.config))[1], "")
        self.assertEqual(self.run_cli("hosts", "revoke", hostkit.HOST_ID, "--config", str(self.config))[0], 1)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["hosts", "list"])  # no config anywhere

    def test_a_first_build_database_gains_the_revocation_column(self):
        path = self.folder / "old.sqlite3"
        db = sqlite3.connect(path)
        db.executescript("""CREATE TABLE hosts(host_id TEXT PRIMARY KEY, public_key BLOB NOT NULL,
            enrolled_at REAL NOT NULL, descriptor BLOB NOT NULL, descriptor_seq INTEGER NOT NULL,
            observed_at REAL NOT NULL, withdrawn_at REAL);
            INSERT INTO hosts VALUES ('h', x'00', 1, x'7b7d', 1, 1, NULL);""")
        db.close()
        store = DirectoryStore(path)
        self.addCleanup(store.close)
        self.assertEqual(store.host_rows()[0]["revoked_at"], None)
        self.assertTrue(store.revoke_host("h"))

    def test_a_second_opener_racing_the_column_upgrade_is_fine(self):
        path = self.folder / "old2.sqlite3"
        db = sqlite3.connect(path)
        db.executescript("""CREATE TABLE hosts(host_id TEXT PRIMARY KEY, public_key BLOB NOT NULL,
            enrolled_at REAL NOT NULL, descriptor BLOB NOT NULL, descriptor_seq INTEGER NOT NULL,
            observed_at REAL NOT NULL, withdrawn_at REAL);""")
        db.close()
        store = DirectoryStore.__new__(DirectoryStore)
        store.path, store._lock = path, threading.RLock()
        store._db = _Racing(sqlite3.connect(path, check_same_thread=False), path)
        store._db.row_factory = sqlite3.Row
        store._migrate()  # another process adds the column between its look and its ALTER: no error
        self.assertTrue(store._db.raced)
        store.close()
        columns = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(hosts)")]
        self.assertEqual(columns.count("revoked_at"), 1)

    def test_refusals(self):
        for changes, words in (({"rotate_identity": True}, "rotate_identity"),
                               ({"base_url": "http://x/"}, "base_url"),
                               ({"generate_identity_if_absent": False}, "no directory key")):
            with self.subTest(changes=changes):
                folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
                code, _, err = self.run_cli("--config", str(write_config(folder, **changes)))
                self.assertEqual(code, 1)
                self.assertIn(words, err)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--config", str(self.config), "--listen", "0.0.0.0:8791"])
        code, _, err = self.run_cli("--config", str(write_config(self.folder, expected_build_id="other")))
        self.assertEqual(code, 1)
        self.assertIn("expects build other", err)


class _Racing:
    """A connection that lets "another process" add hosts.revoked_at just before this one's ALTER TABLE."""

    def __init__(self, connection, path):
        self.__dict__.update(_db=connection, _path=path, raced=False)

    def execute(self, sql, *args):
        if sql.startswith("ALTER TABLE hosts ADD COLUMN revoked_at") and not self.raced:
            self.__dict__["raced"] = True
            other = sqlite3.connect(self._path)
            other.execute(sql)
            other.commit()
            other.close()
        return self._db.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._db, name)

    def __setattr__(self, name, value):
        setattr(self._db, name, value)

    def __enter__(self):
        return self._db.__enter__()

    def __exit__(self, *exc):
        return self._db.__exit__(*exc)


if __name__ == "__main__":
    unittest.main()
