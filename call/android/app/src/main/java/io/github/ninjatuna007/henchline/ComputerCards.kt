package io.github.ninjatuna007.henchline

import android.content.Context
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject

/** A computer's colour in the list: its card's tint, its heading's square, the dot on its pinned rows. */
enum class Accent(val colourId: Int, val label: String) {
    LAVENDER(R.color.host_lavender, "Lavender"),
    CORAL(R.color.host_coral, "Coral"),
    SKY(R.color.host_sky, "Sky"),
    ROSE(R.color.host_rose, "Rose"),
    SAND(R.color.host_sand, "Sand"),
}

/** How the home list draws each computer (multi-host): a card of its own, tinted with its colour, that folds to a
 *  pill of its heading. The sizes and timings, and the fold's frames, as plain numbers. */
object ComputerCards {
    /** How much of its colour a card's surface takes. */
    const val TINT = 0.06f
    const val RADIUS_DP = 20      // an open card's corners
    const val GAP_DP = 12         // around and between the cards
    const val HEADING_DP = 56     // the heading's height; a folded card is this tall, so its pill's radius is half
    const val SQUARE_DP = 32      // the heading's coloured square
    const val PIN_DOT_DP = 6      // on a pinned row from another computer

    /** Unfolding grows over 400 ms, folding closes in 250. */
    const val OPEN_MS = Motion.LONGER
    const val CLOSE_MS = 250L

    /** More rows than this fade out or in together instead of the card's height moving (a tall card relaid out
     *  every frame would stutter). */
    const val CROSSFADE_ROWS = 30

    /** The colour of the computer at order (-1: the laptop, else its place among the others, by pairing order):
     *  the one the user chose (by name), else the laptop's lavender, then coral, sky, rose and sand in turn. */
    fun accentOf(chosen: String?, order: Int): Accent =
        Accent.values().firstOrNull { it.name == chosen } ?: defaultAccent(order)

    fun defaultAccent(order: Int): Accent =
        if (order < 0) Accent.LAVENDER else listOf(Accent.CORAL, Accent.SKY, Accent.ROSE, Accent.SAND)[order % 4]

    /** Every computer's colour: the laptop (key laptop) and the others (in pairing order). One the user chose keeps
     *  it; the rest take their default ([defaultAccent]) unless another computer already has that colour (chosen,
     *  or given to one before it), then the next free one after it; when all five are taken, the default. */
    fun accents(laptop: String, others: List<String>, chosen: Map<String, String>): Map<String, Accent> {
        val keys = listOf(laptop) + others
        val out = LinkedHashMap<String, Accent>()
        for (key in keys) Accent.values().firstOrNull { it.name == chosen[key] }?.let { out[key] = it }
        val all = Accent.values()
        keys.forEachIndexed { i, key ->
            if (key in out) return@forEachIndexed
            val first = defaultAccent(i - 1)
            val taken = out.values.toSet()
            out[key] = (0 until all.size).map { all[(first.ordinal + it) % all.size] }.firstOrNull { it !in taken } ?: first
        }
        return keys.associateWith { out.getValue(it) }
    }

    /** What a screen reader says for a computer's heading: its name, its state words, the unread count (shown only
     *  while folded), and whether it is folded. */
    fun describe(label: String, words: String, unread: Int, open: Boolean): String =
        listOfNotNull(label, words.trim().takeIf { it.isNotEmpty() && it != "…" },
            if (!open && unread > 0) "$unread unread" else null, if (open) "open" else "folded").joinToString(", ")

    /** over laid on base at share (0..1) of its strength, as an opaque colour. */
    fun mix(base: Int, over: Int, share: Float): Int {
        fun ch(shift: Int): Int {
            val a = (base shr shift) and 0xFF
            val b = (over shr shift) and 0xFF
            return (a + (b - a) * share + 0.5f).toInt().coerceIn(0, 255)
        }
        return (0xFF shl 24) or (ch(16) shl 16) or (ch(8) shl 8) or ch(0)
    }

    fun foldMs(opening: Boolean) = if (opening) OPEN_MS else CLOSE_MS

    fun crossfades(rows: Int) = rows > CROSSFADE_ROWS

