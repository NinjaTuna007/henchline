package io.github.ninjatuna007.henchline

import android.media.AudioManager
import org.junit.Assert.assertEquals
import org.junit.Test

/** The laptop's "is the phone on a call?": any call but Henchline's own, on which earbud taps must keep working. */
class ProbeAnswerTest {
    @Test fun aPhoneCallOrAnAppsCallIsOnCall() {
        for (mode in listOf(AudioManager.MODE_IN_CALL, AudioManager.MODE_IN_COMMUNICATION, AudioManager.MODE_CALL_SCREENING,
                AudioManager.MODE_CALL_REDIRECT, AudioManager.MODE_COMMUNICATION_REDIRECT))
            assertEquals("mode $mode", ProbeAnswer.ON_CALL, ProbeAnswer.of(mode, henchlineCallUp = false))
    }

    @Test fun aRingingPhoneIsOnCall() {  // in-band ringing pauses the laptop as the phone starts to ring
        assertEquals(ProbeAnswer.ON_CALL, ProbeAnswer.of(AudioManager.MODE_RINGTONE, henchlineCallUp = false))
    }

    @Test fun noCallIsFree() {
        assertEquals(ProbeAnswer.FREE, ProbeAnswer.of(AudioManager.MODE_NORMAL, henchlineCallUp = false))
    }

    @Test fun henchlinesOwnCallIsFree() {
        assertEquals(ProbeAnswer.FREE, ProbeAnswer.of(AudioManager.MODE_IN_COMMUNICATION, henchlineCallUp = true))
    }
}
