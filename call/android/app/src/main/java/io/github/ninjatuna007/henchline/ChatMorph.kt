package io.github.ninjatuna007.henchline

import android.animation.ValueAnimator
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorFilter
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Picture
import android.graphics.PixelFormat
import android.graphics.Rect
import android.graphics.RectF
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.Drawable
import android.graphics.drawable.DrawableWrapper
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.view.ViewGroup
import android.view.ViewTreeObserver
import android.view.animation.LinearInterpolator

/** A rectangle as four numbers (px), for the container transform's sums (kept off Android's own classes so they can
 *  be tested on the computer). */
data class Box(val left: Float, val top: Float, val right: Float, val bottom: Float) {
    val width get() = right - left
    val height get() = bottom - top
    val empty get() = width <= 0f || height <= 0f
}

/** The laptop's chat growing out of the list's row that was tapped, and shrinking back into it: the plain numbers. */
object ChatMorph {
    /** The whole grow or shrink. */
    const val MS = 450L
    /** Closing with no row to land on (scrolled away, in a folded card): the chat fades and shrinks, as another
     *  computer's chat does ([RemoteChatMotion]). */
    const val FALLBACK_MS = RemoteChatMotion.CLOSE_MS
    const val FALLBACK_SCALE = RemoteChatMotion.CLOSE_SCALE
    /** How dark the list goes behind the chat as it opens (the sheets' scrim). */
    const val SCRIM = 0.32f
    /** A row less tall than this (px) on screen, cut by the list's edges, isn't one to grow from or land on. */
    const val MIN_VISIBLE_PX = 8f

    /** from..to at eased progress e, edge by edge. */
    fun lerp(from: Box, to: Box, e: Float) = Box(
        from.left + (to.left - from.left) * e, from.top + (to.top - from.top) * e,
        from.right + (to.right - from.right) * e, from.bottom + (to.bottom - from.bottom) * e)

    /** The part of a row (box) the list shows (viewport: the scrolling list's place), or null when too little of it
     *  is on screen to grow from or land on. */
    fun visiblePart(row: Box, viewport: Box): Box? {
        val b = Box(maxOf(row.left, viewport.left), maxOf(row.top, viewport.top),
            minOf(row.right, viewport.right), minOf(row.bottom, viewport.bottom))
        return if (b.empty || b.height < MIN_VISIBLE_PX) null else b
    }

    /** Where a view drawn scaled about its pivot and moved sits in its parent: left/top its place in the parent,
     *  width/height its own size. */
    fun scaledBox(left: Float, top: Float, width: Float, height: Float, scaleX: Float, scaleY: Float,
                  pivotX: Float, pivotY: Float, dx: Float, dy: Float): Box {
        val l = left + pivotX - pivotX * scaleX + dx
        val t = top + pivotY - pivotY * scaleY + dy
        return Box(l, t, l + width * scaleX, t + height * scaleY)
    }

    /** box shrunk to scale about its centre (the fallback close). */
    fun shrunk(box: Box, scale: Float): Box {
        val cx = (box.left + box.right) / 2; val cy = (box.top + box.bottom) / 2
        val hw = box.width * scale / 2; val hh = box.height * scale / 2
        return Box(cx - hw, cy - hh, cx + hw, cy + hh)
    }

    /** Whether a chat opens out of its row: animations on and the row's place on screen known. */
    fun grows(on: Boolean, row: Box?) = on && row != null && !row.empty

    /** What was there before (the row going in, the chat going out): gone by a third of the way (linear time t). */
    fun outgoing(t: Float) = TabMotion.outAlpha(t)

    /** Closing: the container's own fill, solid while the chat fades from it, then fading as it settles on the row
     *  (which shows through it). */
    fun closingFill(t: Float) = 1f - TabMotion.inAlpha(t)

    /** The darkness over the list at eased progress e: opening from none to [SCRIM]; closing from start to none. */
    fun openingScrim(e: Float) = SCRIM * e.coerceIn(0f, 1f)
    fun closingScrim(start: Float, e: Float) = start.coerceIn(0f, 1f) * (1f - e.coerceIn(0f, 1f))

    /** How dark the list starts closing: as dark as a back swipe left the space behind the chat, or [SCRIM] (the
     *  same as the open ended) when no swipe moved it. */
    fun closeScrimFrom(swipeDarkness: Float?) = swipeDarkness?.coerceIn(0f, 1f) ?: SCRIM

    /** The scale a picture of width pictureWidth is drawn at to fill box's width. */
    fun fitWidth(box: Box, pictureWidth: Float) = if (pictureWidth <= 0f) 1f else box.width / pictureWidth
}

