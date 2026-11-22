package io.github.ninjatuna007.henchline

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Matrix
import android.graphics.Outline
import android.graphics.RectF
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import android.view.ViewOutlineProvider
import android.widget.ImageView
import android.widget.OverScroller
import androidx.dynamicanimation.animation.SpringAnimation

/** A picture to look into, as a messaging app's viewer (the user's wish, 2026-11-16): it opens fitted to the
 *  screen; pinch to zoom (up to six times, a little further with resistance, springing back on release), drag
 *  around while zoomed (a flick keeps it going, giving a little at the edges), double tap to zoom in there or back
 *  out (an eased zoom toward the tap). A single tap is the screen's (it shows or hides the bar); not zoomed, its
 *  pager takes sideways and downward drags. A new touch stops whatever it was doing where it is. */
class ZoomImage(context: Context) : ImageView(context) {
    private val shown = Matrix()
    private var fitted = 0f  // the scale that fits the whole picture: zoom is over this
    private val values = FloatArray(9)
    private val zoom: Float get() {
        if (fitted <= 0f) return 1f
        shown.getValues(values)
        return values[Matrix.MSCALE_X] / fitted
    }
    var onTap: (() -> Unit)? = null
    val zoomed get() = zoom > 1.01f

    private var easing: android.animation.ValueAnimator? = null  // a double tap's zoom
    private var springing: SpringAnimation? = null  // a pinch going back to its limits
    private val scroller = OverScroller(context)
    private val edge = (24 * resources.displayMetrics.density).toInt()  // how far a flick may run past an edge
    private var pull = 1f  // the pinch's own zoom, before resistance past the limits
    private var focusX = 0f
    private var focusY = 0f
    private var pinched = false  // this touch had two fingers: its last finger's lift is no flick

    init {
        scaleType = ScaleType.MATRIX
    }

    override fun setImageBitmap(bitmap: Bitmap?) {
        super.setImageBitmap(bitmap)
        post { fit() }
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        fit()
    }

    /** Back to the whole picture: when it is swiped away. */
    fun reset() = fit()

    /** Whatever it was doing (a zoom, a spring, a flick) stops where it is. */
    private fun stop() {
        easing?.cancel(); easing = null
        springing?.cancel(); springing = null
        scroller.forceFinished(true)
        removeCallbacks(flinging)
    }

    /** The whole picture, centred: where it opens, and where a double tap goes back to. */
    private fun fitted(into: Matrix): Boolean {
        val picture = drawable ?: return false
        if (width == 0 || height == 0 || picture.intrinsicWidth <= 0 || picture.intrinsicHeight <= 0) return false
        fitted = minOf(width.toFloat() / picture.intrinsicWidth, height.toFloat() / picture.intrinsicHeight)
        into.reset()
        into.postScale(fitted, fitted)
        into.postTranslate((width - picture.intrinsicWidth * fitted) / 2, (height - picture.intrinsicHeight * fitted) / 2)
        return true
    }

    private fun fit() {
        stop()
        if (fitted(shown)) imageMatrix = shown
    }

    /** To zoom z (over the fitted size), the point at (x, y) staying put as far as the edges allow. */
    private fun zoomTo(z: Float, x: Float, y: Float) {
        val by = z / zoom
        shown.postScale(by, by, x, y)
        keepInView(shown)
        imageMatrix = shown
    }

    /** No empty edge where the picture could fill the screen; centred along a side it doesn't fill. */
    private fun keepInView(m: Matrix) {
        val box = bounds(m) ?: return
        m.postTranslate(keepAlong(box.left, box.right, width.toFloat()), keepAlong(box.top, box.bottom, height.toFloat()))
    }

    /** Where m puts the picture on the screen (one rect, reused every frame: read it before the next call). */
    private val box = RectF()
    private fun bounds(m: Matrix): RectF? {
        val picture = drawable ?: return null
        box.set(0f, 0f, picture.intrinsicWidth.toFloat(), picture.intrinsicHeight.toFloat())
        m.mapRect(box)
        return box
    }

    /** Past the limits (a pinch let go, or its spring back cut short by a touch): springs back to them. */
    private fun backToLimits() {
        val now = zoom
        val limit = now.coerceIn(MIN, MAX)
        if (limit == now || springing != null) return
        springing = Motion.spring(now, limit, visible = Motion.SMALL, update = { zoomTo(it, focusX, focusY) },
            done = { springing = null })
    }

