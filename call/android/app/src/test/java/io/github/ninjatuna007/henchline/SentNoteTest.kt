package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

/** A message the computer typed but couldn't confirm (a Cursor terminal's bridge that never answered) says so. */
class SentNoteTest {
    @Test fun aTextTypedWithANoteKeepsIt() {
        val sent = Messages.sentFrom(JSONObject().put("to", "Claude").put("queued", true).put("note", "I may have typed that"),
            200, "pc")
        assertEquals(Messages.Sent("", true, told = "I may have typed that"), sent)
    }

    @Test fun aPlainTextHasNone() {
        assertEquals(Messages.Sent("", false), Messages.sentFrom(JSONObject().put("to", "Claude"), 200, "pc"))
    }

    @Test fun aRefusedTextSaysWhy() {
        assertEquals(Messages.Sent("Not sent: gone", false), Messages.sentFrom(JSONObject().put("note", "Not sent: gone"), 200, "pc"))
        assertEquals(Messages.Sent("pc said 502", false), Messages.sentFrom(JSONObject(), 502, "pc"))
    }

    @Test fun aKeptMessageShowsItsNoteUnderIt() {
        val event = JSONObject().put("state", "delivered")
        assertEquals("✓ delivered", ChatEvents.mineStatus(event))
        assertEquals("✓ delivered · I may have typed that", ChatEvents.mineStatus(event.put("note", "I may have typed that")))
        assertEquals("not delivered: no agent", ChatEvents.mineStatus(JSONObject().put("state", "failed").put("note", "no agent")))
    }
}
