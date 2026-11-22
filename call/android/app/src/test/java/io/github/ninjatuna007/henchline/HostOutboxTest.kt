package io.github.ninjatuna007.henchline

import okhttp3.HttpUrl
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.KeyPairGenerator
import java.security.spec.ECGenParameterSpec

/** Per-computer queues over v1: order, retries with the same bytes, nothing run twice, one computer never blocks
 *  another. */
@Suppress("NewApi")
class HostOutboxTest {
    private val a = "00000000-0000-4000-8000-00000000000a"
    private val b = "00000000-0000-4000-8000-00000000000b"
    private val device = "11111111-1111-4111-8111-111111111111"

    private fun descriptor(hostId: String, chat: Boolean = true) = JSONObject().put("host_id", hostId)
        .put("capabilities", JSONObject().put("chat", chat)).toString()

    private fun host(id: String, chat: Boolean = true, pending: Boolean = false) =
        Host(id, "Host $id".take(10), "https://h$id.example.ts.net/".replace("-", ""), "h${id.replace("-", "")}.example.ts.net",
            "", device, descriptor(id, chat), 1L, pending)

    private val key = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
        .let { P256Key(it.private, it.public.encoded) }

    class Memory : OutboxStore {
        val rows = ArrayList<Pair<Queued, String>>()
        override fun enqueue(hostId: String, requestId: String, clientMessageId: String, sessionId: String, body: String): Queued {
            val q = Queued(hostId, rows.count { it.first.hostId == hostId }.toLong(), requestId, clientMessageId, sessionId, body)
            rows += q to "waiting"; return q
        }
        override fun waiting(hostId: String) = rows.filter { it.first.hostId == hostId && it.second == "waiting" }.map { it.first }
        override fun settle(requestId: String, state: String, note: String) {
            val i = rows.indexOfFirst { it.first.requestId == requestId && it.second == "waiting" }
            if (i >= 0) rows[i] = rows[i].first to state
        }
        fun state(requestId: String) = rows.first { it.first.requestId == requestId }.second
    }

    /** A computer: answers each message by its text (the test's script), records what it got. */
    inner class Computer(val hostId: String, val script: (String) -> HttpAnswer) : Http {
        val messages = ArrayList<Triple<String, String, String>>()  // request id, body, signature header present
        val acks = ArrayList<String>()
        override fun send(method: String, url: HttpUrl, body: ByteArray?, headers: Map<String, String>): HttpAnswer {
            if (url.encodedPath.endsWith("/ack")) { acks += url.pathSegments[3]; return HttpAnswer(200, """{"host_id":"$hostId"}""") }
            val sent = JSONObject(String(body!!))
            messages += Triple(headers.getValue("X-Henchline-Request-Id"), String(body), headers["X-Henchline-Signature"] ?: "")
            return script(sent.getJSONObject("content").getString("text")).let { answer ->
                if (answer.code == 201) HttpAnswer(201, JSONObject(answer.body).put("host_id", hostId)
                    .put("session_id", sent.getString("session_id")).put("client_message_id", sent.getString("client_message_id")).toString())
                else answer
            }
        }
    }

    private fun delivered(replayed: Boolean = false) = HttpAnswer(201, JSONObject().put("event_id", "e").put("state", "delivered")
        .put("replayed", replayed).toString())

    private fun chatFor(computers: Map<String, Computer>): (Host) -> HostChat =
        { h -> HostChat(HostClient(h, key, computers.getValue(h.hostId)), h.hostId) }

    @Test fun messagesGoInOrderAndAreAcknowledged() {
        val store = Memory()
        val computer = Computer(a) { delivered() }
        val first = HostOutbox.enqueue(store, host(a), "claude-1", "one")
        val second = HostOutbox.enqueue(store, host(a), "claude-1", "two\nlines")
        val result = HostOutbox.flush(store, listOf(host(a)), chatFor(mapOf(a to computer))).getValue(a)
        assertEquals(HostOutbox.Result(2, 0, null), result)
        assertEquals(listOf(first.requestId, second.requestId), computer.messages.map { it.first })
        assertEquals(listOf(first.requestId, second.requestId), computer.acks)
        assertEquals("two\nlines", JSONObject(computer.messages[1].second).getJSONObject("content").getString("text"))
        assertTrue(computer.messages.all { it.third.isNotEmpty() })
    }