    override fun onDetachedFromWindow() {
        stop()  // the viewer closed: nothing runs on after it
        super.onDetachedFromWindow()
    }

    /** A flick's run, frame by frame, until the scroller (and its little spring at an edge) is done. */
    private val flinging = object : Runnable {
        override fun run() {
            val box = bounds(shown) ?: return
            if (!scroller.computeScrollOffset()) return
            shown.postTranslate(scroller.currX - box.left, scroller.currY - box.top)
            imageMatrix = shown
            postOnAnimation(this)
        }
    }

    private fun fling(vx: Float, vy: Float) {
        val box = bounds(shown) ?: return
        val (minX, maxX) = panRange(box.width(), width.toFloat())
        val (minY, maxY) = panRange(box.height(), height.toFloat())
        scroller.fling(box.left.toInt(), box.top.toInt(), vx.toInt(), vy.toInt(),
            minX.toInt(), maxX.toInt(), minY.toInt(), maxY.toInt(),
            if (minX < maxX) edge else 0, if (minY < maxY) edge else 0)
        postOnAnimation(flinging)
    }

    private val pinch = ScaleGestureDetector(context, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
        override fun onScaleBegin(detector: ScaleGestureDetector): Boolean {
            stop()
            pull = unresist(zoom, MIN, MAX, HARD_MIN, HARD_MAX)
            return true
        }

        override fun onScale(detector: ScaleGestureDetector): Boolean {
            pull *= detector.scaleFactor
            focusX = detector.focusX; focusY = detector.focusY
            zoomTo(resist(pull, MIN, MAX, HARD_MIN, HARD_MAX), focusX, focusY)
            return true
        }

        override fun onScaleEnd(detector: ScaleGestureDetector) {
            backToLimits()
        }
    })

    private val touch = GestureDetector(context, object : GestureDetector.SimpleOnGestureListener() {
        override fun onDown(e: MotionEvent) = true

        override fun onScroll(e1: MotionEvent?, e2: MotionEvent, dx: Float, dy: Float): Boolean {
            if (zoom <= 1f) return false
            shown.postTranslate(-dx, -dy)
            keepInView(shown)
            imageMatrix = shown
            return true
        }

        override fun onFling(e1: MotionEvent?, e2: MotionEvent, vx: Float, vy: Float): Boolean {
            if (zoom <= 1f || pinched || springing != null || !Motion.on()) return false
            fling(vx, vy)
            return true
        }

        override fun onDoubleTap(e: MotionEvent): Boolean {
            stop()
            val target = Matrix()
            if (zoomed) { if (!fitted(target)) return true }
            else { target.set(shown); target.postScale(2.5f, 2.5f, e.x, e.y); keepInView(target) }
            easing = Motion.matrixTo(this@ZoomImage, Matrix(shown), target, Motion.LONG, Motion.emphasized,
                fix = { frame -> keepInView(frame); shown.set(frame) })
            return true
        }

        override fun onSingleTapConfirmed(e: MotionEvent): Boolean {
            onTap?.invoke()
            return true
        }
    })

    @android.annotation.SuppressLint("ClickableViewAccessibility")  // a picture: its gestures have no click to stand for
    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (event.actionMasked == MotionEvent.ACTION_DOWN) { stop(); pinched = false }
        pinch.onTouchEvent(event)
        if (pinch.isInProgress) pinched = true else touch.onTouchEvent(event)
        val lifted = event.actionMasked == MotionEvent.ACTION_UP || event.actionMasked == MotionEvent.ACTION_CANCEL
        if (lifted && !pinch.isInProgress) backToLimits()
        parent?.requestDisallowInterceptTouchEvent(zoom > 1f || event.pointerCount > 1)
        return true
    }

    private companion object {
        const val MIN = 1f  // fitted
        const val MAX = 6f
        const val HARD_MIN = 0.85f  // how far a pinch may pull past them, against resistance
        const val HARD_MAX = 7f
    }
}

/** A picture in the viewer or an album: an agent's (on the laptop, at url) or yours (its small copy, thumb). */
data class Pic(val name: String, val url: String?, val type: String, val thumb: String?)

