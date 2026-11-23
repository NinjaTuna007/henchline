package io.github.ninjatuna007.henchline

import android.animation.ValueAnimator
import android.view.View
import android.view.ViewGroup
import android.view.ViewTreeObserver
import android.view.animation.LinearInterpolator

/** The chat list redrawn after something the user did (a pin, a sort, a chat cleared or ended): each row, known by
 *  its key (its transitionName), slides from where it was to where it is now, rows new to the list fade in once the
 *  others have made room, and rows gone fade out where they were. The plain numbers and rules. */
object ListMotion {
    /** More keyed views than this (before or after) and the list just redraws: moving that many would stutter. */
    const val MAX_VIEWS = 60
    const val MOVE_MS = Motion.LONG

    /** Keys made unique in order: a key seen again (a chat drawn in two places) is told apart by its turn ("k#2"). */
    fun unique(keys: List<String>): List<String> {
        val seen = HashMap<String, Int>()
        return keys.map { key ->
            val n = (seen[key] ?: 0) + 1
            seen[key] = n
            if (n == 1) key else "$key#$n"
        }
    }

    /** Whether to move the list at all: animations on, something to move from, and not too many views either side. */
    fun moves(on: Boolean, before: Int, after: Int) = on && before in 1..MAX_VIEWS && after in 1..MAX_VIEWS

    /** How far (dx, dy, px) a view must be put back to stand where it stood: its old place less its new one, less
     *  what its keyed parent is put back by (nearer keyed parents move it already). parentOld null: the parent is
     *  not keyed, or new (it doesn't move). */
    fun offset(old: Box, new: Box, parentOld: Box?, parentNew: Box?): Pair<Float, Float> {
        var dx = old.left - new.left
        var dy = old.top - new.top
        if (parentOld != null && parentNew != null) {
            dx -= parentOld.left - parentNew.left
            dy -= parentOld.top - parentNew.top
        }
        return dx to dy
    }

    /** box moved by (dx, dy). */
    fun shifted(box: Box, dx: Float, dy: Float) = Box(box.left + dx, box.top + dy, box.right + dx, box.bottom + dy)

    /** A move too small to see is no move. */
    fun moved(dx: Float, dy: Float) = kotlin.math.abs(dx) >= 0.5f || kotlin.math.abs(dy) >= 0.5f

    /** The offset still left at eased progress e. */
    fun at(offset: Float, e: Float) = offset * (1f - e)

    /** A row going: gone by a third of the move (linear time t), before new ones come. A row coming: in after it. */
    fun goingAlpha(t: Float) = TabMotion.outAlpha(t)
    fun comingAlpha(t: Float) = TabMotion.inAlpha(t)

    /** Whether an unread count pops (a quick spring up from small): only when it goes up from a count drawn before;
     *  a first drawing (before: null), the same count again on a redraw, or a lower one, just shows. */
    fun pops(before: Int?, now: Int) = before != null && now > before

    /** The count kept as last seen for a badge drawn now with now: itself when on screen (seen) or new; else the
     *  lower of it and the last seen (a rise waits to pop until the list shows, a fall counts at once). */
    fun seenCount(before: Int?, now: Int, seen: Boolean) = if (seen || before == null) now else minOf(before, now)

    /** A badge popping starts at this fraction of its size. */
    const val POP_FROM = 0.6f
}

/**
 * The keyed moves of a list ([ListMotion]): [capture] where every keyed view is before a redraw, then [play] once it
 * is redrawn. Only translation and alpha are touched (no layout is held or changed), and rows gone are their old
 * views on the list's overlay. While it plays, a redraw asked (a poll, a push) is held ([ask]) and done once at the
 * end. [stop] leaves the list as drawn, whole and in place; whoever stops it redraws if it needs to.
 */
class KeyedMoves(private val list: ViewGroup) {
    private val guard = RedrawGuard()
    private var running: ValueAnimator? = null
    private var waiting: Pair<ViewTreeObserver, ViewTreeObserver.OnPreDrawListener>? = null
    private val touched = ArrayList<View>()
    private val ghosts = ArrayList<View>()
    private var turn = 0

    val busy get() = guard.busy || running != null

    /** Draw the list now? False while a move plays (the redraw is owed and done at its end). */
    fun ask() = guard.ask()

    class Before internal constructor(val boxes: Map<String, Box>, val views: Map<String, View>)

    /** Where each keyed view is now; null when nothing should move (animations off, the list not on screen, none or
     *  too many keyed views). Stops a move under way, taking each view where it is drawn mid-way (its place and
     *  the moves still on it and the views above it), so a second change goes on from there rather than jumping. */
    fun capture(): Before? {
        if (!Motion.on() || !list.isShown || !list.isLaidOut) { stop(); return null }
        val found = keyed()
        if (!ListMotion.moves(true, found.size, ListMotion.MAX_VIEWS)) { stop(); return null }
        val boxes = LinkedHashMap<String, Box>()
        val views = LinkedHashMap<String, View>()
        for ((key, view) in found) {
            boxIn(view)?.let { box ->
                val (tx, ty) = movedBy(view)
                boxes[key] = ListMotion.shifted(box, tx, ty); views[key] = view
            }
        }
        stop()
        return Before(boxes, views)
    }

    /** How far view is drawn from its laid-out place by a move under way: its own translation and its parents'. */
    private fun movedBy(view: View): Pair<Float, Float> {
        var tx = 0f; var ty = 0f
        var v: View? = view
        while (v != null && v !== list) { tx += v.translationX; ty += v.translationY; v = v.parent as? View }
        return tx to ty
    }

