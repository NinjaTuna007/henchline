package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

/** The self-test's embedded vector is the fixture's, byte for byte, and passes on the JVM. */
@Suppress("NewApi")  // the JVM has Ed25519; SelfTest is API 33+ only on a phone
class VectorsTest {
    private val vectors = JSONObject(javaClass.classLoader!!.getResource("host-signatures.json")!!.readText())

    @Test fun embeddedVectorIsTheFixtures() {
        val descriptor = vectors.getJSONObject("descriptor")
        assertEquals(vectors.getJSONObject("host_key").getString("public_b64url"), SelfTest.HOST_KEY)
        assertEquals(descriptor.getString("canonical_json"), SelfTest.CANONICAL)
        val signed = JSONObject(SelfTest.SIGNED_DESCRIPTOR)
        assertEquals(descriptor.getString("signature"), signed.getString("signature"))
        signed.remove("signature")
        assertEquals(descriptor.getString("canonical_json"), String(Canonical.jcs(signed), Charsets.UTF_8))
    }

    @Test fun selfTestPassesOnTheJvm() {
        assertEquals("", SelfTest.run())
    }
}
