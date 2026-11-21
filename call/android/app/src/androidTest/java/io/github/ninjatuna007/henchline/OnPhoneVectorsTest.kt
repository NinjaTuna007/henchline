package io.github.ninjatuna007.henchline

import android.os.Build
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

/** On the phone itself (connectedDebugAndroidTest): Android's own org.json, Ed25519 provider and Keystore against
 *  the laptop's vectors (tests/fixtures/host-signatures.json, packed as test assets). Nothing is paired or sent. */
@RunWith(AndroidJUnit4::class)
class OnPhoneVectorsTest {
    private val vectors = JSONObject(InstrumentationRegistry.getInstrumentation().context.assets
        .open("host-signatures.json").bufferedReader().readText())

    @Test fun selfTestPasses() {
        assumeTrue("multi-host needs Android 13+", Build.VERSION.SDK_INT >= MultiHost.MIN_SDK)
        assertEquals("", SelfTest.run())
        assertTrue(MultiHost.available())
    }

    @Test fun everyRequestVectorCanonicalizes() {
        val requests = vectors.getJSONArray("requests")
        for (i in 0 until requests.length()) {
            val vector = requests.getJSONObject(i)
            val input = vector.getJSONObject("input")
            val bytes = Canonical.requestBytes(
                input.getString("api_version"), input.getString("authority_id"), input.getString("scheme"),
                input.getString("authority"), input.getString("method"), input.getString("path"),
                input.getString("query"), input.getString("body_utf8").toByteArray(), input.getString("device_id"),
                input.getString("timestamp").toLong(), input.getString("nonce"), input.getString("request_id"))
            assertEquals(vector.getString("name"), vector.getString("canonical"), String(bytes))
        }
    }

    @Test fun descriptorThroughAndroidsOrgJson() {
        assumeTrue(Build.VERSION.SDK_INT >= MultiHost.MIN_SDK)
        val descriptor = vectors.getJSONObject("descriptor")
        val unsigned = JSONObject(descriptor.getJSONObject("unsigned").toString())
        assertEquals(descriptor.getString("canonical_json"), String(Canonical.jcs(unsigned), Charsets.UTF_8))
        val signed = JSONObject(unsigned.toString()).put("signature", descriptor.getString("signature"))
        assertTrue(HostDescriptor.verify(signed, vectors.getJSONObject("host_key").getString("public_b64url")))
    }

    @Test fun keystoreP256KeySignsAsTheHostChecks() {
        val key = HostKeys.forAuthority("selftest", "00000000-0000-4000-8000-0000000000ff")
        try {
            val message = "HENCHLINE-SIG-V1\n".toByteArray()
            val public = java.security.KeyFactory.getInstance("EC")
                .generatePublic(java.security.spec.X509EncodedKeySpec(Canonical.unb64url(key.publicKey)))
            val signature = key.sign(message)
            assertTrue(java.security.Signature.getInstance("SHA256withECDSA")
                .run { initVerify(public); update(message); verify(signature) })
            assertEquals(key.publicKey, HostKeys.forAuthority("selftest", "00000000-0000-4000-8000-0000000000ff").publicKey)
        } finally {
            HostKeys.delete("selftest", "00000000-0000-4000-8000-0000000000ff")
        }
    }
}
