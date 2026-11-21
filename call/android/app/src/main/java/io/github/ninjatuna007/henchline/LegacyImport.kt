package io.github.ninjatuna007.henchline

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * The single-laptop app's data, read (never changed) and planned as host-qualified rows for the multi-host store.
 * Pure Kotlin over files and a preferences map, so the plan is tested on the JVM against
 * tests/fixtures/phone-legacy; Store applies it in one SQLite transaction.
 *
 * What is read: filesDir/chats/<session>.jsonl (one JSON line per message, kept verbatim, even a line that isn't
 * JSON), chats/media.json, calls.jsonl, outbox.json, and the "call" preferences that belong to a chat (crew,
 * current, pinned, seen_*, helpers_seen_*, model_*). Global preferences (sound, theme, names…) stay global.
 * A chat file's name is the session id with every character outside [A-Za-z0-9._-] made "_": the real id is taken
 * from the ids the app still knows (crew, outbox, media, preferences) when exactly one of them maps to that name;
 * otherwise the file name stays the key and the chat is marked unresolved, never guessed.
 */
object LegacyImport {
    data class Chat(val key: String, val session: String?, val file: String, val lines: Int)
    data class Line(val chat: String, val ordinal: Int, val at: Long?, val raw: String)
    data class Pref(val chat: String?, val name: String, val value: String)  // chat null: crew list, current
    data class Outboxed(val position: Int, val requestId: String, val session: String, val raw: String)

    data class Plan(
        val hostId: String,
        val chats: List<Chat>,
        val lines: List<Line>,
        val outbox: List<Outboxed>,
        val calls: List<String>,     // calls.jsonl lines, verbatim, in order
        val media: String?,          // chats/media.json verbatim
        val thumbs: List<String>,    // chats/thumbs/* file names (the files stay where they are)
        val prefs: List<Pref>,
        val sources: Map<String, String>,  // legacy file (relative) -> SHA-256: proof they were not changed
    ) {
        val counts: Map<String, Int> get() = mapOf("chats" to chats.size, "lines" to lines.size, "outbox" to outbox.size,
            "calls" to calls.size, "media" to (if (media == null) 0 else 1), "thumbs" to thumbs.size, "prefs" to prefs.size)

        /** One digest over every planned row, in order: the store recomputes it from what it wrote. */
        val digest: String get() = digestOf(rows())

        fun rows(): List<String> = buildList {
            chats.forEach { add("chat\u0000${it.key}\u0000${it.session ?: ""}\u0000${it.file}\u0000${it.lines}") }
            lines.forEach { add("line\u0000${it.chat}\u0000${it.ordinal}\u0000${it.at ?: ""}\u0000${it.raw}") }
            outbox.forEach { add("outbox\u0000${it.position}\u0000${it.requestId}\u0000${it.session}\u0000${it.raw}") }
            calls.forEachIndexed { i, raw -> add("call\u0000$i\u0000$raw") }
            media?.let { add("media\u0000$it") }
            thumbs.forEach { add("thumb\u0000$it") }
            prefs.forEach { add("pref\u0000${it.chat ?: ""}\u0000${it.name}\u0000${it.value}") }
        }
    }

    fun digestOf(rows: List<String>): String {
        val sha = MessageDigest.getInstance("SHA-256")
        rows.forEach { sha.update(it.toByteArray(Charsets.UTF_8)); sha.update(byteArrayOf(10)) }
        return sha.digest().joinToString("") { "%02x".format(it) }
    }

    fun sanitized(session: String) = session.replace(Regex("[^A-Za-z0-9._-]"), "_")

    private fun sha256(file: File) = MessageDigest.getInstance("SHA-256").digest(file.readBytes()).joinToString("") { "%02x".format(it) }

    private val CHAT_PREFS = listOf("seen_", "helpers_seen_", "model_")

