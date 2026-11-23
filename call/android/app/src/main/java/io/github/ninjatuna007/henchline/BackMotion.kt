package io.github.ninjatuna007.henchline

import android.animation.ValueAnimator
import android.graphics.Outline
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.Drawable
import android.view.View
import android.view.ViewOutlineProvider
import android.view.ViewTreeObserver
import android.window.BackEvent
import android.window.OnBackAnimationCallback
import android.window.OnBackInvokedDispatcher
import androidx.annotation.RequiresApi

/** A screen followed by a back swipe: it shrinks to this fraction of its size. */
const val BACK_SCREEN_SCALE = 0.9f

/** ...its corners rounding to this (dp) as it does. */
const val BACK_SCREEN_CORNER_DP = 28f

/** ...over the space behind it, darkened by up to this fraction. */
const val BACK_BACKDROP = 0.5f

/** A sheet followed by a back swipe narrows by up to this (dp) across and this (dp) down, about its bottom edge. */
const val BACK_SHEET_X_DP = 48f
const val BACK_SHEET_Y_DP = 24f

/** The picture viewer followed by a back swipe: the darkness behind it lightens to this share of what it was. */
const val BACK_PICTURE_DARK = 0.7f

/** How far a back swipe has gone, 0..1 (anything else held to it). */
fun backFraction(progress: Float): Float = if (progress.isNaN()) 0f else progress.coerceIn(0f, 1f)

/** A screen's size at that progress: whole at 0, [BACK_SCREEN_SCALE] at 1. */
fun backScreenScale(progress: Float): Float = 1f - (1f - BACK_SCREEN_SCALE) * backFraction(progress)

/** How far a screen of width px moves sideways at that progress, toward the side the swipe came from (right for a
 *  swipe from the left edge): a twentieth of the width less 8 dp, never less than none. */
fun backScreenShift(progress: Float, width: Float, fromLeft: Boolean, density: Float): Float =
    (if (fromLeft) 1f else -1f) * maxOf(0f, width / 20f - 8f * density) * backFraction(progress)

/** A size (px) shrunk by up to distance px at that progress, as a scale: never below half, whole when unmeasured. */
fun backShrink(progress: Float, size: Float, distance: Float): Float =
    if (size <= 0f) 1f else (1f - distance / size * backFraction(progress)).coerceIn(0.5f, 1f)

/** from..to at that progress. */
fun backBetween(from: Float, to: Float, progress: Float): Float = from + (to - from) * backFraction(progress)

/** An opaque colour darkened toward black by share (0 itself, 1 black); its alpha kept. */
fun darken(colour: Int, share: Float): Int {
    val keep = 1f - share.coerceIn(0f, 1f)
    fun ch(shift: Int) = (((colour ushr shift) and 0xFF) * keep).toInt().coerceIn(0, 255) shl shift
    return (colour and 0xFF000000.toInt()) or ch(16) or ch(8) or ch(0)
}

/** What back does on the main screen, the first that applies: the share sheet, the sheet on top, the selection,
 *  a chat or settings (to the list); null on the list, where back leaves the app. */
enum class BackStep { SHEET, PANEL, SELECTION, SCREEN }

fun mainBackStep(sheetOpen: Boolean, panelOpen: Boolean, selecting: Boolean, onList: Boolean): BackStep? = when {
    sheetOpen -> BackStep.SHEET
    panelOpen -> BackStep.PANEL
    selecting -> BackStep.SELECTION
    !onList -> BackStep.SCREEN
    else -> null
}

/** Whether a swipe's look is left as it is when back goes ahead: only when what it moved is what back now closes
 *  and that closing takes over from where it is (a sheet sliding away). Anything else is put back whole first. */
fun keepsBackPreview(keeps: Boolean, started: Any?, now: Any?): Boolean = keeps && started != null && started === now

/** Whether the keyboard is up in the window that view is in. Its own back (hiding it) has the same priority as a
 *  screen's and the one registered last wins, so a screen's callback stays unregistered while it is up: back hides
 *  the keyboard first, and the next frame drawn once it has gone registers the screen's again. */
fun keyboardUp(view: View): Boolean =
    android.os.Build.VERSION.SDK_INT >= 30 && view.rootWindowInsets?.isVisible(android.view.WindowInsets.Type.ime()) == true

