package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** When paired computers' descriptions are looked at again, and that a background look never revives a removal. */
class HostRefreshTest {
    private fun host(id: String, pending: Boolean = false, seq: Int = 1, key: String = "K", device: String = "dev", paired: Long = 1L) =
        Host(id, "Host $id", "https://h$id.example.ts.net/", "h$id.example.ts.net", key, device,
            JSONObject().put("descriptor_seq", seq).toString(), paired, pending)

    private fun registry() = HostRegistry(java.io.File.createTempFile("hosts", ".json").apply { delete() })
    private fun seqOf(r: HostRegistry, id: String) = JSONObject(r.get(id)!!.descriptor).getInt("descriptor_seq")

    @Test fun eachComputerIsDueOnItsOwnSchedule() {
        val now = 10 * HostRefresh.EVERY_MS
        val hosts = listOf(host("a"), host("b"), host("c"), host("d", pending = true))
        val next = mapOf("a" to now - 1, "b" to now + 1)  // c: never looked at
        assertEquals(listOf("a", "c"), HostRefresh.due(hosts, next, now).map { it.hostId })  // d: being removed
    }

    @Test fun aFailedLookIsTriedSoonerThanAWorkingOne() {
        assertEquals(1000 + HostRefresh.EVERY_MS, HostRefresh.nextAfter(true, 1000))
        assertEquals(1000 + HostRefresh.RETRY_MS, HostRefresh.nextAfter(false, 1000))
        assertTrue(HostRefresh.RETRY_MS < HostRefresh.EVERY_MS)
    }

    @Test fun aBackgroundLookNeverBringsBackARemovedComputer() {
        val registry = registry()
        assertFalse(registry.replace(host("a", seq = 2)))  // never paired (or removed): not added
        assertTrue(registry.all().isEmpty())
        registry.put(host("a")); registry.put(host("b"))
        assertTrue(registry.replace(host("a", seq = 2)))
        assertEquals(listOf("a", "b"), registry.all().map { it.hostId })  // kept in place
        assertEquals(2, JSONObject(registry.get("a")!!.descriptor).getInt("descriptor_seq"))
        registry.put(host("b", pending = true))
        assertFalse(registry.replace(host("b", seq = 3)))  // its removal began meanwhile
        assertTrue(registry.get("b")!!.revocationPending)
    }

    @Test fun aLookStartedBeforeCheckNowNeverTakesItsDescriptionBack() {
        val r = registry()
        r.put(host("a", seq = 5))
        val snapshot = r.get("a")!!                          // the background look starts here
        r.put(host("a", seq = 6))                            // Check now saves a newer one meanwhile
        assertFalse(r.replace(snapshot.copy(descriptor = JSONObject().put("descriptor_seq", 5).toString())))
        assertEquals(6, seqOf(r, "a"))
        assertTrue(r.replace(snapshot.copy(descriptor = JSONObject().put("descriptor_seq", 7).toString())))  // newer: kept
        assertEquals(7, seqOf(r, "a"))
    }

    @Test fun aLookStartedBeforeRemovingAndPairingAgainIsDropped() {
        val r = registry()
        r.put(host("a", seq = 5))
        val snapshot = r.get("a")!!
        r.remove("a"); r.put(host("a", seq = 1, key = "K2", device = "dev2", paired = 2L))  // paired again, same id
        for (stale in listOf(snapshot.copy(descriptor = JSONObject().put("descriptor_seq", 9).toString()),
                             host("a", seq = 9, key = "K2", device = "dev"), host("a", seq = 9, key = "K", device = "dev2", paired = 2L),
                             host("a", seq = 9, key = "K2", device = "dev2", paired = 1L))) {
            assertFalse(r.replace(stale))
            assertEquals("K2", r.get("a")!!.hostKey); assertEquals("dev2", r.get("a")!!.deviceId)
        }
    }

    @Test fun schedulesForComputersNoLongerPairedAreDropped() {
        assertEquals(setOf("gone"), HostRefresh.stale(setOf("a", "gone"), listOf(host("a"), host("b"))))
        assertEquals(emptySet<String>(), HostRefresh.stale(emptySet(), listOf(host("a"))))
    }

