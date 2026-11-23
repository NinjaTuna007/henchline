package io.github.ninjatuna007.henchline

import android.animation.ValueAnimator
import android.graphics.Canvas
import android.graphics.ColorFilter
import android.graphics.Paint
import android.graphics.Picture
import android.graphics.PixelFormat
import android.graphics.Rect
import android.graphics.RectF
import android.graphics.drawable.Drawable
import android.view.View
import android.view.ViewGroup
import android.view.animation.LinearInterpolator
import androidx.dynamicanimation.animation.FloatValueHolder
import androidx.dynamicanimation.animation.SpringAnimation
import androidx.dynamicanimation.animation.SpringForce

/** Moving between the list's tabs, and between a chat's Chat and Plan: along a shared axis (what was there slides
 *  30 dp away and fades out, what comes slides in 30 dp from the side it lies on), with a pill behind the chosen tab
 *  that springs across. And the call bar coming and going. The timings and frames as plain numbers. */
object TabMotion {
    const val AXIS_DP = 30
    const val AXIS_MS = Motion.LONG
    /** The old content is gone by this share of the move; the new one fades in over the rest (they never cross). */
    const val FADE_SPLIT = 0.35f
    /** The call bar coming or going, and the status bar's colour changing with it. */
    const val CALL_BAR_MS = Motion.LONGER

    /** Which way the move goes from tab from to tab to (their places, left to right): 1 the new one lies to the
     *  right (it comes in from the right, the old leaves left), -1 to the left, 0 no move (the same, or unknown). */
    fun direction(from: Int, to: Int): Int = when {
        from < 0 || to < 0 || from == to -> 0
        to > from -> 1
        else -> -1
    }

    /** The old content's opacity at t (0..1, linear time) of the move. */
    fun outAlpha(t: Float) = (1f - t / FADE_SPLIT).coerceIn(0f, 1f)

    /** The new content's opacity at t: nothing until the old has gone, then in. */
    fun inAlpha(t: Float) = ((t - FADE_SPLIT) / (1f - FADE_SPLIT)).coerceIn(0f, 1f)

    /** Where the old content is (px across) at eased progress e: away from the side the new one comes from. */
    fun outOffset(e: Float, dir: Int, distance: Float) = -dir * distance * e

    /** Where the new content is at eased progress e: distance off on its own side, then home. */
    fun inOffset(e: Float, dir: Int, distance: Float) = dir * distance * (1f - e)

    /** The call bar's height (px) at eased progress e of its move from one height to another. */
    fun barHeightAt(e: Float, from: Int, to: Int): Int = ComputerCards.heightAt(e, from, to)

    /** The pill's left and right edges at position (a tab's place; between two while it moves) among slots equal
     *  tabs spanning left..right, inset from each tab's edges. */
    fun pillSpan(position: Float, slots: Int, left: Float, right: Float, inset: Float): Pair<Float, Float> {
        val slot = (right - left) / slots.coerceAtLeast(1)
        val at = position.coerceIn(0f, (slots - 1).coerceAtLeast(0).toFloat())
        return (left + at * slot + inset) to (left + (at + 1) * slot - inset)
    }

    /** What a screen reader says for a tab (its selected state is said on its own). */
    fun describeTab(label: String, index: Int, count: Int) = "$label, tab ${index + 1} of $count"

    /** The status bar's colour at t of its change, from one opaque colour to another. */
    fun statusAt(from: Int, to: Int, t: Float) = ComputerCards.mix(from, to, t.coerceIn(0f, 1f))
}

/** A track with a pill on it, behind a row of equal tabs: the pill sits behind the tab at [position] (fractional
 *  while it moves; [PillSpring] moves it). trackColour 0 draws no track (the view's own colour shows). */
