package io.github.ninjatuna007.henchline

import android.content.Context
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.ConcurrentHashMap

/**
 * The other paired computers' chats for the home list, grouped by computer (multi-host preview). Each computer's
 * last list is kept on the phone (host-lists.json), so the list draws at once and while that computer is offline;
 * it is asked again while the list is on screen, on a pull, and when one of its pushes arrives. Whether a chat
 * there is unread is this phone's own record of when that chat was last open (seen).
 *
 * The laptop is not among them: it keeps its own list (MainActivity), and is recognised by its learned host id or,
 * while that isn't known, by its address, so it is never listed twice.
 */
object HostLists {
    /** One computer's list as last asked: its chats (as sent), when it last answered and last failed, and how many
     *  of this phone's messages wait for it. */
    data class Look(val chats: List<JSONObject>, val okAt: Long, val failedAt: Long, val waiting: Int) {
        val summaries: List<ChatSummary> by lazy { chats.mapNotNull { runCatching { ChatSummary.of(it) }.getOrNull() } }  // once
    }

    enum class Dot { LIVE, OFFLINE, WAITING, UNKNOWN }

    /** Called on the main thread when a computer's list changed (MainActivity redraws). */
    @Volatile var changed: (() -> Unit)? = null

    // ---- which computers, and how each is shown ------------------------------------------------------------

    /** The paired computers other than the laptop, in pairing order, not those being removed. The laptop is the one
     *  with its learned host id, or at its address; while its id isn't learned yet, also one paired under the same
     *  machine name (laptop.tailnet.ts.net and https://laptop/, another port): in doubt, not "another computer", so
     *  the laptop is never listed twice. */
    fun others(hosts: List<Host>, laptopOrigin: String?, laptopId: String?): List<Host> {
        val laptop = HostPush.origin(laptopOrigin)
        val machine = shortName(laptop)
        return hosts.filter { h ->
            val at = HostPush.origin(h.baseUrl)
            !h.revocationPending && h.hostId != laptopId?.lowercase() && (at == null || at != laptop) &&
                !(laptopId == null && machine != null && shortName(at) == machine)
        }
    }

    /** An address's machine name: its host's first label, lowercased ("laptop" for https://Laptop.x.ts.net:8443/);
     *  null for an IP address or none. */
    fun shortName(origin: String?): String? {
        val host = origin?.let { runCatching { java.net.URI(it).host }.getOrNull() } ?: return null
        if (host.any { it == ':' } || host.split('.').all { part -> part.isNotEmpty() && part.all { it.isDigit() } }) return null
        return host.substringBefore('.').lowercase().ifEmpty { null }
    }

    /** The heading's dot and the few words after the computer's name. */
    fun status(look: Look?, now: Long): Pair<Dot, String> = when {
        look == null || (look.okAt == 0L && look.failedAt == 0L) -> Dot.UNKNOWN to "…"
        look.failedAt > look.okAt -> Dot.OFFLINE to (if (look.okAt == 0L) "can't reach" else "offline · ${ago(look.okAt, now)}")
        look.waiting > 0 -> Dot.WAITING to "${look.waiting} waiting"
        look.summaries.none { it.running } -> Dot.LIVE to "nothing running"
        else -> Dot.LIVE to ""
    }

    fun ago(at: Long, now: Long): String {
        val minutes = ((now - at) / 60_000L).coerceAtLeast(0)
        return when {
            minutes < 1 -> "just now"
            minutes < 60 -> "$minutes min"
            minutes < 48 * 60 -> "${minutes / 60} h"
            else -> "${minutes / (24 * 60)} d"
        }
    }

    /** A chat's name parts on its row: the agent (as the laptop's rows name it) and its project. */
    fun named(c: ChatSummary): Pair<String, String> {
        if (c.agent == "reminders" || HostChatRules.isReminders(c.sessionId)) return "Reminders" to "reminders"
        val agent = c.name.substringBefore(",").trim().ifEmpty { IncomingActivity.nameOf(c.agent) }
        val project = if (", " in c.name) Strays.projectOf(c.name) else c.sessionId.take(32)
        return agent to project
    }

    fun pinKey(hostId: String, session: String) = "$hostId|$session"

