package io.github.ninjatuna007.henchline

import android.app.NotificationManager
import android.content.Context
import org.json.JSONObject

/**
 * The ring now under way, if any, for as long as it rings: so the app shows it when opened, a call made by hand
 * while it rings counts as answering it, and answering by any way stops every part of it (2026-11-15: a call
 * made from the app left the ring sounding).
 */
object Ringing {
    private const val KEY = "ringing"

    /** host: a ring from another paired computer (multi-host): its id, the agent's session there, its name. */
    fun started(context: Context, agent: String, why: String, reply: String?, host: RingHost? = null) {
        prefs(context).edit().putString(KEY, JSONObject().put("agent", agent).put("why", why).put("reply", reply ?: "")
            .put("until", System.currentTimeMillis() + RingService.RING_SECONDS * 1000)
            .apply { host?.let { put("host_id", it.hostId).put("session", it.session).put("host_label", it.label) } }
            // Every ring, the laptop's too: only the app's own Answer (RingService.answerIntent, an immutable explicit
            // PendingIntent) carries it, since MainActivity and the chat screen are reachable by other apps and links
            .put("answer_key", java.util.UUID.randomUUID().toString())
            .toString()).apply()
    }

    /** The computer a ring comes from when it isn't the laptop: null for the laptop's (every ring until multi-host). */
    fun hostOf(ring: JSONObject?): RingHost? = ring?.optString("host_id")?.takeIf { it.isNotEmpty() }
        ?.let { RingHost(it, ring.optString("session"), ring.optString("host_label")) }

    /** Whether a chat screen opened with these may answer the ring sounding now: it is that computer's ring for that
     *  chat, with that reply, and the key matches the one only the app's own Answer carries. */
    fun mayAnswer(ring: JSONObject?, hostId: String, session: String, reply: String?, key: String): Boolean {
        val host = hostOf(ring) ?: return false
        val kept = ring!!.optString("answer_key")
        return host.hostId == hostId && host.session == session && ring.optString("reply") == (reply ?: "") &&
            key.isNotEmpty() && java.security.MessageDigest.isEqual(kept.toByteArray(), key.toByteArray())
    }

    /** Whether MainActivity's ANSWER may start the laptop's call: the laptop's ring sounds now and the key is the one
     *  only the app's own Answer carries (any installed app can send ANSWER to the exported MainActivity). */
    fun mayAnswerLaptop(ring: JSONObject?, key: String): Boolean {
        if (ring == null || hostOf(ring) != null || key.isEmpty()) return false
        val kept = ring.optString("answer_key")
        return kept.isNotEmpty() && java.security.MessageDigest.isEqual(kept.toByteArray(), key.toByteArray())
    }

    /** The key of the ring sounding now, for its Answer. */
    fun answerKey(ring: JSONObject?): String = ring?.optString("answer_key") ?: ""

    /** The ring sounding now if it is the laptop's: what the laptop's screens may answer. */
    fun laptops(context: Context): JSONObject? = now(context)?.takeIf { hostOf(it) == null }

    /** The ring still sounding: {agent, why, reply, until}, or null. */
    fun now(context: Context): JSONObject? {
        val ring = prefs(context).getString(KEY, null)?.let { try { JSONObject(it) } catch (_: Exception) { null } } ?: return null
        return ring.takeIf { it.optLong("until") > System.currentTimeMillis() }
    }

    fun secondsLeft(context: Context): Long = ((now(context)?.optLong("until") ?: 0) - System.currentTimeMillis()) / 1000

    /** Answered, declined or over: the notification, the ring screen and the record all go. */
    fun end(context: Context) {
        prefs(context).edit().remove(KEY).apply()
        context.getSystemService(NotificationManager::class.java).cancel(RingService.NOTIFICATION)
        IncomingActivity.showing?.get()?.let { it.runOnUiThread { it.quietly() } }
    }

    private fun prefs(context: Context) = context.getSharedPreferences("call", Context.MODE_PRIVATE)
}

/** Another paired computer ringing: who, and which agent's chat a call to it opens with. */
data class RingHost(val hostId: String, val session: String, val label: String)