class PillTrack(private val slots: Int, trackColour: Int, pillColour: Int, private val padding: Float,
                private val insetX: Float, private val insetY: Float, private val trackRadius: Float,
                private val pillRadius: Float) : Drawable() {
    private val track = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = trackColour }
    private val pill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = pillColour }
    private val rect = RectF()
    private val drawTrack = trackColour != 0

    var position = 0f
        set(value) { if (field != value) { field = value; invalidateSelf() } }

    override fun draw(canvas: Canvas) {
        val b = bounds
        if (drawTrack) {
            rect.set(b)
            canvas.drawRoundRect(rect, trackRadius, trackRadius, track)
        }
        val (left, right) = TabMotion.pillSpan(position, slots, b.left + padding, b.right - padding, insetX)
        rect.set(left, b.top + padding + insetY, right, b.bottom - padding - insetY)
        if (rect.width() > 0 && rect.height() > 0) canvas.drawRoundRect(rect, pillRadius, pillRadius, pill)
    }

    override fun setAlpha(alpha: Int) {}
    override fun setColorFilter(colorFilter: ColorFilter?) {}
    @Deprecated("Deprecated in Java")
    override fun getOpacity() = PixelFormat.TRANSLUCENT
}

/** Moves a [PillTrack]'s pill from tab to tab on the spatial spring, keeping its speed when a new tab is picked
 *  mid-move. At once when animations are off or when asked not to animate. */
class PillSpring(private val track: PillTrack) {
    private var anim: SpringAnimation? = null

    fun to(target: Float, animate: Boolean) {
        val running = anim?.takeIf { it.isRunning }
        if (!animate || !Motion.on()) {
            // A redraw that keeps the same tab doesn't cut short the spring already on its way there
            if (running != null && running.spring.finalPosition == target) return
            anim?.cancel()
            track.position = target
            return
        }
        val s = anim ?: SpringAnimation(FloatValueHolder(track.position)).apply {
            spring = SpringForce().setDampingRatio(Motion.SPATIAL.damping).setStiffness(Motion.SPATIAL.stiffness)
            minimumVisibleChange = Motion.SMALL
            addUpdateListener { _, value, _ -> track.position = value }
        }.also { anim = it }
        if (!s.isRunning) s.setStartValue(track.position)
        s.animateToFinalPosition(target)
    }

    /** Stopped where it is (the screen going). */
    fun cancel() { anim?.cancel() }
}

/** Switches what a view shows along the shared axis: a picture of it as it was slides and fades away (drawn over
 *  its parent), then the view, changed, slides in from dir's side and fades in. A new switch mid-move ends the last
 *  at once (its picture gone, the view home). At once when animations are off, the view not on screen, or dir 0. */
class AxisSwitch {
    private var running: ValueAnimator? = null
    private var ghost: Ghost? = null
    private var ghostHost: ViewGroup? = null
    private var moving: View? = null
    private var layerWas = View.LAYER_TYPE_NONE
    private var turn = 0

    val busy get() = moving != null

    fun switch(content: View, dir: Int, change: () -> Unit) {
        stop()
        val parent = content.parent as? ViewGroup
        if (dir == 0 || !Motion.on() || parent == null || !content.isLaidOut || content.width == 0 || !content.isShown) {
            change(); return
        }
        val picture = snapshot(content)
        change()
        val distance = TabMotion.AXIS_DP * content.resources.displayMetrics.density
        val g = picture?.let { Ghost(it, Rect(content.left, content.top, content.right, content.bottom)) }
        if (g != null) {
            g.setBounds(0, 0, parent.width, parent.height)
            parent.overlay.add(g)
        }
        ghost = g; ghostHost = parent; moving = content
        layerWas = content.layerType
        content.setLayerType(View.LAYER_TYPE_HARDWARE, null)  // one layer faded, not every row each frame
        fun frame(t: Float) {
            val e = Motion.emphasized.getInterpolation(t)
            content.translationX = TabMotion.inOffset(e, dir, distance)
            // No picture of the old (it couldn't be drawn): the new one fades in over the whole move instead
            content.alpha = if (g != null) TabMotion.inAlpha(t) else t
            g?.let { it.dx = TabMotion.outOffset(e, dir, distance); it.shade = (TabMotion.outAlpha(t) * 255).toInt(); it.invalidateSelf() }
        }
        frame(0f)
        val mine = ++turn
        running = Motion.run(TabMotion.AXIS_MS, LinearInterpolator(), ::frame) { if (mine == turn) finish() }
    }

