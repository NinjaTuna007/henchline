package io.github.ninjatuna007.henchline

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PathMeasure
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.Gravity
import android.view.View
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.LinearLayout
import android.widget.TextView
import androidx.dynamicanimation.animation.SpringAnimation

/** What a model's download is doing, as its bar says it. */
enum class DownloadPhase(val words: String, val spoken: String) {
    CHECKING("Checking files…", "Checking files"),
    DOWNLOADING("Downloading…", "Downloading"),
    WAITING_WIFI("Waiting for Wi-Fi…", "Waiting for Wi-Fi"),
    DONE("Done", "Download done"),
}

/** The bar a sheet draws for a download's job (its [ModelJob.phase], whether it runs, the bytes it has): while it
 *  runs, checking or downloading; stopped off Wi-Fi with part of it here, waiting. null: no bar (never started,
 *  done, or stopped for another reason, which the sheet says in words). */
fun barPhase(phase: DownloadPhase?, running: Boolean, bytes: Long): DownloadPhase? = when {
    running -> if (phase == DownloadPhase.DOWNLOADING) phase else DownloadPhase.CHECKING
    phase == DownloadPhase.WAITING_WIFI && bytes > 0 -> DownloadPhase.WAITING_WIFI
    else -> null
}

/** Whole percent of size that bytes are, 0..100 (0 while the size isn't known). */
fun downloadPercent(bytes: Long, size: Long): Int =
    if (size <= 0) 0 else (bytes.coerceIn(0, size) * 100 / size).toInt()

/** Megabytes (1,000,000 bytes, as the sheets' sizes), cut down, not rounded up: with a tenth when tenths says so. */
fun megabytes(bytes: Long, tenths: Boolean): String {
    val b = bytes.coerceAtLeast(0)
    return if (tenths) (b / 100_000).let { "${it / 10}.${it % 10}" } else "${b / 1_000_000}"
}

/** "45% · 28.8 of 64.0 MB": the share here and how much of the model that is (whole megabytes from 100 MB up). Empty
 *  while the size isn't known (the laptop's list not read yet). */
fun downloadAmount(bytes: Long, size: Long): String {
    if (size <= 0) return ""
    val tenths = size < 100_000_000
    val b = bytes.coerceIn(0, size)
    return "${downloadPercent(b, size)}% · ${megabytes(b, tenths)} of ${megabytes(size, tenths)} MB"
}

/** What a screen reader says for the bar: "Downloading, 45 percent". */
fun downloadSpoken(phase: DownloadPhase, percent: Int): String = when (phase) {
    DownloadPhase.DOWNLOADING, DownloadPhase.WAITING_WIFI -> "${phase.spoken}, ${percent.coerceIn(0, 100)} percent"
    else -> phase.spoken
}

/** What a screen reader is told as the bar goes from one phase to another; null: nothing. Only the first
 *  Downloading (saidDownloading: it was said or shown already), Waiting for Wi-Fi and Done are said, never Checking
 *  and never ticks (focused, the bar reads its percent). from null is a bar just drawn: silent, unless it was drawn
 *  because the download just paused for Wi-Fi (justPaused). */
fun downloadAnnouncement(from: DownloadPhase?, to: DownloadPhase, percent: Int, saidDownloading: Boolean,
                         justPaused: Boolean = false): String? = when {
    from == to -> null
    from == null -> if (justPaused && to == DownloadPhase.WAITING_WIFI) downloadSpoken(to, percent) else null
    to == DownloadPhase.DOWNLOADING -> if (saidDownloading) null else downloadSpoken(to, percent)
    to == DownloadPhase.WAITING_WIFI || to == DownloadPhase.DONE -> downloadSpoken(to, percent)
    else -> null
}

/** Where the fill heads: bytes of size as 0..1; where it was (keep) while the size isn't known. */
fun fillTarget(keep: Float, bytes: Long, size: Long): Float =
    if (size <= 0) keep else (bytes.coerceIn(0, size).toDouble() / size).toFloat()

