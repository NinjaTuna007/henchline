package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** A computer's chat events as the chat draws them, call lines included. */
class ChatEventsTest {
    private fun e(kind: String, vararg pairs: Pair<String, String>) = JSONObject().put("kind", kind).apply { pairs.forEach { put(it.first, it.second) } }

    @Test fun eachKindHasItsLook() {
        assertEquals(EventLook.AGENT_TEXT, ChatEvents.lookOf(e("agent_text")))
        assertEquals(EventLook.MINE, ChatEvents.lookOf(e("message")))
        assertEquals(EventLook.CALL_YOU, ChatEvents.lookOf(e("call_line", "from" to "you")))
        assertEquals(EventLook.CALL_THEM, ChatEvents.lookOf(e("call_line", "from" to "Claude")))
        assertEquals(EventLook.NONE, ChatEvents.lookOf(e("something_newer")))  // kept in place, not drawn as a message
    }

    @Test fun aKeptCallLineMatchesItsLiveCopy() {
        assertEquals(ChatEvents.callKey("you", "hello"), ChatEvents.callKey("You", "hello"))
        assertNotEquals(ChatEvents.callKey("you", "hello"), ChatEvents.callKey("Claude", "hello"))
        assertNotEquals(ChatEvents.callKey("a", "b c"), ChatEvents.callKey("a b", "c"))
    }

    @Test fun aPageOfEventsAsksForTheCallLines() {
        assertTrue(("with" to "call_lines") in HostChatQuery.of(null))
        assertEquals(listOf("limit" to "200", "with" to "call_lines", "cursor" to "c1"), HostChatQuery.of("c1"))
    }
}