/** The viewer's pictures side by side, as a messaging app's: drag sideways and the picture goes with your finger,
 *  the next one coming in beside it (from a little faded); let go past a quarter (or with a flick) and it springs
 *  onto that one at the speed it was let go, else it springs back. Drag down and the picture follows, shrinking a
 *  little, the dark behind it fading: let go far enough and it closes, else it springs back.
 *  A zoomed picture keeps its fingers to itself (it pans). */
class PicturePager(context: Context, private val count: Int, private val load: (Int, (Bitmap?) -> Unit) -> Unit) :
    android.widget.FrameLayout(context) {
    var index = 0
        private set
    var onIndex: ((Int) -> Unit)? = null  // the picture shown changed
    var onDown: ((Float) -> Unit)? = null  // dragged down, 0..1 of the way to closing
    var onClose: (() -> Unit)? = null
    var onTap: (() -> Unit)? = null
    private val slots = List(3) { ZoomImage(context) }  // the one before, this one, the next one
    private val shownAt = IntArray(3) { -1 }  // which picture each slot holds
    private val slop = android.view.ViewConfiguration.get(context).scaledTouchSlop
    private var downX = 0f
    private var downY = 0f
    private var mode = 0  // 0 undecided, 1 sideways, 2 down
    private var tracker: android.view.VelocityTracker? = null
    private var settling: SpringAnimation? = null  // a slide to its place, finished at once by a new touch
    private var returning: SpringAnimation? = null  // a picture dragged down going back, put back by a new touch
    private var detached = false

    init {
        for (slot in slots) {
            addView(slot, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
            slot.onTap = { onTap?.invoke() }
        }
    }

    fun show(at: Int) {
        index = at.coerceIn(0, count - 1)
        place(0f)
        onIndex?.invoke(index)
    }

    private fun slot(offset: Int) = slots[(((index + offset) % 3) + 3) % 3]

    /** Each slot at its place for a drag of dx, holding its picture (loaded when it first gets it). */
    private fun place(dx: Float) {
        for (offset in -1..1) {
            val slot = slot(offset)
            val picture = index + offset
            slot.visibility = if (picture in 0 until count) VISIBLE else INVISIBLE
            slot.translationX = offset * width.toFloat() + dx
            slot.translationY = 0f
            slot.scaleX = 1f; slot.scaleY = 1f
            // the one coming in fades up from 0.85 as it slides in
            slot.alpha = if (offset == 0 || width == 0) 1f else 0.85f + 0.15f * minOf(1f, kotlin.math.abs(dx) / width)
            val i = slots.indexOf(slot)
            if (picture in 0 until count && shownAt[i] != picture) {
                shownAt[i] = picture
                slot.setImageDrawable(null)
                load(picture) { bitmap -> if (shownAt[i] == picture) slot.setImageBitmap(bitmap) }
            }
        }
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        place(0f)
    }

    override fun onInterceptTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                settling?.cancel()  // a new drag starts from where the last one settles
                returning?.cancel()
                downX = event.x; downY = event.y; mode = 0
                tracker?.recycle(); tracker = android.view.VelocityTracker.obtain()
                tracker?.addMovement(event)
            }
            MotionEvent.ACTION_MOVE -> {
                tracker?.addMovement(event)
                if (event.pointerCount > 1 || slot(0).zoomed) return false
                val dx = event.x - downX
                val dy = event.y - downY
                if (kotlin.math.abs(dx) > slop && kotlin.math.abs(dx) > kotlin.math.abs(dy) && count > 1) mode = 1
                else if (dy > slop && dy > kotlin.math.abs(dx)) mode = 2
                if (mode != 0) return true
            }
        }
        return false
    }

    @android.annotation.SuppressLint("ClickableViewAccessibility")  // drags only; taps go to the picture
    override fun onTouchEvent(event: MotionEvent): Boolean {
        tracker?.addMovement(event)
        val dx = event.x - downX
        val dy = event.y - downY
        when (event.actionMasked) {
            MotionEvent.ACTION_MOVE -> when (mode) {
                1 -> {  // at the ends it gives a little, then stops
                    val edge = (index == 0 && dx > 0) || (index == count - 1 && dx < 0)
                    place(if (edge) dx / 3 else dx)
                }
                2 -> {
                    val current = slot(0)
                    current.translationY = dy.coerceAtLeast(0f)
                    current.translationX = dx / 2
                    downward(current)
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                tracker?.computeCurrentVelocity(1000)
                val vx = tracker?.xVelocity ?: 0f
                val vy = tracker?.yVelocity ?: 0f
                tracker?.recycle(); tracker = null
                when (mode) {
                    1 -> settle(dx, vx)
                    2 -> if (dy > height * 0.18f || vy > 2500f) onClose?.invoke() else springBack(vy)
                }
                mode = 0
            }
        }
        return true
    }

    private fun settle(dx: Float, vx: Float) {
        val step = when {
            (dx < -width / 4f || vx < -1200f) && index < count - 1 -> 1
            (dx > width / 4f || vx > 1200f) && index > 0 -> -1
            else -> 0
        }
        val target = -step * width.toFloat()
        settling = Motion.spring(slot(0).translationX, target, vx, Motion.SPATIAL, update = { place(it) }, done = {
            settling = null  // ended or cut short by a touch: either way it is on its picture now
            if (detached) return@spring
            if (step != 0) { slot(0).reset(); index += step; onIndex?.invoke(index) }
            place(0f)
        })
    }

    override fun onDetachedFromWindow() {
        detached = true  // closed mid-move: what was moving stops, and tells the gone screen nothing
        settling?.cancel(); returning?.cancel()
        super.onDetachedFromWindow()
    }

    /** How far down the picture is, 0..1 of the way to closing: it shrinks toward 0.85 and tells the screen. */
    private fun downward(current: View) {
        val part = (current.translationY / (height * 0.4f)).coerceIn(0f, 1f)
        current.scaleX = 1f - 0.15f * part; current.scaleY = current.scaleX
        onDown?.invoke(part)
    }

    /** Not far enough to close: back in place on a spring that starts at the speed it was let go. */
    private fun springBack(vy: Float) {
        val current = slot(0)
        val fromX = current.translationX
        val fromY = current.translationY
        val velocity = if (fromY > 1f) (vy / fromY).coerceIn(-10f, 10f) else 0f  // of the way down, per second
        returning = Motion.spring(1f, 0f, velocity, Motion.SPATIAL, Motion.SMALL, update = { left ->
            current.translationX = fromX * left; current.translationY = fromY * left
            downward(current)
        }, done = {
            returning = null
            if (detached) return@spring
            current.translationX = 0f; current.translationY = 0f
            downward(current)
        })
    }

    /** The picture on screen now, for opening and closing animations. */
    fun current(): ZoomImage = slot(0)
}

