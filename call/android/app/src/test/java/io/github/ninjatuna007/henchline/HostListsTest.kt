package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The home list grouped by computer: which computers, how each is shown, and its chats in order. */
class HostListsTest {
    private val laptopId = "00000000-0000-4000-8000-00000000000a"
    private fun host(id: String, base: String, pending: Boolean = false) =
        Host(id, "Host $id".take(9), base, base.removePrefix("https://").trimEnd('/'), "", "dev", "{}", 1L, pending)

    private fun chat(session: String, name: String = "Claude, in site", at: Long = 0L, boss: String = "", running: Boolean = true,
                     state: String = "", text: String = "", mine: Boolean = false, agent: String = "claude") =
        ChatSummary(session, name, false, running, state, agent, boss, "", at, text, mine)

    @Test fun theLaptopIsNeverListedTwice() {
        val laptop = host(laptopId, "https://laptop.example.ts.net/")
        val desk = host("00000000-0000-4000-8000-00000000000b", "https://desk.example.ts.net/")
        val gone = host("00000000-0000-4000-8000-00000000000c", "https://old.example.ts.net/", pending = true)
        val hosts = listOf(laptop, desk, gone)
        assertEquals(listOf(desk), HostLists.others(hosts, "https://laptop.example.ts.net", laptopId))  // by id
        assertEquals(listOf(desk), HostLists.others(hosts, "https://LAPTOP.example.ts.net:443/", null))  // by address meanwhile
        val shortName = host(laptopId, "https://laptop/")
        assertEquals(listOf(desk), HostLists.others(listOf(shortName, desk), "https://laptop.example.ts.net", laptopId))
        assertEquals(listOf(laptop, desk), HostLists.others(listOf(laptop, desk), null, null))  // no laptop set: all of them
    }

    @Test fun aComputersHeadingSaysHowItIs() {
        val now = 10_000_000L
        assertEquals(HostLists.Dot.UNKNOWN to "…", HostLists.status(null, now))
        val one = listOf(JSONObject().put("session_id", "claude-1").put("name", "Claude"))
        assertEquals(HostLists.Dot.LIVE to "", HostLists.status(HostLists.Look(one, now - 1000, 0L, 0), now))
        assertEquals(HostLists.Dot.OFFLINE to "offline · 12 min", HostLists.status(HostLists.Look(one, now - 12 * 60_000, now, 0), now))
        assertEquals(HostLists.Dot.OFFLINE to "can't reach", HostLists.status(HostLists.Look(emptyList(), 0L, now, 0), now))
        assertEquals(HostLists.Dot.WAITING to "2 waiting", HostLists.status(HostLists.Look(one, now, 0L, 2), now))
        val quiet = listOf(JSONObject().put("session_id", "reminders-x").put("running", false))
        assertEquals(HostLists.Dot.LIVE to "nothing running", HostLists.status(HostLists.Look(quiet, now, 0L, 0), now))
        assertEquals("3 h", HostLists.ago(0L, 3 * 3_600_000L))
        assertEquals("2 d", HostLists.ago(0L, 50 * 3_600_000L))
    }

    @Test fun theSortWorksInsideAComputerWithHelpersUnderTheirBoss() {
        val chats = listOf(chat("claude-1", "Claude, in site", at = 100), chat("cursor-2", "Cursor, in kite", at = 300),
            chat("codex-3", "Codex, in site", at = 200, boss = "claude-1"), chat("claude-4", "Claude, in kite", at = 50))
        val recent = HostLists.groups(chats, "recent", emptySet(), "")
        assertEquals(listOf(""), recent.map { it.first })
        assertEquals(listOf("cursor-2" to false, "claude-1" to false, "codex-3" to true, "claude-4" to false),
            recent[0].second.map { it.first.sessionId to it.second })
        val byProject = HostLists.groups(chats, "project", emptySet(), "")
        assertEquals(listOf("kite", "site"), byProject.map { it.first })
        assertEquals(listOf("Cursor", "Claude"), HostLists.groups(chats, "agent", emptySet(), "").map { it.first })
        // A pinned boss and its helper go to Pinned (drawn there, the helper under it), not in the computer's groups
        assertEquals(listOf("cursor-2" to false, "claude-4" to false),
            HostLists.groups(chats, "recent", setOf("claude-1"), "")[0].second.map { it.first.sessionId to it.second })
        // In a search pinned ones are found like any other
        assertEquals(listOf("claude-1"), HostLists.groups(chats, "recent", setOf("claude-1"), "claude, in site")[0].second.map { it.first.sessionId })
    }