    /** A computer's chats as the list draws them: grouped as the sort says (recent: one group; by project; by
     *  agent), newest first in each, each helper right after its boss (nested) when the boss is drawn too. Pinned
     *  ones and their helpers are left out (Pinned draws them); ended ones only in a search. In a search: those
     *  whose name, project or last line matches, pinned or not, flat. */
    fun groups(chats: List<ChatSummary>, sort: String, pinned: Set<String>, query: String):
            List<Pair<String, List<Pair<ChatSummary, Boolean>>>> {
        val q = query.trim().lowercase()
        val all = chats.map { it.sessionId }.toSet()
        val shown = chats.filter { (q.isNotEmpty() || !inPinned(it, pinned, all)) && Strays.listed(it.state, false, q.isNotEmpty()) }
            .sortedByDescending { it.lastAt }
        if (q.isNotEmpty()) {
            val hits = shown.filter { c ->
                val (agent, project) = named(c)
                q in agent.lowercase() || q in project.lowercase() || q in c.name.lowercase() || q in c.lastText.lowercase()
            }
            return if (hits.isEmpty()) emptyList() else listOf("" to hits.map { it to false })
        }
        val sessions = shown.map { it.sessionId }.toSet()
        fun nested(c: ChatSummary) = c.boss.isNotEmpty() && c.boss != c.sessionId && c.boss in sessions
        val grouped = when (sort) {
            "project" -> shown.groupBy { named(it).second }.toList()
            "agent" -> shown.groupBy { named(it).first }.toList()
            else -> if (shown.isEmpty()) emptyList() else listOf("" to shown)
        }
        return grouped.map { (heading, members) ->
            heading to members.filter { !nested(it) }.flatMap { boss ->
                listOf(boss to false) + shown.filter { nested(it) && it.boss == boss.sessionId }.map { it to true }
            }
        }.filter { it.second.isNotEmpty() }
    }

    /** Drawn in Pinned (outside a search): a pinned chat, or a helper whose pinned boss is still in that computer's
     *  list (sessions); a helper whose pinned boss is gone keeps its own place. */
    fun inPinned(c: ChatSummary, pinned: Set<String>, sessions: Set<String>) = c.sessionId in pinned ||
        (c.boss.isNotEmpty() && c.boss != c.sessionId && c.boss in pinned && c.boss in sessions)

    /** The Pinned block's rows: every pinned chat, the laptop's and the other computers', newest first, each followed
     *  by its helpers (not pinned themselves) nested under it, as the laptop's own Pinned group always did. */
    fun <T> pinnedRows(pinned: List<T>, at: (T) -> Long, helpers: (T) -> List<T>): List<Pair<T, Boolean>> =
        pinned.sortedByDescending(at).flatMap { p -> listOf(p to false) + helpers(p).map { it to true } }

    /** What "seen" records for a chat open now: never earlier than its newest line, so a computer whose clock runs
     *  ahead of this phone's can't keep it unread. */
    fun seenStamp(now: Long, newest: Long) = maxOf(now, newest)

    /** The computer a New chat starts on: the one asked for (a heading's, the sheet's row) when it is still paired;
     *  else the one whose chat was opened last, unless it is no longer paired or can't be reached now; else the
     *  laptop. */
    fun newChatOn(asked: String?, remembered: String?, computers: List<String>, offline: Set<String>, laptop: String): String = when {
        asked != null -> asked.takeIf { it in computers } ?: laptop
        remembered != null && remembered in computers && remembered !in offline -> remembered
        else -> laptop
    }

    /** Unread: the agent spoke last, after the chat was last open here. */
    fun unread(c: ChatSummary, seenAt: Long): Boolean = !c.lastMine && c.lastText.isNotEmpty() && c.lastAt > seenAt

    // ---- this phone's records ----------------------------------------------------------------------------

    private const val SEEN = "host_seen"
    private const val COLLAPSED = "collapsed_hosts"

    private fun prefs(context: Context) = context.getSharedPreferences("call", Context.MODE_PRIVATE)

    /** When each chat on another computer was last open here, by pinKey: read once per drawing of the list. */
    fun seen(context: Context): Map<String, Long> = runCatching {
        val all = JSONObject(prefs(context).getString(SEEN, "{}") ?: "{}")
        all.keys().asSequence().associateWith { all.optLong(it) }
    }.getOrDefault(emptyMap())

    /** That chat was open on this phone just now: what it holds is read, up to its newest line as kept. */
    fun markSeen(context: Context, hostId: String, session: String, at: Long = System.currentTimeMillis()) = synchronized(this) {
        val newest = cached(context)[hostId]?.summaries?.firstOrNull { it.sessionId == session }?.lastAt ?: 0L
        val seen = runCatching { JSONObject(prefs(context).getString(SEEN, "{}") ?: "{}") }.getOrDefault(JSONObject())
        seen.put(pinKey(hostId, session), seenStamp(at, newest))
        prefs(context).edit().putString(SEEN, seen.toString()).apply()
    }