    @Test fun oneLockForEveryRegistryOfTheFile() {
        val file = java.io.File.createTempFile("hosts", ".json").apply { delete() }
        val threads = (0 until 8).map { t ->
            Thread { repeat(25) { i -> HostRegistry(file).put(host("t$t-$i")) } }  // a new instance per write, as the app does
        }
        threads.forEach { it.start() }; threads.forEach { it.join() }
        assertEquals(200, HostRegistry(file).all().size)  // no write lost
    }

    private val noSigning = object : RequestKey {
        override val algorithm = "p256"; override val publicKey = ""
        override fun sign(message: ByteArray) = ByteArray(0)
    }
    private fun withSeq(h: Host, seq: Int, extra: String = "") =
        h.copy(descriptor = JSONObject().put("descriptor_seq", seq).apply { if (extra.isNotEmpty()) put("label", extra) }.toString())

    @Test fun checkNowSavesOnlyWhatStillApplies() {
        val r = registry()
        r.put(host("a", seq = 5))
        val snapshot = r.get("a")!!
        // Kept: nothing changed meanwhile
        assertEquals(HostRefresh.Look.KEPT, HostRefresh.look(snapshot, r, { noSigning }) { h, _ -> withSeq(h, 6) })
        assertEquals(6, seqOf(r, "a"))
        // A newer description saved meanwhile: not overwritten
        val old = r.get("a")!!
        assertEquals(HostRefresh.Look.STALE, HostRefresh.look(old, r, { noSigning }) { h, _ -> r.put(withSeq(h, 8)); withSeq(h, 7) })
        assertEquals(8, seqOf(r, "a"))
        // Its removal begun meanwhile: still pending
        val before = r.get("a")!!
        assertEquals(HostRefresh.Look.STALE, HostRefresh.look(before, r, { noSigning }) { h, _ ->
            r.update("a") { it.copy(revocationPending = true) }; withSeq(h, 9) })
        assertTrue(r.get("a")!!.revocationPending)
        // Removed meanwhile: not brought back
        r.put(host("b", seq = 1))
        val b = r.get("b")!!
        assertEquals(HostRefresh.Look.STALE, HostRefresh.look(b, r, { noSigning }) { h, _ -> r.remove("b"); withSeq(h, 2) })
        assertEquals(null, r.get("b"))
        // Nothing new
        r.put(host("c", seq = 3))
        assertEquals(HostRefresh.Look.UNCHANGED, HostRefresh.look(r.get("c")!!, r, { noSigning }) { h, _ -> h })
    }

    @Test fun noKeyMeansNoLookAndNoNewKey() {
        val r = registry(); r.put(host("a"))
        var asked = false
        assertEquals(HostRefresh.Look.NO_KEY, HostRefresh.look(r.get("a")!!, r, { null }) { h, _ -> asked = true; h })
        assertFalse(asked)
    }

    @Test fun markingARemovalPendingChangesTheHostAsStoredNow() {
        val r = registry()
        r.put(host("a", seq = 5))
        val stale = r.get("a")!!
        r.put(host("a", seq = 6))                                    // a newer description saved after the copy was read
        val marked = r.update(stale.hostId) { it.copy(revocationPending = true) }!!
        assertTrue(marked.revocationPending)
        assertEquals(6, seqOf(r, "a"))                               // not the copy's 5
        assertEquals(null, r.update("gone") { it.copy(revocationPending = true) })
        assertEquals(listOf("a"), r.all().map { it.hostId })         // nothing written for one not paired
    }

    @Test fun theSameNumberWithOtherContentIsNotNewer() {
        val r = registry()
        r.put(withSeq(host("a"), 4, "one"))
        assertFalse(r.replace(withSeq(host("a"), 4, "other")))
        assertTrue(r.replace(withSeq(host("a"), 4, "one")))
    }

    @Test fun aScheduleIsWrittenOnlyForThePairingLookedAt() {
        val r = registry()
        r.put(host("a", paired = 1L))
        val looked = r.get("a")!!
        assertTrue(HostRefresh.samePairing(r, looked))
        r.remove("a"); r.put(host("a", paired = 2L))                  // removed and paired again during the look
        assertFalse(HostRefresh.samePairing(r, looked))
        r.remove("a")
        assertFalse(HostRefresh.samePairing(r, looked))
    }
}