    /** Whatever moves ends now, the view home and whole. */
    fun stop() {
        turn++
        running?.cancel()
        finish()
    }

    private fun finish() {
        running = null
        ghost?.let { g -> ghostHost?.overlay?.remove(g) }
        ghost = null; ghostHost = null
        moving?.let { it.translationX = 0f; it.alpha = 1f; it.setLayerType(layerWas, null) }
        moving = null
    }
}

/** The view as it is drawn now, kept as drawing commands; null if it can't be drawn that way (a picture held
 *  only on the graphics card). */
internal fun snapshot(view: View): Picture? = try {
    Picture().also { p ->
        val canvas = p.beginRecording(view.width, view.height)
        try { view.draw(canvas) } finally { p.endRecording() }
    }
} catch (_: RuntimeException) { null }

/** The old content, at at in its parent, moved dx across and faded to shade (0..255), clipped to its place. */
internal class Ghost(private val picture: Picture, private val at: Rect) : Drawable() {
    var dx = 0f
    var shade = 255

    override fun draw(canvas: Canvas) {
        if (shade <= 0) return
        val saved = canvas.save()
        canvas.clipRect(at)
        canvas.translate(at.left + dx, at.top.toFloat())
        if (shade < 255) canvas.saveLayerAlpha(0f, 0f, at.width().toFloat(), at.height().toFloat(), shade)
        canvas.drawPicture(picture)
        canvas.restoreToCount(saved)
    }

    override fun setAlpha(alpha: Int) { shade = alpha }
    override fun setColorFilter(colorFilter: ColorFilter?) {}
    @Deprecated("Deprecated in Java")
    override fun getOpacity() = PixelFormat.TRANSLUCENT
}

/** The call bar coming (its height opens from nothing to its own as it fades in) or going (it closes and fades,
 *  then is gone), as a computer card folds: what is below makes room or closes up, while the bottom of the screen
 *  (the tabs, the chat's box) stays put. A change mid-way turns back from the height it has reached. [stop] leaves
 *  it as it is meant to end: its own height, whole, shown or gone. At once when off. */
class BarSlide {
    private var running: ValueAnimator? = null
    private var bar: View? = null
    private var natural = 0  // the bar's own height (its layout's), put back after every move
    private var target = false  // shown or gone, at the end of the move under way
    private var turn = 0

    /** Whether the bar is shown, or on its way to being shown. */
    fun showing(bar: View) = if (running != null && this.bar === bar) target else bar.visibility == View.VISIBLE

    /** The bar shown (show) or gone, its height moving when animate. */
    fun change(bar: View, show: Boolean, animate: Boolean) {
        if (running != null && this.bar === bar && target == show) return  // already on its way there
        val params = bar.layoutParams
        if (running == null && params.height > 0) natural = params.height
        // Turned back mid-way: from the height it has reached, else from closed or open
        val from = if (running != null && this.bar === bar) params.height.coerceAtLeast(0)
            else if (bar.visibility == View.VISIBLE) natural else 0
        turn++; running?.cancel(); running = null
        this.bar = bar; target = show
        val parent = bar.parent as? View
        if (!animate || !Motion.on() || natural <= 0 || parent == null || !parent.isLaidOut) { finish(); return }
        val to = if (show) natural else 0
        bar.visibility = View.VISIBLE
        fun frame(t: Float) {
            val h = TabMotion.barHeightAt(Motion.emphasized.getInterpolation(t), from, to)
            params.height = h
            bar.layoutParams = params  // laid out again: what is below moves with it
            bar.alpha = h.toFloat() / natural
        }
        frame(0f)
        val mine = ++turn
        running = Motion.run(TabMotion.CALL_BAR_MS, LinearInterpolator(), ::frame) { if (mine == turn) finish() }
    }

    /** Whatever moves ends now, the bar as it was meant to end. */
    fun stop() {
        turn++
        running?.cancel()
        finish()
    }

    private fun finish() {
        running = null
        val b = bar ?: return
        if (natural > 0) { val params = b.layoutParams; params.height = natural; b.layoutParams = params }
        b.alpha = 1f
        b.visibility = if (target) View.VISIBLE else View.GONE
    }
}
