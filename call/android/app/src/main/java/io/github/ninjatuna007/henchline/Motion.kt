package io.github.ninjatuna007.henchline

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.ValueAnimator
import android.graphics.Matrix
import android.graphics.Path
import android.view.View
import android.view.animation.PathInterpolator
import android.widget.ImageView
import androidx.dynamicanimation.animation.DynamicAnimation
import androidx.dynamicanimation.animation.FloatValueHolder
import androidx.dynamicanimation.animation.SpringAnimation
import androidx.dynamicanimation.animation.SpringForce

/** The app's one way of moving: the same easings, durations and springs everywhere, so a sheet, a picture and a
 *  bubble move alike. Everything here is instant when the phone has animations turned off ([on]). */
object Motion {
    /** Most moves on screen: quick out of the start, a long gentle settle. */
    val emphasized = PathInterpolator(Path().apply {
        moveTo(0f, 0f)
        cubicTo(0.05f, 0f, 0.133f, 0.06f, 0.167f, 0.4f)
        cubicTo(0.208f, 0.82f, 0.25f, 1f, 1f, 1f)
    })
    /** Something coming in: it arrives fast and slows into place. */
    val emphasizedDecelerate = PathInterpolator(0.05f, 0.7f, 0.1f, 1f)
    /** Something leaving for good: it gathers speed on the way out. */
    val emphasizedAccelerate = PathInterpolator(0.3f, 0f, 0.8f, 0.15f)
    /** Small changes that stay on screen (a colour, a fade). */
    val standard = PathInterpolator(0.2f, 0f, 0f, 1f)

    const val SHORT = 100L
    const val MEDIUM = 200L
    const val LONG = 300L
    const val LONGER = 400L
    const val LONGEST = 500L

    /** A spring: damping ratio (1 never overshoots) and stiffness. */
    class Spring(val damping: Float, val stiffness: Float)
    /** Things that move across the screen (a page, a picture going back). */
    val SPATIAL = Spring(0.8f, 380f)
    /** Small, quick moves (a button, a knob). */
    val FAST = Spring(0.6f, 800f)
    /** Opacity and colour: no overshoot. */
    val EFFECTS = Spring(1f, 1600f)

    /** The smallest visible change of a scale or a 0..1 fraction (for [spring]'s visible). */
    const val SMALL = DynamicAnimation.MIN_VISIBLE_CHANGE_SCALE

    /** Whether to animate at all: false when the phone's animator scale is off. */
    fun on(): Boolean = ValueAnimator.areAnimatorsEnabled()

    private fun SpringAnimation.with(spring: Spring) = apply {
        this.spring = SpringForce().setDampingRatio(spring.damping).setStiffness(spring.stiffness)
    }

    /** A view's property springing to target, starting at velocity (per second). Instant when animations are off:
     *  then it returns null, the value set and done called. */
    fun spring(view: View, property: DynamicAnimation.ViewProperty, target: Float, velocity: Float = 0f,
               spring: Spring = SPATIAL, done: (() -> Unit)? = null): SpringAnimation? {
        if (!on()) { property.setValue(view, target); done?.invoke(); return null }
        return SpringAnimation(view, property).with(spring).apply {
            this.spring.finalPosition = target
            setStartVelocity(velocity)
            if (done != null) addEndListener { _, _, _, _ -> done() }
            start()
        }
    }

    /** A plain number springing from start to target at velocity, each frame given to update. The end listener
     *  (canceled or not) gets done: cancel() is how a new touch stops it where it is. Null when animations are off
     *  (update and done called at once with target). visible is the smallest change worth a frame: the default
     *  suits pixels; a zoom or a 0..1 fraction needs [SMALL], else it stops (and jumps) well short of target. */
    fun spring(start: Float, target: Float, velocity: Float = 0f, spring: Spring = SPATIAL, visible: Float = 1f,
               update: (Float) -> Unit, done: ((canceled: Boolean) -> Unit)? = null): SpringAnimation? {
        if (!on() || start == target && velocity == 0f) { update(target); done?.invoke(false); return null }
        return SpringAnimation(FloatValueHolder(start)).with(spring).apply {
            this.spring.finalPosition = target
            setStartVelocity(velocity)
            minimumVisibleChange = visible
            addUpdateListener { _, value, _ -> update(value) }
            addEndListener { _, canceled, _, _ -> done?.invoke(canceled) }
            start()
        }
    }

