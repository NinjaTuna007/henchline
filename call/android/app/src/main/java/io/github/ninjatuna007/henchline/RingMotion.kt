package io.github.ninjatuna007.henchline

import android.view.HapticFeedbackConstants

/** The ring screen coming up: the henchman springs up from small, and Decline and Answer slide up 24 dp into place
 *  one after the other. Answer is felt as a confirmation, Decline as a rejection. The plain numbers and rules. */
object RingMotion {
    /** The henchman starts at this fraction of its size (and clear), springing (a little past) to whole. */
    const val AVATAR_FROM = 0.6f
    /** How far below their place the buttons start, and over how long they rise and fade in. */
    const val SLIDE_DP = 24
    const val BUTTON_MS = Motion.LONG
    /** The second button starts this long after the first. */
    const val STAGGER_MS = 60L
    /** The buttons start once the henchman is on its way. */
    const val BUTTONS_AFTER_MS = 80L

    /** When the button at place (0 the first, Decline) starts. */
    fun delayOf(place: Int) = BUTTONS_AFTER_MS + STAGGER_MS * place.coerceAtLeast(0)

    /** Whether the screen comes in moving: animations on, and the screen new (not one made again with what it had). */
    fun enters(on: Boolean, restored: Boolean) = on && !restored

    /** What a tap on Answer feels like: a confirmation (11 and later's own), else a plain key tap. */
    fun answerHaptic(sdk: Int) = if (sdk >= 30) HapticFeedbackConstants.CONFIRM else HapticFeedbackConstants.VIRTUAL_KEY

    /** What a tap on Decline feels like: a rejection, else a long press's firmer buzz. */
    fun declineHaptic(sdk: Int) = if (sdk >= 30) HapticFeedbackConstants.REJECT else HapticFeedbackConstants.LONG_PRESS
}
