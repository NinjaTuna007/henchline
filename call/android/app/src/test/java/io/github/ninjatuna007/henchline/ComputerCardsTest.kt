package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ComputerCardsTest {
    @Test fun theLaptopIsLavenderAndTheOthersTakeTheirColoursByPairingOrder() {
        assertEquals(Accent.LAVENDER, ComputerCards.accentOf(null, -1))
        assertEquals(Accent.CORAL, ComputerCards.accentOf(null, 0))
        assertEquals(Accent.SKY, ComputerCards.accentOf(null, 1))
        assertEquals(Accent.ROSE, ComputerCards.accentOf(null, 2))
        assertEquals(Accent.SAND, ComputerCards.accentOf(null, 3))
        assertEquals(Accent.CORAL, ComputerCards.accentOf(null, 4))  // a fifth other computer: round again
    }

    @Test fun theUsersPickWinsAndAnUnknownNameIsIgnored() {
        assertEquals(Accent.SAND, ComputerCards.accentOf("SAND", -1))
        assertEquals(Accent.LAVENDER, ComputerCards.accentOf("LAVENDER", 2))
        assertEquals(Accent.SKY, ComputerCards.accentOf("TEAL", 1))
        assertEquals(Accent.SKY, ComputerCards.accentOf("", 1))
    }

    @Test fun aCardIsTheSurfaceWithSixPercentOfItsColour() {
        val surface = 0xFF14204A.toInt()
        val lavender = 0xFFA99BFF.toInt()
        val tinted = ComputerCards.mix(surface, lavender, ComputerCards.TINT)
        // 0x14 + (0xA9 - 0x14) * 0.06 = 28.9 -> 29; 0x20 + 0x7B * 0.06 = 39.4 -> 39; 0x4A + 0xB5 * 0.06 = 84.9 -> 85
        assertEquals(0xFF1D2755.toInt(), tinted)
        assertEquals(surface, ComputerCards.mix(surface, lavender, 0f))
        assertEquals(lavender, ComputerCards.mix(surface, lavender, 1f))
        assertEquals(0xFF, tinted ushr 24)  // opaque
    }

    @Test fun unfoldingTakesLongerThanFolding() {
        assertEquals(400L, ComputerCards.foldMs(opening = true))
        assertEquals(250L, ComputerCards.foldMs(opening = false))
    }

    @Test fun onlyACardOfMoreThanThirtyRowsCrossfades() {
        assertFalse(ComputerCards.crossfades(0))
        assertFalse(ComputerCards.crossfades(30))
        assertTrue(ComputerCards.crossfades(31))
    }

    @Test fun theChevronTurnsHalfATurnFromFoldedToOpen() {
        assertEquals(0f, ComputerCards.chevronAngle(0f), 0f)
        assertEquals(90f, ComputerCards.chevronAngle(0.5f), 1e-4f)
        assertEquals(180f, ComputerCards.chevronAngle(1f), 0f)
        assertEquals(180f, ComputerCards.chevronAngle(1.2f), 0f)  // an easing past its end never over-turns it
    }

    @Test fun theCornersRoundToAPillWhenFolded() {
        assertEquals(28f, ComputerCards.cornerAt(0f, open = 20f, folded = 28f), 0f)
        assertEquals(20f, ComputerCards.cornerAt(1f, open = 20f, folded = 28f), 0f)
        assertEquals(24f, ComputerCards.cornerAt(0.5f, open = 20f, folded = 28f), 1e-4f)
        assertEquals(28, ComputerCards.HEADING_DP / 2)
    }

    @Test fun unfoldingRowsComeInLateAndFoldingOnesGoEarly() {
        assertEquals(0f, ComputerCards.rowsAlpha(0f, opening = true), 0f)
        assertEquals(0f, ComputerCards.rowsAlpha(0.3f, opening = true), 0f)
        assertEquals(0.5f, ComputerCards.rowsAlpha(0.65f, opening = true), 1e-4f)
        assertEquals(1f, ComputerCards.rowsAlpha(1f, opening = true), 0f)
        assertEquals(1f, ComputerCards.rowsAlpha(0f, opening = false), 0f)
        assertEquals(0.5f, ComputerCards.rowsAlpha(0.25f, opening = false), 1e-4f)
        assertEquals(0f, ComputerCards.rowsAlpha(0.5f, opening = false), 0f)
        assertEquals(0f, ComputerCards.rowsAlpha(1f, opening = false), 0f)
    }

    @Test fun theRowsHeightRunsBetweenItsEnds() {
        assertEquals(0, ComputerCards.heightAt(0f, 0, 600))
        assertEquals(300, ComputerCards.heightAt(0.5f, 0, 600))
        assertEquals(600, ComputerCards.heightAt(1f, 0, 600))
        assertEquals(600, ComputerCards.heightAt(0f, 600, 0))
        assertEquals(0, ComputerCards.heightAt(1f, 600, 0))
        assertEquals(0, ComputerCards.heightAt(1.3f, 600, 0))  // never negative
    }

    @Test fun aRedrawDuringAFoldWaitsAndRunsOnceAfterIt() {
        val guard = RedrawGuard()
        assertTrue(guard.ask())
        guard.begin()
        assertTrue(guard.busy)
        assertFalse(guard.ask())
        assertFalse(guard.ask())
        assertTrue(guard.end())  // one redraw owed, now
        assertFalse(guard.busy)
        assertTrue(guard.ask())
    }

    @Test fun aFoldWithNoRedrawAskedOwesNone() {
        val guard = RedrawGuard()
        guard.begin()
        assertFalse(guard.end())
        assertFalse(guard.end())  // an extra end doesn't go below zero
        assertTrue(guard.ask())
    }

    @Test fun overlappingFoldsHoldTheRedrawUntilTheLastEnds() {
        val guard = RedrawGuard()
        guard.begin(); guard.begin()
        assertFalse(guard.ask())
        assertFalse(guard.end())
        assertTrue(guard.busy)
        assertTrue(guard.end())
    }

    @Test fun withNoPicksEachComputerHasItsDefault() {
        val got = ComputerCards.accents("laptop", listOf("a", "b", "c", "d"), emptyMap())
        assertEquals(listOf(Accent.LAVENDER, Accent.CORAL, Accent.SKY, Accent.ROSE, Accent.SAND), got.values.toList())
        assertEquals(listOf("laptop", "a", "b", "c", "d"), got.keys.toList())
    }

    @Test fun aDefaultSkipsAColourTheUserPickedForAnotherComputer() {
        // b picked coral (a's default): a takes the next free one after coral
        val got = ComputerCards.accents("laptop", listOf("a", "b"), mapOf("b" to "CORAL"))
        assertEquals(Accent.LAVENDER, got["laptop"])
        assertEquals(Accent.SKY, got["a"])
        assertEquals(Accent.CORAL, got["b"])
    }

    @Test fun theLaptopToo() {
        val got = ComputerCards.accents("laptop", listOf("a"), mapOf("a" to "LAVENDER"))
        assertEquals(Accent.LAVENDER, got["a"])
        assertEquals(Accent.CORAL, got["laptop"])
    }

    @Test fun twoPicksOfOneColourAreKeptAsPicked() {
        val got = ComputerCards.accents("laptop", listOf("a", "b"), mapOf("a" to "ROSE", "b" to "ROSE"))
        assertEquals(Accent.ROSE, got["a"]); assertEquals(Accent.ROSE, got["b"])
        assertEquals(Accent.LAVENDER, got["laptop"])
    }

    @Test fun withEveryColourTakenAComputerKeepsItsDefault() {
        val picks = mapOf("a" to "LAVENDER", "b" to "CORAL", "c" to "SKY", "d" to "ROSE", "e" to "SAND")
        val got = ComputerCards.accents("laptop", listOf("a", "b", "c", "d", "e", "f"), picks)
        assertEquals(Accent.LAVENDER, got["laptop"])
        assertEquals(Accent.SKY, got["f"])  // the sixth other computer: its default (5 % 4 -> sky)
    }

    @Test fun aComputersHeadingIsReadWithItsStateAndUnreadWhenFolded() {
        assertEquals("Desk, offline · 3 min, 2 unread, folded", ComputerCards.describe("Desk", "offline · 3 min", 2, open = false))
        assertEquals("Desk, open", ComputerCards.describe("Desk", "", 2, open = true))
        assertEquals("This laptop, folded", ComputerCards.describe("This laptop", "  ", 0, open = false))
        assertEquals("Desk, folded", ComputerCards.describe("Desk", "…", 0, open = false))
    }

    @Test fun aFoldStoppedShortOwesNothing() {
        val guard = RedrawGuard()
        guard.begin()
        assertFalse(guard.ask())
        guard.reset()
        assertFalse(guard.busy)
        assertTrue(guard.ask())
        guard.begin()
        assertFalse(guard.end())  // the redraw asked before the reset isn't owed again
    }
}
