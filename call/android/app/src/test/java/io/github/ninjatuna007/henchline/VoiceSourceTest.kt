package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The Voices sheet's "Laptop | Phone" switch: where the thumb sits and what the line under it says. */
class VoiceSourceTest {
    private fun state(enabled: Boolean = false, ready: Boolean = true, onCall: Boolean = false, otherHost: Boolean = false,
                      fellBehind: Boolean = false, preparing: Boolean = false) =
        VoiceSource.state(enabled, ready, onCall, otherHost, fellBehind, preparing)

    @Test fun offACallTheChoiceIsForTheNextCall() {
        assertEquals(VoiceSource.State(false, true, VoiceSource.LAPTOP_LINE + VoiceSource.NEXT_CALL), state())
        assertEquals(VoiceSource.State(true, true, VoiceSource.PHONE_LINE + VoiceSource.NEXT_CALL), state(enabled = true))
    }

    @Test fun onACallItAppliesNow() {
        assertEquals(VoiceSource.LAPTOP_LINE, state(onCall = true).line)
        assertEquals(VoiceSource.PHONE_LINE, state(enabled = true, onCall = true).line)
    }

    @Test fun phoneChosenWithoutTheModelIsTheLaptop() {
        assertFalse(state(enabled = true, ready = false).phone)
        assertTrue(state(enabled = true, ready = false).phoneUsable)  // a tap offers the download
    }

    @Test fun anotherComputersCallIsAlwaysThePhonesVoiceAndFixed() {
        for (enabled in listOf(true, false)) {  // whatever the laptop-call choice is
            val s = state(enabled = enabled, onCall = true, otherHost = true)
            assertTrue(s.phone)
            assertTrue(s.fixed)
            assertEquals(VoiceSource.OTHER_HOST_LINE, s.line)
        }
        val behind = state(enabled = false, onCall = true, otherHost = true, fellBehind = true)
        assertEquals(VoiceSource.OTHER_HOST_BEHIND_LINE, behind.line)
        assertTrue(behind.waiting && behind.fixed)
        assertEquals(VoiceSource.PHONE_LINE + VoiceSource.NEXT_CALL, state(enabled = true, otherHost = true).line)  // no call
        assertFalse(state(enabled = true, onCall = true).fixed)  // the laptop's calls: switchable as before
    }

    @Test fun fallingBehindKeepsTheThumbOnPhoneInTheWaitingColour() {
        val s = state(enabled = true, onCall = true, fellBehind = true)
        assertTrue(s.phone)
        assertTrue(s.waiting)
        assertEquals(VoiceSource.FELL_BEHIND_LINE, s.line)
        assertFalse(state(enabled = false, onCall = true, fellBehind = true).waiting)  // the laptop chosen: nothing to say
    }

    @Test fun loadingJennySaysSo() {
        assertEquals(VoiceSource.PREPARING_LINE, state(enabled = true, onCall = true, preparing = true, fellBehind = true).line)
    }

    @Test fun settingsSummary() {
        assertEquals("Phone · Jenny", VoiceSource.summary(true, "own"))
        assertEquals("Computer · Each its own", VoiceSource.summary(false, "own"))
        assertEquals("Computer · One voice", VoiceSource.summary(false, "one"))
        assertEquals("Computer · Surprise me", VoiceSource.summary(false, "surprise"))
        assertEquals("Computer", VoiceSource.summary(false, null))
        assertEquals("Desk · One voice", VoiceSource.summary(false, "one", "Desk"))  // the computer's own name, when known
        assertEquals("Phone · Jenny", VoiceSource.summary(true, "one", "Desk"))
    }

    @Test fun mobileDataStartsUntickedMidCallOnMobileData() {
        assertTrue(VoiceSource.mobileTicked(pref = true, onCall = false, metered = true))
        assertTrue(VoiceSource.mobileTicked(pref = true, onCall = true, metered = false))
        assertFalse(VoiceSource.mobileTicked(pref = true, onCall = true, metered = true))
        assertFalse(VoiceSource.mobileTicked(pref = false, onCall = false, metered = false))
    }
}
