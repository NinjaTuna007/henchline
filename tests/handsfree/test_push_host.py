"""A push to Henchline names the computer it comes from when this computer has a host identity (host.json)."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # first: a throwaway HOME

hf_spec = importlib.util.spec_from_file_location("handsfree_push", sandbox.REPO / "handsfree/handsfree.py")
handsfree = importlib.util.module_from_spec(hf_spec)
hf_spec.loader.exec_module(handsfree)

HOST_ID = "00000000-0000-4000-8000-000000000001"


class PushNamesTheHost(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        ring = self.folder / "ring-endpoint"
        ring.write_text("https://push.example/up-test")
        self.config = self.folder / "host.json"
        for p in (mock.patch.object(handsfree, "CALL_RING", ring), mock.patch.object(handsfree, "HOST_CONFIG", self.config)):
            p.start()
            self.addCleanup(p.stop)

    def pushed(self, body: bytes) -> bytes:
        with mock.patch.object(handsfree.urllib.request, "urlopen") as urlopen:
            self.assertTrue(handsfree.push(body))
        return urlopen.call_args.args[0].data

    def test_a_text_names_this_computer(self):
        self.config.write_text(json.dumps({"host_id": HOST_ID, "label": "Laptop"}))
        sent = json.loads(self.pushed(json.dumps({"agent": "claude", "session": "claude-1", "text": "hi", "id": "ab"}).encode()))
        self.assertEqual(sent, {"agent": "claude", "session": "claude-1", "text": "hi", "id": "ab", "host_id": HOST_ID})

    def test_without_host_json_the_push_is_byte_for_byte_as_before(self):
        body = json.dumps({"agent": "claude", "why": "done", "reply": "r", "kind": "done"}).encode()
        self.assertEqual(self.pushed(body), body)

    def test_an_unreadable_host_json_changes_nothing(self):
        body = json.dumps({"agent": "claude", "text": "hi"}).encode()
        for broken in ("{not json", json.dumps({"host_id": 7}), json.dumps(["x"]), json.dumps({"host_id": ""})):
            with self.subTest(broken=broken):
                self.config.write_text(broken)
                self.assertEqual(self.pushed(body), body)


if __name__ == "__main__":
    unittest.main()