/** Where a back swipe left the chat screen when back went ahead: its place, corner (px) and the darkness behind it. */
class SwipeLook(val box: Box, val corner: Float, val darkness: Float)

/**
 * The laptop's chat as a container that grows out of the row tapped on the list (450 ms) and shrinks back into it
 * on close, within the main screen's frame (screens) holding the list and the chat.
 *
 * Opening: the chat screen is shown at once (its views live from the first frame: lines, the working bubble, the
 * scroll to its end), clipped (clipBounds) to the row's place and opening out to its own; a picture of the row,
 * over its card's colour, fades from the container in its first third; behind it, a picture of the list darkens.
 * Closing: the list is shown at once and a picture of the chat shrinks to the row's place, fading to the row's
 * colour and then to the row itself, while the list lightens. A back swipe's shrink, or a grow cut short, is where
 * the close starts.
 *
 * Only clipBounds, the frame's background and one drawable on the frame's overlay are touched, and [stop] puts all
 * three back; nothing is scaled, faded or put on a layer. With animations off nothing of this runs.
 */
class ChatContainer(private val screens: ViewGroup, private val chat: View, private val base: Int) {
    private var running: ValueAnimator? = null
    private var ghost: MorphGhost? = null
    private var backdropWas: Drawable? = null
    private var backdropSet = false
    private var clipped = false
    private var waiting: Pair<ViewTreeObserver, ViewTreeObserver.OnPreDrawListener>? = null
    private var turn = 0
    private var growing: Box? = null  // where a grow under way has reached (a close then starts there)
    private var growScrim = 0f

    val busy get() = running != null || ghost != null

    /**
     * The chat opening out of row (call before the chat is shown; then [grow] after). Returns what [grow] needs, or
     * null when it doesn't apply (animations off, no row, not on screen): the chat then just appears.
     */
    fun capture(row: View?, list: View, viewport: View?): Captured? {
        stop()
        if (row == null || !Motion.on() || !row.isAttachedToWindow || !row.isShown || !row.isLaidOut) return null
        val box = boxIn(row) ?: return null
        val seen = viewport?.let { v -> boxIn(v)?.let { ChatMorph.visiblePart(box, it) } } ?: box
        if (!ChatMorph.grows(true, seen)) return null
        return Captured(seen, box, snapshot(row), surfaceBehind(row, base), snapshot(list), boxIn(list))
    }

    class Captured internal constructor(val seen: Box, val row: Box, val rowPicture: Picture?, val fill: Int,
                                        val listPicture: Picture?, val listBox: Box?)

    /** The chat, just shown, grows from what [capture] took. */
    fun grow(from: Captured) {
        stop()
        if (!Motion.on() || chat.visibility != View.VISIBLE) return
        val backdrop = from.listPicture?.let { ListBackdrop(it, from.listBox ?: Box(0f, 0f, 0f, 0f), base) }
        if (backdrop != null) { backdropWas = screens.background; backdropSet = true; screens.background = backdrop }
        // The row's picture sits at its own place: the part the list cut off stays off (offset within the box)
        val g = MorphGhost(from.rowPicture, from.row.width, from.fill, top = from.row.top - from.seen.top,
            left = from.row.left - from.seen.left).also { ghost = it; screens.overlay.add(it) }
        val clip = Rect()
        fun frame(t: Float) {
            val e = Motion.emphasized.getInterpolation(t)
            val full = ownBox()
            val box = ChatMorph.lerp(from.seen, full, e)
            clip.set((box.left - chat.left).toInt(), (box.top - chat.top).toInt(),
                (box.right - chat.left + 0.5f).toInt(), (box.bottom - chat.top + 0.5f).toInt())
            chat.clipBounds = clip; clipped = true
            g.setBounds(0, 0, screens.width, screens.height)
            g.box = box
            g.content = ChatMorph.outgoing(t); g.fillAlpha = ChatMorph.outgoing(t)
            g.invalidateSelf()
            growing = box; growScrim = ChatMorph.openingScrim(e)
            backdrop?.let { it.scrim = growScrim; it.invalidateSelf() }
        }
        frame(0f)
        val mine = ++turn
        running = Motion.run(ChatMorph.MS, LinearInterpolator(), ::frame) { if (mine == turn) finish() }
        if (running == null) finish()
    }

