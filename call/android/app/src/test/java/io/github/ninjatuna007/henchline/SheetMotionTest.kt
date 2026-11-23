package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SheetMotionTest {
    private val fling = 1500f * 2.75f  // SHEET_FLING_DP on a 2.75x screen

    @Test fun aFlingDownClosesItHoweverLittleItWasPulled() {
        assertTrue(sheetCloses(offset = 4f, height = 1000f, velocity = fling, fling = fling))
        assertTrue(sheetCloses(offset = 0f, height = 1000f, velocity = fling * 3, fling = fling))
    }

    @Test fun aFlingUpKeepsItHoweverFarItWasPulled() {
        assertFalse(sheetCloses(offset = 900f, height = 1000f, velocity = -fling, fling = fling))
    }

    @Test fun letGoSlowlyItClosesOnlyPastAQuarter() {
        assertFalse(sheetCloses(offset = 249f, height = 1000f, velocity = 0f, fling = fling))
        assertFalse(sheetCloses(offset = 250f, height = 1000f, velocity = fling - 1, fling = fling))
        assertTrue(sheetCloses(offset = 251f, height = 1000f, velocity = 0f, fling = fling))
        assertTrue(sheetCloses(offset = 600f, height = 1000f, velocity = -(fling - 1), fling = fling))
    }

    @Test fun aSheetNotYetMeasuredIsNotClosedByASlowRelease() {
        assertFalse(sheetCloses(offset = 10f, height = 0f, velocity = 0f, fling = fling))
    }

    @Test fun theScrimFollowsTheSheet() {
        assertEquals(1f, scrimShare(0f, 1000f), 0f)
        assertEquals(1f, scrimShare(-15f, 1000f), 0f)  // a spring settling past rest: still whole
        assertEquals(0.75f, scrimShare(250f, 1000f), 1e-6f)
        assertEquals(0f, scrimShare(1000f, 1000f), 0f)
        assertEquals(0f, scrimShare(5000f, 1000f), 0f)  // out of sight, waiting for its first frame
    }

    @Test fun atRestTheScrimIsWholeEvenBeforeTheSheetIsMeasured() {
        assertEquals(1f, scrimShare(0f, 0f), 0f)  // shown at once (animations off): never left clear
        assertEquals(0f, scrimShare(10f, 0f), 0f)
    }

    @Test fun aSheetOverAnotherDimsLess() {
        assertEquals(0.32f, SCRIM, 0f)
        assertTrue(STACKED_SCRIM in 0f..SCRIM)
    }
}

class SheetStackTest {
    @Test fun theBottomSheetDimsFullyAndEachOverItLess() {
        assertEquals(emptyList<Float>(), stackDims(0))
        assertEquals(listOf(SCRIM), stackDims(1))
        assertEquals(listOf(SCRIM, STACKED_SCRIM, STACKED_SCRIM), stackDims(3))
    }

    @Test fun whenTheLowerSheetGoesTheTopOneBecomesTheBottomAndDarkensToFull() {
        // two open, then the lower one closes: the one left is worked out again as the only one
        assertEquals(STACKED_SCRIM, stackDims(2)[1], 0f)
        assertEquals(SCRIM, stackDims(1)[0], 0f)
    }
}
