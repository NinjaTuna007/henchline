package io.github.ninjatuna007.henchline

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Color
import android.graphics.drawable.ColorDrawable
import android.view.MotionEvent
import android.view.View
import android.view.ViewTreeObserver
import android.widget.FrameLayout
import androidx.dynamicanimation.animation.SpringAnimation
import kotlin.math.roundToInt

/** How dark the scrim behind a sheet gets, at rest. */
const val SCRIM = 0.32f

/** The scrim of a sheet opened over another: lighter, so the one below dims only a little more than the screen did
 *  (and is whole again once the top one has gone, its scrim with it). */
const val STACKED_SCRIM = 0.16f

/** The scrims of the sheets open now, the bottom one first: the bottom one at full [SCRIM], each over it at
 *  [STACKED_SCRIM]. Worked out again whenever one opens or closes, so the top one darkens to full once the one
 *  under it goes. */
fun stackDims(count: Int): List<Float> = List(count) { if (it == 0) SCRIM else STACKED_SCRIM }

/** A downward fling of a sheet, in dp per second, that closes it however little it was pulled. */
const val SHEET_FLING_DP = 1500f

/** Where a dragged sheet goes when let go, offset px below its rest, of height, moving at velocity px/s (down is
 *  positive): a fling down closes it however little it was pulled, a fling up keeps it however far; slower than
 *  fling, it closes once pulled past a quarter of its height, else springs back. */
fun sheetCloses(offset: Float, height: Float, velocity: Float, fling: Float): Boolean = when {
    velocity >= fling -> true
    velocity <= -fling -> false
    else -> height > 0f && offset > height / 4f
}

/** The scrim's share of its full darkness with the sheet offset px below its rest, out of sight at away px: whole at
 *  rest (or above it), none once out of sight, in between in step with the sheet. So the scrim can't be left dark
 *  or light whatever stops the sheet: it is drawn from where the sheet is. */
fun scrimShare(offset: Float, away: Float): Float = when {
    offset <= 0f -> 1f
    away <= 0f -> 0f
    else -> (1f - offset / away).coerceIn(0f, 1f)
}

/**
 * The full-screen layer a sheet sits in, at its bottom, over a scrim: it brings the sheet up on a spring, follows
 * a drag of it and lets it go (springing back, or closing), and takes it away (200 ms, gathering speed) before
 * it is removed. Each move stops the one before; while leaving, touches are swallowed, so nothing in the sheet acts
 * twice. Taken off the screen (the activity gone), whatever is moving stops, so nothing outlives it.
 */
class SheetFrame(context: Context, private var dim: Float = SCRIM) : FrameLayout(context) {
    private val scrim = ColorDrawable(Color.BLACK).also { it.alpha = 0 }
    private var sheet: View? = null
    private var spring: SpringAnimation? = null
    private var run: ValueAnimator? = null
    private var turn = 0  // which move is the latest: a stopped one's late callbacks are ignored
    private var entering: (() -> Unit)? = null  // takes back the wait for the first frame, while it waits
    private var dimming: ValueAnimator? = null  // the scrim's darkness changing as sheets over or under it come and go
    private var touching = false  // a touch is down in it (a row may be held, a drag going)
    private var backRun: ValueAnimator? = null  // easing back to whole after a back swipe let go short
    private var backAt = 0f  // how far a back swipe has narrowed it (0: whole)

    /** Going away: touches are no longer the sheet's. */
    var leaving = false
        private set

    init { background = scrim }

    /** The sheet this frame moves (already added to it, at its bottom). */
    fun hold(sheet: View) { this.sheet = sheet }

    /** How far below its rest the sheet is out of sight (above a keyboard, the frame's bottom padding too). */
    private fun away(): Float = (sheet?.height ?: 0) + paddingBottom.toFloat()

    /** The sheet offset px below its rest, and the scrim with it. Never above its rest: a spring settling a little
     *  past it would show a gap under the sheet's bottom edge. */
    private fun place(offset: Float) {
        sheet?.translationY = offset.coerceAtLeast(0f)
        scrim.alpha = (255 * dim * scrimShare(offset, away())).roundToInt()
    }

    /** The scrim's full darkness becomes to: eased over a short while when shown, at once when not yet drawn,
     *  leaving, or with animations off (the scrim is redrawn from the sheet's place on every frame of it). */
    fun dimTo(to: Float) {
        dimming?.cancel(); dimming = null
        if (to == dim || leaving) return
        val from = dim
        val set = { d: Float -> dim = d; place(sheet?.translationY ?: 0f) }
        if (!isAttachedToWindow || entering != null) { set(to); return }
        dimming = Motion.run(Motion.MEDIUM, Motion.standard, { t -> set(from + (to - from) * t) })
    }

    private fun stop() {
        turn++
        spring?.cancel(); spring = null
        run?.cancel(); run = null
    }

    private fun stopEntering() { entering?.invoke(); entering = null }

    /** The sheet narrowed as a back swipe at progress has it ([backShrink]: 48 dp across, 24 dp down, about its
     *  bottom edge). Whole at 0. */
    private fun backTo(progress: Float) {
        val s = sheet ?: return
        backAt = backFraction(progress)
        s.pivotX = s.width / 2f
        s.pivotY = s.height.toFloat()
        val density = resources.displayMetrics.density
        s.scaleX = backShrink(backAt, s.width.toFloat(), BACK_SHEET_X_DP * density)
        s.scaleY = backShrink(backAt, s.height.toFloat(), BACK_SHEET_Y_DP * density)
    }

