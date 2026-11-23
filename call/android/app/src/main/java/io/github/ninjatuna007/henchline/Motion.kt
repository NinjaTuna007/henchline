package io.github.ninjatuna007.henchline

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.ValueAnimator
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Path
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.Drawable
import android.graphics.drawable.DrawableWrapper
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.view.View
import android.view.ViewGroup
import android.view.animation.PathInterpolator
import android.widget.Button
import android.widget.CompoundButton
import android.widget.EditText
import android.widget.ImageButton
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

    /** A chat line coming in (see [entranceOf]): from its smaller size about its bottom corner on its own side, and
     *  transparent, to whole. Laid out first, so the corner is known. Nothing (already whole) when off. */
    fun enter(view: View, how: Entrance?) {
        if (how == null || !on()) return
        if (!view.isAttachedToWindow) return
        view.scaleX = how.from; view.scaleY = how.from; view.alpha = 0f
        // Removed from the observer it was added to: a row taken away before the next frame (the chat redrawn)
        // has a fresh observer of its own, and the window's would keep calling this every frame
        val observer = view.viewTreeObserver
        observer.addOnPreDrawListener(object : android.view.ViewTreeObserver.OnPreDrawListener {
            override fun onPreDraw(): Boolean {
                if (observer.isAlive) observer.removeOnPreDrawListener(this)
                else view.viewTreeObserver.removeOnPreDrawListener(this)
                if (!view.isAttachedToWindow) { view.scaleX = 1f; view.scaleY = 1f; view.alpha = 1f; return true }
                view.pivotX = if (how.atEnd) view.width.toFloat() else 0f
                view.pivotY = view.height.toFloat()
                view.animate().scaleX(1f).scaleY(1f).alpha(1f).setDuration(how.duration)
                    .setInterpolator(emphasizedDecelerate).start()
                return true
            }
        })
    }

    /** A row going for good: it fades and its height closes up, so what is under it moves up rather than jumps;
     *  then done (which removes it). At once when off. */
    fun collapse(view: View, duration: Long = MEDIUM, done: () -> Unit) {
        val from = view.height
        if (!on() || from == 0 || !view.isShown) { done(); return }
        val params = view.layoutParams
        view.animate().alpha(0f).setDuration(duration).setInterpolator(emphasizedAccelerate).start()
        run(duration, emphasized, { t ->
            params.height = (from * (1 - t)).toInt().coerceAtLeast(0)
            view.layoutParams = params
        }, done)
    }

    /** The touch ripple's colour on the app's dark surfaces: 12% white. */
    const val RIPPLE = 0x1FFFFFFF

    /** A bounded ripple on a view that is tapped: over its own background, whose shape bounds it, or over its
     *  rectangle when it has none (a circle, for an icon button). One that already ripples takes this colour. */
    fun ripple(view: View) {
        view.setTag(R.id.rippled, true)
        val colour = ColorStateList.valueOf(RIPPLE)
        val bg = view.background
        view.background = when {
            bg is RippleDrawable -> (bg.mutate() as RippleDrawable).apply { setColor(colour) }
            bg == null || bg is ColorDrawable && Color.alpha(bg.color) == 0 -> RippleDrawable(colour, null,
                if (view is ImageButton) GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(Color.WHITE) }
                else ColorDrawable(Color.WHITE))
            else -> RippleDrawable(colour, bg, maskOf(bg))
        }
    }

    /** The shape a ripple fills over this background: a solid copy of it, so an outline (a clear fill, maybe inset as
     *  the chips are) ripples over its whole shape, not just its 1 dp line. Null: the background itself bounds it. */
    private fun maskOf(bg: Drawable): Drawable? {
        val copy = bg.constantState?.newDrawable()?.mutate() ?: return null
        var inner: Drawable? = copy
        while (inner is DrawableWrapper) inner = inner.drawable  // an inset around the shape: the same inset
        return (inner as? GradientDrawable)?.let { it.setColor(Color.WHITE); copy }
    }

    /** A filled button: pressed, it shrinks to 0.96 and its corners square off a little, springing back on release,
     *  with the ripple over it. Its background must be a [GradientDrawable] (else it only ripples). */
    fun press(view: View) {
        val shape = view.background as? GradientDrawable ?: return ripple(view)
        view.setTag(R.id.rippled, true)
        val round = shape.cornerRadius
        var corner: SpringAnimation? = null
        val scaleX = SpringAnimation(view, DynamicAnimation.SCALE_X, 1f).with(FAST)
        val scaleY = SpringAnimation(view, DynamicAnimation.SCALE_Y, 1f).with(FAST)
        view.background = RippleDrawable(ColorStateList.valueOf(RIPPLE), Pressable(shape) { down ->
            val size = if (down) PRESSED_SCALE else 1f
            if (!on()) { view.scaleX = size; view.scaleY = size }
            else { scaleX.animateToFinalPosition(size); scaleY.animateToFinalPosition(size) }
            corner?.cancel()
            corner = spring(shape.cornerRadius, if (down) pressedCorner(round) else round, spring = FAST,
                update = { shape.cornerRadius = it })
        }, null)
    }

    /** [ripple] on every view under root that acts when tapped and has no ripple yet, [press] on filled buttons;
     *  not on text boxes or switches (they have their own). Safe to run again over the same views. */
    fun rippleAll(root: View) {
        if (root is ViewGroup) for (i in 0 until root.childCount) rippleAll(root.getChildAt(i))
        if (!root.hasOnClickListeners() || root is EditText || root is CompoundButton || root.foreground != null ||
            root.getTag(R.id.rippled) == true) return
        val bg = root.background
        if (root is Button && bg is GradientDrawable && Color.alpha(bg.color?.defaultColor ?: 0) == 255) press(root)
        else ripple(root)
    }

    /** A drawable that tells when the view it is on is pressed or let go (the pressed state the view gives its
     *  background), drawing the shape it wraps. */
    private class Pressable(shape: Drawable, private val pressed: (Boolean) -> Unit) : DrawableWrapper(shape) {
        private var down = false
        override fun isStateful() = true
        override fun onStateChange(state: IntArray): Boolean {
            val now = isPressedIn(state)
            if (now != down) { down = now; pressed(now) }
            return super.onStateChange(state)
        }
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

/** How a chat line comes in: from this fraction of its size, over duration, growing from its bottom corner at the
 *  end (yours, on the right) or the start (an agent's). */
data class Entrance(val from: Float, val duration: Long, val atEnd: Boolean)

/** Whether and how a chat line comes in. Only a line sent or arriving while its chat is open (live) and on screen
 *  (shown) moves: a chat's history, a rebuilt list, a note in the middle and a line of another chat just appear,
 *  as does everything when the phone has animations off. Yours grows from bottom-right (0.8, 250 ms), an agent's
 *  from bottom-left (0.9, 300 ms). */
fun entranceOf(live: Boolean, mine: Boolean, note: Boolean, shown: Boolean, animating: Boolean): Entrance? = when {
    !animating || !live || note || !shown -> null
    mine -> Entrance(0.8f, 250L, atEnd = true)
    else -> Entrance(0.9f, Motion.LONG, atEnd = false)
}

/** Whether the chat counts as at its end, to stay there as the box or the keyboard changes: it is there, or on its
 *  way there (a move to the end asked and not yet made, or the glide still running until glideEnds). */
fun followsEnd(canScrollDown: Boolean, downAsked: Boolean, now: Long, glideEnds: Long) =
    !canScrollDown || downAsked || now < glideEnds

/** A filled button held down: this fraction of its size. */
const val PRESSED_SCALE = 0.96f

/** A filled button's corner radius while held down: a little squarer (a pill's 24 dp becomes about 17). */
fun pressedCorner(radius: Float) = radius * 0.7f

/** Whether a view's drawable state is pressed (and enabled: a disabled button doesn't press). */
fun isPressedIn(state: IntArray) =
    android.R.attr.state_pressed in state && android.R.attr.state_enabled in state

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
