package io.github.ninjatuna007.henchline

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/** The app's own routes signed for a paired computer offering legacy_routes, and nothing else signed. */
@Suppress("NewApi")
class LegacySignerTest {
    private val id = "00000000-0000-4000-8000-00000000000a"
    private val device = "11111111-1111-4111-8111-111111111111"
    private val pair = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
    private val key = P256Key(pair.private, pair.public.encoded)

    private fun host(legacy: Boolean = true, pending: Boolean = false, base: String = "https://laptop.example.ts.net/") =
        Host(id, "Laptop", base, base.removePrefix("https://").trimEnd('/'), "", device,
            JSONObject().put("host_id", id).put("capabilities", JSONObject().put("chat", true).put("legacy_routes", legacy)).toString(),
            1L, pending)

    private fun sign(request: Request, vararg hosts: Host = arrayOf(host())) =
        LegacySigning.sign(request, hosts.toList(), { key }, { 1_800_000_000L })

    /** What the computer checks: the canonical bytes rebuilt from what is sent, under this phone's key. */
    private fun verifies(request: Request, digest: String, h: Host = host()): Boolean {
        val message = Canonical.requestBytes("1.0", h.hostId, "https", h.tlsAuthority, request.method,
            request.url.encodedPath, request.url.encodedQuery ?: "", ByteArray(0), request.header("X-Henchline-Device")!!,
            request.header("X-Henchline-Timestamp")!!.toLong(), request.header("X-Henchline-Nonce")!!,
            request.header("X-Henchline-Request-Id")!!, digest)
        return Signature.getInstance("SHA256withECDSA").run {
            initVerify(pair.public); update(message); verify(Canonical.unb64url(request.header("X-Henchline-Signature")!!))
        }
    }

    @Test fun aPostIsSignedOverItsExactBodyAndKeepsItsOrigin() {
        val body = """{"text":"hi","session":"claude-1"}"""
        val request = Request.Builder().url("https://laptop.example.ts.net/text").header("Origin", "https://laptop.example.ts.net")
            .post(body.toRequestBody("application/json".toMediaType())).build()
        val signed = sign(request)!!
        assertTrue(verifies(signed, Canonical.sha256Hex(body.toByteArray())))
        assertEquals("https://laptop.example.ts.net", signed.header("Origin"))
        assertEquals(device, signed.header("X-Henchline-Device"))
        assertEquals(id, signed.header("X-Henchline-Host"))
        assertNull(signed.header("X-Henchline-Content-SHA256"))  // only uploads declare it
    }

    @Test fun aGetWithAQueryAndAWebSocketUpgrade() {
        val get = sign(Request.Builder().url("https://laptop.example.ts.net/chat/minions?session=a%2Fb&x=1").build())!!
        assertTrue(verifies(get, Canonical.sha256Hex(ByteArray(0))))
        val ws = sign(Request.Builder().url("wss://laptop.example.ts.net/call?resume=1").build())!!  // OkHttp's https form
        assertEquals("/call", ws.url.encodedPath)
        assertTrue(verifies(ws, Canonical.sha256Hex(ByteArray(0))))
    }

    @Test fun uploadsDeclareTheDigestTheyAreSignedOver() {
        val wav = ByteArray(300_000) { (it % 251).toByte() }
        val request = Request.Builder().url("https://laptop.example.ts.net/voice-note?session=claude-1")
            .post(wav.toRequestBody("audio/wav".toMediaType())).build()
        val signed = sign(request)!!
        val digest = Canonical.sha256Hex(wav)
        assertEquals(digest, signed.header("X-Henchline-Content-SHA256"))
        assertTrue(verifies(signed, digest))
    }

    @Test fun onlyAPairedComputerThatOffersIt() {
        val text = Request.Builder().url("https://laptop.example.ts.net/text").post("{}".toRequestBody()).build()
        assertNull(sign(text, host(legacy = false)))                                       // not offered
        assertNull(sign(text, host(pending = true)))                                       // being removed
        assertNull(sign(text, host(base = "https://other.example.ts.net/")))              // another address
        assertNull(sign(Request.Builder().url("https://laptop.example.ts.net:8443/text").build()))  // another port
        assertNull(sign(Request.Builder().url("http://laptop.example.ts.net/text").build()))        // not https
        assertNull(LegacySigning.sign(text, listOf(host()), { null }))                     // no key: never a new one
        assertNotNull(sign(text, host(base = "https://other.example.ts.net/"), host()))    // the right one of several
    }

    @Test fun theSignedApiAndSignedRequestsAreLeftAlone() {
        assertNull(sign(Request.Builder().url("https://laptop.example.ts.net/api/v1/chats").build()))
        assertNull(sign(Request.Builder().url("https://laptop.example.ts.net/.well-known/henchline/v1/host").build()))
        val once = sign(Request.Builder().url("https://laptop.example.ts.net/away").build())!!
        assertNull(sign(once))
    }

    @Test fun aBodyThatCanBeReadOnlyOnceGoesUnsigned() {
        val oneShot = object : RequestBody() {
            override fun contentType() = null
            override fun isOneShot() = true
            override fun writeTo(sink: BufferedSink) { sink.writeUtf8("x") }
        }
        assertNull(sign(Request.Builder().url("https://laptop.example.ts.net/share/file?name=a").post(oneShot).build()))
    }
}
