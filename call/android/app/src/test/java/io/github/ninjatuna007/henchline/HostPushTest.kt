package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertNull
import org.junit.Test

/** Which pushes are shown as another computer's, and which computers get this phone's push address. */
@Suppress("NewApi")  // the JVM has Ed25519
class HostPushTest {
    private val laptopId = "00000000-0000-4000-8000-00000000000a"
    private val otherId = "00000000-0000-4000-8000-00000000000b"

    private fun host(id: String, base: String, legacy: Boolean = true, pending: Boolean = false, key: String = "") =
        Host(id, "Host", base, base.removePrefix("https://").trimEnd('/'), key, "dev",
            JSONObject().put("capabilities", JSONObject().put("legacy_routes", legacy)).toString(), 1L, pending)

    private val laptop = host(laptopId, "https://laptop.example.ts.net/")
    private val other = host(otherId, "https://desk.example.ts.net/")
    private val hosts = listOf(laptop, other)
    private val laptopOrigin = "https://laptop.example.ts.net"

    private fun text(hostId: String?) = JSONObject().put("agent", "claude").put("text", "hi").apply { hostId?.let { put("host_id", it) } }
    private fun ring(hostId: String) = JSONObject().put("agent", "claude").put("why", "done").put("reply", "r").put("host_id", hostId)
    private fun file(hostId: String) = JSONObject().put("agent", "claude").put("file", JSONObject().put("url", "u")).put("host_id", hostId)

    @Test fun aPushFromAnotherPairedComputerIsItsOwn() {
        assertEquals(other, HostPush.sender(text(otherId), hosts, laptopOrigin, laptopId))
        assertEquals(other, HostPush.sender(text(otherId.uppercase()), hosts, laptopOrigin, laptopId))
        assertEquals(other, HostPush.sender(ring(otherId), hosts, laptopOrigin, laptopId))
    }

    @Test fun theLaptopPairedUnderAnotherSpellingIsStillTheLaptop() {
        // Paired as its MagicDNS short name, an IP or another port: no address comparison can tell; its id does
        for (paired in listOf("https://laptop/", "https://100.64.0.7/", "https://laptop.example.ts.net:8443/")) {
            val hosts = listOf(host(laptopId, paired), other)
            assertNull(paired, HostPush.sender(text(laptopId), hosts, laptopOrigin, laptopId))
            assertNull(paired, HostPush.sender(ring(laptopId), hosts, laptopOrigin, laptopId))
            assertEquals(paired, listOf(other), HostPush.recipients(hosts, laptopOrigin, laptopId))
        }
    }

    @Test fun whileTheLaptopsIdIsUnknownARingNeverTakesTheNewPath() {
        val hosts = listOf(host(laptopId, "https://laptop/"), other)
        assertNull(HostPush.sender(ring(laptopId), hosts, laptopOrigin, null))  // stays a call
        assertNull(HostPush.sender(ring(otherId), hosts, laptopOrigin, null))   // in doubt, even from the other
        assertEquals(other, HostPush.sender(text(otherId), hosts, laptopOrigin, null))
        assertEquals(other, HostPush.sender(file(otherId), hosts, laptopOrigin, null))
    }

    @Test fun theLaptopIsKnownHoweverItsAddressIsWritten() {
        for (written in listOf("https://LAPTOP.example.ts.net", "https://laptop.example.ts.net:443", "https://laptop.example.ts.net:443/",
                               " https://laptop.example.ts.net/ ")) {
            assertNull(written, HostPush.sender(text(laptopId), hosts, written, null))
            assertEquals(written, other, HostPush.sender(text(otherId), hosts, written, null))
        }
        val upper = host(laptopId, "https://Laptop.Example.ts.net:443/")
        assertNull(HostPush.sender(text(laptopId), listOf(upper, other), laptopOrigin, null))
    }

    @Test fun withNoLaptopAddressKnownEverythingGoesTheOldWay() {
        for (none in listOf(null, "", "not an address")) {
            assertNull(HostPush.sender(text(otherId), hosts, none, laptopId))
            assertNull(HostPush.sender(ring(otherId), hosts, none, laptopId))
            assertEquals(emptyList<Host>(), HostPush.recipients(hosts, none, laptopId))
        }
    }

