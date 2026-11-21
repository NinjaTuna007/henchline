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

/** One computer's chats over the signed v1 API. */
@RequiresApi(MultiHost.MIN_SDK)
class HostChat(private val client: HostClient, private val hostId: String) {
    data class Summary(val sessionId: String, val name: String, val current: Boolean)

    /** The chats there now, every page (bounded). */
    fun chats(): List<Summary> {
        client.require("chat")
        val out = ArrayList<Summary>()
        var cursor: String? = null
        repeat(20) {
            val answer = client.call("GET", listOf("api", "v1", "chats"),
                listOfNotNull("limit" to "200", cursor?.let { "cursor" to it }))
            if (answer.code != 200) throw java.io.IOException("chats: ${answer.code} ${answer.errorCode}")
            val page = answer.json()
            val list = page.optJSONArray("chats") ?: return out
            for (i in 0 until list.length()) list.getJSONObject(i).let {
                if (it.optString("host_id") == hostId) out += Summary(it.getString("session_id"), it.optString("name"), it.optBoolean("current"))
            }
            cursor = page.optString("next_cursor").takeIf { c -> !page.isNull("next_cursor") && c.isNotEmpty() } ?: return out
        }
        return out
    }

    /** That chat's events after cursor (null: from the start), and the cursor for more (null: none). */
    fun events(sessionId: String, cursor: String? = null): Pair<List<JSONObject>, String?> {
        client.require("chat")
        val answer = client.call("GET", listOf("api", "v1", "chats", sessionId, "events"),
            listOfNotNull("limit" to "200", cursor?.let { "cursor" to it }))
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

    /** The phone has kept the answer: the host may compact it (it still never runs twice). */
    fun ack(requestId: String): Boolean = try {
        client.call("POST", listOf("api", "v1", "requests", requestId, "ack")).code == 200
    } catch (e: java.io.IOException) {
        false
    }

    companion object {
        /** The message as it will be sent, every time. */
        fun body(hostId: String, sessionId: String, clientMessageId: String, text: String): String =
            JSONObject().put("host_id", hostId).put("session_id", sessionId).put("client_message_id", clientMessageId)
                .put("content", JSONObject().put("type", "text").put("text", text)).toString()

        fun newIds(): Pair<String, String> = UUID.randomUUID().toString() to UUID.randomUUID().toString()
    }
}

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

    /** chatFor: that computer's HostChat. A host whose removal is pending sends nothing. */
    fun flush(store: OutboxStore, hosts: List<Host>, chatFor: (Host) -> HostChat): Map<String, Result> =
        hosts.associate { host ->
            host.hostId to if (host.revocationPending) Result(0, store.waiting(host.hostId).size, "removal pending")
            else flushOne(store, host, chatFor)
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
