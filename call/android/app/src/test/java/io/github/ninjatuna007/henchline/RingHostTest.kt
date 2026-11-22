package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** A ring record says which computer rings when it isn't the laptop, and every laptop ring stays the laptop's. */
class RingHostTest {
    @Test fun aLaptopRingNamesNoComputer() {
        assertNull(Ringing.hostOf(JSONObject().put("agent", "claude").put("reply", "r").put("until", 1L)))  // as before
        assertNull(Ringing.hostOf(JSONObject().put("host_id", "")))
        assertNull(Ringing.hostOf(null))
    }

    @Test fun anotherComputersRingCarriesWhereToAnswer() {
        val ring = JSONObject().put("agent", "claude").put("reply", "r").put("host_id", "h-b").put("session", "claude-1")
            .put("host_label", "Desk")
        assertEquals(RingHost("h-b", "claude-1", "Desk"), Ringing.hostOf(ring))
    }

    private val ring = JSONObject().put("agent", "claude").put("reply", "https://example.invalid/r1").put("host_id", "h-b")
        .put("session", "claude-1").put("host_label", "Desk").put("answer_key", "k-123")

    @Test fun onlyTheAppsOwnAnswerForTheRingNowStartsTheCall() {
        assertTrue(Ringing.mayAnswer(ring, "h-b", "claude-1", "https://example.invalid/r1", "k-123"))
        // A link or another app: no key, a guessed key, another chat, another computer, another reply
        assertFalse(Ringing.mayAnswer(ring, "h-b", "claude-1", "https://example.invalid/r1", ""))
        assertFalse(Ringing.mayAnswer(ring, "h-b", "claude-1", "https://example.invalid/r1", "k-124"))
        assertFalse(Ringing.mayAnswer(ring, "h-b", "claude-2", "https://example.invalid/r1", "k-123"))
        assertFalse(Ringing.mayAnswer(ring, "h-c", "claude-1", "https://example.invalid/r1", "k-123"))
        assertFalse(Ringing.mayAnswer(ring, "h-b", "claude-1", "x", "k-123"))
        assertFalse(Ringing.mayAnswer(ring, "h-b", "claude-1", null, "k-123"))
        // No ring sounding, or the laptop's: nothing to answer here
        assertFalse(Ringing.mayAnswer(null, "h-b", "claude-1", "https://example.invalid/r1", "k-123"))
        assertFalse(Ringing.mayAnswer(JSONObject().put("reply", "https://example.invalid/r1"), "h-b", "claude-1",
            "https://example.invalid/r1", ""))
        // A ring with no key recorded never matches an empty one
        assertFalse(Ringing.mayAnswer(JSONObject(ring.toString()).apply { remove("answer_key") }, "h-b", "claude-1",
            "https://example.invalid/r1", ""))
    }
}
