package io.github.ninjatuna007.henchline

import android.content.Context
import android.graphics.Canvas
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint
import android.widget.TextView

/**
 * A chat bubble whose time (and, on yours, its ticks) sits in its bottom-right corner, as a messaging app's does
 * (the user's wish, 2026-11-16: following the last word, it landed all over the place). The words end with an
 * invisible copy of the time, so the last line keeps room for it, or wraps to a line of its own; the time itself is
 * drawn at the corner, on the last line's baseline.
 */
class Stamped(context: Context) : TextView(context) {
    var tail: CharSequence? = null
        set(value) { field = value; drawn = null; invalidate() }
    private var drawn: StaticLayout? = null
    private var before: StaticLayout? = null  // the tail it is changing from, fading out while the new one fades in
    private var mix = 1f  // how far into the new tail
    private var fading: android.animation.ValueAnimator? = null

    /** The tail changed in place (the ticks of a line on screen moving on, grey to cyan): the old one fades into the
     *  new over 200 ms. Set at once when it was never drawn, or animations are off. */
    fun changeTail(value: CharSequence?) {
        val was = drawn
        fading?.cancel()
        tail = value
        before = null; mix = 1f
        if (was == null || value == null || !isAttachedToWindow || !Motion.on()) return
        before = was
        fading = Motion.run(Motion.MEDIUM, Motion.standard, { mix = it; invalidate() }) {
            before = null; mix = 1f; fading = null; invalidate()
        }
    }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        fading?.cancel()  // its end listener lets the old tail go
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val tail = tail ?: return
        val text = layout ?: return
        val paint = TextPaint(paint).apply { textSize = this@Stamped.textSize }  // the spans scale it, as in the words
        val laid = drawn ?: StaticLayout.Builder.obtain(tail, 0, tail.length, paint, 10_000)
            .setAlignment(Layout.Alignment.ALIGN_NORMAL).setIncludePad(false).build().also { drawn = it }
        val baseline = totalPaddingTop + text.getLineBaseline(text.lineCount - 1)
        val old = before
        if (old != null) corner(canvas, old, baseline, 1 - mix)
        corner(canvas, laid, baseline, if (old != null) mix else 1f)
    }

    /** A tail drawn at the bottom-right corner, on the last line's baseline, at that opacity. */
    private fun corner(canvas: Canvas, laid: StaticLayout, baseline: Int, opacity: Float) {
        val width = laid.getLineWidth(0)
        val left = this.width - totalPaddingRight - width
        val top = (baseline - laid.getLineBaseline(0)).toFloat()
        val saved = if (opacity < 1f) canvas.saveLayerAlpha(left, top, left + width, top + laid.height,
            (opacity * 255).toInt().coerceIn(0, 255)) else canvas.save()
        canvas.translate(left, top)
        laid.draw(canvas)
        canvas.restoreToCount(saved)
    }
}
