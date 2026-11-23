package io.github.ninjatuna007.henchline

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class RemoteChatMotionTest {
    @Test fun theChatRevealsFromTheWholeRow() {
        assertArrayEquals(intArrayOf(0, 0, 1080, 196), RemoteChatMotion.revealFrom(true, true, true, 1080, 196))
    }

    @Test fun noRevealWithAnimationsOffOrNoRowOnScreen() {
        assertNull(RemoteChatMotion.revealFrom(false, true, true, 1080, 196))  // animations off
        assertNull(RemoteChatMotion.revealFrom(true, false, true, 1080, 196))  // the row has left the screen
        assertNull(RemoteChatMotion.revealFrom(true, true, false, 1080, 196))  // not measured yet
        assertNull(RemoteChatMotion.revealFrom(true, true, true, 0, 196))
        assertNull(RemoteChatMotion.revealFrom(true, true, true, 1080, 0))
    }

    @Test fun onlyAChatThatWasDrawnClosesWithTheFadeAndOnlyWithAnimationsOn() {
        assertTrue(RemoteChatMotion.closeAnimated(on = true, shown = true))
        assertFalse(RemoteChatMotion.closeAnimated(on = true, shown = false))  // closed at once (preview off, not paired)
        assertFalse(RemoteChatMotion.closeAnimated(on = false, shown = true))
    }

    @Test fun theCloseAnimationFadesAndShrinksToTheScaleOverTheTime() {
        // the window animation is a resource the system reads: it must say what RemoteChatMotion says
        val xml = File("src/main/res/anim/chat_close.xml").readText()
        assertEquals(0.94f, RemoteChatMotion.CLOSE_SCALE, 0f)
        assertTrue(xml.contains("android:toXScale=\"0.94\"") && xml.contains("android:toYScale=\"0.94\""))
        assertTrue(xml.contains("android:toAlpha=\"0.0\""))
        assertTrue(xml.contains("android:duration=\"${RemoteChatMotion.CLOSE_MS}\""))
        assertTrue(xml.contains("@interpolator/emphasized_accelerate"))
        val easing = File("src/main/res/interpolator/emphasized_accelerate.xml").readText()
        for (c in listOf("controlX1=\"0.3\"", "controlY1=\"0\"", "controlX2=\"0.8\"", "controlY2=\"0.15\"")) assertTrue(c, easing.contains(c))
    }
}