/** The fill's drawn width in a track of width and height: none at 0, else at least a round dot (height wide). */
fun fillWidth(fraction: Float, width: Float, height: Float): Float = when {
    fraction <= 0f || width <= 0f -> 0f
    else -> (fraction.coerceAtMost(1f) * width).coerceAtLeast(minOf(height, width))
}

/** The left edge of the shimmer's band at t (0..1 of one pass): from just off the left to just off the right. */
fun shimmerAt(t: Float, width: Float, band: Float): Float = -band + t * (width + band)

/**
 * A model's download, drawn the same in every sheet: what is happening ("Downloading…", "Checking files…", "Waiting
 * for Wi-Fi…", "Done") over a rounded track whose cyan fill glides to each new amount, and under it "45% · 28.8
 * of 64.0 MB". While files are checked a soft band slides along the track; paused for Wi-Fi the fill greys; once whole,
 * a check mark comes in on the right. Instant when the phone has animations off. A screen reader hears it as one
 * progress bar ("Downloading, 45 percent"), told only as the download starts, pauses and is done
 * ([downloadAnnouncement]).
 */
class DownloadBar(context: Context) : LinearLayout(context) {
    private val dp = resources.displayMetrics.density
    private fun px(v: Float) = (v * dp).toInt()
    private val textColour = context.getColor(R.color.text)
    private val muted = context.getColor(R.color.muted)

    private val phaseText = TextView(context).apply {
        textSize = 14f; setTextColor(textColour); typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
    }
    private val mark = DoneMark(context).apply { alpha = 0f }
    private val track = Track(context)
    private val amount = TextView(context).apply {
        textSize = 13f; setTextColor(muted); fontFeatureSettings = "tnum"  // figures that don't shuffle as they change
    }

    private var phase: DownloadPhase? = null
    private var saidDownloading = false  // Downloading was said (or shown from the start): not said again
    private var pending: String? = null  // said once the bar is on screen
    private var size = 0L
    private var finishing = false

