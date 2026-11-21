package io.github.ninjatuna007.henchline

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** The import plan over tests/fixtures/phone-legacy (synthetic): every row, keyed right, the old files untouched. */
class LegacyImportTest {
    private val hostId = "00000000-0000-4000-8000-000000000001"

    private fun fixture(): File {
        val source = File(javaClass.classLoader!!.getResource("phone-legacy/files")!!.toURI())
        return Files.createTempDirectory("legacy").toFile().also { source.copyRecursively(it) }
    }

    private fun prefs(): Map<String, Any?> {
        val raw = JSONObject(javaClass.classLoader!!.getResource("phone-legacy/prefs-call.json")!!.readText())
        return raw.keys().asSequence().associateWith { k ->
            when (val v = raw.get(k)) {
                is JSONArray -> (0 until v.length()).map { v.getString(it) }.toSet()
                is Int -> v.toLong()
                else -> v
            }
        }
    }

    @Test fun everyRowIsPlanned() {
        val plan = LegacyImport.plan(fixture(), prefs(), hostId)
        assertEquals(mapOf("chats" to 6, "lines" to 11, "outbox" to 2, "calls" to 2, "media" to 1, "thumbs" to 1, "prefs" to 9),
            plan.counts)
        assertEquals(listOf("a", "a-b", "a_b", "claude-1a2b", "cursor_chat_7f", "orphan_9"), plan.chats.map { it.key })
    }

    @Test fun sessionIdsAreRecoveredOnlyWhenCertain() {
        val ids = LegacyImport.plan(fixture(), prefs(), hostId).chats.associate { it.key to it.session }
        assertEquals("claude-1a2b", ids["claude-1a2b"])
        assertEquals("cursor:chat 7f", ids["cursor_chat_7f"])  // from the crew
        assertNull("a:b and a/b both rewrite to a_b: not guessed", ids["a_b"])
        assertNull("nobody names it, and it has an underscore", ids["orphan_9"])
        assertEquals("a", ids["a"])
        assertEquals("a-b", ids["a-b"])
    }

    @Test fun linesAreKeptVerbatimEvenWhenNotJson() {
        val lines = LegacyImport.plan(fixture(), prefs(), hostId).lines.filter { it.chat == "cursor_chat_7f" }
        assertEquals(listOf(0, 1, 2), lines.map { it.ordinal })
        assertEquals("not json {", lines[1].raw)
        assertNull(lines[1].at)
        assertEquals(1795000200000, lines[0].at)
    }

    @Test fun theOutboxKeepsItsOrder() {
        val outbox = LegacyImport.plan(fixture(), prefs(), hostId).outbox
        assertEquals(listOf(0 to "m-3", 1 to "m-4"), outbox.map { it.position to it.requestId })
        assertEquals("a/b", outbox[0].session)
    }

    @Test fun outboxItemsKeepTheirOwnText() {
        val dir = fixture()
        val text = File(dir, "outbox.json").readText()
        val outbox = LegacyImport.plan(dir, prefs(), hostId).outbox
        outbox.forEach { assertTrue(it.raw, text.contains(it.raw)) }
        assertEquals("""{"id":"m-3","session":"a/b","words":"Which one am I?","at":1795000300000}""", outbox[0].raw)
    }

    @Test fun arrayElementsAreSplitExactly() {
        assertEquals(listOf("""{"a":"x,]}\"y"}""", "[1, 2]", "\"s,\"", "3"),
            LegacyImport.elements("""[ {"a":"x,]}\"y"} , [1, 2],"s,", 3 ]"""))
        assertEquals(emptyList<String>(), LegacyImport.elements("[]"))
        assertEquals(null, LegacyImport.elements("{\"a\":1}"))
        assertEquals(null, LegacyImport.elements("[1,"))
    }

    @Test fun noGlobalPreferenceLooksLikeAChatOne() {
        // Every "call" preference name the app writes (from its sources): only per-chat ones carry these prefixes
        val global = setOf("server", "dance", "my_name", "clear", "clear_back", "crew", "current", "current_name", "pinned",
            "sort", "sound", "speaker", "stay_default", "cut_default", "thoughts", "away", "away_card", "asked_full",
            "asked_dnd", "helpers_hint_shown", "ring_endpoint", "multihost")
        assertFalse(global.any { g -> listOf("seen_", "helpers_seen_", "model_").any { g.startsWith(it) } })
    }

    @Test fun onlyChatPreferencesMove() {
        val prefs = LegacyImport.plan(fixture(), prefs(), hostId).prefs
        val names = prefs.map { "${it.chat ?: "-"}/${it.name}" }.toSet()
        assertTrue(names.containsAll(setOf("claude-1a2b/seen", "a:b/seen", "claude-1a2b/helpers_seen", "claude-1a2b/model",
            "claude-1a2b/pinned", "a:b/pinned", "-/crew", "-/current", "-/current_name")))
        assertFalse(prefs.any { it.name in setOf("server", "dance", "my_name", "clear") })
    }

    @Test fun theDigestIsStableAndNoticesChange() {
        val dir = fixture()
        val first = LegacyImport.plan(dir, prefs(), hostId)
        assertEquals(first.digest, LegacyImport.plan(dir, prefs(), hostId).digest)
        assertTrue(LegacyImport.unchanged(dir, first))
        File(dir, "chats/a.jsonl").appendText("""{"from":"Codex","text":"later","at":1795001000000}""" + "\n")
        assertFalse(LegacyImport.unchanged(dir, first))
        assertNotEquals(first.digest, LegacyImport.plan(dir, prefs(), hostId).digest)
    }

    @Test fun anEmptyPhoneIsAnEmptyPlan() {
        val plan = LegacyImport.plan(Files.createTempDirectory("empty").toFile(), emptyMap(), hostId)
        assertTrue(plan.counts.values.all { it == 0 })
    }
}
