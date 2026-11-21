package io.github.ninjatuna007.henchline

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec

/** Pairing, signed calls and the host list against a fake host that checks what the real one checks. */
@Suppress("NewApi")
class PairingTest {
    private val hostId = "00000000-0000-4000-8000-000000000001"
    private val deviceId = "11111111-1111-4111-8111-111111111111"
    private val origin = "https://host-a.example.ts.net/"
    private val hostPair = KeyPairGenerator.getInstance("Ed25519").generateKeyPair()
    private val hostKey = Canonical.b64url(hostPair.public.encoded.takeLast(32).toByteArray())

    private fun phoneKey(): P256Key {
        val pair = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
        return P256Key(pair.private, pair.public.encoded)
    }

    private fun descriptor(base: String = origin, signer: java.security.PrivateKey = hostPair.private,
                           key: String = hostKey, id: String = hostId): JSONObject {
        val authority = base.toHttpUrl().let { if (it.port == 443) it.host else "${it.host}:${it.port}" }
        val unsigned = JSONObject().put("schema", "henchline.host/v1").put("host_id", id).put("label", "Host A")
            .put("base_url", base).put("tls_authority", authority).put("api_version", "1.0").put("descriptor_seq", 3)
            .put("updated_at", "2026-11-21T12:00:00Z").put("host_signing_key", key).put("host_key_algorithm", "ed25519")
            .put("min_app_version", 1).put("capabilities", JSONObject().put("chat", true).put("calls", false))
        val signature = Signature.getInstance("Ed25519").run { initSign(signer); update(Canonical.jcs(unsigned)); sign() }
        return JSONObject(unsigned.toString()).put("signature", Canonical.b64url(signature))
    }

