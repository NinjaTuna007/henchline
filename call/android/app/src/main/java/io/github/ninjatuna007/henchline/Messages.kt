package io.github.ninjatuna007.henchline

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Person
import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/**
 * Agents texting you (`handsfree text` on the laptop) while no call is up: a messaging notification per agent
 * session, with a reply box right in it (the user's wish, 2026-11-15: "it's a messaging system now, not
 * calling"). A reply goes to that agent over Tailscale, tagged [phone · text]. Both
 * sides are kept in that agent's chat in the app.
 */
object Messages {
    const val CHANNEL = "texts"
    @Volatile var viewing = ""  // the chat on screen right now (MainActivity), so it gets no banner
    const val REPLY = "io.github.ninjatuna007.henchline.REPLY_TEXT"
    private const val KEY = "reply"

    private fun me(context: Context) = Person.Builder().setName("You")
        .setIcon(Icon.createWithResource(context, R.drawable.avatar_you)).build()

    private fun them(context: Context, agent: String) = Person.Builder().setName(IncomingActivity.nameOf(agent))
        .setIcon(Icon.createWithResource(context, MainActivity.avatarOf(agent))).setImportant(true).build()

    /** An agent's message: shown with the last few of the conversation, and a reply box. */
    fun show(context: Context, agent: String, session: String, text: String, tid: String = "") {
        // Once: it may come both as a push and from the app's own catch-up (Messages.catchUp)
        val line = JSONObject().put("from", IncomingActivity.nameOf(agent)).put("text", text).put("mine", false)
        if (tid.isNotEmpty()) line.put("tid", tid)
        val added = synchronized(Messages) {  // the look and the add as one: a push and the catch-up can race
            if (tid.isNotEmpty() && Chats.load(context, session).any { it.optString("tid") == tid }) false
            else { Chats.add(context, session, line); true }
        }
        if (!added) {
            receipt(context, listOf(tid), "delivered")
            return
        }
        if (tid.isNotEmpty()) receipt(context, listOf(tid), "delivered")  // it reached the phone: one tick for the agent
        Chats.readAll(context, session)  // the agent answered: your earlier lines there are read
        if (viewing != session) post(context, agent, session)  // no banner for the chat on screen (2026-11-16)
        // The app open on that chat shows it at once, as a line from a call would (2026-11-15: a text reached the
        // notification and the kept chat, but the open screen didn't redraw)
        CallService.Bus.listener?.invoke(JSONObject().put("chat", JSONObject().put("from", IncomingActivity.nameOf(agent))
            .put("text", text).put("session", session)).toString())
    }

    /** Agents' texts the laptop sent that never arrived as a push: asked for while the app is open (pushes once sat
     *  undelivered for half an hour with the phone in hand, 2026-11-18). Off the main thread. */
    fun catchUp(context: Context) {
        val origin = Files.origin(context) ?: return
        try {
            QUICK.newCall(Request.Builder().url("$origin/texts/pending").header("Origin", origin).build()).execute().use { answer ->
                val list = JSONObject(answer.body?.string() ?: "{}").optJSONArray("texts") ?: return
                for (i in 0 until list.length()) {
                    val t = list.optJSONObject(i) ?: continue
                    show(context, t.optString("agent"), t.optString("session"), t.optString("text"), t.optString("id"))
                }
            }
        } catch (_: Exception) {}  // the laptop unreachable: the next look tries again
    }

