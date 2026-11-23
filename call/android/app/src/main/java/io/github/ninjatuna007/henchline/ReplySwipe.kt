package io.github.ninjatuna007.henchline

import android.os.Build
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.accessibility.AccessibilityNodeInfo
import androidx.dynamicanimation.animation.DynamicAnimation
import androidx.dynamicanimation.animation.SpringAnimation

/** A message dragged right to reply to it: the plain numbers and rules. It follows the finger to the threshold
 *  (where a tick says letting go replies), then pulls like a rubber band toward a hard stop; let go, it springs home
 *  carrying the finger's speed. */
object ReplySwipeRules {
    const val START_DP = 12      // sideways this far (and twice as far as down) before it is a swipe, not a scroll
    const val THRESHOLD_DP = 72  // let go past this: it replies
    const val MAX_DP = 96        // never drawn past this

    /** Where the row is drawn for a pull of dx (px): with the finger up to threshold, then resisting toward max;
     *  never left of home. */
    fun shown(dx: Float, threshold: Float, max: Float): Float =
        if (dx <= 0f) 0f else resist(dx, 0f, threshold, 0f, max)

    /** The pull that puts the row where it is drawn now: a touch on a row still springing home picks it up there. */
    fun pullFor(shown: Float, threshold: Float, max: Float): Float =
        if (shown <= 0f) 0f else unresist(shown, 0f, threshold, 0f, max)

    /** Whether letting go now replies. */
    fun armed(shown: Float, threshold: Float) = shown >= threshold

    /** The tick felt as the threshold is crossed (armed: outward; else back in), or null for none. 14 and later
     *  have the gesture threshold's own; older ones tick going out only. */
    fun haptic(sdk: Int, armed: Boolean): Int? = when {
        sdk >= 34 -> if (armed) HapticFeedbackConstants.GESTURE_THRESHOLD_ACTIVATE else HapticFeedbackConstants.GESTURE_THRESHOLD_DEACTIVATE
        armed -> HapticFeedbackConstants.CONTEXT_CLICK
        else -> null
    }

    /** A touch starting this close to the screen's left edge (dp) is the edge's back swipe, not a reply. */
    const val EDGE_DP = 24

    fun inEdge(rawX: Float, edge: Float) = rawX < edge

    /** Whether a move (mx, my: px since the finger went down) makes this a sideways drag. A row caught while it
     *  springs home (caught) takes any clear sideways move past slop, either way; else only rightward, past
     *  start, twice as much across as down. Until then the list may still take it as a scroll. */
    fun startsDrag(caught: Boolean, mx: Float, my: Float, slop: Float, start: Float): Boolean =
        if (caught) kotlin.math.abs(mx) > slop && kotlin.math.abs(mx) > 2 * kotlin.math.abs(my)
        else mx > start && mx > 2 * kotlin.math.abs(my)

    /** The speed the spring home starts with: only the row's speed back toward home (a fling outward would carry it
     *  past the stop). */
    fun homeVelocity(velocity: Float) = velocity.coerceAtMost(0f)

    /** The row's speed (px/s) at release, from its last two places drawn and their times (ms); none for no time. */
    fun velocity(from: Float, fromAt: Long, to: Float, toAt: Long, limit: Float): Float {
        val dt = toAt - fromAt
        if (dt <= 0L) return 0f
        return ((to - from) * 1000f / dt).coerceIn(-limit, limit)
    }
}

/**
 * The swipe-to-reply touch on a chat line's row: the row takes every touch (a row that doesn't take the DOWN never
 * sees the drag); the list still scrolls, as the ScrollView takes over a vertical move. Held still, the line's menu
 * (onLong). A screen reader gets Reply as an action on the row instead of the swipe.
 */