    @Test fun aRetryResendsTheSameRequestAndBytes() {
        val store = Memory()
        var busy = true
        val computer = Computer(a) { if (busy) HttpAnswer(429, """{"error":{"code":"rate_limited","retryable":true}}""") else delivered(true) }
        val item = HostOutbox.enqueue(store, host(a), "claude-1", "hello")
        assertEquals("rate_limited", HostOutbox.flush(store, listOf(host(a)), chatFor(mapOf(a to computer))).getValue(a)!!.stoppedBy)
        assertEquals("waiting", store.state(item.requestId))
        busy = false
        HostOutbox.flush(store, listOf(host(a)), chatFor(mapOf(a to computer)))
        assertEquals("sent", store.state(item.requestId))
        assertEquals(1, computer.messages.map { it.first to it.second }.toSet().size)  // same id, same bytes, twice
    }

    @Test fun oneComputerOfflineNeverHoldsAnother() {
        val store = Memory()
        val down = Computer(a) { throw java.net.SocketTimeoutException("down") }
        val up = Computer(b) { delivered() }
        HostOutbox.enqueue(store, host(a), "claude-1", "to a")
        HostOutbox.enqueue(store, host(b), "claude-1", "to b")
        val results = HostOutbox.flush(store, listOf(host(a), host(b)), chatFor(mapOf(a to down, b to up)))
        assertEquals(1, results.getValue(a)!!.left)
        assertEquals(HostOutbox.Result(1, 0, null), results.getValue(b))
    }

    @Test fun outcomesThatEndAMessageLetTheNextGo() {
        val store = Memory()
        val computer = Computer(a) { text -> when (text) {
            "unknown" -> HttpAnswer(409, """{"error":{"code":"outcome_unknown","retryable":false}}""")
            "unreachable" -> HttpAnswer(200, """{"host_id":"$a","state":"failed","note":"That agent isn't reachable."}""")
            "crash" -> HttpAnswer(500, """{"error":{"code":"internal_error","retryable":false}}""")
            "bad" -> HttpAnswer(422, """{"error":{"code":"validation_failed"}}""")
            else -> delivered()
        } }
        val items = listOf("unknown", "unreachable", "crash", "bad", "fine").map { HostOutbox.enqueue(store, host(a), "c", it) }
        assertEquals(HostOutbox.Result(1, 0, null), HostOutbox.flush(store, listOf(host(a)), chatFor(mapOf(a to computer))).getValue(a))
        assertEquals(listOf("maybe", "failed", "maybe", "failed", "sent"), items.map { store.state(it.requestId) })
    }

    @Test fun notAuthorizedKeepsTheQueue() {
        val store = Memory()
        val computer = Computer(a) { HttpAnswer(403, """{"error":{"code":"device_revoked"}}""") }
        val item = HostOutbox.enqueue(store, host(a), "c", "hi")
        assertTrue(HostOutbox.flush(store, listOf(host(a)), chatFor(mapOf(a to computer))).getValue(a)!!.stoppedBy!!.contains("not authorized"))
        assertEquals("waiting", store.state(item.requestId))
    }

