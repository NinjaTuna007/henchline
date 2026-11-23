package io.github.ninjatuna007.henchline

import android.app.Activity
import android.app.ActivityOptions
import android.content.Intent
import android.os.Build
import android.view.View

/** A computer's chat (HostChatActivity) opening out of the row that was tapped (a clip reveal from the row's
 *  bounds), and closing by fading and shrinking to [CLOSE_SCALE] over the list (res/anim/chat_close.xml). Both are
 *  window animations the system runs: nothing in either screen's views moves, so nothing can be left offset, faded
 *  or on a layer if they are cut short. With animations off ([Motion.on]) neither is asked for: the system opens
 *  and closes it as it always does (and its own transition scale decides whether that moves at all). */
object RemoteChatMotion {
    /** How far the chat shrinks as it fades away. */
    const val CLOSE_SCALE = 0.94f
    const val CLOSE_MS = Motion.MEDIUM

    /** The rectangle (x, y, width, height, in the row's own coordinates) the chat reveals from: the whole row.
     *  Null (a plain open) with animations off, or a row not on screen or not measured yet. */
    fun revealFrom(on: Boolean, attached: Boolean, laidOut: Boolean, width: Int, height: Int): IntArray? =
        if (on && attached && laidOut && width > 0 && height > 0) intArrayOf(0, 0, width, height) else null

    /** Whether closing asks for the fade-and-shrink: only a chat that was drawn (one that closes at once, the
     *  preview off or the computer not paired, never showed: a plain close), and only with animations on. */
    fun closeAnimated(on: Boolean, shown: Boolean) = on && shown

    /** Opens the chat intent, out of row when there is one to reveal from. */
    fun open(activity: Activity, intent: Intent, row: View?) {
        val r = row?.let { revealFrom(Motion.on(), it.isAttachedToWindow, it.isLaidOut, it.width, it.height) }
        if (row == null || r == null) activity.startActivity(intent)
        else activity.startActivity(intent, ActivityOptions.makeClipRevealAnimation(row, r[0], r[1], r[2], r[3]).toBundle())
    }

    /** Before the chat's finish(): asks for the close animation (34 and later). The list underneath doesn't move. */
    fun beforeFinish(activity: Activity, shown: Boolean) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE && closeAnimated(Motion.on(), shown))
            activity.overrideActivityTransition(Activity.OVERRIDE_TRANSITION_CLOSE, 0, R.anim.chat_close)
    }

    /** After the chat's finish(): asks for the close animation (before 34, where it is asked for afterwards). */
    fun afterFinish(activity: Activity, shown: Boolean) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE && closeAnimated(Motion.on(), shown))
            @Suppress("DEPRECATION") activity.overridePendingTransition(0, R.anim.chat_close)
    }
}