    /** prefs: the "call" preferences as SharedPreferences.getAll() gives them (String, Long, Boolean, Set<String>…). */
    fun plan(filesDir: File, prefs: Map<String, Any?>, hostId: String): Plan {
        val sources = sortedMapOf<String, String>()
        fun read(rel: String): String? = File(filesDir, rel).takeIf { it.isFile }?.let { sources[rel] = sha256(it); it.readText() }

        // Every session id the app still knows by its real spelling
        val known = linkedSetOf<String>()
        prefs.keys.forEach { name -> CHAT_PREFS.firstOrNull { name.startsWith(it) }?.let { known += name.removePrefix(it) } }
        (prefs["current"] as? String)?.takeIf { it.isNotEmpty() }?.let { known += it }
        (prefs["pinned"] as? Set<*>)?.forEach { if (it is String) known += it }
        val crew = (prefs["crew"] as? String)?.let { runCatching { JSONArray(it) }.getOrNull() }
        crew?.let { for (i in 0 until it.length()) it.optJSONObject(i)?.optString("session")?.takeIf { s -> s.isNotEmpty() }?.let { s -> known += s } }
        val outboxText = read("outbox.json")
        val outboxItems = outboxText?.let { runCatching { JSONArray(it) }.getOrNull() }
        outboxItems?.let { for (i in 0 until it.length()) it.optJSONObject(i)?.optString("session")?.takeIf { s -> s.isNotEmpty() }?.let { s -> known += s } }
        val media = read("chats/media.json")
        media?.let { runCatching { JSONObject(it) }.getOrNull() }?.let { all ->
            all.keys().forEach { k -> all.optJSONObject(k)?.optJSONArray("sessions")?.let { a -> for (i in 0 until a.length()) known += a.optString(i) } }
        }
        val byName = known.filter { it.isNotEmpty() }.groupBy { sanitized(it) }

        val chats = ArrayList<Chat>()
        val lines = ArrayList<Line>()
        // By key (what the store orders by), not by file name: "a-b.jsonl" sorts before "a.jsonl", "a" before "a-b"
        File(filesDir, "chats").listFiles { f -> f.isFile && f.name.endsWith(".jsonl") }.orEmpty()
            .sortedBy { it.name.removeSuffix(".jsonl") }.forEach { file ->
            val key = file.name.removeSuffix(".jsonl")
            val text = read("chats/${file.name}") ?: return@forEach
            val raw = text.split("\n").filter { it.isNotBlank() }
            raw.forEachIndexed { i, line ->
                val at = runCatching { JSONObject(line) }.getOrNull()?.let { if (it.has("at")) it.optLong("at") else null }
                lines += Line(key, i, at, line)
            }
            // Without "_" the name can only be the id itself (the rewrite only ever adds underscores)
            val session = byName[key]?.singleOrNull() ?: key.takeIf { !it.contains('_') && byName[key] == null }
            chats += Chat(key, session, "chats/${file.name}", raw.size)
        }

        val outbox = ArrayList<Outboxed>()
        outboxItems?.let { for (i in 0 until it.length()) it.optJSONObject(i)?.let { item ->
            outbox += Outboxed(i, item.optString("id"), item.optString("session"), item.toString())
        } }

        val calls = read("calls.jsonl")?.split("\n")?.filter { it.isNotBlank() } ?: emptyList()
        val thumbs = File(filesDir, "chats/thumbs").listFiles()?.filter { it.isFile }?.map { it.name }?.sorted() ?: emptyList()

        val chatPrefs = ArrayList<Pref>()
        prefs.keys.sorted().forEach { name ->
            val value = prefs[name]
            val prefix = CHAT_PREFS.firstOrNull { name.startsWith(it) }
            when {
                prefix != null -> chatPrefs += Pref(name.removePrefix(prefix), prefix.removeSuffix("_"), value.toString())
                name == "pinned" && value is Set<*> -> value.map { it.toString() }.sorted().forEach { chatPrefs += Pref(it, "pinned", "true") }
                name == "crew" || name == "current" || name == "current_name" -> chatPrefs += Pref(null, name, value.toString())
            }
        }
        return Plan(hostId, chats, lines, outbox, calls, media, thumbs, chatPrefs, sources)
    }

    /** Same SHA-256 for every legacy file the plan read: the import changed nothing of the old app's data. */
    fun unchanged(filesDir: File, plan: Plan): Boolean =
        plan.sources.all { (rel, digest) -> File(filesDir, rel).let { it.isFile && sha256(it) == digest } }
}
