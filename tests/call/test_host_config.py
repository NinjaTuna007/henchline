"""call/host_config.py: a host's identity is validated before it is written, and never replaced."""

import json
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

from call import host_config
from call.host_config import HostConfig
from tests.call import hostkit


class Initialize(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.path = self.folder / "henchline/host.json"

    def test_a_new_identity_is_private_and_complete(self):
        config = host_config.initialize(self.path, label="Laptop", base_url="https://m.example.ts.net",
                                        capabilities={"chat": True}, now="2026-11-21T10:00:00Z")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.base_url, "https://m.example.ts.net/")
        self.assertEqual(config.tls_authority, "m.example.ts.net")
        self.assertEqual(config.updated_at, "2026-11-21T10:00:00Z")
        self.assertEqual(config.descriptor_seq, 1)
        self.assertTrue(config.capabilities["chat"])
        self.assertFalse(config.capabilities["calls"])  # nothing is advertised by default
        self.assertEqual(HostConfig.load(self.path), config)

    def test_updated_at_defaults_to_now_in_utc(self):
        config = host_config.initialize(self.path, label="M", base_url="https://m.example/", capabilities={})
        self.assertRegex(config.updated_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_an_existing_identity_is_kept(self):
        first = host_config.initialize(self.path, label="M", base_url="https://m.example/", capabilities={})
        again = host_config.initialize(self.path, label="Other", base_url="https://o.example/", capabilities={})
        self.assertEqual(again.host_id, first.host_id)
        self.assertEqual(again.label, "M")

    def test_a_bad_base_url_writes_nothing(self):
        for url in ("http://m.example/", "https://m.example/sub/", "https://m.example/?q=1",
                    "https://user@m.example/", "https:///", "m.example"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    host_config.initialize(self.path, label="M", base_url=url, capabilities={})
                self.assertFalse(self.path.exists())

    def test_a_bad_label_or_capability_writes_nothing(self):
        for label, caps in (("a\nb", {}), ("x" * 81, {}), ("M", {"teleport": True}),
                            ("M", {"chat": "yes"})):
            with self.subTest(label=label, caps=caps):
                with self.assertRaises(ValueError):
                    host_config.initialize(self.path, label=label, base_url="https://m.example/", capabilities=caps)
                self.assertFalse(self.path.exists())


def tailnet(name: str | None = None, *, missing: bool = False):
    """Patch `tailscale status --json` to report Self.HostName = name (or tailscale to be absent)."""
    def run(argv, **kwargs):
        if missing:
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, 0, json.dumps({"Self": {"HostName": name}}), "")
    return mock.patch.object(host_config.subprocess, "run", side_effect=run)


class Label(unittest.TestCase):
    """Without a label of its own, a computer is called by its own name, never by a fixed one."""

    def test_an_explicit_label_wins(self):
        with tailnet("other"), mock.patch.object(host_config.socket, "gethostname", return_value="box"):
            self.assertEqual(hostkit.config(label="  Desk  ").label, "Desk")

    def test_a_missing_or_blank_label_is_the_tailscale_name(self):
        for raw in ({"label": None}, {"label": ""}, {"label": "   "}):
            with self.subTest(raw=raw), tailnet("Studio"), \
                    mock.patch.object(host_config.socket, "gethostname", return_value="localhost"):
                self.assertEqual(hostkit.config(**raw).label, "Studio")
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        (folder / "host.json").write_text(json.dumps({
            "host_id": hostkit.HOST_ID, "base_url": f"https://{hostkit.AUTHORITY}/",
            "tls_authority": hostkit.AUTHORITY, "updated_at": "2026-11-21T12:00:00Z"}))
        with tailnet("Studio"), mock.patch.object(host_config.socket, "gethostname", return_value="x"):
            self.assertEqual(HostConfig.load(folder / "host.json").label, "Studio")

    def test_the_hostname_spells_a_tailscale_name_that_matches_it(self):
        with tailnet("studiobox"), mock.patch.object(host_config.socket, "gethostname", return_value="StudioBox"):
            self.assertEqual(host_config.machine_name(), "StudioBox")

    def test_without_tailscale_the_hostname(self):
        for patch in (tailnet(missing=True), tailnet(""),
                      mock.patch.object(host_config.subprocess, "run",
                                        side_effect=subprocess.TimeoutExpired("tailscale", 2))):
            with self.subTest(patch=patch), patch, \
                    mock.patch.object(host_config.socket, "gethostname", return_value="desk.local"):
                self.assertEqual(host_config.machine_name(), "desk")

    def test_a_new_identity_without_a_label_writes_down_the_name(self):
        path = Path(tempfile.mkdtemp(dir=sandbox.HOME)) / "host.json"
        with tailnet("Studio"), mock.patch.object(host_config.socket, "gethostname", return_value="x"):
            config = host_config.initialize(path, label="", base_url="https://m.example/", capabilities={})
        self.assertEqual(config.label, "Studio")
        self.assertEqual(json.loads(path.read_text())["label"], "Studio")


class Load(unittest.TestCase):
    def test_the_authority_must_match_the_url(self):
        with self.assertRaises(ValueError):
            hostkit.config(tls_authority="other.example.ts.net")

    def test_updated_at_is_required_and_strict(self):
        for stamp in (None, "2026-11-21", "2026-11-21T12:00:00+00:00", "2026-13-01T00:00:00Z"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                hostkit.config(updated_at=stamp)

    def test_an_unknown_api_version_is_refused(self):
        with self.assertRaises(ValueError):
            hostkit.config(api_version="2.0")

    def test_host_id_is_normalized(self):
        self.assertEqual(hostkit.config(host_id=hostkit.HOST_ID.upper()).host_id, hostkit.HOST_ID)

    def test_unreadable_or_malformed_files_are_errors(self):
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        with self.assertRaises(ValueError):
            HostConfig.load(folder / "missing.json")
        (folder / "list.json").write_text(json.dumps([1]))
        with self.assertRaises(ValueError):
            HostConfig.load(folder / "list.json")

    def test_private_paths_must_be_private(self):
        config = hostkit.config()
        config.database.parent.mkdir(parents=True, exist_ok=True)
        config.database.write_bytes(b"")
        config.database.chmod(0o644)
        with self.assertRaises(ValueError):
            config.validate_private_paths()
        config.database.chmod(0o600)
        config.validate_private_paths()


if __name__ == "__main__":
    unittest.main()