    /** The fold's frames, by how open the card is (0 folded, 1 open). The chevron points down when folded (more
     *  to see) and has turned half a turn, up, when open. */
    fun chevronAngle(openness: Float) = 180f * openness.coerceIn(0f, 1f)

    /** The card's corner radius: its open corners, and a pill (half its heading) when folded. */
    fun cornerAt(openness: Float, open: Float, folded: Float) = folded + (open - folded) * openness.coerceIn(0f, 1f)

    /** The rows' opacity at t (0..1) of a fold: unfolding, they come in once there is room for them (the last
     *  70%); folding, they are gone by the time it is half shut. */
    fun rowsAlpha(t: Float, opening: Boolean): Float =
        if (opening) ((t - 0.3f) / 0.7f).coerceIn(0f, 1f) else (1f - t / 0.5f).coerceIn(0f, 1f)

    /** The rows' height at t of a fold, from to to (px). */
    fun heightAt(t: Float, from: Int, to: Int): Int = (from + (to - from) * t.coerceIn(0f, 1f) + 0.5f).toInt().coerceAtLeast(0)

    // ---- the user's choice of colour ---------------------------------------------------------------------

    private const val ACCENTS = "computer_accents"  // computer key (the laptop's, or a host id) -> Accent name

    private fun prefs(context: Context) = context.getSharedPreferences("call", Context.MODE_PRIVATE)

    fun chosen(context: Context): Map<String, String> = runCatching {
        val all = JSONObject(prefs(context).getString(ACCENTS, "{}") ?: "{}")
        all.keys().asSequence().associateWith { all.optString(it) }
    }.getOrDefault(emptyMap())

    fun choose(context: Context, key: String, accent: Accent) {
        val all = runCatching { JSONObject(prefs(context).getString(ACCENTS, "{}") ?: "{}") }.getOrDefault(JSONObject())
        prefs(context).edit().putString(ACCENTS, all.put(key, accent.name).toString()).apply()
    }
}

/** Holds back redraws of the list while a card folds or unfolds (a poll or a push redrawing mid-fold would cut it
 *  short): asked to draw then, it says no and remembers; when the last fold ends, it says whether one was asked. */
class RedrawGuard {
    private var running = 0
    private var pending = false

    val busy get() = running > 0

    fun begin() { running++ }

    /** Draw now? False while a fold runs (and the redraw is owed). */
    fun ask(): Boolean {
        if (running > 0) { pending = true; return false }
        return true
    }

    /** A fold stopped short (another tap, a tab change, the screen gone): nothing runs and nothing is owed; the
     *  caller draws the list itself. */
    fun reset() { running = 0; pending = false }

    /** A fold ended: true when it was the last and a redraw was held back meanwhile. */
    fun end(): Boolean {
        running = (running - 1).coerceAtLeast(0)
        if (running == 0 && pending) { pending = false; return true }
        return false
    }
}

/** Picks a computer's colour: the five, the current one ticked. */
class AccentSheet(activity: android.app.Activity, private val label: String, private val current: Accent,
                  private val then: (Accent) -> Unit) : BottomPanel(activity) {
    fun show() {
        val sheet = open("Colour", label)
        sheet.addView(View(activity), LinearLayout.LayoutParams(1, px(12)))
        for (accent in Accent.values()) sheet.addView(LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setPadding(px(14), px(14), px(14), px(14))
            background = rounded(colour(R.color.navy), 16f)
            addView(View(activity).apply {
                background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(colour(accent.colourId)) }
            }, LinearLayout.LayoutParams(px(22), px(22)).apply { marginEnd = px(16) })
            addView(TextView(activity).apply {
                text = accent.label; setTextColor(colour(R.color.text)); textSize = 16f
                typeface = android.graphics.Typeface.DEFAULT_BOLD
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            if (accent == current) addView(TextView(activity).apply {
                text = "✓"; setTextColor(colour(accent.colourId)); textSize = 18f
                importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            })
            // Read out as the chosen one, not only marked with a tick
            isSelected = accent == current
            contentDescription = accent.label
            if (android.os.Build.VERSION.SDK_INT >= 30) stateDescription = if (accent == current) "Selected" else "Not selected"
            setOnClickListener { close(); then(accent) }
            Motion.ripple(this)
        }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
            bottomMargin = px(8)
        })
    }
}