    private fun stopBack() { val r = backRun; backRun = null; r?.cancel() }

    /** Whole again at once, whatever a back swipe left. */
    private fun backWhole() { stopBack(); if (backAt != 0f || sheet?.scaleX != 1f) backTo(0f) }

    /** How the sheet follows a back swipe (Android 14 and later): it narrows; back going ahead closes it from
     *  there ([leave] keeps the look as it slides away), let go short it eases back to whole. Nothing moves while it
     *  leaves, or with animations off. */
    fun backPreview(): BackPreview = object : BackPreview {
        override val surface: Any get() = this@SheetFrame
        override val keepsOnCommit = true
        private val live = Motion.on()

        override fun progress(progress: Float) {
            if (!live || leaving) return
            stopBack()
            backTo(progress)
        }

        override fun cancel() {
            if (!live || leaving) return backWhole()
            stopBack()
            val from = backAt
            backRun = Motion.run(Motion.SHORT, Motion.standard, { t -> backTo(from * (1 - t)) },
                { if (backRun != null) { backRun = null; backTo(0f) } })
            if (backRun == null) backTo(0f)
        }

        override fun reset() = backWhole()
    }

    /** Up from below its resting place on the spatial spring, the scrim darkening with it; at rest at once when
     *  instant (the same sheet drawn again) or animations are off. Starts at the first frame after layout, when
     *  the sheet's height is known (and the first frame's own work can't eat the start of the move). */
    fun enter(instant: Boolean = false) {
        stopEntering(); stop(); backWhole()
        if (instant || !Motion.on()) { place(0f); return }
        scrim.alpha = 0
        sheet?.translationY = 100_000f  // out of sight until it is measured
        val observer = viewTreeObserver
        val listener = object : ViewTreeObserver.OnPreDrawListener {
            override fun onPreDraw(): Boolean {
                stopEntering()
                if (!leaving) springTo(away(), 0f, 0f)
                return true
            }
        }
        observer.addOnPreDrawListener(listener)
        entering = {
            if (observer.isAlive) observer.removeOnPreDrawListener(listener)
            else viewTreeObserver.removeOnPreDrawListener(listener)
        }
    }

    private fun springTo(from: Float, to: Float, velocity: Float) {
        stop()
        val mine = turn
        place(from)
        spring = Motion.spring(from, to, velocity, update = { if (mine == turn) place(it) },
            done = { if (mine == turn) spring = null })
    }

    /** A drag takes hold: whatever was moving stops where it is. Where the sheet is now. */
    fun grab(): Float { stopEntering(); stop(); backWhole(); return sheet?.translationY ?: 0f }

    /** Dragged to offset px below its rest. */
    fun drag(offset: Float) { if (!leaving) place(offset.coerceAtLeast(0f)) }

    /** Let go at offset, moving at velocity px/s: true when it should close (the caller closes it), else it
     *  springs back to rest from that speed. */
    fun release(offset: Float, velocity: Float, fling: Float): Boolean {
        if (leaving) return false
        if (sheetCloses(offset, away(), velocity, fling)) return true
        springTo(offset, 0f, velocity)
        return false
    }

    /** Back to rest from offset (a drag taken away by the system, not let go): never closes. */
    fun settle(offset: Float) { if (!leaving) springTo(offset, 0f, 0f) }

    /** Down and out of sight (200 ms, gathering speed), the scrim lightening with it, then gone: done once. Again
     *  while leaving does nothing. At once when it was never drawn, is off the screen, or animations are off. */
    fun leave(done: () -> Unit) {
        if (leaving) return
        leaving = true
        dimming?.cancel(); dimming = null
        if (touching) {  // let go of whatever is held: a pressed row un-presses, a drag lets its tracker go
            touching = false
            val now = android.os.SystemClock.uptimeMillis()
            MotionEvent.obtain(now, now, MotionEvent.ACTION_CANCEL, 0f, 0f, 0).also { super.dispatchTouchEvent(it); it.recycle() }
        }
        stopEntering(); stop()
        val r = backRun; backRun = null; r?.cancel()  // narrowed by a back swipe: it slides away as it is
        var called = false
        val finish = { if (!called) { called = true; done() } }
        val s = sheet
        if (s == null || !isAttachedToWindow || s.height == 0) return finish()
        val mine = turn
        val from = s.translationY.coerceAtMost(away())
        val to = away()
        run = Motion.run(Motion.MEDIUM, Motion.emphasizedAccelerate, { t -> if (mine == turn) place(from + (to - from) * t) },
            { if (mine == turn) { run = null; finish() } })  // not when stopped by leaving the screen: nothing to remove
    }

    /** While leaving, every touch is swallowed: nothing in the sheet, nor the scrim, acts during the leave. */
    override fun dispatchTouchEvent(event: MotionEvent): Boolean {
        if (leaving) return true
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> touching = true
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> touching = false
        }
        return super.dispatchTouchEvent(event)
    }

    override fun onDetachedFromWindow() {
        // The activity gone, or the sheet removed: no frame callback keeps it (and the activity) alive
        stopEntering(); stop()
        dimming?.cancel(); dimming = null
        stopBack()
        super.onDetachedFromWindow()
    }
}