    /**
     * Before the list is shown in the chat's place: a picture of the chat as it is, and where it is (swipe: how a
     * back swipe left it, else its own place). Null with animations off: the list just appears.
     */
    fun captureClose(swipe: SwipeLook?): Closing? {
        val mid = growing.takeIf { running != null }  // closed while still growing: back from where it got to
        val midScrim = growScrim
        stop()
        if (!Motion.on() || chat.visibility != View.VISIBLE || !chat.isLaidOut || chat.width == 0) return null
        val box = swipe?.box ?: mid ?: ownBox()
        val scrim = if (swipe == null && mid != null) midScrim else ChatMorph.closeScrimFrom(swipe?.darkness)
        return Closing(snapshot(chat), chat.width.toFloat(), box, swipe?.corner ?: 0f, scrim)
    }

    class Closing internal constructor(val picture: Picture?, val width: Float, val box: Box, val corner: Float,
                                       val scrim: Float)

    /** The list, just shown, with the chat's picture over it shrinking into the row that rowOf finds once the list
     *  is laid out (null: fading and shrinking where it is instead). */
    fun shrink(from: Closing, rowOf: () -> View?, viewport: View?) {
        stop()
        if (!Motion.on()) return
        val g = MorphGhost(from.picture, from.width, base).also { ghost = it; screens.overlay.add(it) }
        g.setBounds(0, 0, screens.width, screens.height)
        g.box = from.box; g.corner = from.corner; g.scrim = from.scrim
        g.invalidateSelf()
        // The row's place is known once the list is laid out: the frame about to be drawn
        val mine = ++turn
        val observer = screens.viewTreeObserver
        val listener = object : ViewTreeObserver.OnPreDrawListener {
            override fun onPreDraw(): Boolean {
                unwait()
                if (mine != turn) return true
                val row = rowOf()?.takeIf { it.isShown && it.isLaidOut }
                val box = row?.let { boxIn(it) }
                val target = box?.let { b -> viewport?.let { v -> boxIn(v)?.let { ChatMorph.visiblePart(b, it) } } ?: b }
                if (row == null || target == null) fallback(g, from) else land(g, from, target, surfaceBehind(row, base))
                return true
            }
        }
        observer.addOnPreDrawListener(listener)
        waiting = observer to listener
        screens.invalidate()
    }

    private fun land(g: MorphGhost, from: Closing, target: Box, fill: Int) {
        fun frame(t: Float) {
            val e = Motion.emphasized.getInterpolation(t)
            g.setBounds(0, 0, screens.width, screens.height)
            g.box = ChatMorph.lerp(from.box, target, e)
            g.corner = from.corner * (1f - e)
            g.content = ChatMorph.outgoing(t)
            g.fillColour = ComputerCards.mix(base, fill, e)
            g.fillAlpha = ChatMorph.closingFill(t)
            g.scrim = ChatMorph.closingScrim(from.scrim, e)
            g.invalidateSelf()
        }
        frame(0f)
        val mine = turn
        running = Motion.run(ChatMorph.MS, LinearInterpolator(), ::frame) { if (mine == turn) finish() }
        if (running == null) finish()
    }

    private fun fallback(g: MorphGhost, from: Closing) {
        val to = ChatMorph.shrunk(from.box, ChatMorph.FALLBACK_SCALE)
        fun frame(t: Float) {
            val e = Motion.emphasizedAccelerate.getInterpolation(t)
            g.setBounds(0, 0, screens.width, screens.height)
            g.box = ChatMorph.lerp(from.box, to, e)
            g.content = 1f - e; g.fillAlpha = 1f - e
            g.scrim = ChatMorph.closingScrim(from.scrim, e)
            g.invalidateSelf()
        }
        frame(0f)
        val mine = turn
        running = Motion.run(ChatMorph.FALLBACK_MS, LinearInterpolator(), ::frame) { if (mine == turn) finish() }
        if (running == null) finish()
    }

    /** Whatever moves ends now: the chat unclipped, the frame's own background back, no picture left over it. */
    fun stop() {
        turn++
        val r = running; running = null
        r?.cancel()
        finish()
    }

    private fun unwait() {
        waiting?.let { (observer, listener) ->
            if (observer.isAlive) observer.removeOnPreDrawListener(listener)
            else screens.viewTreeObserver.removeOnPreDrawListener(listener)
        }
        waiting = null
    }

    private fun finish() {
        running = null
        growing = null
        unwait()
        ghost?.let { screens.overlay.remove(it) }
        ghost = null
        if (clipped) { chat.clipBounds = null; clipped = false }
        if (backdropSet) { screens.background = backdropWas; backdropWas = null; backdropSet = false }
    }

