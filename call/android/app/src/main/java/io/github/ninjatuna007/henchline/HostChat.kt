package io.github.ninjatuna007.henchline

import androidx.annotation.RequiresApi
import org.json.JSONObject
import java.util.UUID

/** A message waiting to go to one computer's chat. body: the exact bytes sent every time (a retry must match the
 *  first send, or the host answers idempotency_mismatch). */
data class Queued(
    val hostId: String, val position: Long, val requestId: String, val clientMessageId: String,
    val sessionId: String, val body: String,
)

/** What became of one send. */
sealed class Sent {
    data class Delivered(val eventId: String, val replayed: Boolean) : Sent()   // typed into the agent
    data class Failed(val note: String) : Sent()        // the host refused or couldn't deliver: not sent
    data class MaybeSent(val note: String) : Sent()     // the host stopped mid-way: it may have gone, never retried
    data class Later(val why: String) : Sent()          // offline, busy, rate limited: try again, same request id
}

/** A chat as a paired computer lists it (any API level: the home list reads it from what is kept): running or known only from its events lately (a timer's texts, an agent no
 *  longer running: state gone or ended), with its agent, boss (a helper's), team, and last line. */
data class ChatSummary(val sessionId: String, val name: String, val current: Boolean, val running: Boolean = true,
                   val state: String = "", val agent: String = "", val boss: String = "", val team: String = "",
                   val lastAt: Long = 0L, val lastText: String = "", val lastMine: Boolean = false) {
    companion object {
        /** From the computer's answer (older ones send only session_id, name and current). */
        fun of(it: org.json.JSONObject): ChatSummary {
            val last = it.optJSONObject("last")
            val at = try { java.time.Instant.parse(it.optString("last_at")).toEpochMilli() } catch (e: Exception) { 0L }
            return ChatSummary(it.getString("session_id"), it.optString("name"), it.optBoolean("current"),
                it.optBoolean("running", true), it.optString("state"), it.optString("agent"), it.optString("boss"),
                it.optString("team"), at, last?.optString("text") ?: "", last?.optBoolean("mine") ?: false)
        }
    }
}

/** What a chat event is drawn as (HostChatActivity): an agent's text, a message from this phone, a line said on a
 *  call (yours or the agent's), or nothing (a kind this app doesn't know yet: kept in place, not shown). */
enum class EventLook { AGENT_TEXT, MINE, CALL_YOU, CALL_THEM, NONE }

/** A page of a chat's events asks for the call lines too (with=call_lines: a computer without them ignores it). */
object HostChatQuery {
    fun of(cursor: String?) = listOfNotNull("limit" to "200", "with" to "call_lines", cursor?.let { "cursor" to it })
}

object ChatEvents {
    fun lookOf(e: JSONObject): EventLook = when (e.optString("kind")) {
        "agent_text" -> EventLook.AGENT_TEXT
        "message" -> EventLook.MINE
        "call_line" -> if (e.optString("from").equals("you", ignoreCase = true)) EventLook.CALL_YOU else EventLook.CALL_THEM
        else -> EventLook.NONE
    }

    /** Said on the latest call with that chat (it began at startedAt, this phone's clock; null: no call this
     *  run): kept lines from before it are an older call's. Two minutes' grace for the computers' clocks. */
    fun ofCall(e: JSONObject, startedAt: Long?): Boolean {
        val at = try { java.time.Instant.parse(e.optString("created_at")).toEpochMilli() } catch (ex: Exception) { return false }
        return startedAt != null && at >= startedAt - CLOCK_GRACE
    }

    const val CLOCK_GRACE = 120_000L

    /** A call line's identity among the ones told live (CallService.HostLines): the same words from the same speaker,
     *  so the kept copy replaces the live one rather than showing twice. */
    fun callKey(from: String, text: String) = from.lowercase() + "\u0000" + text
}

/** One computer's chats over the signed v1 API. */
@RequiresApi(MultiHost.MIN_SDK)
class HostChat(private val client: HostClient, private val hostId: String) {
    /** The chats there now, every page (bounded). */
    fun chats(): List<ChatSummary> = listed().map { ChatSummary.of(it) }

