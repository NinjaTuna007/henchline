package io.github.ninjatuna007.henchline

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MotionTest {
    @Test fun matrixFramesRunFromOneToTheOther() {
        val from = floatArrayOf(1f, 0f, 0f, 0f, 1f, 0f, 0f, 0f, 1f)
        val to = floatArrayOf(2.5f, 0f, -300f, 0f, 2.5f, -150f, 0f, 0f, 1f)
        val out = FloatArray(9)
        lerpValues(from, to, 0f, out); assertArrayEquals(from, out, 1e-6f)
        lerpValues(from, to, 1f, out); assertArrayEquals(to, out, 1e-6f)
        lerpValues(from, to, 0.5f, out)
        assertEquals(1.75f, out[0], 1e-6f); assertEquals(-150f, out[2], 1e-6f); assertEquals(1f, out[8], 1e-6f)
    }

    @Test fun aPointBothMatricesPutInOnePlaceStaysThereEveryFrame() {
        // zoom 1 -> 2.5 about (200, 100): x' = s x + t, with t = 200 - 2.5 * 200 at the end
        val from = floatArrayOf(1f, 0f, 0f, 0f, 1f, 0f, 0f, 0f, 1f)
        val to = floatArrayOf(2.5f, 0f, 200f - 2.5f * 200f, 0f, 2.5f, 100f - 2.5f * 100f, 0f, 0f, 1f)
        val out = FloatArray(9)
        for (step in 0..10) {
            lerpValues(from, to, step / 10f, out)
            assertEquals(200f, out[0] * 200f + out[2], 1e-3f)
            assertEquals(100f, out[4] * 100f + out[5], 1e-3f)
        }
    }

    @Test fun withinTheLimitsAZoomIsItself() {
        for (z in listOf(1f, 2.5f, 6f)) assertEquals(z, resist(z, 1f, 6f, 0.85f, 7f), 1e-6f)
    }

    @Test fun pastTheLimitsItGivesLessAndLessNeverReachingTheHardOnes() {
        var last = 6f
        for (pull in listOf(6.5f, 8f, 12f, 50f, 1000f)) {
            val shown = resist(pull, 1f, 6f, 0.85f, 7f)
            assertTrue(shown > last && shown < 7f && shown < pull); last = shown
        }
        last = 1f
        for (pull in listOf(0.9f, 0.5f, 0.1f, 0.001f)) {
            val shown = resist(pull, 1f, 6f, 0.85f, 7f)
            assertTrue(shown < last && shown > 0.85f && shown > pull); last = shown
        }
        // a pinch just past the limit moves the picture almost as much as the fingers (no jolt at the limit)
        assertEquals(6.01f, resist(6.01f, 1f, 6f, 0.85f, 7f), 1e-3f)
    }

    @Test fun aPinchResumesFromWhereTheLastLeftIt() {
        for (pull in listOf(0.2f, 0.9f, 1f, 3f, 6f, 6.4f, 9f)) {
            val shown = resist(pull, 1f, 6f, 0.85f, 7f)
            assertEquals(pull, unresist(shown, 1f, 6f, 0.85f, 7f), 1e-3f * pull)
        }
        assertTrue(unresist(7f, 1f, 6f, 0.85f, 7f).isFinite())
    }

    @Test fun aPictureLeavesNoEmptyEdgeItCouldFillAndCentresAlongOneItDoesnt() {
        assertEquals(100f, keepAlong(0f, 800f, 1000f), 1e-6f)  // narrower: centred
        assertEquals(-50f, keepAlong(50f, 2050f, 1000f), 1e-6f)  // gap on the left
        assertEquals(100f, keepAlong(-1100f, 900f, 1000f), 1e-6f)  // gap on the right
        assertEquals(0f, keepAlong(-500f, 1500f, 1000f), 1e-6f)  // fills it
    }

    @Test fun aFlickRunsBetweenTheEdges() {
        assertEquals(-1000f to 0f, panRange(2000f, 1000f))
        assertEquals(100f to 100f, panRange(800f, 1000f))
    }

    @Test fun onlyALineSentOrArrivingInTheOpenChatMoves() {
        // a chat's history, a rebuilt list: no line moves
        assertNull(entranceOf(live = false, mine = true, note = false, shown = true, animating = true))
        assertNull(entranceOf(live = false, mine = false, note = false, shown = true, animating = true))
        // another chat's line (hidden), a note in the middle, the phone's animations off
        assertNull(entranceOf(live = true, mine = false, note = false, shown = false, animating = true))
        assertNull(entranceOf(live = true, mine = false, note = true, shown = true, animating = true))
        assertNull(entranceOf(live = true, mine = true, note = false, shown = true, animating = false))
    }

    @Test fun yoursGrowsFromBottomRightAndTheirsFromBottomLeft() {
        val sent = entranceOf(live = true, mine = true, note = false, shown = true, animating = true)!!
        assertEquals(0.8f, sent.from, 0f); assertEquals(250L, sent.duration); assertTrue(sent.atEnd)
        val arrived = entranceOf(live = true, mine = false, note = false, shown = true, animating = true)!!
        assertEquals(0.9f, arrived.from, 0f); assertEquals(300L, arrived.duration); assertFalse(arrived.atEnd)
    }

    @Test fun aPressedButtonShrinksALittleAndSquaresOffALittle() {
        assertEquals(0.96f, PRESSED_SCALE, 0f)
        val pill = 24f
        assertTrue(pressedCorner(pill) < pill && pressedCorner(pill) > pill / 2)
        assertEquals(0f, pressedCorner(0f), 0f)
    }

    @Test fun pressedOnlyWhenEnabled() {
        val pressed = android.R.attr.state_pressed
        val enabled = android.R.attr.state_enabled
        assertTrue(isPressedIn(intArrayOf(enabled, pressed)))
        assertFalse(isPressedIn(intArrayOf(pressed)))  // a disabled button doesn't give
        assertFalse(isPressedIn(intArrayOf(enabled)))
        assertFalse(isPressedIn(intArrayOf()))
    }

    @Test fun theChatCountsAsAtItsEndWhileGoingThere() {
        assertTrue(followsEnd(canScrollDown = false, downAsked = false, now = 1000, glideEnds = 0))  // there
        assertTrue(followsEnd(canScrollDown = true, downAsked = true, now = 1000, glideEnds = 0))  // asked, not yet made
        assertTrue(followsEnd(canScrollDown = true, downAsked = false, now = 1000, glideEnds = 1200))  // gliding
        assertFalse(followsEnd(canScrollDown = true, downAsked = false, now = 1300, glideEnds = 1200))  // scrolled up
    }

    @Test fun aBenchLanguageIsATagOrNothing() {
        assertEquals("en-US", benchLanguage("en-US"))
        assertEquals("de", benchLanguage("de"))
        assertEquals("", benchLanguage(null))
        assertEquals("", benchLanguage("english"))
        assertEquals("", benchLanguage("en_us"))
    }
}
