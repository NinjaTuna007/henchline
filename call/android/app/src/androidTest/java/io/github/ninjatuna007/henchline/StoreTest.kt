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

    @Test fun importVerifiesAndRunsOnce() {
        val host = "00000000-0000-4000-8000-000000000001"
        val plan = LegacyImport.plan(dir, prefs(), host)
        store.apply(plan)
        assertEquals(host, store.migratedTo())
        store.apply(LegacyImport.plan(dir, prefs(), host))  // the same again: nothing to do
        assertTrue(LegacyImport.unchanged(dir, plan))
        try { store.apply(LegacyImport.plan(dir, prefs(), "00000000-0000-4000-8000-000000000002")); fail("second host") }
        catch (expected: Store.MigrationRefused) {}
        File(dir, "chats/a.jsonl").appendText("{\"text\":\"later\",\"at\":1}\n")
        try { store.apply(LegacyImport.plan(dir, prefs(), host)); fail("changed data") } catch (expected: Store.MigrationRefused) {}
        assertEquals(host, store.migratedTo())
    }
}