/** How something on screen follows a back swipe before it is let go (Android 14 and later). */
interface BackPreview {
    /** What it moves: compared with what back closes when it goes ahead. */
    val surface: Any
    /** Whether the closing takes over from its look (else it is put back whole first). */
    val keepsOnCommit: Boolean
    /** The swipe at progress 0..1. */
    fun progress(progress: Float)
    /** The swipe let go short: back to whole, eased. */
    fun cancel()
    /** Whole at once, whatever is moving stopped. Again does nothing. */
    fun reset()
}

/**
 * Back on Android 14 and later, for a window that opts into it: a callback registered only while there is
 * something in the app for back to do (else the system's own back, and its look of going home, takes it), kept in
 * step on every frame drawn, and never dropped mid-swipe. A swipe's start asks for a [BackPreview], its progress
 * moves it, a swipe let go short puts it back, and back going ahead (a swipe finished, the back button) calls
 * invoke. Before 14 nothing here runs: the screens' onBackPressed does it all.
 */
@RequiresApi(34)
class PredictiveBack(
    private val dispatcher: OnBackInvokedDispatcher,
    private val wanted: () -> Boolean,
    private val begin: (fromLeft: Boolean) -> BackPreview?,
    private val surfaceNow: () -> Any?,
    private val invoke: () -> Unit,
) {
    private var registered = false
    private var swiping = false
    private var released = false
    private var preview: BackPreview? = null
    private var settling: BackPreview? = null  // one let go short, still easing back
    private var watched: Pair<ViewTreeObserver, ViewTreeObserver.OnPreDrawListener>? = null
    private var watchedView: View? = null

    private val callback = object : OnBackAnimationCallback {
        override fun onBackStarted(event: BackEvent) {
            settling?.reset(); settling = null
            preview?.reset()
            swiping = true
            preview = begin(event.swipeEdge == BackEvent.EDGE_LEFT)
        }

        override fun onBackProgressed(event: BackEvent) { preview?.progress(event.progress) }

        override fun onBackCancelled() {
            swiping = false
            preview?.let { it.cancel(); settling = it }
            preview = null
            sync()
        }

        override fun onBackInvoked() {
            swiping = false
            val p = preview
            preview = null
            if (p != null && !keepsBackPreview(p.keepsOnCommit, p.surface, surfaceNow())) p.reset()
            invoke()
            sync()
        }
    }

    /** Registered when back has something to do here, not when it hasn't; never changed mid-swipe. */
    fun sync() {
        if (released || swiping) return
        val want = wanted()
        if (want == registered) return
        if (want) dispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, callback)
        else dispatcher.unregisterOnBackInvokedCallback(callback)
        registered = want
    }

    /** Kept in step with what view's window draws: a sheet opening, a screen changing, all come with a frame. */
    fun watch(view: View) {
        val listener = ViewTreeObserver.OnPreDrawListener { sync(); true }
        val observer = view.viewTreeObserver
        observer.addOnPreDrawListener(listener)
        watched = observer to listener
        watchedView = view
        sync()
    }

    /** The window going: unregistered, nothing left moved or moving. */
    fun release() {
        released = true
        preview?.reset(); preview = null
        settling?.reset(); settling = null
        watched?.let { (observer, listener) ->
            if (observer.isAlive) observer.removeOnPreDrawListener(listener)
            else watchedView?.viewTreeObserver?.removeOnPreDrawListener(listener)
        }
        watched = null; watchedView = null
        if (registered) dispatcher.unregisterOnBackInvokedCallback(callback)
        registered = false
    }
}

/**
 * A full screen (a chat, settings) following a back swipe: it shrinks toward [BACK_SCREEN_SCALE], moves a little
 * toward the swipe's side and rounds its corners, while the space behind it (backdrop, an opaque colour) darkens.
 * Back going ahead puts it whole at once (the list then replaces it); let go short, it eases back. With animations
 * off it never moves. Whatever it changed is put back as it was: scale, offset, outline, clip and backdrop.
 */