    @Test fun anUnreadableHostsFileSendsEveryPushTheOldWay() {
        val file = java.io.File.createTempFile("hosts", ".json").apply { writeText("{broken") }
        assertNull(HostPush.hostsOrNull(HostRegistry(file)))
        file.writeText("[" + other.toJson() + "]")
        assertEquals(listOf(otherId), HostPush.hostsOrNull(HostRegistry(file))!!.map { it.hostId })
    }

    @Test fun aStaleLaptopIdIsLearnedAgain() {
        val newId = "00000000-0000-4000-8000-00000000000d"
        val repaired = listOf(host(newId, "https://laptop.example.ts.net/"), other)  // the laptop's id changed, same address
        assertTrue(HostPush.shouldRelearn(ring(newId), repaired, laptopOrigin, laptopId))   // a ring from it says so
        assertTrue(HostPush.shouldRelearn(text(otherId), repaired, laptopOrigin, laptopId)) // the id held names no one now
        assertTrue(HostPush.shouldRelearn(text(otherId), hosts, laptopOrigin, null))        // not known yet
        assertFalse(HostPush.shouldRelearn(ring(otherId), hosts, laptopOrigin, laptopId))   // another computer: fine
        assertFalse(HostPush.shouldRelearn(ring(laptopId), hosts, laptopOrigin, laptopId))  // the laptop as known
        assertFalse(HostPush.shouldRelearn(text(newId), listOf(laptop, host(newId, "https://laptop.example.ts.net/")),
            laptopOrigin, laptopId))  // only a ring triggers the address rule
        assertFalse(HostPush.shouldRelearn(ring(newId), repaired, null, laptopId))          // no laptop address: nothing to ask
    }

    @Test fun everythingElseGoesWhereItAlwaysWent() {
        assertNull(HostPush.sender(text(null), hosts, laptopOrigin, laptopId))       // an older computer: no host_id
        assertNull(HostPush.sender(text(laptopId), hosts, laptopOrigin, laptopId))   // the laptop itself
        assertNull(HostPush.sender(text("00000000-0000-4000-8000-00000000000c"), hosts, laptopOrigin, laptopId))  // not paired
    }

    @Test fun theOtherComputersThatTakeSignedRequestsGetThePushAddress() {
        assertEquals(listOf(other), HostPush.recipients(hosts, laptopOrigin, null))
        assertEquals(emptyList<Host>(), HostPush.recipients(listOf(laptop, host(otherId, "https://desk.example.ts.net/", legacy = false)), laptopOrigin, laptopId))
        assertEquals(emptyList<Host>(), HostPush.recipients(listOf(host(otherId, "https://desk.example.ts.net/", pending = true)), laptopOrigin, laptopId))
    }

    @Test fun textsAndFilesFromAnotherComputerAreMessagesLikeTheLaptops() {
        val text = HostPush.messageOf(text(otherId), other)!!
        assertEquals(android.app.Notification.CATEGORY_MESSAGE, text.category)
        assertEquals("msg", text.category)  // what Messages.post sets on the laptop's texts
        assertEquals("Claude on Host", text.sender)
        assertEquals("hi", text.text)
        val file = HostPush.messageOf(file(otherId).put("why", "the plot"), other)!!
        assertEquals("msg", file.category)
        assertEquals("📎 a file\nthe plot", file.text)
        assertNull(HostPush.messageOf(ring(otherId), other))  // a ring's notice stays as it was
    }

    /** The test vector's signed descriptor stands in for what the laptop's address serves. */
    private val served = JSONObject(SelfTest.SIGNED_DESCRIPTOR)
    private val servedId = served.getString("host_id")

    @Test fun theLaptopsIdIsLearnedFromItsSignedDescriptor() {
        // Paired: only when signed by that computer's pinned key
        assertEquals(servedId, HostPush.laptopIdFrom(served, listOf(host(servedId, "https://laptop/", key = SelfTest.HOST_KEY))))
        assertNull(HostPush.laptopIdFrom(served, listOf(host(servedId, "https://laptop/", key = "AAAA"))))
        // Not paired: its own key will do (it then names no paired computer anyway)
        assertEquals(servedId, HostPush.laptopIdFrom(served, emptyList()))
        val tampered = JSONObject(served.toString()).put("label", "Someone else")
        assertNull(HostPush.laptopIdFrom(tampered, emptyList()))
        assertNull(HostPush.laptopIdFrom(JSONObject(), emptyList()))
    }
}