    @Test fun aSkewedClockOrAMissingCapabilityPausesThatQueueOnly() {
        val store = Memory()
        val skewed = Computer(a) { HttpAnswer(401, """{"error":{"code":"timestamp_out_of_window","server_time":1}}""") }
        HostOutbox.enqueue(store, host(a), "c", "hi")
        assertTrue(HostOutbox.flush(store, listOf(host(a)), chatFor(mapOf(a to skewed))).getValue(a)!!.stoppedBy!!.contains("clock"))
        val noChat = Computer(b) { delivered() }
        HostOutbox.enqueue(store, host(b), "c", "hi")
        val result = HostOutbox.flush(store, listOf(host(b, chat = false)), chatFor(mapOf(b to noChat))).getValue(b)!!
        assertEquals(1, result.left)
        assertTrue(noChat.messages.isEmpty())
    }

    @Test fun aHostBeingRemovedSendsNothing() {
        val store = Memory()
        val computer = Computer(a) { delivered() }
        HostOutbox.enqueue(store, host(a), "c", "hi")
        assertEquals("removal pending", HostOutbox.flush(store, listOf(host(a, pending = true)), chatFor(mapOf(a to computer))).getValue(a)!!.stoppedBy)
        assertTrue(computer.messages.isEmpty())
    }

    @Test fun oneSenderPerComputerAndNothingSentTwice() {
        val store = object : OutboxStore {  // the real queue is called from several threads: this one is synchronized too
            val inner = Memory()
            @Synchronized override fun enqueue(hostId: String, requestId: String, clientMessageId: String, sessionId: String, body: String) =
                inner.enqueue(hostId, requestId, clientMessageId, sessionId, body)
            @Synchronized override fun waiting(hostId: String) = inner.waiting(hostId)
            @Synchronized override fun settle(requestId: String, state: String, note: String) = inner.settle(requestId, state, note)
        }
        val inside = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        val computer = Computer(a) { text -> if (text == "first") { inside.countDown(); release.await() }; delivered() }
        HostOutbox.enqueue(store, host(a), "c", "first")
        var firstResult: HostOutbox.Result? = null
        val sender = Thread { firstResult = HostOutbox.flushHost(store, host(a), chatFor(mapOf(a to computer))) }.apply { start() }
        inside.await()
        HostOutbox.enqueue(store, host(a), "c", "second")  // written while the first is being sent
        assertNull("a second sender stands aside", HostOutbox.flushHost(store, host(a), chatFor(mapOf(a to computer))))
        release.countDown()
        sender.join(5000)
        assertEquals(listOf("first", "second"), computer.messages.map { JSONObject(it.second).getJSONObject("content").getString("text") })
        assertEquals(2, firstResult!!.sent)  // the one sender went round again for the message added meanwhile
        assertTrue(store.inner.rows.all { it.second == "sent" })
    }

    @Test fun aMessageQueuedInTheLastGapIsStillSent() {
        val store = object : OutboxStore {
            val inner = Memory()
            @Synchronized override fun enqueue(hostId: String, requestId: String, clientMessageId: String, sessionId: String, body: String) =
                inner.enqueue(hostId, requestId, clientMessageId, sessionId, body)
            @Synchronized override fun waiting(hostId: String) = inner.waiting(hostId)
            @Synchronized override fun settle(requestId: String, state: String, note: String) = inner.settle(requestId, state, note)
        }
        val computer = Computer(a) { delivered() }
        val chats = chatFor(mapOf(a to computer))
        HostOutbox.enqueue(store, host(a), "c", "first")
        var late: HostOutbox.Result? = HostOutbox.Result(-1, -1, "not run")
        HostOutbox.afterPass = {
            HostOutbox.afterPass = null  // once: B arrives after A's last look, before A lets go
            HostOutbox.enqueue(store, host(a), "c", "late")
            Thread { late = HostOutbox.flushHost(store, host(a), chats) }.apply { start(); join(5000) }
        }
        try {
            val result = HostOutbox.flushHost(store, host(a), chats)
            assertNull("B found A busy and left it to A", late)
            assertEquals(2, result!!.sent)
            assertEquals(listOf("first", "late"), computer.messages.map { JSONObject(it.second).getJSONObject("content").getString("text") })
        } finally {
            HostOutbox.afterPass = null
        }
    }

