package io.github.ninjatuna007.henchline

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Chats that aren't running: named by the laptop, and how the list shows them. */
class StraysTest {
    private val answer = JSONObject().put("others", JSONArray()
        .put(JSONObject().put("session", "claude-1").put("name", "Claude, in garden planner").put("state", "gone"))
        .put(JSONObject().put("session", "cursor-2").put("name", "Cursor, in website · its title").put("state", "ended"))
        .put(JSONObject().put("session", "").put("name", "nobody")))

    @Test fun theLaptopNamesItsProjectAndState() {
        val others = Strays.parse(answer)!!
        assertEquals(setOf("claude-1", "cursor-2"), others.keys)
        val closed = Strays.describe("claude-1", others["claude-1"])
        assertEquals(Triple("Claude", "garden planner", "gone"), Triple(closed.agent, closed.project, closed.state))
        assertEquals("website", Strays.describe("cursor-2", others["cursor-2"]).project)
    }

    @Test fun anOlderLaptopSendsNoneAndTheChatStaysUnnamed() {
        assertNull(Strays.parse(JSONObject().put("agents", JSONArray())))
        val unknown = Strays.describe("codex-_home_me", null)
        assertEquals(Triple("Codex", Strays.UNKNOWN, ""), Triple(unknown.agent, unknown.project, unknown.state))
    }

    @Test fun anEndedChatLeavesTheListButASearchFindsIt() {
        assertFalse(Strays.listed("ended", endedHere = false, searching = false))
        assertFalse(Strays.listed("", endedHere = true, searching = false))
        assertTrue(Strays.listed("ended", endedHere = false, searching = true))
        assertTrue(Strays.listed("gone", endedHere = false, searching = false))
        assertTrue(Strays.listed("", endedHere = false, searching = false))
    }

    @Test fun theRowSaysItIsNotRunning() {
        assertEquals("Claude (not running)", Strays.label("Claude", "gone"))
        assertEquals("Cursor (ended)", Strays.label("Cursor", "ended"))
        assertEquals("Codex", Strays.label("Codex", ""))
    }

    @Test fun keptAcrossRestarts() {
        val others = Strays.parse(answer)!!
        assertEquals(others.keys, Strays.load(Strays.store(others)).keys)
        assertEquals(emptyMap<String, JSONObject>(), Strays.load("not json"))
    }

    @Test fun aCrewNameSaysItsProject() {
        assertEquals("kite designer", Strays.projectOf("Claude, in kite designer · Demo"))
        assertEquals("home", Strays.projectOf("Claude"))
    }
}