    /** An eased run of 0..1, each frame given to update; done once it ends or is cancelled. Instant (1, then done)
     *  when animations are off, returning null. */
    fun run(duration: Long, easing: android.animation.TimeInterpolator, update: (Float) -> Unit,
            done: (() -> Unit)? = null): ValueAnimator? {
        if (!on()) { update(1f); done?.invoke(); return null }
        return ValueAnimator.ofFloat(0f, 1f).apply {
            this.duration = duration
            interpolator = easing
            addUpdateListener { update(it.animatedValue as Float) }
            if (done != null) addListener(object : AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: Animator) = done()
            })
            start()
        }
    }

    /** An image's matrix moving from one to another (zoom and pan together), fix applied to every frame (to keep
     *  it in bounds). With two scale-and-move matrices, a point that both put at the same place stays there. */
    fun matrixTo(view: ImageView, from: Matrix, to: Matrix, duration: Long = LONG,
                 easing: android.animation.TimeInterpolator = emphasized, fix: ((Matrix) -> Unit)? = null,
                 done: (() -> Unit)? = null): ValueAnimator? {
        val a = FloatArray(9).also { from.getValues(it) }
        val b = FloatArray(9).also { to.getValues(it) }
        val values = FloatArray(9)
        val frame = Matrix()
        return run(duration, easing, { t ->
            lerpValues(a, b, t, values)
            frame.setValues(values)
            fix?.invoke(frame)
            view.imageMatrix = frame
        }, done)
    }

    /** A view coming into being: from 0.8 of its size about (pivotX, pivotY) and transparent, to whole. */
    fun appear(view: View, pivotX: Float = view.width / 2f, pivotY: Float = view.height / 2f,
               duration: Long = LONG) {
        view.pivotX = pivotX; view.pivotY = pivotY
        if (!on()) { view.scaleX = 1f; view.scaleY = 1f; view.alpha = 1f; return }
        view.scaleX = 0.8f; view.scaleY = 0.8f; view.alpha = 0f
        view.animate().scaleX(1f).scaleY(1f).alpha(1f).setDuration(duration)
            .setInterpolator(emphasizedDecelerate).start()
    }
}

/** from..to at t, value by value (into out): the frames between two matrices. */
fun lerpValues(from: FloatArray, to: FloatArray, t: Float, out: FloatArray) {
    for (i in out.indices) out[i] = from[i] + (to[i] - from[i]) * t
}

/** A value held within low..high, but pulled past it with resistance, as a rubber band: within, it is the value
 *  itself; past high it creeps toward hardHigh (never reaching it), and past low toward hardLow. */
fun resist(value: Float, low: Float, high: Float, hardLow: Float, hardHigh: Float): Float = when {
    value > high -> { val room = hardHigh - high; val over = value - high; high + room * over / (over + room) }
    value < low -> { val room = low - hardLow; val over = low - value; low - room * over / (over + room) }
    else -> value
}

/** The pull that [resist] turns into shown: where a gesture resumes from a value that is already past the limits. */
fun unresist(shown: Float, low: Float, high: Float, hardLow: Float, hardHigh: Float): Float = when {
    shown > high -> { val room = hardHigh - high; val d = minOf(shown - high, room * 0.999f); high + room * d / (room - d) }
    shown < low -> { val room = low - hardLow; val d = minOf(low - shown, room * 0.999f); low - room * d / (room - d) }
    else -> shown
}

/** How far to move a picture of span low..high along a side of size so it leaves no empty edge it could fill,
 *  and sits centred along a side it doesn't fill. */
fun keepAlong(low: Float, high: Float, size: Float): Float = when {
    high - low <= size -> (size - (high - low)) / 2 - low
    low > 0 -> -low
    high < size -> size - high
    else -> 0f
}

/** Where a picture of span length (at its left or top, start) may be flung to: (min, max) of its start, a single
 *  place (centred) when it doesn't fill size. */
fun panRange(length: Float, size: Float): Pair<Float, Float> =
    if (length <= size) ((size - length) / 2).let { it to it } else (size - length) to 0f
