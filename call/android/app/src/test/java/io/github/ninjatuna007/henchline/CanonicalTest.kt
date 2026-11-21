package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.PKCS8EncodedKeySpec

/** The phone's canonical bytes against tests/fixtures/host-signatures.json: the same vectors the laptop runs. */
class CanonicalTest {
    private val vectors = JSONObject(javaClass.classLoader!!.getResource("host-signatures.json")!!.readText())

    private fun hex(text: String) = ByteArray(text.length / 2) { text.substring(2 * it, 2 * it + 2).toInt(16).toByte() }

    private fun ed25519Sign(seedHex: String, message: ByteArray): ByteArray {
        val pkcs8 = hex("302e020100300506032b657004220420") + hex(seedHex)
        val key = KeyFactory.getInstance("Ed25519").generatePrivate(PKCS8EncodedKeySpec(pkcs8))
        return Signature.getInstance("Ed25519").run { initSign(key); update(message); sign() }
    }

    private fun request(input: JSONObject, path: String = input.getString("path"), query: String = input.getString("query")) =
        Canonical.requestBytes(
            apiVersion = input.getString("api_version"), authorityId = input.getString("authority_id"),
            scheme = input.getString("scheme"), authority = input.getString("authority"),
            method = input.getString("method"), path = path, query = query,
            body = input.getString("body_utf8").toByteArray(), deviceId = input.getString("device_id"),
            timestamp = input.getString("timestamp").toLong(), nonce = input.getString("nonce"),
            requestId = input.getString("request_id"))

    @Test fun everyRequestVectorCanonicalizesByteForByte() {
        val requests = vectors.getJSONArray("requests")
        assertTrue(requests.length() >= 8)
        for (i in 0 until requests.length()) {
            val vector = requests.getJSONObject(i)
            val bytes = request(vector.getJSONObject("input"))
            assertEquals(vector.getString("name"), vector.getString("canonical"), String(bytes))
            assertEquals(vector.getString("name"), vector.getString("canonical_sha256"), Canonical.sha256Hex(bytes))
        }
    }

    @Test fun ed25519IsDeterministicSoSignaturesMatchExactly() {
        val seed = vectors.getJSONObject("device_key").getString("private_seed_hex")
        val requests = vectors.getJSONArray("requests")
        for (i in 0 until requests.length()) {
            val vector = requests.getJSONObject(i)
            assertEquals(vector.getString("ed25519_signature"),
                Canonical.b64url(ed25519Sign(seed, vector.getString("canonical").toByteArray())))
        }
    }

    @Test fun rejectedRequestsAreRefused() {
        val input = vectors.getJSONArray("requests").getJSONObject(0).getJSONObject("input")
        val rejected = vectors.getJSONArray("rejected_requests")
        for (i in 0 until rejected.length()) {
            val item = rejected.getJSONObject(i)
            try {
                request(input, item.getString("path"), item.getString("query"))
                fail("not refused: ${item.getString("name")}")
            } catch (expected: IllegalArgumentException) {
            }
        }
    }

    @Test fun pairingProofVector() {
        val vector = vectors.getJSONObject("pairing_proof")
        val bytes = Canonical.pairingBytes(vector.getString("host_id"), vector.getString("nonce"),
            vector.getString("algorithm"), vector.getString("public_key"))
        assertEquals(vector.getString("canonical"), String(bytes))
        assertEquals(vector.getString("ed25519_signature"),
            Canonical.b64url(ed25519Sign(vectors.getJSONObject("device_key").getString("private_seed_hex"), bytes)))
    }

    @Test fun descriptorVectorIsJcsAndVerifiesWithThePlatformsEd25519() {
        val vector = vectors.getJSONObject("descriptor")
        val unsigned = vector.getJSONObject("unsigned")
        assertEquals(vector.getString("canonical_json"), String(Canonical.jcs(unsigned), Charsets.UTF_8))
        val signed = JSONObject(unsigned.toString()).put("signature", vector.getString("signature"))
        val key = vectors.getJSONObject("host_key").getString("public_b64url")
        assertTrue(HostDescriptor.verify(signed, key))
        assertFalse(HostDescriptor.verify(JSONObject(signed.toString()).put("label", "Other"), key))
        assertFalse(HostDescriptor.verify(signed, vectors.getJSONObject("device_key").getString("public_b64url")))
    }

    @Test fun jcsDetails() {
        assertEquals("{\"😀\":2,\"｡\":1}",
            String(Canonical.jcs(JSONObject().put("｡", 1).put("😀", 2))))
        assertEquals("\"a\\n\\u0001\\\"\\\\/\"", String(Canonical.jcs("a\n\u0001\"\\/")))
        for (bad in listOf<Any>(1.5, 9007199254740992L, "\uD83D")) {
            try { Canonical.jcs(bad); fail("signed $bad") } catch (expected: IllegalArgumentException) {}
        }
    }

    @Test fun base64urlHasOneSpelling() {
        assertArrayEquals(byteArrayOf(1), Canonical.unb64url("AQ"))
        for (bad in listOf("AA AA", "AA!AA", "AA==", "AB", "A", "AQ+/", "")) {
            try { Canonical.unb64url(bad); fail("decoded $bad") } catch (expected: IllegalArgumentException) {}
        }
    }

    @Test fun traps() {
        assertEquals("a=&b=1", Canonical.query("&b=1&&a&"))
        assertEquals("q=a%2Bb", Canonical.query("q=a+b"))
        assertEquals("/chats/a%2Fb", Canonical.path("/chats/a%2fb"))
        assertEquals("/~A", Canonical.path("/%7e%41"))
        assertEquals("/%C5%BC", Canonical.path("/ż"))
    }

    @Test fun p256SignaturesVerifyAsTheHostChecksThem() {
        val pair = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
        val key = P256Key(pair.private, pair.public.encoded)
        val message = "HENCHLINE-SIG-V1\n".toByteArray()
        val signature = key.sign(message)
        assertTrue(Signature.getInstance("SHA256withECDSA").run { initVerify(pair.public); update(message); verify(signature) })
        assertEquals("p256", key.algorithm)
        assertArrayEquals(pair.public.encoded, Canonical.unb64url(key.publicKey))
    }
}