    @Test fun endedChatsShowOnlyInASearchAndASearchIsFlat() {
        val chats = listOf(chat("claude-1", "Claude, in site", at = 1, text = "tests pass"),
            chat("codex-2", "Codex, in kite", at = 2, running = false, state = "ended"),
            chat("cursor-3", "Cursor, in site", at = 3, boss = "claude-1"))
        assertEquals(listOf("claude-1" to false, "cursor-3" to true),
            HostLists.groups(chats, "recent", emptySet(), "")[0].second.map { it.first.sessionId to it.second })
        assertFalse(HostLists.groups(chats, "recent", emptySet(), "")[0].second.any { it.first.sessionId == "codex-2" })
        assertEquals(listOf("codex-2"), HostLists.groups(chats, "recent", emptySet(), "kite")[0].second.map { it.first.sessionId })
        assertEquals(listOf("claude-1"), HostLists.groups(chats, "recent", emptySet(), "TESTS")[0].second.map { it.first.sessionId })
        assertTrue(HostLists.groups(chats, "recent", emptySet(), "nothing like it").isEmpty())
        assertTrue(HostLists.groups(chats, "recent", emptySet(), "site")[0].second.none { it.second })  // flat: no nesting
    }

    @Test fun aHelperWhosePinnedBossIsGoneKeepsItsOwnPlace() {
        val helper = chat("codex-3", "Codex, in site", at = 200, boss = "claude-1")
        // Its boss is pinned but no longer in that computer's list: nothing in Pinned would draw it
        assertEquals(listOf("codex-3" to false), HostLists.groups(listOf(helper), "recent", setOf("claude-1"), "")[0].second
            .map { it.first.sessionId to it.second })
        assertFalse(HostLists.inPinned(helper, setOf("claude-1"), setOf("codex-3")))
        assertTrue(HostLists.inPinned(helper, setOf("claude-1"), setOf("codex-3", "claude-1")))
    }

    @Test fun aHelperOfAPinnedBossIsDrawnUnderItInPinned() {
        // The review's case: helper H of pinned laptop boss B (H not pinned) must not vanish
        data class P(val name: String, val at: Long, val helpers: List<P> = emptyList())
        val h = P("H", 50)
        val pinned = listOf(P("B", 100, listOf(h)), P("D", 300))  // D: another computer's pinned chat, newer
        assertEquals(listOf("D" to false, "B" to false, "H" to true),
            HostLists.pinnedRows(pinned, { it.at }, { it.helpers }).map { it.first.name to it.second })
    }

    @Test fun theLaptopPairedUnderItsMachineNameIsNotAnotherComputerWhileItsIdIsUnknown() {
        val laptop = host("00000000-0000-4000-8000-00000000000d", "https://laptop:8443/")  // paired by short name and port
        val desk = host("00000000-0000-4000-8000-00000000000b", "https://desk.example.ts.net/")
        assertEquals(listOf(desk), HostLists.others(listOf(laptop, desk), "https://Laptop.example.ts.net", null))
        // Once the laptop's id is learned, only the id (and the exact address) decide: another id there is another computer
        assertEquals(listOf(laptop, desk), HostLists.others(listOf(laptop, desk), "https://laptop.example.ts.net", laptopId))
        assertEquals("laptop", HostLists.shortName("https://LAPTOP.example.ts.net:8443/"))
        assertEquals(null, HostLists.shortName("https://100.64.0.7/"))  // an address names no machine
        assertEquals(listOf(host("00000000-0000-4000-8000-00000000000e", "https://100.64.0.7/")),
            HostLists.others(listOf(host("00000000-0000-4000-8000-00000000000e", "https://100.64.0.7/")), "https://100.64.0.8", null))
    }

    @Test fun seenIsNeverEarlierThanTheChatsNewestLine() {
        assertEquals(500L, HostLists.seenStamp(now = 100L, newest = 500L))  // that computer's clock runs ahead
        assertEquals(800L, HostLists.seenStamp(now = 800L, newest = 500L))
        assertFalse(HostLists.unread(chat("c", at = 500, text = "done"), seenAt = HostLists.seenStamp(100L, 500L)))
    }