    /** The same, as the computer sent each one (kept as is by HostLists, read again with ChatSummary.of). */
    fun listed(): List<org.json.JSONObject> {
        client.require("chat")
        val out = ArrayList<org.json.JSONObject>()
        var cursor: String? = null
        repeat(20) {
            val answer = client.call("GET", listOf("api", "v1", "chats"),
                listOfNotNull("limit" to "200", cursor?.let { "cursor" to it }))
            if (answer.code != 200) throw java.io.IOException("chats: ${answer.code} ${answer.errorCode}")
            val page = answer.json()
            val list = page.optJSONArray("chats") ?: return out
            for (i in 0 until list.length()) list.getJSONObject(i).let {
                if (it.optString("host_id") == hostId && it.optString("session_id").isNotEmpty()) out += it
            }
            cursor = page.optString("next_cursor").takeIf { c -> !page.isNull("next_cursor") && c.isNotEmpty() } ?: return out
        }
        return out
    }

    /** That chat's events after cursor (null: from the start), and the cursor for more (null: none). */
    fun events(sessionId: String, cursor: String? = null): Pair<List<JSONObject>, String?> {
        client.require("chat")
        val answer = client.call("GET", listOf("api", "v1", "chats", sessionId, "events"), HostChatQuery.of(cursor))
        if (answer.code != 200) throw java.io.IOException("events: ${answer.code} ${answer.errorCode}")
        val page = answer.json()
        val list = page.optJSONArray("events") ?: return emptyList<JSONObject>() to null
        val events = (0 until list.length()).map { list.getJSONObject(it) }
            .filter { it.optString("host_id") == hostId && it.optString("session_id") == sessionId }
        return events to page.optString("next_cursor").takeIf { !page.isNull("next_cursor") && it.isNotEmpty() }
    }

    /** One queued message, sent or re-sent with its own request id and bytes. ClockSkew and CapabilityMissing
     *  propagate: the caller pauses this computer's queue. */
    fun send(item: Queued): Sent {
        client.require("chat")
        val answer = try {
            client.call("POST", listOf("api", "v1", "chats", item.sessionId, "messages"),
                body = item.body.toByteArray(Charsets.UTF_8), requestId = item.requestId)
        } catch (e: java.io.IOException) {
            return Sent.Later(e.message ?: "offline")
        }
        val reply = answer.json()
        val error = reply.optJSONObject("error")
        return when {
            answer.code == 201 || (answer.code == 200 && reply.optString("state") == "delivered") ->
                if (reply.optString("session_id") != item.sessionId || reply.optString("client_message_id") != item.clientMessageId)
                    Sent.MaybeSent("the computer's answer named another message")
                else Sent.Delivered(reply.optString("event_id"), reply.optBoolean("replayed"))
            answer.code == 200 -> Sent.Failed(reply.optString("note").ifEmpty { "not delivered" })
            error?.optString("code") == "outcome_unknown" || answer.code == 500 ->
                Sent.MaybeSent("the computer stopped while sending it: it may or may not have gone")
            // Not authorized there (revoked, unknown): kept, and this computer's queue waits for the user
            answer.code == 401 || answer.code == 403 -> Sent.Later("not authorized: ${error?.optString("code") ?: answer.code}")
            error?.optBoolean("retryable") == true || answer.code == 429 || answer.code == 503 ->
                Sent.Later(error?.optString("code") ?: "busy")
            answer.code >= 500 -> Sent.Later("${answer.code}")
            else -> Sent.Failed("${answer.code} ${error?.optString("code") ?: ""}".trim())
        }
    }