class ReplySwipe private constructor(private val row: View, private val reply: () -> Unit,
                                     private val onLong: (() -> Unit)?) : View.OnTouchListener {
    private val density = row.resources.displayMetrics.density
    private val threshold = ReplySwipeRules.THRESHOLD_DP * density
    private val max = ReplySwipeRules.MAX_DP * density
    private val slop = ViewConfiguration.get(row.context).scaledTouchSlop
    private val fling = ViewConfiguration.get(row.context).scaledMaximumFlingVelocity.toFloat()
    private val edge = ReplySwipeRules.EDGE_DP * density
    private var startX = 0f
    private var startY = 0f
    private var downX = 0f
    private var caught = false  // the touch began on the row still springing home
    private var dragging = false
    private var longDone = false
    private var armed = false
    private var lastShown = 0f
    private var lastAt = 0L
    private var prevShown = 0f
    private var prevAt = 0L
    private var home: SpringAnimation? = null
    private val held = Runnable {
        longDone = true
        row.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
        onLong?.invoke()
    }

    override fun onTouch(view: View, event: MotionEvent): Boolean = when (event.actionMasked) {
        MotionEvent.ACTION_DOWN -> {
            if (ReplySwipeRules.inEdge(event.rawX, edge)) false  // the edge's back swipe: left to it (a row still springing home goes on home)
            else {
                // Still springing home: caught where it is, and the pull goes on from there once it is a sideways
                // drag; till then the list may still scroll
                home?.cancel(); home = null
                caught = row.translationX > 0.5f
                val pull = ReplySwipeRules.pullFor(row.translationX, threshold, max)
                startX = event.rawX - pull; startY = event.rawY; downX = event.rawX
                dragging = false
                armed = ReplySwipeRules.armed(row.translationX, threshold)
                longDone = false
                lastShown = row.translationX; lastAt = event.eventTime; prevShown = lastShown; prevAt = lastAt
                if (onLong != null && !caught) row.postDelayed(held, ViewConfiguration.getLongPressTimeout().toLong())
                true
            }
        }
        MotionEvent.ACTION_MOVE -> {
            val dx = event.rawX - startX
            val dy = event.rawY - startY
            val mx = event.rawX - downX
            if (kotlin.math.abs(mx) > slop || kotlin.math.abs(dy) > slop) row.removeCallbacks(held)
            if (!dragging && ReplySwipeRules.startsDrag(caught, mx, dy, slop.toFloat(), ReplySwipeRules.START_DP * density)) {
                dragging = true
                row.parent?.requestDisallowInterceptTouchEvent(true)
            }
            if (dragging && !longDone) {
                val shown = ReplySwipeRules.shown(dx, threshold, max)
                row.translationX = shown
                prevShown = lastShown; prevAt = lastAt; lastShown = shown; lastAt = event.eventTime
                val now = ReplySwipeRules.armed(shown, threshold)
                if (now != armed) {
                    armed = now
                    ReplySwipeRules.haptic(Build.VERSION.SDK_INT, now)?.let { row.performHapticFeedback(it) }
                }
            }
            dragging
        }
        MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
            row.removeCallbacks(held)
            val was = dragging
            if (was && !longDone && event.actionMasked == MotionEvent.ACTION_UP && armed) reply()
            springHome(if (was) ReplySwipeRules.homeVelocity(
                ReplySwipeRules.velocity(prevShown, prevAt, lastShown, lastAt, fling)) else 0f)
            dragging = false; armed = false; caught = false
            was
        }
        else -> false
    }

    private fun springHome(velocity: Float) {
        home?.cancel()
        if (row.translationX == 0f) { home = null; return }
        home = Motion.spring(row, DynamicAnimation.TRANSLATION_X, 0f, velocity, Motion.SPATIAL) { home = null }
    }

    companion object {
        /** row replies (reply) when dragged right past the threshold, or by a screen reader's Reply. */
        fun attach(row: View, reply: () -> Unit, onLong: (() -> Unit)?) {
            val swipe = ReplySwipe(row, reply, onLong)
            row.setOnTouchListener(swipe)
            // Taken off the screen mid-spring (the chat redrawn): it stops, home
            row.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
                override fun onViewAttachedToWindow(v: View) {}
                override fun onViewDetachedFromWindow(v: View) {
                    swipe.home?.cancel(); swipe.home = null
                    v.removeCallbacks(swipe.held)
                    v.translationX = 0f
                }
            })
            row.accessibilityDelegate = object : View.AccessibilityDelegate() {
                override fun onInitializeAccessibilityNodeInfo(host: View, info: AccessibilityNodeInfo) {
                    super.onInitializeAccessibilityNodeInfo(host, info)
                    info.addAction(AccessibilityNodeInfo.AccessibilityAction(R.id.replyAction, "Reply"))
                    // The hold's menu, which the row sees from its own touches (not a long-click listener)
                    if (onLong != null) {
                        info.isLongClickable = true
                        info.addAction(AccessibilityNodeInfo.AccessibilityAction(AccessibilityNodeInfo.ACTION_LONG_CLICK, "Message menu"))
                    }
                }
                override fun performAccessibilityAction(host: View, action: Int, args: android.os.Bundle?): Boolean {
                    if (action == R.id.replyAction) { reply(); return true }
                    if (action == AccessibilityNodeInfo.ACTION_LONG_CLICK && onLong != null) { onLong.invoke(); return true }
                    return super.performAccessibilityAction(host, action, args)
                }
            }
        }
    }
}
