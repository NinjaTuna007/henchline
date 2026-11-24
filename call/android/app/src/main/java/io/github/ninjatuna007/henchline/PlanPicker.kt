package io.github.ninjatuna007.henchline

import android.app.Activity
import android.content.Context
import android.text.TextUtils
import android.view.Gravity
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject

/** A plan the user picked in a chat, and which plan that chat had touched last when they picked it. */
data class PlanPick(val file: String, val latest: String)

/**
 * Which of a chat's plans the Plan tab shows (the user's wish, 2026-11-24: a chat can have several). The laptop
 * lists the chat's own plans, the one it touched last first. Without a pick the tab follows the chat; a pick holds
 * until the user picks another or the chat touches some other plan than the one it was on at the pick (then the
 * tab follows the chat again). Picking the plan the chat is on is no pick: it just follows.
 */
object PlanChoice {
    /** The laptop's answer when the plan asked for isn't one of this chat's (call/server.py's chat_plan). */
    const val NOT_ITS_OWN = "That plan isn't one of this chat's."

    /** What to do with an answer to /chat/plan: show it, drop it (a newer ask is on its way), or ask again. */
    enum class Verdict { SHOW, IGNORE, ASK_AGAIN }

    /** The chat's plans from the laptop's answer, the last touched first (none from a laptop that lists none). */
    fun files(answer: JSONObject): List<String> {
        val list = answer.optJSONArray("plans") ?: return emptyList()
        return (0 until list.length()).mapNotNull { list.optJSONObject(it)?.optString("file")?.takeIf(String::isNotEmpty) }
    }

    /** The pick still standing after this answer, or null: the chat moved to another plan or the plan is gone, as
     *  the answer's own list says. An answer with no list (an error, an older laptop) leaves the pick as it is;
     *  only the laptop's "not one of this chat's" drops it too. */
    fun settle(answer: JSONObject, pick: PlanPick?): PlanPick? {
        if (pick == null) return null
        if (!answer.has("plans")) return pick.takeIf { answer.optString("note") != NOT_ITS_OWN }
        val files = files(answer)
        val latest = files.firstOrNull() ?: return null
        if (pick.file !in files) return null
        return pick.takeIf { latest == it.latest }
    }

    /** An answer to an ask that carried `asked` (the picked file, "" for none), while `pick` is the pick now. An
     *  answer to an older ask (the pick changed since it went) is dropped: the ask the change made is on its way. */
    fun verdict(asked: String, pick: PlanPick?, answer: JSONObject): Verdict = when {
        asked != (pick?.file ?: "") -> Verdict.IGNORE
        pick == null -> Verdict.SHOW
        settle(answer, pick) == null -> Verdict.ASK_AGAIN
        else -> Verdict.SHOW
    }

    /** The pick when the user taps `file` among `files`; null when that is the plan the chat is on. */
    fun pick(files: List<String>, file: String): PlanPick? {
        val latest = files.firstOrNull() ?: return null
        return if (file == latest || file !in files) null else PlanPick(file, latest)
    }

    /** The saved picks (oldest first) with this chat's set (as the newest) or cleared, `keep` at most. */
    fun put(saved: JSONArray, session: String, pick: PlanPick?, keep: Int): JSONArray {
        val rows = (0 until saved.length()).mapNotNull { saved.optJSONObject(it) }
            .filter { it.optString("session") != session }.toMutableList()
        if (pick != null) rows.add(JSONObject().put("session", session).put("file", pick.file).put("latest", pick.latest))
        return JSONArray(rows.takeLast(keep))
    }

    /** This chat's pick among the saved ones. */
    fun find(saved: JSONArray, session: String): PlanPick? {
        val row = (0 until saved.length()).mapNotNull { saved.optJSONObject(it) }
            .lastOrNull { it.optString("session") == session } ?: return null
        val file = row.optString("file")
        val latest = row.optString("latest")
        return if (file.isEmpty() || latest.isEmpty()) null else PlanPick(file, latest)
    }
}