    @Test fun newChatStartsWhereAskedElseOnTheLastComputerOnlyIfItCanBeReached() {
        val ids = listOf("desk", "studio")
        assertEquals("studio", HostLists.newChatOn("studio", "desk", ids, emptySet(), "laptop"))   // asked
        assertEquals("laptop", HostLists.newChatOn("laptop", "desk", ids, emptySet(), "laptop"))
        assertEquals("laptop", HostLists.newChatOn("gone", "desk", ids, emptySet(), "laptop"))     // asked, no longer paired
        assertEquals("desk", HostLists.newChatOn(null, "desk", ids, emptySet(), "laptop"))         // the last one
        assertEquals("laptop", HostLists.newChatOn(null, "desk", ids, setOf("desk"), "laptop"))    // offline: not stuck there
        assertEquals("laptop", HostLists.newChatOn(null, "gone", ids, emptySet(), "laptop"))
        assertEquals("laptop", HostLists.newChatOn(null, null, ids, emptySet(), "laptop"))
        assertEquals("laptop", HostLists.newChatOn(null, "desk", emptyList(), emptySet(), "laptop"))  // multi-host off
    }

    @Test fun aLaptopThatDoesntAnswerHandsTheNewChatToAComputerThatMight() {
        assertEquals("desk", HostLists.fallbackFor(listOf("desk", "studio"), emptySet()))
        assertEquals("studio", HostLists.fallbackFor(listOf("desk", "studio"), setOf("desk")))  // offline: skipped
        assertNull(HostLists.fallbackFor(listOf("desk"), setOf("desk")))
        assertNull(HostLists.fallbackFor(emptyList(), emptySet()))  // multi-host off: the laptop's failure stands
    }

    @Test fun rowsAreNamedAsTheLaptopsAre() {
        assertEquals("Reminders" to "Desk", HostLists.named(chat("reminders-desk", "Reminders", agent = "reminders")))  // by sender
        assertEquals("Reminders" to "Reminders", HostLists.named(chat("reminders-reminders", "Reminders", agent = "reminders")))
        // Two of them on one computer: two rows told apart by who sent them, both "Reminders" in the agent line
        assertEquals(listOf("Aquabase", "Reminders"), HostLists.groups(listOf(chat("reminders-reminders", "Reminders", at = 1, agent = "reminders"),
            chat("reminders-aquabase", "Reminders", at = 2, agent = "reminders")), "recent", emptySet(), "")[0].second
            .map { HostLists.named(it.first).second })
        assertEquals("Claude" to "site", HostLists.named(chat("claude-1", "Claude, in site")))
        assertEquals("Codex" to "codex-gone", HostLists.named(chat("codex-gone", "Codex", agent = "codex")))  // a fallback name
    }

    @Test fun unreadIsTheAgentsLineAfterTheChatWasLastOpen() {
        assertTrue(HostLists.unread(chat("c", at = 200, text = "done"), seenAt = 100))
        assertFalse(HostLists.unread(chat("c", at = 200, text = "done"), seenAt = 200))
        assertFalse(HostLists.unread(chat("c", at = 200, text = "thanks", mine = true), seenAt = 0))
        assertFalse(HostLists.unread(chat("c", at = 200, text = ""), seenAt = 0))
    }

    @Test fun aComputersAnswerIsReadWithItsLastLine() {
        val c = ChatSummary.of(JSONObject("""{"session_id":"claude-1","name":"Claude, in site","current":true,"running":true,
            "state":"running","agent":"claude","boss":"","team":"boss","last_at":"2026-11-22T21:00:00Z",
            "last":{"text":"Tests pass.","mine":false,"kind":"agent_text"}}"""))
        assertEquals(java.time.Instant.parse("2026-11-22T21:00:00Z").toEpochMilli(), c.lastAt)
        assertEquals("Tests pass.", c.lastText)
        assertEquals("boss", c.team)
        val old = ChatSummary.of(JSONObject("""{"session_id":"claude-1","name":"Claude 1"}"""))  // an older computer
        assertTrue(old.running)
        assertEquals(0L, old.lastAt)
    }
}