    init {
        orientation = VERTICAL
        addView(LinearLayout(context).apply {
            orientation = HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            addView(phaseText, LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f))
            addView(mark, LayoutParams(px(20f), px(20f)))
        }, LayoutParams(LayoutParams.MATCH_PARENT, px(22f)))
        addView(track, LayoutParams(LayoutParams.MATCH_PARENT, px(8f)).apply { topMargin = px(10f) })
        addView(amount, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.WRAP_CONTENT).apply { topMargin = px(8f) })
        // One node for a screen reader: its words are the contentDescription, its children are drawing only
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_YES
        isScreenReaderFocusable = true
        for (i in 0 until childCount) getChildAt(i).importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
    }

    override fun onInitializeAccessibilityNodeInfo(info: AccessibilityNodeInfo) {
        super.onInitializeAccessibilityNodeInfo(info)
        info.className = android.widget.ProgressBar::class.java.name
    }

    /** The job's state now: the first call draws it (the fill growing in from the left when grow, else already
     *  there: a sheet redrawn mid-download), later ones glide to it. justPaused: drawn because the download just
     *  stopped for Wi-Fi, which a screen reader is then told. */
    fun show(now: DownloadPhase, bytes: Long, size: Long, grow: Boolean = true, justPaused: Boolean = false) {
        if (finishing || now == DownloadPhase.DONE) return
        if (size > 0) this.size = size
        val p = downloadPercent(bytes, size)
        val first = phase == null
        if (now != phase) {
            swapWords(phaseText, now.words, animate = !first)
            downloadAnnouncement(phase, now, p, saidDownloading, justPaused)?.let { announce(it) }
        }
        if (now == DownloadPhase.DOWNLOADING) saidDownloading = true
        phase = now
        track.paused = now == DownloadPhase.WAITING_WIFI
        track.indeterminate = now == DownloadPhase.CHECKING
        if (first && !grow) track.jump(bytes, size) else track.to(bytes, size)
        amount.text = amountText(bytes, size)
        contentDescription = downloadSpoken(now, p)
    }

    /** The download is whole: the fill runs to the end, "Done" and the check mark fade in, and after hold, then (the
     *  sheet draws the model as ready). then comes at once when the bar is no longer on screen. */
    fun finish(hold: Long = 900L, then: () -> Unit) {
        if (!isAttachedToWindow) { then(); return }
        if (finishing) return
        finishing = true
        phase = DownloadPhase.DONE
        track.indeterminate = false; track.paused = false
        track.to(1, 1) {
            swapWords(phaseText, DownloadPhase.DONE.words, animate = true)
            amount.text = amountText(size, size)
            contentDescription = DownloadPhase.DONE.spoken
            announce(DownloadPhase.DONE.spoken)
            Motion.appear(mark, duration = Motion.LONG)
            Motion.run(Motion.LONGER, Motion.emphasized, { mark.drawn = it })
            postDelayed({ then() }, hold)
        }
    }

    private fun amountText(bytes: Long, size: Long): CharSequence {
        val words = downloadAmount(bytes, size)
        val cut = words.indexOf('%')
        if (cut < 0) return words
        return SpannableStringBuilder(words).apply {  // the percent stands out, the megabytes stay quiet
            setSpan(ForegroundColorSpan(textColour), 0, cut + 1, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            setSpan(android.text.style.TypefaceSpan(Typeface.create("sans-serif-medium", Typeface.NORMAL)), 0, cut + 1,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
        }
    }

    /** Said now, or as soon as the bar is on screen (a bar drawn because the download paused isn't yet). */
    private fun announce(words: String) {
        if (isAttachedToWindow) announceForAccessibility(words) else pending = words
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        pending?.let { words -> pending = null; post { if (isAttachedToWindow) announceForAccessibility(words) } }
    }

    /** New words for a line: the old fade out, the new fade in (at once when off, or the first time). */
    private fun swapWords(view: TextView, words: CharSequence, animate: Boolean) {
        view.animate().cancel()
        if (!animate || !Motion.on() || !view.isAttachedToWindow) { view.text = words; view.alpha = 1f; return }
        view.animate().alpha(0f).setDuration(Motion.SHORT).setInterpolator(Motion.standard).withEndAction {
            view.text = words
            view.animate().alpha(1f).setDuration(Motion.MEDIUM).setInterpolator(Motion.standard).start()
        }.start()
    }

    /** The track and its fill, with the band that slides while files are checked. */
    private class Track(context: Context) : View(context) {
        private val trackPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = context.getColor(R.color.navy) }
        private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = context.getColor(R.color.cyan) }
        private val pausedPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = context.getColor(R.color.muted) }
        private val bandPaint = Paint(Paint.ANTI_ALIAS_FLAG)
        private val stillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {  // the band's stand-in with animations off
            color = context.getColor(R.color.cyan); alpha = 0x40
        }
        private val clip = Path()
        private val rect = RectF()
        private var band = 0f

        private var shown = 0f  // where the fill is drawn, 0..1
        private var heading = 0f  // where it is going
        private var spring: SpringAnimation? = null
        private var landed: (() -> Unit)? = null
        private var shimmer: ValueAnimator? = null
        private var shimmerT = 0f

        var paused = false
            set(value) { if (field != value) { field = value; invalidate() } }
        var indeterminate = false
            set(value) { if (field != value) { field = value; shimmerOnOff(); invalidate() } }

        /** The fill gliding to bytes of size (a soft spring, no overshoot, retargeted as new amounts come), then done. */
        fun to(bytes: Long, size: Long, done: (() -> Unit)? = null) {
            heading = fillTarget(heading, bytes, size)
            if (done != null) landed = done
            val running = spring
            if (running != null && running.isRunning) { running.animateToFinalPosition(heading); return }
            spring = Motion.spring(shown, heading, spring = PROGRESS, visible = Motion.SMALL,
                update = { shown = it; invalidate() }) {
                spring = null
                landed?.let { landed = null; it() }
            }
        }

        /** The fill at bytes of size at once. */
        fun jump(bytes: Long, size: Long) {
            spring?.cancel()
            heading = fillTarget(heading, bytes, size); shown = heading
            invalidate()
        }

        override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
            val r = h / 2f
            clip.reset(); clip.addRoundRect(0f, 0f, w.toFloat(), h.toFloat(), r, r, Path.Direction.CW)
            band = w * 0.35f
            // Pale cyan: a glow over the empty track, a sheen over the fill
            bandPaint.shader = LinearGradient(0f, 0f, band, 0f,
                intArrayOf(Color.TRANSPARENT, 0xB3B3F7FF.toInt(), Color.TRANSPARENT), null, Shader.TileMode.CLAMP)
        }

        override fun onDraw(canvas: Canvas) {
            val w = width.toFloat(); val h = height.toFloat(); val r = h / 2
            rect.set(0f, 0f, w, h)
            canvas.drawRoundRect(rect, r, r, trackPaint)
            val fill = fillWidth(shown, w, h)
            if (fill > 0f) { rect.set(0f, 0f, fill, h); canvas.drawRoundRect(rect, r, r, if (paused) pausedPaint else fillPaint) }
            if (!indeterminate) return
            canvas.save()
            canvas.clipPath(clip)
            if (shimmer != null) {
                canvas.translate(shimmerAt(shimmerT, w, band), 0f)
                canvas.drawRect(0f, 0f, band, h, bandPaint)
            } else canvas.drawRect(fill, 0f, w, h, stillPaint)
            canvas.restore()
        }

        private fun shimmerOnOff() {
            val want = indeterminate && isAttachedToWindow && Motion.on()
            if (want && shimmer == null) shimmer = ValueAnimator.ofFloat(0f, 1f).apply {
                duration = 1300L; repeatCount = ValueAnimator.INFINITE; interpolator = Motion.standard
                addUpdateListener { shimmerT = it.animatedValue as Float; invalidate() }
                start()
            } else if (!want) { shimmer?.cancel(); shimmer = null }
        }

        override fun onAttachedToWindow() { super.onAttachedToWindow(); shimmerOnOff() }

        override fun onDetachedFromWindow() {
            super.onDetachedFromWindow()
            shimmer?.cancel(); shimmer = null
            spring?.cancel()  // its end gives landed its turn
            shown = heading
        }
    }

    /** A cyan disc with a navy tick, drawn on as drawn goes 0..1. */
    private class DoneMark(context: Context) : View(context) {
        private val disc = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = context.getColor(R.color.cyan) }
        private val ink = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = context.getColor(R.color.navy); style = Paint.Style.STROKE
            strokeWidth = 2.2f * context.resources.displayMetrics.density
            strokeCap = Paint.Cap.ROUND; strokeJoin = Paint.Join.ROUND
        }
        private val tick = Path()
        private val part = Path()
        private val measure = PathMeasure()
        var drawn = 0f
            set(value) { field = value; invalidate() }

        override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
            tick.reset()
            tick.moveTo(w * 0.28f, h * 0.52f); tick.lineTo(w * 0.44f, h * 0.67f); tick.lineTo(w * 0.73f, h * 0.36f)
            measure.setPath(tick, false)
        }

        override fun onDraw(canvas: Canvas) {
            val w = width.toFloat(); val h = height.toFloat()
            canvas.drawCircle(w / 2, h / 2, minOf(w, h) / 2, disc)
            part.reset()
            measure.getSegment(0f, measure.length * drawn.coerceIn(0f, 1f), part, true)
            canvas.drawPath(part, ink)
        }
    }

    companion object {
        /** The fill's glide: no overshoot, slow enough that the job's steps (a percent or a megabyte) run into one sweep. */
        val PROGRESS = Motion.Spring(1f, 50f)
    }
}
