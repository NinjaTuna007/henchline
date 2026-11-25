package io.github.ninjatuna007.henchline

import android.media.AudioManager

/**
 * The laptop's question on an earbud tap: is the phone on a call? Multipoint earbuds pause the laptop as the phone
 * answers one, and that pause was taken for a tap: it recorded the call (2026-11-25). Any call counts, a phone
 * call's mode or an app's (communication mode), except Henchline's own, on which taps must keep working. A ringing
 * phone counts too: buds that ring in-band pause the laptop as it starts, and the user's "Hello?" must not go out.
 */
object ProbeAnswer {
    const val ON_CALL = "on-call"
    const val FREE = "free"

    // The later modes are plain ints: a phone too old for one never reports it
    private val CALL_MODES = setOf(AudioManager.MODE_RINGTONE, AudioManager.MODE_IN_CALL,
        AudioManager.MODE_IN_COMMUNICATION, AudioManager.MODE_CALL_SCREENING, AudioManager.MODE_CALL_REDIRECT,
        AudioManager.MODE_COMMUNICATION_REDIRECT)

    fun of(mode: Int, henchlineCallUp: Boolean): String = if (mode in CALL_MODES && !henchlineCallUp) ON_CALL else FREE
}
