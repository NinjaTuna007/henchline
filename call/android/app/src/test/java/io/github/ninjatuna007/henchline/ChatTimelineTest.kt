package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertNull
import org.junit.Test

class ChatTimelineTest {
    private fun event(id: String, at: String, cursor: String = "c-$id") =
        JSONObject().put("event_id", id).put("created_at", at).put("cursor", cursor)

    @Test fun aFirstLookStartsFromTheBeginning() {
        assertNull(ChatTimeline().resumeFrom())
    }

    @Test fun eachEventIsHeldOnceAndPlacedInTheHostsOrder() {
        val t = ChatTimeline()
        val first = t.add(listOf(event("a", "2026-11-22T10:00:00Z"), event("c", "2026-11-22T10:05:00Z")))
        assertEquals(listOf(0, 1), first.map { it.first })
        // The overlap brings c again; b was filed late and sorts before it
        val second = t.add(listOf(event("b", "2026-11-22T10:04:00Z"), event("c", "2026-11-22T10:05:00Z"), event("d", "2026-11-22T10:06:00Z")))
        assertEquals(listOf(1 to "b", 3 to "d"), second.map { it.first to it.second.getString("event_id") })
        assertEquals(4, t.size)
        assertEquals(emptyList<Pair<Int, JSONObject>>(), t.add(listOf(event("a", "2026-11-22T10:00:00Z"))))
    }

    @Test fun equalTimesAreOrderedByEventId() {
        val t = ChatTimeline()
        t.add(listOf(event("b", "2026-11-22T10:00:00Z")))
        assertEquals(listOf(0), t.add(listOf(event("a", "2026-11-22T10:00:00Z"))).map { it.first })
    }

    @Test fun pollingResumesALittleBeforeTheNewest() {
        val t = ChatTimeline()
        t.add(listOf(event("a", "2026-11-22T10:00:00Z")))
        assertNull(t.resumeFrom())  // nothing old enough: from the start, which is short
        t.add(listOf(event("b", "2026-11-22T10:01:00Z"), event("c", "2026-11-22T10:03:00Z"), event("d", "2026-11-22T10:04:00Z")))
        assertEquals("c-b", t.resumeFrom())  // the last event at least OVERLAP_S (2 min) before d
    }

    @Test fun aLongHistoryMovesOnFromItsLastFullPageNotBackOverIt() {
        val t = ChatTimeline()
        t.add(listOf(event("a", "2026-11-22T10:00:00Z"), event("b", "2026-11-22T10:00:01Z")))
        t.pageEnded("c-b")  // a full page: more after it
        assertTrue(t.catchingUp)
        assertEquals("c-b", t.resumeFrom())
        t.add(listOf(event("c", "2026-11-22T10:00:02Z")))
        t.pageEnded(null)  // the end: polling with the overlap again
        assertFalse(t.catchingUp)
        assertNull(t.resumeFrom())
    }
}
