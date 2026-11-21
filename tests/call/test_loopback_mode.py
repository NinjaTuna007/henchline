"""call/server.py's command line: behind someone else's https name it stays on loopback and never runs
`tailscale`; without the new flags it does exactly what it did; `backup` copies the host database."""

import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox

try:
    from aiohttp import web  # noqa: F401
except ImportError:
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

from call.host_store import HostStore

spec = importlib.util.spec_from_file_location("call_server_cli", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class Startup(unittest.TestCase):
    def start(self, *argv, tailscale_ip="100.64.0.7\n", serve_ok=True):
        """main() with argv; returns (run_app kwargs or None, the tailscale commands run, exit code, stderr)."""
        commands = []

        def run(cmd, *a, **k):
            commands.append(cmd)
            out = tailscale_ip if cmd[:2] == ["tailscale", "ip"] else ""
            return mock.Mock(stdout=out, stderr="", returncode=0 if serve_ok else 1)

        started = {}
        err = io.StringIO()
        with mock.patch.object(server.subprocess, "run", side_effect=run), \
                mock.patch.object(server, "pactl", return_value=""), \
                mock.patch.object(server, "in_call", return_value=False), \
                mock.patch.object(server.web, "run_app", side_effect=lambda app, **kw: started.update(kw)), \
                mock.patch("sys.argv", ["voice-call", *argv]), contextlib.redirect_stderr(err):
            try:
                code = server.main()
            except SystemExit as exc:
                code = exc.code
        return (started or None), [c for c in commands if c and c[0] == "tailscale"], code, err.getvalue()

    def test_behind_serve_is_loopback_http_with_no_tailscale_at_all(self):
        started, tailscale, code, _ = self.start("--behind-tailscale-serve")
        self.assertEqual(code, 0)
        self.assertEqual(tailscale, [])
        self.assertEqual((started["host"], started["port"], started["ssl_context"]), ("127.0.0.1", 8790, None))

    def test_behind_serve_on_a_chosen_loopback_port(self):
        for listen, host in (("127.0.0.1:8795", "127.0.0.1"), ("[::1]:8795", "::1")):
            with self.subTest(listen=listen):
                started, tailscale, code, _ = self.start("--behind-tailscale-serve", "--listen", listen)
                self.assertEqual((code, tailscale), (0, []))
                self.assertEqual((started["host"], started["port"]), (host, 8795))

    def test_behind_serve_refuses_anything_but_loopback(self):
        for listen in ("0.0.0.0:8790", "100.64.0.7:8790", "[::]:8790", "localhost:8790"):
            with self.subTest(listen=listen):
                started, tailscale, code, err = self.start("--behind-tailscale-serve", "--listen", listen)
                self.assertEqual((started, tailscale, code), (None, [], 2))
                self.assertIn("loopback", err)

    def test_behind_serve_refuses_a_certificate(self):
        started, _, code, _ = self.start("--behind-tailscale-serve", "--cert", "c.pem", "--key", "k.pem")
        self.assertEqual((started, code), (None, 2))

    def test_malformed_listen_addresses(self):
        for listen in ("8790", "127.0.0.1:", "127.0.0.1:99999", "::1:8790", ":8790"):
            with self.subTest(listen=listen):
                self.assertEqual(self.start("--listen", listen)[2], 2)

    def test_the_default_still_puts_itself_behind_serve(self):
        started, tailscale, code, _ = self.start()
        self.assertEqual(code, 0)
        self.assertEqual(tailscale[0], ["tailscale", "ip", "-4"])
        self.assertIn(["tailscale", "serve", "--bg", "--https=443", "http://100.64.0.7:8790"], tailscale)
        self.assertEqual((started["host"], started["port"], started["ssl_context"]), ("100.64.0.7", 8790, None))

    def test_listen_alone_skips_the_lookup_but_keeps_its_own_certificate(self):
        tls = server.CALL_TLS
        with mock.patch.object(server.ssl, "create_default_context") as context:
            tls.mkdir(parents=True, exist_ok=True)
            (tls / "cert.pem").write_text("x")
            try:
                started, tailscale, code, _ = self.start("--listen", "100.64.0.7:8790")
            finally:
                (tls / "cert.pem").unlink()
        self.assertEqual((code, tailscale), (0, []))
        self.assertIs(started["ssl_context"], context.return_value)


class Commands(unittest.TestCase):
    def test_api_v1_without_a_host_identity_refuses_to_start(self):
        started, _, code, err = Startup.start(Startup(), "--behind-tailscale-serve", "--api-v1")
        self.assertEqual((started, code), (None, 1))
        self.assertIn("--api-v1: cannot read host config", err)

    def test_devices_list_and_revoke(self):
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        config = folder / "host.json"
        config.write_text(json.dumps({
            "host_id": "00000000-0000-4000-8000-000000000001", "label": "M", "base_url": "https://m.example/",
            "tls_authority": "m.example", "updated_at": "2026-11-21T12:00:00Z",
            "database": str(folder / "host.sqlite3")}))
        store = HostStore(folder / "host.sqlite3")
        device = store.claim_pairing(store.new_pairing(), "ed25519", b"k" * 32, "Test phone").device_id
        store.close()
        out = io.StringIO()
        with mock.patch("sys.argv", ["voice-call", "devices", "--config", str(config), "revoke", device]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(server.main(), 0)
        with mock.patch("sys.argv", ["voice-call", "devices", "--config", str(config), "list"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(server.main(), 0)
        self.assertIn(f"{device}  revoked  ed25519  Test phone", out.getvalue())
        with mock.patch("sys.argv", ["voice-call", "devices", "--config", str(config), "revoke", "nope"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(server.main(), 1)


class Backup(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.database = self.folder / "host.sqlite3"
        self.config = self.folder / "host.json"
        self.config.write_text(json.dumps({
            "host_id": "00000000-0000-4000-8000-000000000001", "label": "M", "base_url": "https://m.example/",
            "tls_authority": "m.example", "updated_at": "2026-11-21T12:00:00Z", "database": str(self.database)}))

    def run_backup(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = server.backup_main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_live_database_is_copied_privately_and_completely(self):
        store = HostStore(self.database)
        self.addCleanup(store.close)
        nonce = store.new_pairing()
        device = store.claim_pairing(nonce, "ed25519", b"k" * 32).device_id  # still in the WAL, store still open
        output = self.folder / "backup/application/host.sqlite3"
        code, out, _ = self.run_backup("--config", str(self.config), "--output", str(output))
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["path"], str(output))
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(output.parent.glob("*-wal")) + list(output.parent.glob(".*partial")))
        copy = sqlite3.connect(output)
        self.assertEqual(copy.execute("SELECT device_id FROM devices").fetchall(), [(device,)])
        copy.close()
        import hashlib
        self.assertEqual(summary["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

    def test_an_existing_output_is_never_overwritten(self):
        HostStore(self.database).close()
        output = self.folder / "copy.sqlite3"
        output.write_bytes(b"earlier")
        code, _, err = self.run_backup("--config", str(self.config), "--output", str(output))
        self.assertEqual((code, output.read_bytes()), (1, b"earlier"))
        self.assertIn("already exists", err)

    def test_a_shared_folder_is_refused(self):
        HostStore(self.database).close()
        shared = self.folder / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        code, _, err = self.run_backup("--config", str(self.config), "--output", str(shared / "c.sqlite3"))
        self.assertEqual(code, 1)
        self.assertIn("writable by group or others", err)
        self.assertEqual(list(shared.iterdir()), [])

    def test_a_broken_config_is_a_clean_error(self):
        self.config.write_text("{not json")
        code, _, err = self.run_backup("--config", str(self.config), "--output", str(self.folder / "c.sqlite3"))
        self.assertEqual(code, 1)
        self.assertIn("backup failed", err)

    def test_no_database_is_an_error(self):
        code, _, err = self.run_backup("--config", str(self.config), "--output", str(self.folder / "c.sqlite3"))
        self.assertEqual(code, 1)
        self.assertFalse((self.folder / "c.sqlite3").exists())

    def test_the_subcommand_is_dispatched_before_the_server_parser(self):
        with mock.patch("sys.argv", ["voice-call", "backup", "--output", "x"]), \
                mock.patch.object(server, "backup_main", return_value=0) as backup_main:
            self.assertEqual(server.main(), 0)
        backup_main.assert_called_once_with(["--output", "x"])


if __name__ == "__main__":
    unittest.main()