/** The viewer's ways in and out, for the screen that shows it. */
object PictureMotion {
    /** The bar on top shown or hidden by a tap: a fade and an 8 dp slide; hidden, it takes no taps. */
    fun bar(bar: View, show: Boolean) {
        val up = -8 * bar.resources.displayMetrics.density
        bar.animate().cancel()
        if (show) bar.visibility = View.VISIBLE
        if (!Motion.on()) {
            bar.alpha = if (show) 1f else 0f
            bar.translationY = if (show) 0f else up
            if (!show) bar.visibility = View.INVISIBLE
            return
        }
        val move = bar.animate().alpha(if (show) 1f else 0f).translationY(if (show) 0f else up)
            .setStartDelay(0).setDuration(Motion.MEDIUM)
            .setInterpolator(if (show) Motion.emphasizedDecelerate else Motion.emphasizedAccelerate)
        if (!show) move.withEndAction { bar.visibility = View.INVISIBLE }
        move.start()
    }

    /** The scale that makes the whole screen (w x h) cover box: what a picture opening from box starts at. */
    private fun scaleFor(box: RectF, w: Float, h: Float) = maxOf(box.width() / w, box.height() / h).coerceIn(0.1f, 1f)

    /** In: the picture grows out of the thumbnail at box (on the screen), its clip opening from the thumbnail's
     *  rounded corners (corner px) to the whole screen, as the dark comes up behind it and then the bar. Without a
     *  box it grows from 0.9 of the screen. Laid out first (call it from a post). Returns the run, to cancel it if
     *  closed on the way in (null when instant). */
    fun open(pager: View, dark: View, bar: View, box: RectF?, corner: Float): android.animation.Animator? {
        val w = pager.width.toFloat()
        val h = pager.height.toFloat()
        if (!Motion.on() || w == 0f || h == 0f) {
            pager.scaleX = 1f; pager.scaleY = 1f; pager.translationX = 0f; pager.translationY = 0f
            dark.alpha = 1f; bar.alpha = 1f
            return null
        }
        val scale = if (box == null) 0.9f else scaleFor(box, w, h)
        val dx = if (box == null) 0f else box.centerX() - w / 2
        val dy = if (box == null) 0f else box.centerY() - h / 2
        // the thumbnail's own shape, in the pager's unscaled size: box over scale, centred
        val clipW = if (box == null) w else box.width() / scale
        val clipH = if (box == null) h else box.height() / scale
        var t = 0f
        pager.outlineProvider = object : ViewOutlineProvider() {
            override fun getOutline(view: View, outline: Outline) {
                val cw = clipW + (w - clipW) * t
                val ch = clipH + (h - clipH) * t
                val left = (w - cw) / 2
                val top = (h - ch) / 2
                outline.setRoundRect(left.toInt(), top.toInt(), (left + cw).toInt(), (top + ch).toInt(),
                    corner / scale * (1 - t))
            }
        }
        pager.clipToOutline = true
        bar.animate().alpha(1f).setStartDelay(Motion.MEDIUM).setDuration(Motion.MEDIUM)
            .setInterpolator(Motion.standard).start()
        return Motion.run(Motion.LONGER, Motion.emphasizedDecelerate, { p ->
            t = p
            val s = scale + (1 - scale) * p
            pager.scaleX = s; pager.scaleY = s
            pager.translationX = dx * (1 - p); pager.translationY = dy * (1 - p)
            dark.alpha = p
            pager.invalidateOutline()
        }) {
            pager.clipToOutline = false  // ended or cut short: the clip never stays
            pager.outlineProvider = ViewOutlineProvider.BACKGROUND
        }
    }

