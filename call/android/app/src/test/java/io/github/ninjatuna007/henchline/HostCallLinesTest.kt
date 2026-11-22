package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

/** What is said on a call with another computer is kept for its chat screen, however often that screen is opened. */
class HostCallLinesTest {
    private fun line(text: String) = JSONObject().put("chat", JSONObject().put("from", "Claude").put("text", text))

    @Test fun linesAreKeptInOrderForThatChatOnly() {
        val lines = CallService.HostLines()
        lines.start("host-b", "claude-1")
        val told = listOf(lines.add(line("one")), lines.add(line("two")))
        assertEquals(listOf(0, 1), told.map { JSONObject(it).getInt("seq") })
        assertEquals(told, lines.of("host-b", "claude-1"))          // replayed to a screen opened again
        assertEquals(emptyList<String>(), lines.of("host-b", "claude-2"))
        assertEquals(emptyList<String>(), lines.of("host-c", "claude-1"))
        assertEquals("two", JSONObject(lines.of("host-b", "claude-1")[1]).getJSONObject("chat").getString("text"))
    }

    @Test fun theNextCallStartsAfreshAndOldLinesGoPastTheLimit() {
        val lines = CallService.HostLines()
        lines.start("host-b", "claude-1")
        repeat(CallService.HostLines.MAX + 5) { lines.add(line("l$it")) }
        val kept = lines.of("host-b", "claude-1")
        assertEquals(CallService.HostLines.MAX, kept.size)
        assertEquals(5, JSONObject(kept.first()).getInt("seq"))      // numbers keep rising: a screen never redraws one
        lines.start("host-b", "claude-1")
        assertEquals(emptyList<String>(), lines.of("host-b", "claude-1"))
    }

    @Test fun howTheCallEndedIsShownOnceInItsOwnChat() {
        val lines = CallService.HostLines()
        lines.start("host-b", "claude-1")
        lines.closed("Call ended")
        assertEquals(null, lines.takeClosed("host-b", "claude-2"))   // another chat: not its call
        assertEquals("Call ended", lines.takeClosed("host-b", "claude-1"))
        assertEquals(null, lines.takeClosed("host-b", "claude-1"))   // once
        lines.closed("Dropped"); lines.start("host-b", "claude-1")
        assertEquals(null, lines.takeClosed("host-b", "claude-1"))   // a new call forgets the last one's ending
    }
}
