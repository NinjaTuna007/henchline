package io.github.ninjatuna007.henchline

import android.content.Context

/**
 * Who hears the user on a call: the laptop (as always), or the phone itself with its own speech model (PhoneAsr)
 * when the line is bad. The user's setting, from Settings › Voices:
 *   AUTOMATIC  the laptop listens; the phone takes over while the line is bad and hands back when it recovers
 *   PHONE      the phone listens from the start of every call (needs the model)
 *   LAPTOP     the laptop only, as before
 * Stored here; the calls act on it in later steps.
 */
object PhoneEar {
    enum class Mode(val key: String, val label: String) {
        AUTOMATIC("automatic", "Automatic"),
        PHONE("phone", "Always phone"),
        LAPTOP("laptop", "Always laptop");

        companion object {
            /** A stored value back to its mode; anything unknown is the default. */
            fun of(key: String?): Mode = entries.firstOrNull { it.key == key } ?: AUTOMATIC
        }
    }

    private const val PREF = "phone_ears"

    fun mode(context: Context): Mode = Mode.of(prefs(context).getString(PREF, null))
    fun setMode(context: Context, mode: Mode) = prefs(context).edit().putString(PREF, mode.key).apply()

    /** The mode a call works with: Always phone without the model is Automatic until it's downloaded. */
    fun effective(chosen: Mode, ready: Boolean): Mode = if (chosen == Mode.PHONE && !ready) Mode.AUTOMATIC else chosen

    /** The line under the setting in the Voices sheet. */
    fun line(chosen: Mode, ready: Boolean): String = when (effective(chosen, ready)) {
        Mode.AUTOMATIC -> if (ready) "The laptop listens; when the line is bad, this phone does until it recovers."
            else "The laptop listens. Download the speech model so this phone can listen when the line is bad."
        Mode.PHONE -> "This phone hears you on every call, and sends the laptop your words."
        Mode.LAPTOP -> "Only the laptop listens, as before: a bad line can cut your words."
    }

    private fun prefs(context: Context) = context.getSharedPreferences("call", Context.MODE_PRIVATE)
}