    fun collapsed(context: Context): Set<String> = prefs(context).getStringSet(COLLAPSED, emptySet())?.toSet() ?: emptySet()

    fun setCollapsed(context: Context, key: String, on: Boolean) {
        val next = collapsed(context).toMutableSet()
        if (on) next.add(key) else next.remove(key)
        prefs(context).edit().putStringSet(COLLAPSED, next).apply()
    }

    // ---- the kept lists, and asking again --------------------------------------------------------------

    private fun file(context: Context) = File(context.filesDir, "host-lists.json")

    @Volatile private var memory: Map<String, Look>? = null  // the kept file, read once per process

    /** Each computer's list as last kept: from memory, the file read only the first time. */
    fun cached(context: Context): Map<String, Look> = memory ?: synchronized(this) {
        memory ?: read(context).also { memory = it }
    }

    private fun read(context: Context): Map<String, Look> {
        val all = runCatching { JSONObject(file(context).readText()) }.getOrDefault(JSONObject())
        return all.keys().asSequence().mapNotNull { id ->
            val one = all.optJSONObject(id) ?: return@mapNotNull null
            val list = one.optJSONArray("chats") ?: JSONArray()
            id to Look((0 until list.length()).mapNotNull { list.optJSONObject(it) }, one.optLong("ok_at"),
                one.optLong("failed_at"), one.optInt("waiting"))
        }.toMap()
    }

    private fun keep(context: Context, hostId: String, look: Look, hosts: Set<String>) = synchronized(this) {
        val next = (cached(context) + (hostId to look)).filterKeys { it in hosts }  // a computer no longer paired goes
        val all = JSONObject()
        for ((id, one) in next) all.put(id, JSONObject().put("chats", JSONArray(one.chats)).put("ok_at", one.okAt)
            .put("failed_at", one.failedAt).put("waiting", one.waiting))
        val tmp = File(file(context).path + ".tmp")
        tmp.writeText(all.toString())
        tmp.renameTo(file(context))
        memory = next
    }

    private val asking = ConcurrentHashMap.newKeySet<String>()

    /** Ask that computer for its chats now, off the main thread, one look at a time per computer; `changed` runs
     *  when what is kept changed. */
    fun refresh(context: Context, host: Host) {
        if (Build.VERSION.SDK_INT < MultiHost.MIN_SDK || !asking.add(host.hostId)) return
        val app = context.applicationContext
        Thread {
            try {
                val before = cached(app)[host.hostId]
                val now = System.currentTimeMillis()
                val waiting = runCatching { Store(app).use { it.waiting(host.hostId).size } }.getOrDefault(before?.waiting ?: 0)
                val look = try {
                    val chats = HostChat(HostClient(host, HostKeys.forAuthority("host", host.hostId)), host.hostId).listed()
                    Look(chats, now, before?.failedAt ?: 0L, waiting)
                } catch (e: Exception) {
                    Look(before?.chats ?: emptyList(), before?.okAt ?: 0L, now, waiting)
                }
                val paired = runCatching { HostRegistry.of(app).all().map { it.hostId }.toSet() }.getOrDefault(setOf(host.hostId))
                keep(app, host.hostId, look, paired + host.hostId)
                if (before == null || before.chats.toString() != look.chats.toString() || before.waiting != look.waiting ||
                    (before.failedAt > before.okAt) != (look.failedAt > look.okAt)) {
                    android.os.Handler(android.os.Looper.getMainLooper()).post { changed?.invoke() }
                }
            } finally {
                asking.remove(host.hostId)
            }
        }.start()
    }

    /** One of that computer's pushes arrived: its list is asked for again. */
    fun pushed(context: Context, host: Host) {
        if (MultiHost.enabled(context)) refresh(context, host)
    }
}

/** A short sheet of actions (a computer's heading, a chat on another computer, long-pressed). */
class ActionsSheet(activity: android.app.Activity, private val title: String, private val subtitle: String,
                   private val actions: List<Triple<String, String, () -> Unit>>) : BottomPanel(activity) {
    fun show() {
        val sheet = open(title, subtitle)
        sheet.addView(android.view.View(activity), android.widget.LinearLayout.LayoutParams(1, px(12)))
        for ((icon, name, then) in actions) sheet.addView(actionRow(icon, name, "", R.color.text) { close(); then() })
    }
}
