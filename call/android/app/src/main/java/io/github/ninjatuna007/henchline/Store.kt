package io.github.ninjatuna007.henchline

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * The multi-host store: every row keyed by host. For now it holds the single-laptop data imported for the first
 * paired computer (LegacyImport); the screens still read the old files until a later step moves them over.
 * The old files are only ever read: rollback is switching the preview off, never restoring an old copy.
 */
class Store(context: Context, name: String = NAME) : SQLiteOpenHelper(context, name, null, VERSION) {
    class MigrationRefused(why: String) : Exception(why)

    override fun onConfigure(db: SQLiteDatabase) { db.setForeignKeyConstraintsEnabled(true) }

    override fun onCreate(db: SQLiteDatabase) {
        listOf(
            """CREATE TABLE migration(host_id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('verified')),
                digest TEXT NOT NULL, counts TEXT NOT NULL, sources TEXT NOT NULL, at INTEGER NOT NULL)""",
            """CREATE TABLE chats(host_id TEXT NOT NULL, chat_key TEXT NOT NULL, session_id TEXT, legacy_file TEXT,
                lines INTEGER NOT NULL, PRIMARY KEY(host_id, chat_key))""",
            """CREATE TABLE lines(host_id TEXT NOT NULL, chat_key TEXT NOT NULL, ordinal INTEGER NOT NULL, at INTEGER,
                raw TEXT NOT NULL, PRIMARY KEY(host_id, chat_key, ordinal),
                FOREIGN KEY(host_id, chat_key) REFERENCES chats(host_id, chat_key))""",
            """CREATE TABLE outbox(host_id TEXT NOT NULL, position INTEGER NOT NULL, request_id TEXT NOT NULL,
                session_id TEXT NOT NULL, raw TEXT NOT NULL, PRIMARY KEY(host_id, position))""",
            "CREATE TABLE calls(host_id TEXT NOT NULL, ordinal INTEGER NOT NULL, raw TEXT NOT NULL, PRIMARY KEY(host_id, ordinal))",
            "CREATE TABLE media(host_id TEXT PRIMARY KEY, raw TEXT NOT NULL)",
            "CREATE TABLE thumbs(host_id TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(host_id, name))",
            """CREATE TABLE chat_prefs(host_id TEXT NOT NULL, chat_key TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(host_id, chat_key, name))""",
        ).forEach { db.execSQL(it) }
    }

    override fun onUpgrade(db: SQLiteDatabase, old: Int, new: Int) {
        throw IllegalStateException("no upgrade from store version $old to $new yet")
    }

    /** The host the old data went to, if it was imported. */
    fun migratedTo(): String? = readableDatabase.rawQuery("SELECT host_id FROM migration", null).use {
        if (it.moveToFirst()) it.getString(0) else null
    }

    /** Import the plan in one transaction, then read back what was written and compare it with the plan. Again
     *  for the same host and the same data: nothing to do. Another host, or the same host with different data:
     *  refused, and nothing changes. */
    fun apply(plan: LegacyImport.Plan) {
        val db = writableDatabase
        db.beginTransactionNonExclusive()  // IMMEDIATE: the check and the writes are one
        try {
            db.rawQuery("SELECT host_id, digest FROM migration", null).use {
                if (it.moveToFirst()) {
                    if (it.getString(0) == plan.hostId && it.getString(1) == plan.digest) { db.setTransactionSuccessful(); return }
                    throw MigrationRefused(if (it.getString(0) != plan.hostId) "the old data already went to another computer"
                                           else "the old data changed since it was imported")
                }
            }
            val h = plan.hostId
            plan.chats.forEach { insert(db, "chats", "host_id" to h, "chat_key" to it.key, "session_id" to it.session,
                "legacy_file" to it.file, "lines" to it.lines) }
            plan.lines.forEach { insert(db, "lines", "host_id" to h, "chat_key" to it.chat, "ordinal" to it.ordinal,
                "at" to it.at, "raw" to it.raw) }
            plan.outbox.forEach { insert(db, "outbox", "host_id" to h, "request_id" to it.requestId,
                "position" to it.position, "session_id" to it.session, "raw" to it.raw) }
            plan.calls.forEachIndexed { i, raw -> insert(db, "calls", "host_id" to h, "ordinal" to i, "raw" to raw) }
            plan.media?.let { insert(db, "media", "host_id" to h, "raw" to it) }
            plan.thumbs.forEach { insert(db, "thumbs", "host_id" to h, "name" to it) }
            plan.prefs.forEach { insert(db, "chat_prefs", "host_id" to h, "chat_key" to (it.chat ?: ""), "name" to it.name,
                "value" to it.value) }
            val written = readBack(db, h)
            if (LegacyImport.digestOf(written) != plan.digest) throw MigrationRefused("what was written doesn't match the old data")
            insert(db, "migration", "host_id" to h, "state" to "verified", "digest" to plan.digest,
                "counts" to org.json.JSONObject(plan.counts).toString(), "sources" to org.json.JSONObject(plan.sources.toMap()).toString(),
                "at" to System.currentTimeMillis())
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    private fun insert(db: SQLiteDatabase, table: String, vararg values: Pair<String, Any?>) {
        val row = ContentValues()
        values.forEach { (k, v) ->
            when (v) {
                null -> row.putNull(k); is String -> row.put(k, v); is Int -> row.put(k, v); is Long -> row.put(k, v)
                else -> row.put(k, v.toString())
            }
        }
        db.insertOrThrow(table, null, row)
    }

    /** The rows for that host in the plan's own order and form (LegacyImport.Plan.rows). */
    private fun readBack(db: SQLiteDatabase, h: String): List<String> = buildList {
        fun each(sql: String, row: (android.database.Cursor) -> String) =
            db.rawQuery(sql, arrayOf(h)).use { c -> while (c.moveToNext()) add(row(c)) }
        fun s(c: android.database.Cursor, i: Int) = if (c.isNull(i)) "" else c.getString(i)
        each("SELECT chat_key, session_id, legacy_file, lines FROM chats WHERE host_id=? ORDER BY chat_key") {
            "chat\u0000${s(it, 0)}\u0000${s(it, 1)}\u0000${s(it, 2)}\u0000${it.getInt(3)}" }
        each("SELECT chat_key, ordinal, at, raw FROM lines WHERE host_id=? ORDER BY chat_key, ordinal") {
            "line\u0000${s(it, 0)}\u0000${it.getInt(1)}\u0000${s(it, 2)}\u0000${s(it, 3)}" }
        each("SELECT position, request_id, session_id, raw FROM outbox WHERE host_id=? ORDER BY position") {
            "outbox\u0000${it.getInt(0)}\u0000${s(it, 1)}\u0000${s(it, 2)}\u0000${s(it, 3)}" }
        each("SELECT ordinal, raw FROM calls WHERE host_id=? ORDER BY ordinal") { "call\u0000${it.getInt(0)}\u0000${s(it, 1)}" }
        each("SELECT raw FROM media WHERE host_id=?") { "media\u0000${s(it, 0)}" }
        each("SELECT name FROM thumbs WHERE host_id=? ORDER BY name") { "thumb\u0000${s(it, 0)}" }
        each("SELECT chat_key, name, value FROM chat_prefs WHERE host_id=? ORDER BY rowid") {
            "pref\u0000${s(it, 0)}\u0000${s(it, 1)}\u0000${s(it, 2)}" }
    }

    companion object {
        const val NAME = "henchline-v2.db"
        const val VERSION = 1
    }
}