    @Test fun anAnswerForAnotherMessageIsNotTakenAsDelivery() {
        val store = Memory()
        val computer = object : Http {
            override fun send(method: String, url: HttpUrl, body: ByteArray?, headers: Map<String, String>) =
                if (url.encodedPath.endsWith("/ack")) HttpAnswer(200, "{}")
                else HttpAnswer(201, JSONObject().put("host_id", a).put("session_id", "c").put("client_message_id", "other").toString())
        }
        val item = HostOutbox.enqueue(store, host(a), "c", "hi")
        HostOutbox.flush(store, listOf(host(a)), { h -> HostChat(HostClient(h, key, computer), h.hostId) })
        assertEquals("maybe", store.state(item.requestId))
    }

    @Test fun readReceiptsNameTheChatAndItsEvents() {
        var sent: Pair<HttpUrl, JSONObject>? = null
        val chat = HostChat(HostClient(host(a), key, { _, url, body, _ ->
            sent = url to JSONObject(String(body!!))
            HttpAnswer(200, """{"host_id":"$a","marked":["e1"],"unknown":["m1"]}""") }), a)
        assertEquals(listOf("e1"), chat.receipt("a/b", listOf("e1", "m1")))
        val (url, body) = sent!!
        assertEquals("/api/v1/chats/a%2Fb/receipts", url.encodedPath)
        assertEquals(listOf(a, "a/b", "read"), listOf(body.getString("host_id"), body.getString("session_id"), body.getString("state")))
        assertEquals(2, body.getJSONArray("event_ids").length())
        assertEquals(emptyList<String>(), chat.receipt("a/b", emptyList()))  // nothing to say: nothing sent
        // More than the host takes at once is refused, not cut short: the caller sends chunks and records each
        val many = (0..HostChat.RECEIPT_IDS).map { "e$it" }
        assertTrue(runCatching { chat.receipt("a/b", many) }.exceptionOrNull() is IllegalArgumentException)
        chat.receipt("a/b", many.take(HostChat.RECEIPT_IDS))
        assertEquals(HostChat.RECEIPT_IDS, sent!!.second.getJSONArray("event_ids").length())
    }

    @Test fun agentTextsComeWithTheChatsEvents() {
        val chat = HostChat(HostClient(host(a), key, { _, _, _, _ -> HttpAnswer(200, """{"host_id":"$a","events":[
            {"host_id":"$a","session_id":"c1","event_id":"t1","kind":"agent_text","agent":"claude","text":"Done."},
            {"host_id":"$a","session_id":"c1","event_id":"m1","kind":"message","state":"delivered"}],"next_cursor":null}""") }), a)
        assertEquals(listOf("agent_text", "message"), chat.events("c1").first.map { it.getString("kind") })
    }

    @Test fun chatsAndEventsAreThisHostsOnly() {
        val pages = ArrayDeque(listOf(
            """{"host_id":"$a","chats":[{"host_id":"$a","session_id":"c1","name":"One"}],"next_cursor":"1"}""",
            """{"host_id":"$a","chats":[{"host_id":"$a","session_id":"c2","name":"Two","current":true}],"next_cursor":null}"""))
        val chat = HostChat(HostClient(host(a), key, { _, url, _, _ ->
            if (url.encodedPath.endsWith("/events")) HttpAnswer(200, """{"host_id":"$a","events":[
                {"host_id":"$a","session_id":"c1","event_id":"e1"},{"host_id":"$b","session_id":"c1","event_id":"x"},
                {"host_id":"$a","session_id":"c2","event_id":"y"}],"next_cursor":null}""")
            else HttpAnswer(200, pages.removeFirst()) }), a)
        assertEquals(listOf(HostChat.Summary("c1", "One", false), HostChat.Summary("c2", "Two", true)), chat.chats())
        val (events, next) = chat.events("c1")
        assertEquals(listOf("e1"), events.map { it.getString("event_id") })
        assertNull(next)
    }
}
