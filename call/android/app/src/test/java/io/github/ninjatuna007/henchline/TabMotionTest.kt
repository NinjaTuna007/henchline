package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TabMotionTest {
    @Test fun theAxisIsThirtyDpOverThreeHundredMsAndTheCallBarFourHundred() {
        assertEquals(30, TabMotion.AXIS_DP)
        assertEquals(300L, TabMotion.AXIS_MS)
        assertEquals(400L, TabMotion.CALL_BAR_MS)
    }

    @Test fun theMoveComesFromTheSideTheNewTabLiesOn() {
        // usage, chats, calls as the bar shows them
        assertEquals(1, TabMotion.direction(1, 2))   // chats -> calls: from the right
        assertEquals(-1, TabMotion.direction(1, 0))  // chats -> usage: from the left
        assertEquals(1, TabMotion.direction(0, 2))
        assertEquals(0, TabMotion.direction(1, 1))   // the same tab: no move
        assertEquals(0, TabMotion.direction(-1, 1))  // an unknown tab: no move
        assertEquals(0, TabMotion.direction(1, -1))
    }

    @Test fun theOldFadesOutBeforeTheNewFadesInAndTheyNeverCross() {
        assertEquals(1f, TabMotion.outAlpha(0f), 0f)
        assertEquals(0f, TabMotion.inAlpha(0f), 0f)
        assertEquals(0f, TabMotion.outAlpha(TabMotion.FADE_SPLIT), 1e-6f)
        assertEquals(0f, TabMotion.inAlpha(TabMotion.FADE_SPLIT), 1e-6f)
        assertEquals(0f, TabMotion.outAlpha(1f), 0f)
        assertEquals(1f, TabMotion.inAlpha(1f), 0f)
        for (i in 0..100) {
            val t = i / 100f
            assertTrue("both showing at $t", TabMotion.outAlpha(t) == 0f || TabMotion.inAlpha(t) == 0f)
        }
        // past the ends (an overshooting easing): held within 0..1
        assertEquals(1f, TabMotion.outAlpha(-0.2f), 0f)
        assertEquals(1f, TabMotion.inAlpha(1.2f), 0f)
    }

    @Test fun bothMoveThirtyDpTheSameWay() {
        val d = 30f * 2.75f
        // to a tab on the right: the new starts 30 dp right and comes home, the old goes 30 dp left
        assertEquals(d, TabMotion.inOffset(0f, 1, d), 0f)
        assertEquals(0f, TabMotion.inOffset(1f, 1, d), 0f)
        assertEquals(0f, TabMotion.outOffset(0f, 1, d), 0f)
        assertEquals(-d, TabMotion.outOffset(1f, 1, d), 0f)
        // to the left: mirrored
        assertEquals(-d, TabMotion.inOffset(0f, -1, d), 0f)
        assertEquals(d, TabMotion.outOffset(1f, -1, d), 0f)
        // halfway, both have gone the same way by the same amount
        assertEquals(TabMotion.outOffset(0.5f, 1, d) - 0f, TabMotion.inOffset(0.5f, 1, d) - d, 1e-4f)
    }

    @Test fun thePillSitsBehindItsTabInsetFromItsEdges() {
        // three tabs across 0..300, inset 10
        assertEquals(10f to 90f, TabMotion.pillSpan(0f, 3, 0f, 300f, 10f))
        assertEquals(110f to 190f, TabMotion.pillSpan(1f, 3, 0f, 300f, 10f))
        assertEquals(210f to 290f, TabMotion.pillSpan(2f, 3, 0f, 300f, 10f))
        // between two while it springs across
        assertEquals(60f to 140f, TabMotion.pillSpan(0.5f, 3, 0f, 300f, 10f))
        // Chat | Plan: two halves inside a 3 px padding
        assertEquals(3f to 103f, TabMotion.pillSpan(0f, 2, 3f, 203f, 0f))
        assertEquals(103f to 203f, TabMotion.pillSpan(1f, 2, 3f, 203f, 0f))
    }

    @Test fun aSpringPastTheEndsStaysOnTheTrack() {
        assertEquals(TabMotion.pillSpan(2f, 3, 0f, 300f, 10f), TabMotion.pillSpan(2.08f, 3, 0f, 300f, 10f))
        assertEquals(TabMotion.pillSpan(0f, 3, 0f, 300f, 10f), TabMotion.pillSpan(-0.1f, 3, 0f, 300f, 10f))
    }

    @Test fun aTabIsReadWithItsPlace() {
        assertEquals("Chats, tab 2 of 3", TabMotion.describeTab("Chats", 1, 3))
        assertEquals("Usage, tab 1 of 3", TabMotion.describeTab("Usage", 0, 3))
    }

    @Test fun theStatusBarChangesFromNavyToTheCallsGreen() {
        val navy = 0xFF0A1430.toInt()
        val green = 0xFF17673F.toInt()
        assertEquals(navy, TabMotion.statusAt(navy, green, 0f))
        assertEquals(green, TabMotion.statusAt(navy, green, 1f))
        assertEquals(green, TabMotion.statusAt(navy, green, 1.3f))  // held at the end
        val half = TabMotion.statusAt(navy, green, 0.5f)
        assertEquals(0xFF, half ushr 24)
        assertEquals((0x0A + 0x17) / 2 + 1, (half shr 16) and 0xFF)  // 16.5 rounds up
    }

    @Test fun theCallBarOpensAndClosesItsHeightAndTurnsBackFromWhereItIs() {
        val h = 154  // 56 dp on a 2.75x screen
        assertEquals(0, TabMotion.barHeightAt(0f, 0, h))
        assertEquals(h, TabMotion.barHeightAt(1f, 0, h))
        assertEquals(77, TabMotion.barHeightAt(0.5f, 0, h))
        assertEquals(h, TabMotion.barHeightAt(0f, h, 0))
        assertEquals(0, TabMotion.barHeightAt(1f, h, 0))
        // turned back at 100 px on the way open: closes from there, never jumping
        assertEquals(100, TabMotion.barHeightAt(0f, 100, 0))
        assertEquals(0, TabMotion.barHeightAt(1.2f, 100, 0))  // past the end: held, never negative
    }
}
