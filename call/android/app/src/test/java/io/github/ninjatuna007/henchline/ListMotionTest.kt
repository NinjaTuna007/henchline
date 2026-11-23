package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ListMotionTest {
    @Test fun aKeySeenAgainIsToldApartByItsTurn() {
        assertEquals(listOf("a", "b", "a#2", "a#3", "b#2"), ListMotion.unique(listOf("a", "b", "a", "a", "b")))
        assertEquals(emptyList<String>(), ListMotion.unique(emptyList()))
    }

    @Test fun theSameListDrawnAgainGivesTheSameKeys() {
        val drawn = listOf("heading|Pinned", "row|laptop|x", "card|laptop", "heading|henchline", "row|laptop|y", "heading|henchline")
        assertEquals(ListMotion.unique(drawn), ListMotion.unique(drawn.toList()))
    }

    @Test fun itMovesOnlyWithAnimationsOnAndAtMostSixtyViewsEitherSide() {
        assertEquals(60, ListMotion.MAX_VIEWS)
        assertTrue(ListMotion.moves(true, 10, 12))
        assertTrue(ListMotion.moves(true, 60, 60))
        assertFalse(ListMotion.moves(false, 10, 12))  // animations off
        assertFalse(ListMotion.moves(true, 61, 10))
        assertFalse(ListMotion.moves(true, 10, 61))
        assertFalse(ListMotion.moves(true, 0, 10))  // nothing was there to move from
    }

    @Test fun aRowIsPutBackToWhereItStood() {
        val old = Box(0f, 300f, 400f, 372f)
        val new = Box(12f, 100f, 388f, 172f)
        assertEquals(-12f to 200f, ListMotion.offset(old, new, null, null))
    }

    @Test fun aRowInsideAMovingCardMovesOnlyByWhatTheCardDoesNot() {
        // The card moved down 80; the row inside it moved down 80 + 72 (a row above it left)
        val cardOld = Box(12f, 200f, 388f, 600f)
        val cardNew = Box(12f, 280f, 388f, 608f)
        val rowOld = Box(12f, 400f, 388f, 472f)
        val rowNew = Box(12f, 552f, 388f, 624f)
        val (dx, dy) = ListMotion.offset(rowOld, rowNew, cardOld, cardNew)
        assertEquals(0f, dx, 0f)
        assertEquals(-72f, dy, 0f)
        // together they put it back by all of its move
        assertEquals(rowOld.top - rowNew.top, dy + (cardOld.top - cardNew.top), 0f)
    }

    @Test fun aMoveTooSmallToSeeIsNone() {
        assertFalse(ListMotion.moved(0f, 0.2f))
        assertTrue(ListMotion.moved(0f, -1f))
        assertTrue(ListMotion.moved(3f, 0f))
    }

    @Test fun theOffsetRunsOutAsTheMoveEnds() {
        assertEquals(200f, ListMotion.at(200f, 0f), 0f)
        assertEquals(100f, ListMotion.at(200f, 0.5f), 0f)
        assertEquals(0f, ListMotion.at(200f, 1f), 0f)
        assertEquals(300L, ListMotion.MOVE_MS)
    }

    @Test fun goneRowsFadeBeforeNewOnesComeAndTheyNeverCross() {
        assertEquals(1f, ListMotion.goingAlpha(0f), 0f)
        assertEquals(0f, ListMotion.comingAlpha(0f), 0f)
        assertEquals(0f, ListMotion.goingAlpha(1f), 0f)
        assertEquals(1f, ListMotion.comingAlpha(1f), 0f)
        for (i in 0..100) {
            val t = i / 100f
            assertTrue(ListMotion.goingAlpha(t) == 0f || ListMotion.comingAlpha(t) == 0f)
        }
    }

    @Test fun aBadgePopsOnlyWhenItsCountGoesUp() {
        assertFalse(ListMotion.pops(null, 3))  // first drawing
        assertFalse(ListMotion.pops(3, 3))     // a poll drawing the same
        assertFalse(ListMotion.pops(3, 1))     // read some
        assertTrue(ListMotion.pops(0, 1))      // the first unread
        assertTrue(ListMotion.pops(2, 5))
        assertEquals(0.6f, ListMotion.POP_FROM, 0f)
    }

    @Test fun aRiseWhileTheListIsHiddenWaitsToPopAndAFallCountsAtOnce() {
        assertEquals(4, ListMotion.seenCount(2, 4, seen = true))
        assertEquals(2, ListMotion.seenCount(2, 4, seen = false))  // rose unseen: still pops later
        assertEquals(0, ListMotion.seenCount(2, 0, seen = false))  // read in the chat: seen
        assertEquals(3, ListMotion.seenCount(null, 3, seen = false))  // first drawing: no pop later for it
        // read (0), then a new line while the chat is open: pops when the list shows
        val afterRead = ListMotion.seenCount(2, 0, seen = false)
        val afterNew = ListMotion.seenCount(afterRead, 1, seen = false)
        assertTrue(ListMotion.pops(afterNew, 1))
    }

    @Test fun aSecondChangeMidMoveStartsFromWhereTheRowIsDrawn() {
        // Laid out at 100, still drawn 60 below it (its own move) and 20 above (its card's): seen at 140
        val drawn = ListMotion.shifted(Box(0f, 100f, 400f, 172f), 0f, 60f - 20f)
        assertEquals(Box(0f, 140f, 400f, 212f), drawn)
        // Redrawn back at 300: put back by 160, to where it was seen
        assertEquals(0f to -160f, ListMotion.offset(drawn, Box(0f, 300f, 400f, 372f), null, null))
    }
}