class ScreenBackPreview(private val screen: View, private val backdrop: View, private val fromLeft: Boolean) : BackPreview {
    override val surface: Any get() = screen
    override val keepsOnCommit = false
    private val live = Motion.on()
    private val density = screen.resources.displayMetrics.density
    private val scaleX0 = screen.scaleX
    private val scaleY0 = screen.scaleY
    private val shift0 = screen.translationX
    private val outline0 = screen.outlineProvider
    private val clip0 = screen.clipToOutline
    private val ground0: Drawable? = backdrop.background
    private val groundColour = (ground0 as? ColorDrawable)?.color
    private var shade: ColorDrawable? = null
    private var at = 0f
    private var touched = false
    private var run: ValueAnimator? = null
    private var corner = 0f
    private val rounded = object : ViewOutlineProvider() {
        override fun getOutline(view: View, outline: Outline) = outline.setRoundRect(0, 0, view.width, view.height, corner)
    }

    private fun apply(p: Float) {
        touched = true
        val s = backScreenScale(p)
        screen.scaleX = s; screen.scaleY = s
        screen.translationX = backScreenShift(p, screen.width.toFloat(), fromLeft, density)
        corner = BACK_SCREEN_CORNER_DP * density * backFraction(p)
        if (screen.outlineProvider !== rounded) { screen.outlineProvider = rounded; screen.clipToOutline = true }
        screen.invalidateOutline()
        groundColour?.let { c ->
            val d = shade ?: ColorDrawable(c).also { shade = it; backdrop.background = it }
            d.color = darken(c, BACK_BACKDROP * backFraction(p))
        }
    }

    private fun stopRun() { val r = run; run = null; r?.cancel() }

    override fun progress(progress: Float) {
        if (!live) return
        stopRun()
        at = backFraction(progress)
        apply(at)
    }

    override fun cancel() {
        if (!live || !touched) return reset()
        stopRun()
        val from = at
        run = Motion.run(Motion.SHORT, Motion.standard, { t -> at = from * (1 - t); apply(at) },
            { if (run != null) { run = null; reset() } })
        if (run == null) reset()  // instant: animations went off meanwhile
    }

    override fun reset() {
        stopRun()
        if (!touched) return
        touched = false
        at = 0f
        screen.scaleX = scaleX0; screen.scaleY = scaleY0
        screen.translationX = shift0
        screen.outlineProvider = outline0
        screen.clipToOutline = clip0
        screen.invalidateOutline()
        if (shade != null) { backdrop.background = ground0; shade = null }
    }
}

/**
 * The picture viewer following a back swipe: the pictures (pager) shrink toward [BACK_SCREEN_SCALE] and the dark
 * behind them lightens toward [BACK_PICTURE_DARK] of what it was. Back going ahead hands over to the viewer's own
 * close, which starts from wherever they are; let go short, they ease back to whole (also from an open cut short
 * by the swipe). With animations off nothing moves.
 */
class PictureBackPreview(private val pager: View, private val dark: View) : BackPreview {
    override val surface: Any get() = pager
    override val keepsOnCommit = true
    private val live = Motion.on()
    private val scale0 = pager.scaleX
    private val dark0 = dark.alpha
    private var run: ValueAnimator? = null
    private var touched = false

    private fun stopRun() { val r = run; run = null; r?.cancel() }

    override fun progress(progress: Float) {
        if (!live) return
        stopRun()
        touched = true
        val s = scale0 * backScreenScale(progress)
        pager.scaleX = s; pager.scaleY = s
        dark.alpha = dark0 * backBetween(1f, BACK_PICTURE_DARK, progress)
    }

    override fun cancel() {
        if (!live || !touched) return
        stopRun()
        val s = pager.scaleX; val x = pager.translationX; val y = pager.translationY; val d = dark.alpha
        run = Motion.run(Motion.MEDIUM, Motion.standard, { t ->
            val k = backBetween(s, 1f, t)
            pager.scaleX = k; pager.scaleY = k
            pager.translationX = x * (1 - t); pager.translationY = y * (1 - t)
            dark.alpha = backBetween(d, 1f, t)
        }, { if (run != null) { run = null; whole() } })
        if (run == null) whole()
    }

    private fun whole() {
        touched = false
        pager.scaleX = 1f; pager.scaleY = 1f; pager.translationX = 0f; pager.translationY = 0f
        dark.alpha = 1f
    }

    override fun reset() {
        stopRun()
        if (touched) whole()
    }
}
