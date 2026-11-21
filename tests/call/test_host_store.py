"""call/host_store.py: pairing nonces, replay nonces and the idempotency ledger survive restarts."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

from call import host_store
from call.host_store import HostStore
from tests.call.hostkit import FIXTURES

PHONE = "11111111-1111-4111-8111-111111111111"
T0 = 1_795_000_000.0


class Base(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(dir=sandbox.HOME)) / "state/host.sqlite3"
        self.store = HostStore(self.path)
        self.addCleanup(lambda: self.store.close())

    def reopen(self):
        self.store.close()
        self.store = HostStore(self.path)

    def enroll(self, key=b"k" * 32):
        return self.store.claim_pairing(self.store.new_pairing(T0), "ed25519", key, "phone", now=T0)


class Files(Base):
    def test_database_and_its_folder_are_private(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        for extra in self.path.parent.glob("host.sqlite3-*"):
            self.assertEqual(extra.stat().st_mode & 0o077, 0, extra)

    def test_a_newer_schema_is_refused(self):
        with self.store._db:
            self.store._db.execute("UPDATE schema_meta SET version=99")
        self.store.close()
        with self.assertRaises(RuntimeError):
            HostStore(self.path)
        self.store.close = lambda: None  # already closed


class Pairing(Base):
    def test_a_claim_enrolls_once(self):
        nonce = self.store.new_pairing(T0)
        device = self.store.claim_pairing(nonce, "ed25519", b"k" * 32, now=T0 + 10)
        self.assertEqual(self.store.device(device.device_id).public_key, b"k" * 32)
        with self.assertRaises(TimeoutError):
            self.store.claim_pairing(nonce, "ed25519", b"j" * 32, now=T0 + 20)

    def test_a_nonce_lasts_five_minutes(self):
        nonce = self.store.new_pairing(T0)
        with self.assertRaises(TimeoutError):
            self.store.claim_pairing(nonce, "ed25519", b"k" * 32, now=T0 + host_store.PAIRING_LIFETIME + 1)

    def test_an_unknown_nonce_is_refused(self):
        with self.assertRaises(LookupError):
            self.store.claim_pairing("nope", "ed25519", b"k" * 32, now=T0)

    def test_five_failures_lock_the_nonce(self):
        nonce = self.store.new_pairing(T0)
        for _ in range(host_store.PAIRING_ATTEMPTS):
            self.store.pairing_failure(nonce)
        with self.assertRaises(PermissionError):
            self.store.claim_pairing(nonce, "ed25519", b"k" * 32, now=T0)

    def test_bad_key_material_is_refused_before_the_nonce_is_used(self):
        nonce = self.store.new_pairing(T0)
        for algorithm, key in (("rsa", b"k"), ("ed25519", b""), ("ed25519", b"k" * 4096)):
            with self.subTest(algorithm=algorithm, size=len(key)), self.assertRaises(ValueError):
                self.store.claim_pairing(nonce, algorithm, key, now=T0)
        self.store.claim_pairing(nonce, "p256", b"k" * 91, now=T0)

    def test_nonces_are_stored_only_as_digests(self):
        nonce = self.store.new_pairing(T0)
        self.store.close()
        self.assertNotIn(nonce.encode(), self.path.read_bytes())
        self.store = HostStore(self.path)

    def test_revocation_sticks_and_keeps_its_first_time(self):
        device = self.enroll()
        self.assertTrue(self.store.revoke(device.device_id, now=T0 + 1))
        self.assertTrue(self.store.revoke(device.device_id, now=T0 + 2))
        self.reopen()
        self.assertEqual(self.store.device(device.device_id).revoked_at, T0 + 1)
        self.assertFalse(self.store.revoke("no-such-device"))


class ReplayNonces(Base):
    def test_a_nonce_is_good_once_per_device(self):
        device = self.enroll()
        other = self.enroll(b"o" * 32)
        self.assertTrue(self.store.use_nonce(device.device_id, "n1", now=T0))
        self.assertFalse(self.store.use_nonce(device.device_id, "n1", now=T0 + 1))
        self.assertTrue(self.store.use_nonce(other.device_id, "n1", now=T0 + 1))

    def test_seen_nonces_survive_a_restart(self):
        device = self.enroll()
        self.store.use_nonce(device.device_id, "n1", now=T0)
        self.reopen()
        self.assertFalse(self.store.use_nonce(device.device_id, "n1", now=T0 + 60))

    def test_nonces_are_forgotten_after_ten_minutes(self):
        # By then the timestamp window refuses the request anyway.
        device = self.enroll()
        self.store.use_nonce(device.device_id, "n1", now=T0)
        self.assertTrue(self.store.use_nonce(device.device_id, "n1", now=T0 + host_store.NONCE_LIFETIME + 1))


class Idempotency(Base):
    def setUp(self):
        super().setUp()
        self.device = self.enroll().device_id

    def test_new_then_pending_then_replayed(self):
        self.assertEqual(self.store.reserve(self.device, "r1", "fp", now=T0).state, "new")
        self.assertEqual(self.store.reserve(self.device, "r1", "fp", now=T0).state, "pending")
        self.store.complete(self.device, "r1", 201, "application/json", b'{"ok":1}', now=T0)
        replay = self.store.reserve(self.device, "r1", "fp", now=T0 + 10 ** 8)  # years later
        self.assertEqual((replay.state, replay.status, replay.body), ("replay", 201, b'{"ok":1}'))

    def test_different_content_under_one_request_id_is_a_mismatch(self):
        self.store.reserve(self.device, "r1", "fp", now=T0)
        self.assertEqual(self.store.reserve(self.device, "r1", "other", now=T0).state, "mismatch")
        self.store.complete(self.device, "r1", 200, "application/json", b"{}")
        self.assertEqual(self.store.reserve(self.device, "r1", "other", now=T0).state, "mismatch")

    def test_results_survive_a_restart_and_an_acknowledgement(self):
        self.store.reserve(self.device, "r1", "fp", now=T0)
        self.store.complete(self.device, "r1", 200, "application/json", b"{}")
        self.assertTrue(self.store.acknowledge(self.device, "r1"))
        self.reopen()
        self.assertEqual(self.store.reserve(self.device, "r1", "fp").state, "replay")

    def test_only_a_pending_request_can_be_abandoned(self):
        self.store.reserve(self.device, "r1", "fp")
        self.store.abandon(self.device, "r1")
        self.assertEqual(self.store.reserve(self.device, "r1", "fp").state, "new")
        self.store.complete(self.device, "r1", 200, "application/json", b"{}")
        self.store.abandon(self.device, "r1")
        self.assertEqual(self.store.reserve(self.device, "r1", "fp").state, "replay")

    def test_completing_twice_is_an_error(self):
        self.store.reserve(self.device, "r1", "fp")
        self.store.complete(self.device, "r1", 200, "application/json", b"{}")
        with self.assertRaises(RuntimeError):
            self.store.complete(self.device, "r1", 200, "application/json", b"{}")

    def test_request_ids_are_per_device(self):
        other = self.enroll(b"o" * 32).device_id
        self.store.reserve(self.device, "r1", "fp")
        self.assertEqual(self.store.reserve(other, "r1", "different").state, "new")


class Events(Base):
    def test_events_come_back_in_order_and_bounded(self):
        for i in range(3):
            self.store.append_event("claude-1", "message", b"%d" % i, now=T0 + i)
        self.store.append_event("claude-2", "message", b"x", now=T0)
        self.assertEqual([bytes(r["body"]) for r in self.store.events("claude-1")], [b"0", b"1", b"2"])
        self.assertEqual(len(self.store.events("claude-1", limit=0)), 1)


class Migration(unittest.TestCase):
    """A schema-1 database made by an earlier build opens unchanged and keeps its promises."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp(dir=sandbox.HOME)) / "host.sqlite3"
        db = sqlite3.connect(self.path)
        db.executescript((FIXTURES / "host-store-v1.sql").read_text())
        db.close()
        self.store = HostStore(self.path)
        self.addCleanup(self.store.close)

    def test_devices_and_revocations_are_kept(self):
        self.assertIsNone(self.store.device(PHONE).revoked_at)
        self.assertEqual(self.store.device("44444444-4444-4444-8444-444444444444").revoked_at, 1795000500.0)

    def test_an_acknowledged_result_still_replays(self):
        replay = self.store.reserve(PHONE, "22222222-2222-4222-8222-222222222222", "f" * 64)
        self.assertEqual((replay.state, replay.body), ("replay", b'{"event_id":"e1","state":"accepted"}'))

    def test_an_interrupted_request_stays_pending(self):
        self.assertEqual(self.store.reserve(PHONE, "33333333-3333-4333-8333-333333333333", "e" * 64).state,
                         "pending")

    def test_events_are_kept(self):
        self.assertEqual(len(self.store.events("claude-1")), 1)


if __name__ == "__main__":
    unittest.main()
