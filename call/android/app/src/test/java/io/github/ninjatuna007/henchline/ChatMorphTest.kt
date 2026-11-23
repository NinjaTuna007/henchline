package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatMorphTest {
    private val eps = 1e-4f

    private fun assertBox(expected: Box, actual: Box?) {
        requireNotNull(actual)
        assertEquals(expected.left, actual.left, eps); assertEquals(expected.top, actual.top, eps)
        assertEquals(expected.right, actual.right, eps); assertEquals(expected.bottom, actual.bottom, eps)
    }

    @Test fun theContainerRunsFourHundredFiftyMilliseconds() {
        assertEquals(450L, ChatMorph.MS)
    }

    @Test fun theBoxMovesEdgeByEdgeFromTheRowToTheScreen() {
        val row = Box(12f, 400f, 1068f, 596f)
        val screen = Box(0f, 0f, 1080f, 2000f)
        assertBox(row, ChatMorph.lerp(row, screen, 0f))
        assertBox(screen, ChatMorph.lerp(row, screen, 1f))
        assertBox(Box(6f, 200f, 1074f, 1298f), ChatMorph.lerp(row, screen, 0.5f))
    }

    @Test fun onlyThePartOfTheRowTheListShowsCounts() {
        val list = Box(0f, 300f, 1080f, 1800f)
        assertBox(Box(12f, 400f, 1068f, 596f), ChatMorph.visiblePart(Box(12f, 400f, 1068f, 596f), list))
        assertBox(Box(12f, 300f, 1068f, 396f), ChatMorph.visiblePart(Box(12f, 200f, 1068f, 396f), list))  // cut at the top
        assertNull(ChatMorph.visiblePart(Box(12f, 100f, 1068f, 296f), list))  // scrolled away
        assertNull(ChatMorph.visiblePart(Box(12f, 1796f, 1068f, 1990f), list))  // a sliver: none
    }

    @Test fun aSwipedScreenIsWhereItsScaleAndShiftPutIt() {
        // 0.9 about its centre, moved 30 px right: 1000 wide -> 900 wide starting 50 + 30 in
        assertBox(Box(80f, 100f, 980f, 1900f),
            ChatMorph.scaledBox(0f, 0f, 1000f, 2000f, 0.9f, 0.9f, 500f, 1000f, 30f, 0f))
        assertBox(Box(0f, 56f, 1000f, 2056f), ChatMorph.scaledBox(0f, 56f, 1000f, 2000f, 1f, 1f, 500f, 1000f, 0f, 0f))
    }

    @Test fun theFallbackShrinksAboutTheCentre() {
        assertBox(Box(30f, 60f, 970f, 1940f), ChatMorph.shrunk(Box(0f, 0f, 1000f, 2000f), 0.94f))
        assertEquals(RemoteChatMotion.CLOSE_SCALE, ChatMorph.FALLBACK_SCALE, 0f)
    }

    @Test fun itGrowsOnlyWithAnimationsOnAndARow() {
        assertTrue(ChatMorph.grows(true, Box(0f, 0f, 10f, 10f)))
        assertFalse(ChatMorph.grows(false, Box(0f, 0f, 10f, 10f)))  // animations off: it just appears
        assertFalse(ChatMorph.grows(true, null))  // a notification, the call bar, a Calls row: no row
        assertFalse(ChatMorph.grows(true, Box(0f, 0f, 0f, 10f)))
    }

    @Test fun whatWasThereIsGoneByAThirdAndTheFillLastsUntilThen() {
        assertEquals(1f, ChatMorph.outgoing(0f), eps)
        assertEquals(0f, ChatMorph.outgoing(0.35f), eps)
        assertEquals(0f, ChatMorph.outgoing(1f), eps)
        assertEquals(1f, ChatMorph.closingFill(0f), eps)
        assertEquals(1f, ChatMorph.closingFill(0.35f), eps)
        assertEquals(0f, ChatMorph.closingFill(1f), eps)
        assertTrue(ChatMorph.closingFill(0.7f) in 0f..1f)
    }

    @Test fun theListDarkensOpeningAndLightensClosingFromWhereTheSwipeLeftIt() {
        assertEquals(0f, ChatMorph.openingScrim(0f), eps)
        assertEquals(ChatMorph.SCRIM, ChatMorph.openingScrim(1f), eps)
        assertEquals(ChatMorph.SCRIM, ChatMorph.closeScrimFrom(null), eps)  // as dark as the open left it
        assertEquals(0.3f, ChatMorph.closeScrimFrom(0.3f), eps)  // a swipe's darkness carries on
        assertEquals(0.3f, ChatMorph.closingScrim(0.3f, 0f), eps)
        assertEquals(0.15f, ChatMorph.closingScrim(0.3f, 0.5f), eps)
        assertEquals(0f, ChatMorph.closingScrim(0.3f, 1f), eps)
    }

    @Test fun thePictureFillsTheBoxWidth() {
        assertEquals(0.5f, ChatMorph.fitWidth(Box(0f, 0f, 540f, 100f), 1080f), eps)
        assertEquals(1f, ChatMorph.fitWidth(Box(0f, 0f, 540f, 100f), 0f), eps)
    }

    @Test fun aSwipeAllTheWayIsAtTheBackScale() {
        // The look a back swipe hands the close: the screen at backScreenScale about its centre
        val s = backScreenScale(1f)
        val b = ChatMorph.scaledBox(0f, 0f, 1080f, 2000f, s, s, 540f, 1000f, 0f, 0f)
        assertEquals(1080f * BACK_SCREEN_SCALE, b.width, 0.01f)
        assertEquals(1000f, (b.top + b.bottom) / 2, 0.01f)
    }
}
