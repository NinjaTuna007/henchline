package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** henchline://chat links: what they name, and everything else refused. */
class ChatKeyTest {
    private val host = "00000000-0000-4000-8000-000000000001"

    private fun query(link: String) = link.substringAfter("?").split("&").associate { it.substringBefore("=") to it.substringAfter("=") }

    @Test fun aLinkNamesItsChatExactly() {
        for (session in listOf("claude-1", "cursor:chat 7f", "a/b", "żółw", "x".repeat(256))) {
            val q = query(ChatKey(host, session).link())
            assertEquals(host to session, ChatKey.parse(q["host_id"], q["session_id"]))
        }
    }

    @Test fun withoutASessionItIsThatComputersList() {
        assertEquals(host to null, ChatKey.parse(host, null))
        assertEquals(host to null, ChatKey.parse(host, ""))
    }

    @Test fun anythingElseIsRefused() {
        val ok = Canonical.b64url("claude-1".toByteArray())
        for ((h, s) in listOf(
            null to ok, "not-a-uuid" to ok, "0000000A-0000-4000-8000-00000000000B" to ok, "$host " to ok,
            host to "AA==", host to "claude-1", host to Canonical.b64url("a\nb".toByteArray()),
            host to Canonical.b64url("x".repeat(257).toByteArray()), host to Canonical.b64url(byteArrayOf(-1, -2)),
        )) assertNull("$h / $s", ChatKey.parse(h, s))
    }
}