    /** The agents' texts among eventIds, seen on this phone: moved forward on the host (a read text stops an
     *  until-read ring there). Returns the events the host marked; harmless to send again (forward only).
     *  At most RECEIPT_IDS at once (the host's limit): the caller sends chunks and records each one sent. */
    fun receipt(sessionId: String, eventIds: List<String>, state: String = "read"): List<String> {
        if (eventIds.isEmpty()) return emptyList()
        require(eventIds.size <= RECEIPT_IDS) { "at most $RECEIPT_IDS events per receipt" }
        client.require("chat")
        val body = JSONObject().put("host_id", hostId).put("session_id", sessionId).put("state", state)
            .put("event_ids", org.json.JSONArray(eventIds)).toString().toByteArray()
        val answer = client.call("POST", listOf("api", "v1", "chats", sessionId, "receipts"), body = body)
        if (answer.code != 200) throw java.io.IOException("receipts: ${answer.code} ${answer.errorCode}")
        val marked = answer.json().optJSONArray("marked") ?: return emptyList()
        return (0 until marked.length()).map { marked.getString(it) }
    }

    /** The phone has kept the answer: the host may compact it (it still never runs twice). */
    fun ack(requestId: String): Boolean = try {
        client.call("POST", listOf("api", "v1", "requests", requestId, "ack")).code == 200
    } catch (e: java.io.IOException) {
        false
    }

    companion object {
        const val RECEIPT_IDS = 200

        /** The message as it will be sent, every time. */
        fun body(hostId: String, sessionId: String, clientMessageId: String, text: String): String =
            JSONObject().put("host_id", hostId).put("session_id", sessionId).put("client_message_id", clientMessageId)
                .put("content", JSONObject().put("type", "text").put("text", text)).toString()

        fun newIds(): Pair<String, String> = UUID.randomUUID().toString() to UUID.randomUUID().toString()
    }
}

/** A message as the chat screen shows it: its words, and what became of it. */
data class QueuedRow(val requestId: String, val clientMessageId: String, val text: String, val state: String, val note: String)

/** Where the queues live (Store on the phone; a list in tests). */
interface OutboxStore {
    fun enqueue(hostId: String, requestId: String, clientMessageId: String, sessionId: String, body: String): Queued
    fun waiting(hostId: String): List<Queued>
    fun settle(requestId: String, state: String, note: String = "")  // sent, failed, maybe
}

/** One queue per computer, oldest first. A computer that can't take its next message stops only its own queue. */
@RequiresApi(MultiHost.MIN_SDK)
object HostOutbox {
    data class Result(val sent: Int, val left: Int, val stoppedBy: String?)

    fun enqueue(store: OutboxStore, host: Host, sessionId: String, text: String): Queued {
        val (requestId, clientMessageId) = HostChat.newIds()
        return store.enqueue(host.hostId, requestId, clientMessageId, sessionId,
            HostChat.body(host.hostId, sessionId, clientMessageId, text))
    }

    /** chatFor: that computer's HostChat. A host whose removal is pending sends nothing. A host whose queue is being
     *  sent already (another thread) is left to it: null for that host, and its sender goes round once more. */
    fun flush(store: OutboxStore, hosts: List<Host>, chatFor: (Host) -> HostChat): Map<String, Result?> =
        hosts.associate { host ->
            host.hostId to if (host.revocationPending) Result(0, store.waiting(host.hostId).size, "removal pending")
            else flushHost(store, host, chatFor)
        }

    private val running = java.util.concurrent.ConcurrentHashMap<String, java.util.concurrent.atomic.AtomicBoolean>()
    private val again = java.util.concurrent.ConcurrentHashMap<String, java.util.concurrent.atomic.AtomicBoolean>()

    /** Tests only: runs after a sender's last pass, before it lets go (the gap a late message can fall into). */
    @androidx.annotation.VisibleForTesting
    var afterPass: (() -> Unit)? = null