    /** Out: the picture shown (current) shrinks back into its thumbnail at box; swiped to another picture (no box),
     *  it shrinks to 0.9 and fades. The dark goes with it and the bar first; done when it is gone (or the run is
     *  cancelled). Starts from wherever things are, so it can follow a drag down or an unfinished open: cancel the
     *  open first, and its pager (frame) is brought back to whole along the way, so the picture still lands on box. */
    fun close(current: View, dark: View, bar: View, box: RectF?, frame: View? = null,
              done: () -> Unit): android.animation.Animator? {
        bar.animate().cancel()
        if (!Motion.on()) { done(); return null }
        val w = (current.parent as? View)?.width?.toFloat() ?: current.width.toFloat()
        val h = (current.parent as? View)?.height?.toFloat() ?: current.height.toFloat()
        val scale = if (box == null || w == 0f || h == 0f) 0.9f else scaleFor(box, w, h)
        val fromScale = current.scaleX
        val fromX = current.translationX
        val fromY = current.translationY
        val fromAlpha = current.alpha
        val toX = if (box == null) fromX else box.centerX() - w / 2
        val toY = if (box == null) fromY else box.centerY() - h / 2
        val toAlpha = if (box == null) 0f else fromAlpha
        val fromDark = dark.alpha
        val fromBar = bar.alpha
        val frameScale = frame?.scaleX ?: 1f
        val frameX = frame?.translationX ?: 0f
        val frameY = frame?.translationY ?: 0f
        return Motion.run(CLOSE, Motion.emphasizedAccelerate, { p ->
            val s = fromScale + (scale - fromScale) * p
            current.scaleX = s; current.scaleY = s
            current.translationX = fromX + (toX - fromX) * p
            current.translationY = fromY + (toY - fromY) * p
            current.alpha = fromAlpha + (toAlpha - fromAlpha) * p
            frame?.let { f ->
                val fs = frameScale + (1 - frameScale) * p
                f.scaleX = fs; f.scaleY = fs
                f.translationX = frameX * (1 - p); f.translationY = frameY * (1 - p)
            }
            dark.alpha = fromDark * (1 - p)
            bar.alpha = fromBar * maxOf(0f, 1 - p * CLOSE / Motion.SHORT)  // the bar gone within the first 100 ms
        }, done)
    }

    private const val CLOSE = 250L
}