    /** The list, just redrawn, moves from before. redraw: the list drawn again, when one was held meanwhile. */
    fun play(before: Before, redraw: () -> Unit) {
        stop()
        if (!Motion.on() || !list.isShown) return
        guard.begin()
        val mine = ++turn
        val observer = list.viewTreeObserver
        val listener = object : ViewTreeObserver.OnPreDrawListener {
            override fun onPreDraw(): Boolean {
                unwait()
                if (mine != turn) return true
                start(before, mine, redraw)
                return true
            }
        }
        observer.addOnPreDrawListener(listener)
        waiting = observer to listener
        list.invalidate()
    }

    private fun start(before: Before, mine: Int, redraw: () -> Unit) {
        val found = keyed()
        if (!ListMotion.moves(true, before.boxes.size, found.size)) return end(mine, redraw)
        val now = LinkedHashMap<String, Box>()
        for ((key, view) in found) boxIn(view)?.let { now[key] = it }
        class Move(val view: View, val dx: Float, val dy: Float)
        val moves = ArrayList<Move>()
        val coming = ArrayList<View>()
        for ((key, view) in found) {
            val new = now[key] ?: continue
            val old = before.boxes[key]
            if (old == null) { coming.add(view); continue }
            val parent = keyedParent(view)
            val (dx, dy) = ListMotion.offset(old, new, parent?.let { before.boxes[it] }, parent?.let { now[it] })
            if (ListMotion.moved(dx, dy)) moves.add(Move(view, dx, dy))
        }
        // Rows gone: their old views, drawn on the list's overlay where they stood, fading
        for ((key, view) in before.views) {
            if (key in now) continue
            val box = before.boxes[key] ?: continue
            // Inside a keyed view that is going too: it goes with it
            if (keyedParent(view)?.let { it !in now && it in before.boxes } == true) continue
            (view.parent as? ViewGroup)?.let { if (it !== list.overlay) runCatching { it.removeView(view) } }
            view.translationX = 0f; view.translationY = 0f; view.alpha = 1f
            runCatching {
                list.overlay.add(view)
                view.layout(box.left.toInt(), box.top.toInt(), box.right.toInt(), box.bottom.toInt())
                ghosts.add(view)
            }
        }
        // A new row inside a new keyed view fades with it, not on its own as well
        val fading = coming.filter { v -> generateSequence(v.parent as? View) { it.parent as? View }
            .takeWhile { it !== list }.none { it in coming } }
        if (moves.isEmpty() && fading.isEmpty() && ghosts.isEmpty()) return end(mine, redraw)
        touched.addAll(moves.map { it.view }); touched.addAll(fading)
        fun frame(t: Float) {
            val e = Motion.emphasized.getInterpolation(t)
            for (m in moves) { m.view.translationX = ListMotion.at(m.dx, e); m.view.translationY = ListMotion.at(m.dy, e) }
            val inAlpha = ListMotion.comingAlpha(t)
            for (v in fading) v.alpha = inAlpha
            val outAlpha = ListMotion.goingAlpha(t)
            for (g in ghosts) g.alpha = outAlpha
        }
        frame(0f)
        running = Motion.run(ListMotion.MOVE_MS, LinearInterpolator(), ::frame) { if (mine == turn) end(mine, redraw) }
        if (running == null) end(mine, redraw)
    }

    private fun end(mine: Int, redraw: () -> Unit) {
        if (mine != turn) return
        finish()
        if (guard.end()) redraw()
    }

    /** Whatever moves ends now: every view home and whole, the gone ones gone, nothing owed. */
    fun stop() {
        turn++
        val r = running; running = null
        r?.cancel()
        finish()
        guard.reset()
    }

    private fun finish() {
        running = null
        unwait()
        for (v in touched) { v.translationX = 0f; v.translationY = 0f; v.alpha = 1f }
        touched.clear()
        for (g in ghosts) runCatching { list.overlay.remove(g) }
        ghosts.clear()
    }

    private fun unwait() {
        waiting?.let { (observer, listener) ->
            if (observer.isAlive) observer.removeOnPreDrawListener(listener)
            else list.viewTreeObserver.removeOnPreDrawListener(listener)
        }
        waiting = null
    }

    /** Every keyed view under the list (shown ones), in drawing order, with keys made unique. */
    private fun keyed(): List<Pair<String, View>> {
        val views = ArrayList<View>()
        fun walk(v: View) {
            if (v.visibility != View.VISIBLE) return
            if (v !== list && !v.transitionName.isNullOrEmpty()) views.add(v)
            if (v is ViewGroup) for (i in 0 until v.childCount) walk(v.getChildAt(i))
        }
        walk(list)
        val keys = ListMotion.unique(views.map { it.transitionName })
        keys.forEachIndexed { i, key -> views[i].setTag(R.id.listKey, key) }
        return keys.zip(views)
    }

    /** The key of the nearest keyed view above view, below the list (or null). */
    private fun keyedParent(view: View): String? {
        var p = view.parent as? View
        while (p != null && p !== list) {
            (p.getTag(R.id.listKey) as? String)?.let { if (!p.transitionName.isNullOrEmpty()) return it }
            p = p.parent as? View
        }
        return null
    }

    /** Where view is laid out in the list (its translation left out: a move under way was stopped first). */
    private fun boxIn(view: View): Box? {
        val r = android.graphics.Rect(0, 0, view.width, view.height)
        return try {
            list.offsetDescendantRectToMyCoords(view, r)
            Box(r.left.toFloat(), r.top.toFloat(), r.right.toFloat(), r.bottom.toFloat())
        } catch (_: IllegalArgumentException) { null }
    }
}