    /** One sender per computer at a time: a message is never sent by two threads at once. A caller that finds the
     *  sender busy sets "again" and leaves; the sender checks it after every pass and again after letting go, so a
     *  message queued in that last gap is never left waiting for the next trigger. */
    fun flushHost(store: OutboxStore, host: Host, chatFor: (Host) -> HostChat): Result? {
        val busy = running.getOrPut(host.hostId) { java.util.concurrent.atomic.AtomicBoolean() }
        val more = again.getOrPut(host.hostId) { java.util.concurrent.atomic.AtomicBoolean() }
        var total = 0
        var last: Result? = null
        while (true) {
            if (!busy.compareAndSet(false, true)) { more.set(true); return last?.copy(sent = total) }
            try {
                do {
                    more.set(false)
                    val result = flushOne(store, host, chatFor)
                    total += result.sent
                    last = result
                } while (result.stoppedBy == null && more.get())
                afterPass?.invoke()
            } finally {
                busy.set(false)
            }
            // Stopped (offline, paused): the next trigger tries again. Otherwise, one more look for the late arrival
            if (last!!.stoppedBy != null || !more.get()) return last.copy(sent = total)
        }
    }

    private fun flushOne(store: OutboxStore, host: Host, chatFor: (Host) -> HostChat): Result {
        val chat = try { chatFor(host) } catch (e: Exception) { return Result(0, store.waiting(host.hostId).size, e.message) }
        var sent = 0
        for (item in store.waiting(host.hostId)) {
            val outcome = try { chat.send(item) } catch (e: Exception) {  // ClockSkew, CapabilityMissing: pause
                return Result(sent, store.waiting(host.hostId).size, e.message ?: e.javaClass.simpleName)
            }
            when (outcome) {
                is Sent.Delivered -> { store.settle(item.requestId, "sent"); chat.ack(item.requestId); sent++ }
                is Sent.Failed -> store.settle(item.requestId, "failed", outcome.note)
                is Sent.MaybeSent -> store.settle(item.requestId, "maybe", outcome.note)
                is Sent.Later -> return Result(sent, store.waiting(host.hostId).size, outcome.why)
            }
        }
        return Result(sent, 0, null)
    }
}

/**
 * One chat's events as this phone holds them, in the host's order (created_at, then event id), each once. The chat
 * screen asks the host only for what follows where it got to, and draws only what is new where it belongs.
 * A text the host files late (it reads agents' texts every couple of seconds, or after a restart) can sort before
 * events already held: polling resumes from OVERLAP_S before the newest event held, and repeats are dropped.
 */
class ChatTimeline {
    private data class Held(val at: Long, val eventId: String, val cursor: String)
    private val held = ArrayList<Held>()  // in order
    private val ids = HashSet<String>()

    private var more: String? = null  // a full page's next cursor: the history goes on past what is held

    val size get() = held.size

    /** Still reading a long history up to its end (a page came back full). */
    val catchingUp get() = more != null

    /** The new events among a page, each with the position it takes among those held (apply in the order given). */
    fun add(page: List<JSONObject>): List<Pair<Int, JSONObject>> {
        val placed = ArrayList<Pair<Int, JSONObject>>()
        for (e in page) {
            val id = e.optString("event_id")
            if (id.isEmpty() || !ids.add(id)) continue
            val h = Held(millis(e.optString("created_at")), id, e.optString("cursor"))
            var at = held.size
            while (at > 0 && order(held[at - 1], h) > 0) at--
            held.add(at, h)
            placed += at to e
        }
        return placed
    }

    /** A page has been added: next is the host's next_cursor for it (null: that was the end). */
    fun pageEnded(next: String?) { more = next }

    /** Where the next look starts: while catching up, straight after the last full page (never back over it, so
     *  a long history always moves on); null (from the start) until something is held; else the cursor of the last
     *  event older than the newest by OVERLAP_S (none: from the start, which is then short anyway). */
    fun resumeFrom(): String? {
        more?.let { return it }
        val newest = held.lastOrNull() ?: return null
        return held.lastOrNull { it.at <= newest.at - OVERLAP_S * 1000 && it.cursor.isNotEmpty() }?.cursor
    }

    private fun order(a: Held, b: Held) = compareValuesBy(a, b, { it.at }, { it.eventId })

    companion object {
        const val OVERLAP_S = 120L

        fun millis(utc: String): Long = try { java.time.Instant.parse(utc).toEpochMilli() } catch (e: Exception) { 0L }
    }
}