/**
 * The picks, remembered per chat on this phone (the newest [KEEP]), and the two calls the chat screen makes: [asked]
 * for what to ask /chat/plan for, then [settle] with what that ask carried and its answer.
 */
object PlanPicks {
    private const val KEY = "plan_picks_v1"
    private const val KEEP = 60

    private fun prefs(context: Context) = context.getSharedPreferences("call", Context.MODE_PRIVATE)

    private fun all(context: Context): JSONArray =
        runCatching { JSONArray(prefs(context).getString(KEY, "[]") ?: "[]") }.getOrDefault(JSONArray())

    fun of(context: Context, session: String): PlanPick? = PlanChoice.find(all(context), session)

    private fun save(context: Context, session: String, pick: PlanPick?) =
        prefs(context).edit().putString(KEY, PlanChoice.put(all(context), session, pick, KEEP).toString()).apply()

    /** The picked plan's file to ask for ("" for none: the chat's latest). */
    fun asked(context: Context, session: String): String = of(context, session)?.file ?: ""

    /** What to do with the answer to an ask that carried `asked`; a pick that no longer stands is forgotten. */
    fun settle(context: Context, session: String, asked: String, answer: JSONObject): PlanChoice.Verdict {
        val verdict = PlanChoice.verdict(asked, of(context, session), answer)
        if (verdict == PlanChoice.Verdict.ASK_AGAIN) save(context, session, null)
        return verdict
    }

    fun choose(context: Context, session: String, files: List<String>, file: String) =
        save(context, session, PlanChoice.pick(files, file))
}

/** The chat's plans to pick from: each with its name and progress, the one showing marked, the chat's latest noted. */
class PlanPickerSheet(
    activity: Activity,
    private val plans: List<JSONObject>,  // {file, name, done, total}, the last touched first
    private val showing: String,
    private val picked: (String) -> Unit,
) : BottomPanel(activity) {
    fun show() {
        val sheet = open("Plans", "This chat's plans, the one it worked on last first")
        val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        plans.forEachIndexed { i, plan ->
            val file = plan.optString("file")
            val on = file == showing
            rows.addView(LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                setPadding(px(16), px(12), px(16), px(12))
                background = rounded(colour(R.color.navy), 14f, if (on) 2 else 0, colour(R.color.cyan))
                addView(LinearLayout(activity).apply {
                    orientation = LinearLayout.VERTICAL
                    addView(TextView(activity).apply {
                        text = plan.optString("name").ifEmpty { file.substringAfterLast('/') }
                        setTextColor(colour(R.color.text)); textSize = 16f; maxLines = 2; ellipsize = TextUtils.TruncateAt.END
                    })
                    addView(TextView(activity).apply {
                        text = describe(plan, latest = i == 0)
                        setTextColor(colour(R.color.muted)); textSize = 13f; setPadding(0, px(2), 0, 0)
                    })
                }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                if (on) addView(TextView(activity).apply { text = "✓"; setTextColor(colour(R.color.cyan)); textSize = 18f })
                contentDescription = plan.optString("name") + ", " + describe(plan, i == 0) + if (on) ", showing" else ""
                isSelected = on
                setOnClickListener { close(); if (!on) picked(file) }
                Motion.ripple(this)
            }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                .apply { bottomMargin = px(6) })
        }
        sheet.addView(ScrollView(activity).apply { isVerticalScrollBarEnabled = false; addView(rows) },
            flexible(14, (activity.resources.displayMetrics.heightPixels * 0.5).toInt()))
        sheet.addView(quiet("Cancel") { close() }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }

    private fun describe(plan: JSONObject, latest: Boolean): String {
        val total = plan.optInt("total")
        val done = plan.optInt("done")
        val progress = when {
            total == 0 -> "No steps"
            done == total -> "All $total done"
            else -> "$done of $total done"
        }
        return if (latest) "$progress · the chat's latest" else progress
    }
}
