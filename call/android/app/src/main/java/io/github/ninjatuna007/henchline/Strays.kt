package io.github.ninjatuna007.henchline

import org.json.JSONArray
import org.json.JSONObject

/** Chats that spoke or texted lately but aren't running on the laptop now: the laptop names each (its project, as on
 *  the crew) and says why it is off the crew, "ended" (a helper whose work is over: off the list, still searchable)
 *  or "gone" (its agent isn't running: listed, greyed). Only a chat the laptop can't name stays unnamed (the user's
 *  catch, 2026-11-22: real chats said "not on the crew list"). An older laptop sends no such list: every one of
 *  them is then unnamed, as before. */
object Strays {
    /** The project shown for a chat nobody can name. */
    const val UNKNOWN = "agent not running"

    class Stray(val agent: String, val project: String, val state: String)

    /** The project a crew name says: "Claude, in kite designer · its title" → "kite designer". */
    fun projectOf(name: String) =
        name.substringAfter(", ", "").removePrefix("in ").substringBefore(" · ").ifEmpty { "home" }

    /** The laptop's "others", by session; null when the answer has none (an older laptop: keep what was known). */
    fun parse(answer: JSONObject): Map<String, JSONObject>? {
        val list = answer.optJSONArray("others") ?: return null
        return (0 until list.length()).mapNotNull { list.optJSONObject(it) }
            .filter { it.optString("session").isNotEmpty() && it.optString("name").isNotEmpty() }
            .associateBy { it.getString("session") }
    }

    fun store(others: Map<String, JSONObject>): String = JSONArray(others.values.toList()).toString()

    fun load(saved: String?): Map<String, JSONObject> = try {
        parse(JSONObject().put("others", JSONArray(saved ?: "[]"))) ?: emptyMap()
    } catch (_: Exception) { emptyMap() }

    /** How that chat is shown: named by the laptop, or by its key alone. */
    fun describe(session: String, other: JSONObject?): Stray {
        if (other != null) {
            val name = other.getString("name")
            return Stray(name.substringBefore(","), projectOf(name), other.optString("state"))
        }
        return Stray(session.substringBefore("-").replaceFirstChar { it.uppercase() }, UNKNOWN, "")
    }

    /** In the list without a search: not one whose work is over (ended), which a search still finds. */
    fun listed(state: String, endedHere: Boolean, searching: Boolean) = searching || !(endedHere || state == "ended")

    /** The agent's name on its row, saying it isn't running. */
    fun label(agent: String, state: String) = when (state) {
        "ended" -> "$agent (ended)"
        "gone" -> "$agent (not running)"
        else -> agent
    }
}
