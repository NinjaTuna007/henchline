package io.github.ninjatuna007.henchline

import android.view.HapticFeedbackConstants
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ReplySwipeTest {
    private val threshold = 72f
    private val max = 96f

    @Test fun itFollowsTheFingerUpToTheThreshold() {
        assertEquals(0f, ReplySwipeRules.shown(-30f, threshold, max), 0f)  // never left of home
        assertEquals(0f, ReplySwipeRules.shown(0f, threshold, max), 0f)
        assertEquals(40f, ReplySwipeRules.shown(40f, threshold, max), 0f)
        assertEquals(72f, ReplySwipeRules.shown(72f, threshold, max), 0f)
    }

    @Test fun pastTheThresholdItPullsTowardTheStopButNeverReachesIt() {
        var last = threshold
        for (dx in listOf(80f, 100f, 150f, 300f, 1000f)) {
            val shown = ReplySwipeRules.shown(dx, threshold, max)
            assertTrue(shown > last)
            assertTrue(shown < max)
            assertTrue(shown < dx)
            last = shown
        }
    }

    @Test fun aTouchMidSpringPicksTheRowUpWhereItIs() {
        for (dx in listOf(0f, 30f, 72f, 90f, 200f)) {
            val shown = ReplySwipeRules.shown(dx, threshold, max)
            assertEquals(shown, ReplySwipeRules.shown(ReplySwipeRules.pullFor(shown, threshold, max), threshold, max), 0.01f)
        }
        assertEquals(0f, ReplySwipeRules.pullFor(-1f, threshold, max), 0f)
    }

    @Test fun lettingGoRepliesFromTheThresholdOn() {
        assertFalse(ReplySwipeRules.armed(71.9f, threshold))
        assertTrue(ReplySwipeRules.armed(72f, threshold))
        assertEquals(72, ReplySwipeRules.THRESHOLD_DP)
        assertEquals(96, ReplySwipeRules.MAX_DP)
    }

    @Test fun theThresholdIsFeltEachWayOnNewPhonesAndOutwardOnOlder() {
        assertEquals(HapticFeedbackConstants.GESTURE_THRESHOLD_ACTIVATE, ReplySwipeRules.haptic(34, true))
        assertEquals(HapticFeedbackConstants.GESTURE_THRESHOLD_DEACTIVATE, ReplySwipeRules.haptic(35, false))
        assertEquals(HapticFeedbackConstants.CONTEXT_CLICK, ReplySwipeRules.haptic(29, true))
        assertNull(ReplySwipeRules.haptic(33, false))
    }

    @Test fun theSpringStartsAtTheRowsOwnSpeed() {
        assertEquals(1000f, ReplySwipeRules.velocity(10f, 100L, 20f, 110L, 8000f), 0.01f)
        assertEquals(-500f, ReplySwipeRules.velocity(20f, 100L, 10f, 120L, 8000f), 0.01f)
        assertEquals(0f, ReplySwipeRules.velocity(10f, 100L, 20f, 100L, 8000f), 0f)  // no time between: none
        assertEquals(8000f, ReplySwipeRules.velocity(0f, 0L, 1000f, 1L, 8000f), 0f)  // held to the fling limit
    }

    @Test fun aFlingOutwardDoesNotCarryItPastTheStop() {
        assertEquals(0f, ReplySwipeRules.homeVelocity(4000f), 0f)   // still flung outward: springs home from rest
        assertEquals(-1500f, ReplySwipeRules.homeVelocity(-1500f), 0f)  // already heading home: keeps its speed
        assertEquals(0f, ReplySwipeRules.homeVelocity(0f), 0f)
    }

    @Test fun aTouchAtTheLeftEdgeIsTheBackSwipes() {
        assertEquals(24, ReplySwipeRules.EDGE_DP)
        assertTrue(ReplySwipeRules.inEdge(10f, 24f))
        assertFalse(ReplySwipeRules.inEdge(24f, 24f))
    }

    @Test fun aRowCaughtSpringingHomeLetsTheListScrollUntilASidewaysMove() {
        val slop = 8f; val start = 12f
        assertFalse(ReplySwipeRules.startsDrag(true, 2f, 30f, slop, start))   // a scroll: the list takes it
        assertFalse(ReplySwipeRules.startsDrag(true, 5f, 0f, slop, start))   // within slop: not yet
        assertTrue(ReplySwipeRules.startsDrag(true, -20f, 3f, slop, start))  // pushed back home: a drag
        assertTrue(ReplySwipeRules.startsDrag(true, 20f, 3f, slop, start))
        assertFalse(ReplySwipeRules.startsDrag(false, -20f, 3f, slop, start))  // from home: rightward only
        assertTrue(ReplySwipeRules.startsDrag(false, 20f, 3f, slop, start))
        assertFalse(ReplySwipeRules.startsDrag(false, 20f, 15f, slop, start))  // not twice as much across
    }
}