    /** The chat's own place in the frame (the frame's, before it is first measured). */
    private fun ownBox(): Box =
        if (chat.width > 0 && chat.height > 0) Box(chat.left.toFloat(), chat.top.toFloat(), chat.right.toFloat(), chat.bottom.toFloat())
        else Box(0f, 0f, screens.width.toFloat(), screens.height.toFloat())

    /** Where view is drawn in the frame, or null if it isn't under it. */
    private fun boxIn(view: View): Box? {
        if (view === screens) return Box(0f, 0f, screens.width.toFloat(), screens.height.toFloat())
        val r = Rect(0, 0, view.width, view.height)
        return try {
            screens.offsetDescendantRectToMyCoords(view, r)
            Box(r.left.toFloat(), r.top.toFloat(), r.right.toFloat(), r.bottom.toFloat())
        } catch (_: IllegalArgumentException) { null }  // not a descendant (taken away meanwhile)
    }

    companion object {
        /** The opaque colour a view is drawn on: its own background's or the nearest one's under it; else base. */
        fun surfaceBehind(view: View, base: Int): Int {
            var v: View? = view
            while (v != null) {
                opaque(v.background)?.let { return it }
                v = v.parent as? View
            }
            return base
        }

        private fun opaque(d: Drawable?): Int? {
            var inner = d
            while (inner is DrawableWrapper) inner = inner.drawable
            val c = when (inner) {
                is ColorDrawable -> inner.color
                is GradientDrawable -> inner.color?.defaultColor
                else -> null
            } ?: return null
            return if (Color.alpha(c) == 255) c else null
        }
    }
}

/** The list as it was when a chat opened over it, with a darkness over it: the frame's background while it grows. */
private class ListBackdrop(private val picture: Picture, private val at: Box, base: Int) : Drawable() {
    var scrim = 0f
    private val ground = Paint().apply { color = base }
    private val shade = Paint()

    override fun draw(canvas: Canvas) {
        canvas.drawRect(bounds, ground)
        val saved = canvas.save()
        canvas.translate(at.left, at.top)
        canvas.drawPicture(picture)
        canvas.restoreToCount(saved)
        if (scrim > 0f) { shade.color = Color.argb((scrim * 255).toInt().coerceIn(0, 255), 0, 0, 0); canvas.drawRect(bounds, shade) }
    }

    override fun setAlpha(alpha: Int) {}
    override fun setColorFilter(colorFilter: ColorFilter?) {}
    @Deprecated("Deprecated in Java")
    override fun getOpacity() = PixelFormat.OPAQUE
}

/** The container while it moves: a darkness over everything under it (scrim), then box, its corners rounded to
 *  corner, filled with fillColour (fillAlpha) and the picture (content) drawn at box's width from its top. top/left
 *  shift the picture within the box (a row partly cut off by the list's edge). */
private class MorphGhost(private val picture: Picture?, private val pictureWidth: Float, var fillColour: Int,
                         private val top: Float = 0f, private val left: Float = 0f) : Drawable() {
    var box = Box(0f, 0f, 0f, 0f)
    var corner = 0f
    var scrim = 0f
    var content = 1f
    var fillAlpha = 1f
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val rect = RectF()
    private val path = Path()

    override fun draw(canvas: Canvas) {
        if (scrim > 0f) canvas.drawColor(Color.argb((scrim * 255).toInt().coerceIn(0, 255), 0, 0, 0))
        if (box.empty) return
        rect.set(box.left, box.top, box.right, box.bottom)
        val saved = canvas.save()
        if (corner > 0.5f) { path.reset(); path.addRoundRect(rect, corner, corner, Path.Direction.CW); canvas.clipPath(path) }
        else canvas.clipRect(rect)
        if (fillAlpha > 0f) {
            paint.color = fillColour
            paint.alpha = (fillAlpha * 255).toInt().coerceIn(0, 255)
            canvas.drawRect(rect, paint)
        }
        if (picture != null && content > 0f) {
            if (content < 1f) canvas.saveLayerAlpha(rect, (content * 255).toInt().coerceIn(0, 255))
            val s = ChatMorph.fitWidth(box, pictureWidth)
            canvas.translate(box.left + left * s, box.top + top * s)
            canvas.scale(s, s)
            canvas.drawPicture(picture)
        }
        canvas.restoreToCount(saved)
    }

    override fun setAlpha(alpha: Int) { content = alpha / 255f }
    override fun setColorFilter(colorFilter: ColorFilter?) {}
    @Deprecated("Deprecated in Java")
    override fun getOpacity() = PixelFormat.TRANSLUCENT
}
