package io.github.ninjatuna007.henchline

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

/** The import into SQLite on the phone, from the synthetic legacy fixtures. Its own database and a temp folder:
 *  the app's real data is never read or written. */
@RunWith(AndroidJUnit4::class)
class StoreTest {
    private val target = InstrumentationRegistry.getInstrumentation().targetContext
    private val assets = InstrumentationRegistry.getInstrumentation().context.assets
    private val name = "henchline-v2-test-${UUID.randomUUID()}.db"
    private lateinit var dir: File
    private lateinit var store: Store

    private fun copy(path: String, to: File) {
        val children = assets.list(path).orEmpty()
        if (children.isEmpty()) { to.parentFile?.mkdirs(); assets.open(path).use { i -> to.outputStream().use { i.copyTo(it) } } }
        else children.forEach { copy("$path/$it", File(to, it)) }
    }

    private fun prefs(): Map<String, Any?> {
        val raw = JSONObject(assets.open("phone-legacy/prefs-call.json").bufferedReader().readText())
        return raw.keys().asSequence().associateWith { k ->
            when (val v = raw.get(k)) { is JSONArray -> (0 until v.length()).map { v.getString(it) }.toSet(); is Int -> v.toLong(); else -> v }
        }
    }

    @Before fun setUp() {
        dir = File(target.cacheDir, "legacy-test-${UUID.randomUUID()}")
        copy("phone-legacy/files", dir)
        store = Store(target, name)
    }

    @After fun tearDown() {
        store.close()
        target.deleteDatabase(name)
        dir.deleteRecursively()
    }

    @Test fun queuesArePerHostAndOrdered() {
        val a = "00000000-0000-4000-8000-00000000000a"
        val b = "00000000-0000-4000-8000-00000000000b"
        val first = store.enqueue(a, "r1", "c1", "s", "{}")
        store.enqueue(b, "r2", "c2", "s", "{}")
        val third = store.enqueue(a, "r3", "c3", "s", "{\"x\":1}")
        assertEquals(listOf(0L, 1L), store.waiting(a).map { it.position })
        assertEquals(listOf("r1", "r3"), store.waiting(a).map { it.requestId })
        store.settle(first.requestId, "sent")
        store.settle(first.requestId, "failed")  // settled once: a later outcome doesn't overwrite it
        assertEquals(listOf(third), store.waiting(a))
        assertEquals(1, store.waiting(b).size)
    }

    @Test fun aVersionOneStoreGainsItsQueues() {
        val old = "henchline-v2-old-${UUID.randomUUID()}.db"
        try {
            val v1 = object : android.database.sqlite.SQLiteOpenHelper(target, old, null, 1) {
                override fun onCreate(db: android.database.sqlite.SQLiteDatabase) {
                    db.execSQL("CREATE TABLE migration(host_id TEXT PRIMARY KEY, state TEXT NOT NULL, digest TEXT NOT NULL, counts TEXT NOT NULL, sources TEXT NOT NULL, at INTEGER NOT NULL)")
                }
                override fun onUpgrade(db: android.database.sqlite.SQLiteDatabase, o: Int, n: Int) {}
            }
            v1.writableDatabase.close(); v1.close()
            Store(target, old).use { assertEquals(0, it.waiting("x").size); it.enqueue("x", "r", "c", "s", "{}") }
        } finally {
            target.deleteDatabase(old)
        }
    }

    @Test fun importVerifiesAndRunsOnce() {
        val host = "00000000-0000-4000-8000-000000000001"
        val plan = LegacyImport.plan(dir, prefs(), host)
        store.apply(plan)
        assertEquals(host, store.migratedTo())
        store.apply(LegacyImport.plan(dir, prefs(), host))  // the same again: nothing to do
        assertTrue(LegacyImport.unchanged(dir, plan))
        try { store.apply(LegacyImport.plan(dir, prefs(), "00000000-0000-4000-8000-000000000002")); fail("second host") }
        catch (expected: Store.MigrationRefused) {}
        // The old app keeps writing its files: for the host already imported, a later plan changes nothing
        File(dir, "chats/a.jsonl").appendText("{\"text\":\"later\",\"at\":1}\n")
        store.apply(LegacyImport.plan(dir, prefs(), host))
        assertEquals(host, store.migratedTo())
        assertEquals(1, store.readableDatabase.rawQuery("SELECT lines FROM chats WHERE chat_key='a'", null)
            .use { it.moveToFirst(); it.getInt(0) })
    }
}
