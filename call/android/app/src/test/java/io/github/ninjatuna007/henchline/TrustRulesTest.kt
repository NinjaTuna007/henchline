package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** A new chat's folder the agent doesn't trust yet gets the typed folder's confirm, and only then trust: true. */
class TrustRulesTest {
    @Test fun untrustedOnlyWhenTheComputerSaysSoForThatAgent() {
        val trusted = JSONObject().put("claude", false).put("codex", true).put("cursor", false)
        assertTrue(TrustRules.untrusted(trusted, "claude"))
        assertFalse(TrustRules.untrusted(trusted, "codex"))
        assertFalse(TrustRules.untrusted(null, "claude"))               // an older computer: no map, nothing asked
        assertFalse(TrustRules.untrusted(JSONObject(), "claude"))       // not said for that agent
    }

    @Test fun onlyTheAgentsWhoseQuestionTheComputerAnswers() {
        assertEquals(setOf("claude", "codex"), TrustRules.ASKING)  // call/server.py ASKS_TRUST
    }
}
