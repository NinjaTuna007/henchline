"""call/canonical.py against the shared vectors the phone's implementation also runs."""

import hashlib
import unittest

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

from call import canonical
from tests.call.hostkit import VECTORS


class RequestVectors(unittest.TestCase):
    def test_every_vector_canonicalizes_byte_for_byte(self):
        for vector in VECTORS["requests"]:
            with self.subTest(vector["name"]):
                fields = dict(vector["input"])
                body = fields.pop("body_utf8").encode()
                message = canonical.request_bytes(body=body, **fields)
                self.assertEqual(message.decode(), vector["canonical"])
                self.assertEqual(hashlib.sha256(message).hexdigest(), vector["canonical_sha256"])

    def test_every_vector_signature_verifies_with_the_device_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTORS["device_key"]["private_seed_hex"]))
        public = key.public_key()
        self.assertEqual(canonical.b64url(public.public_bytes_raw()), VECTORS["device_key"]["public_b64url"])
        for vector in VECTORS["requests"]:
            with self.subTest(vector["name"]):
                public.verify(canonical.unb64url(vector["ed25519_signature"]), vector["canonical"].encode())
                # Ed25519 is deterministic: the phone can compare signatures, not just verify them.
                self.assertEqual(canonical.b64url(key.sign(vector["canonical"].encode())),
                                 vector["ed25519_signature"])

    def test_rejected_vectors_are_refused(self):
        for item in VECTORS["rejected_requests"]:
            fields = dict(VECTORS["requests"][0]["input"])
            fields.pop("body_utf8")
            fields.update(path=item["path"], query=item["query"])
            with self.subTest(item["name"]), self.assertRaises(ValueError):
                canonical.request_bytes(body=b"", **fields)

    def test_pairing_proof_vector(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        vector = VECTORS["pairing_proof"]
        message = canonical.pairing_bytes(host_id=vector["host_id"], nonce=vector["nonce"],
                                          algorithm=vector["algorithm"], public_key=vector["public_key"])
        self.assertEqual(message.decode(), vector["canonical"])
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTORS["device_key"]["private_seed_hex"]))
        self.assertEqual(canonical.b64url(key.sign(message)), vector["ed25519_signature"])

    def test_equivalent_spellings_sign_alike(self):
        self.assertEqual(canonical.canonical_path("/a/%7Eb%2f"), canonical.canonical_path("/a/~b%2F"))
        self.assertEqual(canonical.canonical_query("b=1&a=2"), canonical.canonical_query([("a", "2"), ("b", "1")]))

    def test_an_encoded_slash_is_not_a_separator(self):
        self.assertNotEqual(canonical.canonical_path("/chats/a%2Fb"), canonical.canonical_path("/chats/a/b"))

    def test_a_key_without_a_value_is_an_empty_value(self):
        self.assertEqual(canonical.canonical_query("a"), "a=")

    def test_newlines_cannot_smuggle_fields(self):
        vector = dict(VECTORS["requests"][0]["input"])
        vector.pop("body_utf8")
        vector["device_id"] = "dev\nice"
        with self.assertRaises(ValueError):
            canonical.request_bytes(body=b"", **vector)

    def test_relative_paths_are_refused(self):
        with self.assertRaises(ValueError):
            canonical.canonical_path("api/v1/chats")


class DescriptorJson(unittest.TestCase):
    def test_descriptor_vector(self):
        descriptor = VECTORS["descriptor"]
        signed = canonical.canonical_json(descriptor["unsigned"])
        self.assertEqual(signed.decode(), descriptor["canonical_json"])
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(canonical.unb64url(VECTORS["host_key"]["public_b64url"])).verify(
            canonical.unb64url(descriptor["signature"]), signed)

    def test_keys_sort_by_utf16_code_units(self):
        # U+1F600 is D83D DE00 in UTF-16, before U+FF61; code point order would put it last.
        self.assertEqual(canonical.canonical_json({"｡": 1, "\U0001F600": 2}),
                         '{"\U0001F600":2,"｡":1}'.encode())

    def test_control_characters_use_jcs_escapes(self):
        self.assertEqual(canonical.canonical_json("a\n\x01\"\\/"), b'"a\\n\\u0001\\"\\\\/"')

    def test_floats_and_unsafe_integers_are_refused(self):
        for value in (1.5, 2 ** 53, {"seq": -(2 ** 53)}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical.canonical_json(value)

    def test_nested_lists_and_literals(self):
        self.assertEqual(canonical.canonical_json({"b": [True, None, 0], "a": {}}), b'{"a":{},"b":[true,null,0]}')


class Base64Url(unittest.TestCase):
    def test_round_trip_without_padding(self):
        for size in range(0, 5):
            data = bytes(range(size))
            self.assertNotIn("=", canonical.b64url(data))
            self.assertEqual(canonical.unb64url(canonical.b64url(data)) if data else b"", data)

    def test_only_the_one_spelling_decodes(self):
        for bad in ("AA AA", "AA!AA", "AA==", "AB", "A", "AQ+/", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                canonical.unb64url(bad)
        self.assertEqual(canonical.unb64url("AQ"), b"\x01")



class Timestamps(unittest.TestCase):
    """rfc3339() goes into signed descriptors, pairing codes and events: its exact bytes are pinned here."""

    def test_the_exact_form(self):
        self.assertEqual(canonical.rfc3339(0), "1970-01-01T00:00:00Z")
        self.assertEqual(canonical.rfc3339(1795000200), "2026-11-18T11:10:00Z")
        self.assertEqual(canonical.rfc3339(1795000200.999), "2026-11-18T11:10:00Z")  # fractions dropped, not rounded
        self.assertEqual(canonical.rfc3339(951782400), "2000-02-29T00:00:00Z")

    def test_now_and_every_caller_spell_it_the_same(self):
        import datetime as dt
        from unittest import mock
        from call import host_config
        with mock.patch.object(canonical.time, "time", return_value=1795000200.5):
            self.assertEqual(canonical.rfc3339(), "2026-11-18T11:10:00Z")
            self.assertEqual(host_config.utc_now(), "2026-11-18T11:10:00Z")
        at = 1795000200
        old = dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # the form it replaced
        self.assertEqual(canonical.rfc3339(at), old)


if __name__ == "__main__":
    unittest.main()