    /** A host: serves its descriptor, and accepts the code ABCDEFGHJK once with a valid proof. */
    inner class FakeHost(var served: JSONObject = descriptor(), var claimAnswer: ((JSONObject) -> HttpAnswer)? = null) : Http {
        var claims = 0
        var lastClaim: JSONObject? = null
        val sent = mutableListOf<Triple<String, HttpUrl, Map<String, String>>>()
        override fun send(method: String, url: HttpUrl, body: ByteArray?, headers: Map<String, String>): HttpAnswer {
            sent += Triple(method, url, headers)
            if (url.encodedPath == "/.well-known/henchline/v1/host") return HttpAnswer(200, JSONObject().put("descriptor", served).toString())
            if (url.encodedPath == "/api/v1/pairing/claim") {
                claims++
                val claim = JSONObject(String(body!!)).also { lastClaim = it }
                claimAnswer?.let { return it(claim) }
                val public = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(Canonical.unb64url(claim.getString("public_key"))))
                val proved = Signature.getInstance("SHA256withECDSA").run {
                    initVerify(public)
                    update(Canonical.pairingBytes(hostId, claim.getString("nonce"), claim.getString("algorithm"), claim.getString("public_key")))
                    verify(Canonical.unb64url(claim.getString("proof")))
                }
                if (!proved) return HttpAnswer(422, """{"error":{"code":"validation_failed"}}""")
                if (claim.getString("nonce") != "ABCDEFGHJK" || claims > 1) return HttpAnswer(410, """{"error":{"code":"pairing_expired"}}""")
                return HttpAnswer(201, JSONObject().put("host_id", hostId).put("device_id", deviceId).put("descriptor", served).toString())
            }
            return HttpAnswer(404, """{"error":{"code":"not_found"}}""")
        }
    }

    private fun expectError(words: String, block: () -> Unit) {
        try { block(); fail("expected: $words") } catch (e: PairingError) { assertTrue(e.message, e.message!!.contains(words)) }
    }

    @Test fun addressesBecomeHttpsOrigins() {
        val pairing = Pairing(FakeHost())
        assertEquals(origin, pairing.origin(" host-a.example.ts.net ").toString())
        assertEquals(origin, pairing.origin("https://host-a.example.ts.net/some/path?x=1").toString())
        expectError("https") { pairing.origin("http://host-a.example.ts.net/") }
    }

    @Test fun pairWithTheTypedCode() {
        val fake = FakeHost()
        val pairing = Pairing(fake)
        val candidate = pairing.lookUp(pairing.origin("host-a.example.ts.net"))
        assertEquals(Canonical.fingerprint(Canonical.unb64url(hostKey)), candidate.fingerprint)
        val key = phoneKey()
        val host = pairing.claim(candidate, "abcde-fghjk", { key }, "Test phone")
        assertEquals("ABCDEFGHJK", fake.lastClaim!!.getString("nonce"))
        assertEquals("p256", fake.lastClaim!!.getString("algorithm"))
        assertEquals(listOf(hostId, deviceId, hostKey, origin, "host-a.example.ts.net"),
            listOf(host.hostId, host.deviceId, host.hostKey, host.baseUrl, host.tlsAuthority))
        expectError("expired or was used") { pairing.claim(candidate, "ABCDEFGHJK", { key }, "Test phone") }
    }

    @Test fun aDescriptorForAnotherAddressIsRefused() {
        val pairing = Pairing(FakeHost(descriptor(base = "https://elsewhere.example.ts.net/")))
        expectError("lives at") { pairing.lookUp(pairing.origin("host-a.example.ts.net")) }
    }

    @Test fun aDescriptorNotSignedByItsKeyIsRefused() {
        val other = KeyPairGenerator.getInstance("Ed25519").generateKeyPair()
        val pairing = Pairing(FakeHost(descriptor(signer = other.private)))
        expectError("isn't signed") { pairing.lookUp(pairing.origin("host-a.example.ts.net")) }
    }

    @Test fun aClaimAnswerFromAnotherKeyOrHostIsRefused() {
        val other = KeyPairGenerator.getInstance("Ed25519").generateKeyPair()
        val otherKey = Canonical.b64url(other.public.encoded.takeLast(32).toByteArray())
        val fake = FakeHost()
        val pairing = Pairing(fake)
        val candidate = pairing.lookUp(pairing.origin("host-a.example.ts.net"))
        fake.claimAnswer = { HttpAnswer(201, JSONObject().put("host_id", hostId).put("device_id", deviceId)
            .put("descriptor", descriptor(signer = other.private, key = otherKey)).toString()) }
        expectError("isn't signed") { pairing.claim(candidate, "ABCDEFGHJK", { phoneKey() }, "p") }
        fake.claimAnswer = { HttpAnswer(201, JSONObject().put("host_id", "00000000-0000-4000-8000-000000000002")
            .put("device_id", deviceId).put("descriptor", descriptor()).toString()) }
        expectError("doesn't match") { pairing.claim(candidate, "ABCDEFGHJK", { phoneKey() }, "p") }
    }

    @Test fun badCodesNeverLeaveThePhone() {
        val fake = FakeHost()
        val pairing = Pairing(fake)
        val candidate = pairing.lookUp(pairing.origin("host-a.example.ts.net"))
        var made = 0
        expectError("letters and digits") { pairing.claim(candidate, "AB_CD", { made++; phoneKey() }, "p") }
        assertEquals(0, fake.claims)
        assertEquals("no key is made for a code that can't be sent", 0, made)
    }

    private fun sentness(answer: () -> HttpAnswer): PairingError {
        val fake = FakeHost()
        val pairing = Pairing(fake)
        val candidate = pairing.lookUp(pairing.origin("host-a.example.ts.net"))
        fake.claimAnswer = { answer() }
        try { pairing.claim(candidate, "ABCDEFGHJK", { phoneKey() }, "p") } catch (e: PairingError) { return e }
        fail("paired"); throw IllegalStateException()
    }

    @Test fun aClaimThatMayHaveLandedSaysHowToRevokeIt() {
        for (thrown in listOf(java.net.UnknownHostException("x"), java.net.ConnectException("x"))) {
            val e = sentness { throw thrown }
            assertFalse(e.sent); assertFalse(e.message!!.contains("voice-call devices"))
        }
        for (answer in listOf<() -> HttpAnswer>({ throw java.net.SocketTimeoutException("read") }, { HttpAnswer(500, "") },
                { HttpAnswer(201, JSONObject().put("host_id", hostId).put("device_id", "nope").toString()) })) {
            val e = sentness(answer)
            assertTrue(e.sent); assertTrue(e.message, e.message!!.contains("voice-call devices revoke"))
        }
        val refused = sentness { HttpAnswer(410, """{"error":{"code":"pairing_expired"}}""") }
        assertFalse(refused.sent)
    }

    private fun host() = Host(hostId, "Host A", origin, "host-a.example.ts.net", hostKey, deviceId, "{}", 1L)

    @Test fun signedCallsSignWhatIsSent() {
        val fake = FakeHost()
        val key = phoneKey()
        HostClient(host(), key, fake, clock = { 1795000000 }).call("GET", listOf("api", "v1", "chats", "a/b ż", "events"),
            listOf("limit" to "5", "cursor" to "x y+z"))
        val (method, url, headers) = fake.sent.single()
        assertEquals("/api/v1/chats/a%2Fb%20%C5%BC/events", url.encodedPath)
        val message = Canonical.requestBytes("1.0", hostId, "https", "host-a.example.ts.net", method, url.encodedPath,
            url.encodedQuery ?: "", ByteArray(0), deviceId, 1795000000, headers.getValue("X-Henchline-Nonce"),
            headers.getValue("X-Henchline-Request-Id"))
        val public = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(Canonical.unb64url(key.publicKey)))
        assertTrue(Signature.getInstance("SHA256withECDSA").run {
            initVerify(public); update(message); verify(Canonical.unb64url(headers.getValue("X-Henchline-Signature")))
        })
        assertEquals(16, Canonical.unb64url(headers.getValue("X-Henchline-Nonce")).size)
        assertEquals(hostId, headers["X-Henchline-Host"])
    }

    @Test fun anAnswerNamingAnotherHostIsAnError() {
        val client = HostClient(host(), phoneKey(), { _, _, _, _ -> HttpAnswer(200, """{"host_id":"00000000-0000-4000-8000-000000000002"}""") })
        try { client.call("GET", listOf("api", "v1", "capabilities")); fail() } catch (expected: java.io.IOException) {}
    }

    @Test fun revocationIsDoneWhenTheHostSaysSoOrAlreadyHad() {
        assertTrue(HostClient(host(), phoneKey(), { _, _, _, _ -> HttpAnswer(200, """{"host_id":"$hostId","revoked":true}""") }).revokeSelf())
        assertTrue(HostClient(host(), phoneKey(), { _, _, _, _ -> HttpAnswer(403, """{"error":{"code":"device_revoked"}}""") }).revokeSelf())
        assertTrue(HostClient(host(), phoneKey(), { _, _, _, _ ->
            HttpAnswer(401, """{"error":{"code":"bad_signature","reason":"unknown_device"}}""") }).revokeSelf())
        assertFalse(HostClient(host(), phoneKey(), { _, _, _, _ ->
            HttpAnswer(401, """{"error":{"code":"bad_signature","message":"request signature did not verify"}}""") }).revokeSelf())
        assertFalse(HostClient(host(), phoneKey(), { _, _, _, _ -> throw java.io.IOException("offline") }).revokeSelf())
        assertFalse(HostClient(host(), phoneKey(), { _, _, _, _ -> HttpAnswer(503, "") }).revokeSelf())
    }

    @Test fun aSkewedClockIsNamedWithItsSize() {
        val client = HostClient(host(), phoneKey(), { _, _, _, _ ->
            HttpAnswer(401, """{"error":{"code":"timestamp_out_of_window","server_time":1795000300}}""") }, clock = { 1795000000 })
        try { client.call("GET", listOf("api", "v1", "chats")); fail() } catch (e: ClockSkew) {
            assertEquals(300, e.seconds); assertTrue(e.message!!.contains("300 s behind Host A"))
        }
    }

    private fun withDescriptor(d: JSONObject) = host().copy(descriptor = d.toString())

    @Test fun capabilitiesComeOnlyFromTheVerifiedDescriptor() {
        val client = HostClient(withDescriptor(descriptor()), phoneKey(), { _, _, _, _ -> fail(); HttpAnswer(0, "") })
        assertTrue(client.offers("chat"))
        assertFalse(client.offers("calls"))
        assertFalse(client.offers("teleport"))
        client.require("chat")
        try { client.require("calls"); fail() } catch (expected: CapabilityMissing) {}
    }

    private fun refreshed(served: JSONObject, stored: JSONObject = descriptor()): Host =
        HostClient(withDescriptor(stored), phoneKey(), { _, _, _, _ ->
            HttpAnswer(200, JSONObject().put("host_id", hostId).put("descriptor", served).toString()) }).refresh()

    private fun reseq(seq: Int, label: String = "Host A", base: String = origin): JSONObject {
        val d = descriptor(base = base)
        val unsigned = JSONObject(d.toString()).apply { remove("signature"); put("descriptor_seq", seq); put("label", label) }
        val signature = Signature.getInstance("Ed25519").run { initSign(hostPair.private); update(Canonical.jcs(unsigned)); sign() }
        return JSONObject(unsigned.toString()).put("signature", Canonical.b64url(signature))
    }

    @Test fun refreshTakesNewerSignedDescriptorsOnly() {
        val newer = refreshed(reseq(4, "Host A, renamed", "https://host-a2.example.ts.net/"))
        assertEquals(listOf("Host A, renamed", "https://host-a2.example.ts.net/", "host-a2.example.ts.net"),
            listOf(newer.label, newer.baseUrl, newer.tlsAuthority))
        try { refreshed(reseq(2)); fail("older accepted") } catch (expected: DescriptorRefused) {}
        val other = KeyPairGenerator.getInstance("Ed25519").generateKeyPair()
        try { refreshed(descriptor(signer = other.private)); fail("other key accepted") } catch (expected: DescriptorRefused) {}
    }

    @Test fun theRegistryKeepsHostsAcrossReads() {
        val file = File(Files.createTempDirectory("hosts").toFile(), "hosts.json")
        val registry = HostRegistry(file)
        assertEquals(emptyList<Host>(), registry.all())
        registry.put(host())
        registry.put(host().copy(revocationPending = true))
        assertEquals(listOf(host().copy(revocationPending = true)), HostRegistry(file).all())
        registry.remove(hostId)
        assertEquals(emptyList<Host>(), registry.all())
    }

    @Test fun codesAndFingerprintsMatchTheLaptop() {
        val vectors = JSONObject(javaClass.classLoader!!.getResource("host-signatures.json")!!.readText())
        for (name in listOf("host_key", "device_key")) {
            val key = vectors.getJSONObject(name)
            assertEquals(key.getString("fingerprint"), Canonical.fingerprint(Canonical.unb64url(key.getString("public_b64url"))))
        }
        assertEquals("ABCDEFGHJK", Canonical.pairingCode("abcde-fghjk"))
        assertEquals("00111", Canonical.pairingCode(" 0o1il "))
        for (bad in listOf("", "AB_CD", "U1234")) {
            try { Canonical.pairingCode(bad); fail(bad) } catch (expected: IllegalArgumentException) {}
        }
    }
}
