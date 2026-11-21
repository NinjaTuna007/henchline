"""call/pair.py: the host's signing key, its signed descriptor, and the pairing code it shows."""

import calendar
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

from call import canonical, host_config, pair
from call.host_store import HostStore
from tests.call import hostkit


class Signer(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        self.key_file = self.folder / "keys/host.ed25519"

    def test_the_key_is_made_once_privately_and_reused(self):
        first = pair.HostSigner.load_or_create(self.key_file)
        self.assertEqual(self.key_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.key_file.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(pair.HostSigner.load_or_create(self.key_file).public_bytes(), first.public_bytes())

    def test_a_readable_key_is_refused(self):
        pair.HostSigner.load_or_create(self.key_file)
        self.key_file.chmod(0o640)
        with self.assertRaises(ValueError):
            pair.HostSigner.load_or_create(self.key_file)

    def test_a_malformed_key_is_refused(self):
        self.key_file.parent.mkdir(mode=0o700)
        self.key_file.write_bytes(b"short")
        self.key_file.chmod(0o600)
        with self.assertRaises(ValueError):
            pair.HostSigner.load_or_create(self.key_file)

    def test_the_descriptor_is_signed_over_its_canonical_json(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        config = hostkit.config(self.folder)
        signer = pair.HostSigner.load_or_create(config.key_file)
        descriptor = signer.descriptor(config)
        signature = canonical.unb64url(descriptor.pop("signature"))
        public = Ed25519PublicKey.from_public_bytes(canonical.unb64url(descriptor["host_signing_key"]))
        public.verify(signature, canonical.canonical_json(descriptor))
        self.assertEqual(descriptor["updated_at"], "2026-11-21T12:00:00Z")
        self.assertEqual(descriptor["descriptor_seq"], 3)
        self.assertEqual(descriptor["host_id"], hostkit.HOST_ID)
        self.assertEqual(descriptor["schema"], "henchline.host/v1")
        self.assertFalse(descriptor["capabilities"]["calls"])

    def test_the_descriptor_holds_no_secret(self):
        config = hostkit.config(self.folder)
        signer = pair.HostSigner.load_or_create(config.key_file)
        text = json.dumps(signer.descriptor(config))
        self.assertNotIn(canonical.b64url(config.key_file.read_bytes()), text)
        self.assertNotIn(str(config.database), text)


class Command(unittest.TestCase):
    def test_the_code_is_a_live_single_use_nonce(self):
        folder = Path(tempfile.mkdtemp(dir=sandbox.HOME))
        path = folder / "host.json"
        config = host_config.initialize(path, label="Laptop", base_url="https://m.example.ts.net/",
                                        capabilities={"chat": True})
        out = io.StringIO()
        with mock.patch("sys.argv", ["pair", "--config", str(path)]), contextlib.redirect_stdout(out):
            self.assertEqual(pair.main(), 0)
        code = json.loads(out.getvalue())
        self.assertEqual(code["schema"], "henchline.pair/v1")
        self.assertEqual(code["host_id"], config.host_id)
        self.assertEqual(code["tls_authority"], "m.example.ts.net")
        self.assertEqual(code["host_signing_key"],
                         canonical.b64url(pair.HostSigner.load_or_create(config.key_file).public_bytes()))
        expires = calendar.timegm(time.strptime(code["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
        self.assertAlmostEqual(expires, time.time() + 300, delta=5)
        self.assertEqual(set(code), {"schema", "host_id", "label", "base_url", "tls_authority",
                                     "host_signing_key", "host_key_algorithm", "nonce", "expires_at"})
        store = HostStore(config.database)
        self.addCleanup(store.close)
        store.claim_pairing(code["nonce"], "ed25519", b"k" * 32)
        with self.assertRaises(TimeoutError):
            store.claim_pairing(code["nonce"], "ed25519", b"k" * 32)


if __name__ == "__main__":
    unittest.main()