    /** The notification for that session, from its kept chat: the latest lines, yours and the agent's. */
    fun post(context: Context, agent: String, session: String, sending: Boolean = false) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "Messages from your crew", NotificationManager.IMPORTANCE_HIGH))
        val style = Notification.MessagingStyle(me(context))
        for (line in Chats.load(context, session).takeLast(6)) {
            val file = line.optJSONObject("file")
            val text = line.optString("text").ifEmpty { if (file != null) "📎 " + file.optString("name") else "" }
            if (text.isEmpty()) continue
            style.addMessage(text, line.optLong("at"), if (line.optBoolean("mine")) null else them(context, agent))
        }
        val id = session.hashCode()
        val reply = PendingIntent.getBroadcast(context, id, Intent(context, ReplyReceiver::class.java).setAction(REPLY)
            .putExtra("agent", agent).putExtra("session", session),
            PendingIntent.FLAG_MUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)  // mutable: the reply text is added to it
        val open = PendingIntent.getActivity(context, id, Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_IMMUTABLE)
        val built = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_notify)
            .setStyle(style)
            .setCategory(Notification.CATEGORY_MESSAGE)
            .setContentIntent(open)
            .setOnlyAlertOnce(sending)  // your own reply landing in it doesn't buzz
            .setAutoCancel(true)
        // A reminder (a timer's or a script's text) has no agent to read a reply: no reply box on it
        if (!MainActivity.isReminders(session)) built.addAction(Notification.Action.Builder(null, "Reply", reply)
            .addRemoteInput(RemoteInput.Builder(KEY).setLabel("Reply to ${IncomingActivity.nameOf(agent)}").build())
            .setAllowGeneratedReplies(false).build())
        manager.notify(id, built.build())
    }

    /** Words to an agent session over Tailscale, within 8 s: "" when an agent took them, else why not. */
    /** (a note why it wasn't sent, or ""; whether it waits in the agent's queue: one tick until it takes it in).
     *  urgent: the held send button: the agent's work is interrupted so it gets the words now. */
    /** How a text went: why not (""), whether it waits in the agent's queue, whether the laptop couldn't be reached
     *  at all (then it waits in the Outbox, with a clock). */
    data class Sent(val note: String, val queued: Boolean, val offline: Boolean = false)

    fun text(context: Context, session: String, words: String, id: String = "", urgent: Boolean = false,
             quick: Boolean = false): Sent = try {
        val origin = Files.origin(context) ?: throw IllegalStateException("no laptop address set")
        // A delivery can take a while (a Cursor chat, a turn behind another); a timeout is taken as "away" and sent
        // again from the outbox, which the laptop answers by id without typing it twice
        (if (quick) QUICK else PATIENT).newCall(Request.Builder().url("$origin/text").header("Origin", origin)
            .post(JSONObject().put("text", words).put("session", session).put("id", id).put("urgent", urgent).toString()
                .toRequestBody("application/json".toMediaType())).build()).execute().use { answer ->
            val body = try { JSONObject(answer.body?.string() ?: "") } catch (_: Exception) { JSONObject() }
            if (body.has("to")) Sent("", body.optBoolean("queued"))
            else Sent(body.optString("note", "the laptop said ${answer.code}"), false)
        }
    } catch (e: Exception) {
        Sent("Not sent: ${e.message ?: "the laptop couldn't be reached"} (is Tailscale on?)", false, offline = true)
    }

    private val QUICK = okhttp3.OkHttpClient.Builder().callTimeout(8, java.util.concurrent.TimeUnit.SECONDS).addNetworkInterceptor(LegacySigner).build()
    private val PATIENT = okhttp3.OkHttpClient.Builder().callTimeout(30, java.util.concurrent.TimeUnit.SECONDS).addNetworkInterceptor(LegacySigner).build()

    /** How far an agent's texts got, told to the laptop (`handsfree text-status`): "delivered" when on the phone,
     *  "read" when you opened that chat or replied. Best effort: off Tailscale it is just not told. */
    fun receipt(context: Context, ids: List<String>, status: String) {
        // Kept until the laptop has them: one that doesn't arrive (Tailscale off, the laptop restarting) goes again
        // with the next, so `handsfree text --until-read` never rings for a text you've read
        val prefs = context.getSharedPreferences("call", Context.MODE_PRIVATE)
        val key = "pending_$status"
        synchronized(this) {
            val pending = ((prefs.getStringSet(key, emptySet()) ?: emptySet()) + ids).toList().takeLast(300).toSet()  // bounded
            prefs.edit().putStringSet(key, pending).apply()
        }
        Thread {
            val send = synchronized(this) { prefs.getStringSet(key, emptySet())?.toList() ?: emptyList() }
            if (send.isEmpty()) return@Thread
            try {
                val origin = Files.origin(context) ?: return@Thread
                QUICK.newCall(Request.Builder().url("$origin/text-status").header("Origin", origin)
                    .post(JSONObject().put("ids", org.json.JSONArray(send)).put("status", status).toString()
                        .toRequestBody("application/json".toMediaType())).build()).execute().use { answer ->
                    if (answer.isSuccessful) synchronized(this) {
                        val left = (prefs.getStringSet(key, emptySet()) ?: emptySet()) - send.toSet()
                        prefs.edit().putStringSet(key, left).apply()
                    }
                }
            } catch (_: Exception) {
            }
        }.start()
    }

    /** You've seen that chat: its agents' texts not yet marked read are, and the laptop is told. */
    fun seen(context: Context, session: String) {
        // Its notification goes too: you're reading that chat (2026-11-16)
        context.getSystemService(NotificationManager::class.java).cancel(session.hashCode())
        receipt(context, Chats.markSeen(context, session), "read")  // also sends any earlier ones still waiting
    }

    /** A reply typed into the notification: to the laptop, then into the chat and the notification. */
    class ReplyReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val words = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(KEY)?.toString()?.trim() ?: return
            val agent = intent.getStringExtra("agent") ?: "handsfree"
            val session = intent.getStringExtra("session") ?: ""
            val pending = goAsync()
            Thread {
                try {
                    send(context, agent, session, words)
                } finally {
                    pending.finish()
                }
            }.start()
        }

        /** Within the receiver's ten seconds: an unreachable laptop is "not sent", not a hang. */
        private fun send(context: Context, agent: String, session: String, words: String) {
            seen(context, session)  // replying means reading
            val id = java.util.UUID.randomUUID().toString()
            // Behind messages still waiting in the outbox, to keep their order; else within the receiver's ten seconds
            val sent = if (Outbox.all(context).isNotEmpty()) Sent("", false, offline = true)
                       else text(context, session, words, id, quick = true)
            // Queued behind the agent's work: one tick until it takes it in (the chat's poll turns it to two); the
            // laptop away: a clock, and the Outbox sends it once it can
            if (sent.offline) Outbox.add(context, id, session, words)
            Chats.add(context, session, JSONObject().put("from", "you").put("mine", true).put("id", id)
                .put("text", if (sent.note.isEmpty() || sent.offline) words else "$words  (${sent.note})")
                .put("status", when { sent.offline -> "waiting"; sent.note.isEmpty() && !sent.queued -> "delivered"; else -> "sent" }))
            post(context, agent, session, sending = true)
        }
    }
}
