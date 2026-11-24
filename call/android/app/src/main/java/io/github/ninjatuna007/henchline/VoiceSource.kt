package io.github.ninjatuna007.henchline

/**
 * Where the agents' voices come from: the Voices sheet's "Laptop | Phone" switch. The one source of truth is
 * PhoneVoice.enabled (with the model downloaded); this works out what the sheet and Settings show from plain facts,
 * so it can be tested off the phone.
 */
object VoiceSource {
    const val LAPTOP_LINE = "Each agent's own voice, made on the computer."
    const val PHONE_LINE = "Jenny, made on this phone: steady on a weak signal."
    const val NEXT_CALL = " From the next call."
    const val PREPARING_LINE = "Getting Jenny ready…"
    const val FELL_BEHIND_LINE = "The computer's voices for now: the phone fell behind. Tap Phone to try again."
    const val OTHER_HOST_LINE = "Calls with another computer always use the phone's voice."
    const val OTHER_HOST_BEHIND_LINE = "The phone fell behind: the replies show as text until it catches up."

    /** What the switch shows: where the thumb sits, whether Phone can be tapped, the line under it, and whether that
     *  line is in the waiting colour (the phone fell behind). */
    data class State(val phone: Boolean, val phoneUsable: Boolean, val line: String, val waiting: Boolean = false,
                     val fixed: Boolean = false)  // fixed: this call can't change it (another computer's: always the phone)

    /**
     * enabled: the user's choice (PhoneVoice.enabled); ready: the model is downloaded; onCall: a call is up;
     * otherHost: that call is with another computer (its voices aren't the laptop's); fellBehind: the phone fell back
     * to the laptop's audio by itself this call; preparing: the model is loading after a switch to Phone.
     */
    fun state(enabled: Boolean, ready: Boolean, onCall: Boolean, otherHost: Boolean, fellBehind: Boolean,
              preparing: Boolean): State {
        if (onCall && otherHost) return if (fellBehind) State(phone = true, phoneUsable = true, line = OTHER_HOST_BEHIND_LINE,
            waiting = true, fixed = true) else State(phone = true, phoneUsable = true, line = OTHER_HOST_LINE, fixed = true)
        val phone = enabled && ready
        val later = if (onCall) "" else NEXT_CALL
        return when {
            !phone -> State(false, true, LAPTOP_LINE + later)
            onCall && preparing -> State(true, true, PREPARING_LINE)
            onCall && fellBehind -> State(true, true, FELL_BEHIND_LINE, waiting = true)
            else -> State(true, true, PHONE_LINE + later)
        }
    }

    /** The laptop's voice mode as the sheet names it. */
    fun modeName(mode: String?): String? = when (mode) {
        "own" -> "Each its own"
        "one" -> "One voice"
        "surprise" -> "Surprise me"
        else -> null
    }

    /** Settings' "Agents' voices" value: "Phone · Jenny", or "<computer> · Each its own" (the mode last seen, if any);
     *  computer: the main computer's own name when known. */
    fun summary(phone: Boolean, mode: String?, computer: String = "Computer"): String =
        if (phone) "Phone · Jenny" else modeName(mode)?.let { "$computer · $it" } ?: computer

    /** The download card's "over mobile data too" box: the user's setting, but unticked mid-call on mobile data, so
     *  64 MB never goes over it during a call without a fresh tick. */
    fun mobileTicked(pref: Boolean, onCall: Boolean, metered: Boolean): Boolean = pref && !(onCall && metered)
}
