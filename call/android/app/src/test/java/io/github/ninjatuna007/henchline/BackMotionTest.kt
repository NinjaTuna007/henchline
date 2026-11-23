package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BackMotionTest {
    private val eps = 1e-5f

    @Test fun progressIsHeldToZeroToOne() {
        assertEquals(0f, backFraction(-0.5f), eps)
        assertEquals(1f, backFraction(3f), eps)
        assertEquals(0.4f, backFraction(0.4f), eps)
        assertEquals(0f, backFraction(Float.NaN), eps)
    }

    @Test fun aScreenShrinksToNinetyPercent() {
        assertEquals(1f, backScreenScale(0f), eps)
        assertEquals(0.95f, backScreenScale(0.5f), eps)
        assertEquals(BACK_SCREEN_SCALE, backScreenScale(1f), eps)
        assertEquals(BACK_SCREEN_SCALE, backScreenScale(2f), eps)  // never past it
    }

    @Test fun aScreenMovesTowardTheSideTheSwipeCameFrom() {
        // 1080 px wide at density 3: a twentieth (54) less 8 dp (24) = 30 px at the end of the swipe
        assertEquals(30f, backScreenShift(1f, 1080f, fromLeft = true, density = 3f), eps)
        assertEquals(-30f, backScreenShift(1f, 1080f, fromLeft = false, density = 3f), eps)
        assertEquals(15f, backScreenShift(0.5f, 1080f, fromLeft = true, density = 3f), eps)
        assertEquals(0f, backScreenShift(0f, 1080f, fromLeft = true, density = 3f), eps)
        assertEquals(0f, backScreenShift(1f, 300f, fromLeft = true, density = 3f), eps)  // too narrow: no move
        assertEquals(0f, backScreenShift(1f, 0f, fromLeft = false, density = 3f), eps)  // unmeasured
    }

    @Test fun aSheetNarrowsByAFixedDistance() {
        // 48 dp across a 1080 px sheet at density 3: 144 px, so 1 - 144/1080
        assertEquals(1f - 144f / 1080f, backShrink(1f, 1080f, 144f), eps)
        assertEquals(1f - 72f / 1080f, backShrink(0.5f, 1080f, 144f), eps)
        assertEquals(1f, backShrink(0f, 1080f, 144f), eps)
        assertEquals(1f, backShrink(1f, 0f, 144f), eps)  // not measured: whole
        assertEquals(0.5f, backShrink(1f, 100f, 144f), eps)  // never below half
    }

    @Test fun betweenFollowsProgress() {
        assertEquals(1f, backBetween(1f, BACK_PICTURE_DARK, 0f), eps)
        assertEquals(BACK_PICTURE_DARK, backBetween(1f, BACK_PICTURE_DARK, 1f), eps)
        assertEquals(0.85f, backBetween(1f, 0.7f, 0.5f), eps)
    }

    @Test fun darkenKeepsAlphaAndGoesTowardBlack() {
        assertEquals(0xFF102030.toInt(), darken(0xFF102030.toInt(), 0f))
        assertEquals(0xFF000000.toInt(), darken(0xFF102030.toInt(), 1f))
        assertEquals(0xFF081018.toInt(), darken(0xFF102030.toInt(), 0.5f))
        assertEquals(0xFF000000.toInt(), darken(0xFFFFFFFF.toInt(), 5f))  // share held to 1
    }

    @Test fun backTakesTheTopMenuThenTheOneUnderThenSelectionThenTheScreen() {
        assertEquals(BackStep.SHEET, mainBackStep(sheetOpen = true, panelOpen = true, selecting = true, onList = false))
        assertEquals(BackStep.PANEL, mainBackStep(sheetOpen = false, panelOpen = true, selecting = true, onList = false))
        assertEquals(BackStep.SELECTION, mainBackStep(sheetOpen = false, panelOpen = false, selecting = true, onList = false))
        assertEquals(BackStep.SCREEN, mainBackStep(sheetOpen = false, panelOpen = false, selecting = false, onList = false))
        assertEquals(BackStep.PANEL, mainBackStep(sheetOpen = false, panelOpen = true, selecting = false, onList = true))
    }

    @Test fun onTheListWithNothingOpenBackLeavesTheApp() {
        assertNull(mainBackStep(sheetOpen = false, panelOpen = false, selecting = false, onList = true))
    }

    @Test fun aSwipesLookIsKeptOnlyWhenTheSameSheetCloses() {
        val sheet = Any()
        assertTrue(keepsBackPreview(keeps = true, started = sheet, now = sheet))
        assertFalse(keepsBackPreview(keeps = true, started = sheet, now = Any()))  // another sheet came up meanwhile
        assertFalse(keepsBackPreview(keeps = true, started = sheet, now = null))  // it was closed meanwhile
        assertFalse(keepsBackPreview(keeps = false, started = sheet, now = sheet))  // a screen: put back whole first
        assertFalse(keepsBackPreview(keeps = true, started = null, now = null))
    }
}
