package io.github.ninjatuna007.henchline

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/**
 * Pushes from a paired computer other than the laptop (multi-host preview). Each push now names the computer it
 * came from (host_id, from its host.json). One from the laptop, or one naming no computer or one this phone isn't
 * paired with, goes exactly where it always went (RingService). One from another paired computer is shown as that
 * computer's: a text opens its chat (HostChatActivity), a file opens its link, and a ring rings as the laptop's do,
 * its Answer calling that computer from that agent's chat (RingService.answerIntent). A ring that can't be answered
 * that way (a call already up, no valid session) is a notification that opens the chat instead.
 *
 * The push address reaches each paired computer that takes signed requests on its own routes (legacy_routes) the
 * way it reaches the laptop: POST /ring-endpoint, signed by LegacySigner.
 *
 * A push's host_id is not authenticated, and whatever answers at the laptop's address can serve another computer's
 * public descriptor: either can only make a push go the old way (as the laptop's), never grant anything, since
 * nothing here acts on a push beyond showing it and opening that computer's chat.
 */
object HostPush {
    /** An address as an origin to compare: https://host[:port]/ with the host lowercased and the default port
     *  dropped; null when it isn't one. */
    fun origin(address: String?): String? {
        val url = address?.trim()?.toHttpUrlOrNull() ?: return null
        return url.newBuilder().username("").password("").encodedPath("/").query(null).fragment(null).build().toString()
    }

    /** The paired computer (not the laptop) a push came from, or null: handled as before. The laptop is known by
     *  its host id (laptopId, learned from the descriptor at its address), since a paired address may be spelled
     *  differently from the app's (a short name, an IP, another port). In doubt, the old way: no laptop address
     *  known, no host_id, an unpaired one, the laptop's own id or address; and while the laptop's id isn't known,
     *  anything but a text or a file (a ring stays a call). */
    fun sender(push: JSONObject, hosts: List<Host>, laptopOrigin: String?, laptopId: String?): Host? {
        val laptop = origin(laptopOrigin) ?: return null
        val id = push.optString("host_id").lowercase()
        if (id.isEmpty() || id == laptopId?.lowercase()) return null
        if (laptopId == null && !(push.has("text") || push.has("file"))) return null
        val host = hosts.firstOrNull { it.hostId == id } ?: return null
        return host.takeIf { origin(it.baseUrl).let { o -> o != null && o != laptop } }
    }

    /** The paired computers, or null when they can't be read (every push then goes the old way). */
    fun hostsOrNull(registry: HostRegistry): List<Host>? = try { registry.all() } catch (e: Exception) { null }

    /** Whether the laptop's id should be learned again: not known; it names no paired computer any more (re-paired,
     *  or its host.json replaced); or a ring names a paired computer at the laptop's own address that isn't the id
     *  held (the laptop's id changed at the same address). */
    fun shouldRelearn(push: JSONObject, hosts: List<Host>, laptopOrigin: String?, laptopId: String?): Boolean {
        val laptop = origin(laptopOrigin) ?: return false
        if (laptopId == null || hosts.none { it.hostId == laptopId }) return true
        val id = push.optString("host_id").lowercase()
        val isRing = !(push.has("text") || push.has("file"))
        return isRing && id.isNotEmpty() && id != laptopId && hosts.any { it.hostId == id && origin(it.baseUrl) == laptop }
    }

    private val learning = java.util.concurrent.atomic.AtomicBoolean()

    /** One look at a time, off the main thread. */
    private fun relearn(context: Context) {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.TIRAMISU || !learning.compareAndSet(false, true)) return
        Thread {
            try { learnLaptopId(context) } catch (_: Exception) {} finally { learning.set(false) }
        }.start()
    }

    /** The computers (not the laptop, by id or address) that should have this phone's push address: none while the
     *  laptop's address isn't known, since their pushes would then be taken for the laptop's. */
    fun recipients(hosts: List<Host>, laptopOrigin: String?, laptopId: String?): List<Host> {
        val laptop = origin(laptopOrigin) ?: return emptyList()
        return hosts.filter { h ->
            !h.revocationPending && offersLegacyRoutes(h) && h.hostId != laptopId?.lowercase() &&
                origin(h.baseUrl).let { it != null && it != laptop }
        }
    }

    /** The laptop's host id from the descriptor its address serves: a paired computer's id only when signed by that
     *  computer's pinned key (so no address can make another computer's pushes look like the laptop's); any other
     *  id when signed by the key it names (it then matches no paired computer anyway). */
    @androidx.annotation.RequiresApi(MultiHost.MIN_SDK)
    fun laptopIdFrom(descriptor: JSONObject, hosts: List<Host>): String? {
        val id = descriptor.optString("host_id").lowercase()
        if (id.isEmpty()) return null
        val key = hosts.firstOrNull { it.hostId == id }?.hostKey ?: descriptor.optString("host_signing_key")
        return id.takeIf { key.isNotEmpty() && HostDescriptor.verify(descriptor, key) }
    }

    private const val LAPTOP_ID = "laptop_host_id"
    private const val LAPTOP_FOR = "laptop_host_for"  // the laptop address it was learned at: another one forgets it

    private fun knownLaptopId(context: Context): String? {
        val prefs = context.getSharedPreferences("call", Context.MODE_PRIVATE)
        val at = origin(laptop(context)) ?: return null
        return prefs.getString(LAPTOP_ID, null)?.takeIf { prefs.getString(LAPTOP_FOR, null) == at }
    }

    /** Off the main thread: ask the laptop's address who it is (its signed descriptor), and remember it. */
    @androidx.annotation.RequiresApi(MultiHost.MIN_SDK)
    private fun learnLaptopId(context: Context) {
        val at = origin(laptop(context)) ?: return
        val answer = OkHttpTransport.send("GET", "${at}.well-known/henchline/v1/host".toHttpUrl(), null, emptyMap())
        if (answer.code != 200) return
        val descriptor = answer.json().optJSONObject("descriptor") ?: return
        val id = laptopIdFrom(descriptor, HostRegistry.of(context).all()) ?: return
        context.getSharedPreferences("call", Context.MODE_PRIVATE).edit().putString(LAPTOP_ID, id).putString(LAPTOP_FOR, at).apply()
    }

    private fun offersLegacyRoutes(host: Host): Boolean = try {
        JSONObject(host.descriptor).optJSONObject("capabilities")?.optBoolean("legacy_routes", false) ?: false
    } catch (e: Exception) { false }

    private fun laptop(context: Context): String? = Files.origin(context)

    /** Shown as that computer's when it came from one: true. False: RingService handles it as before, also when
     *  anything here fails (an unreadable hosts file): a push is never lost to this preview. */
    fun handle(context: Context, push: JSONObject): Boolean = try {
        handleOrThrow(context, push)
    } catch (e: Exception) {
        android.util.Log.w("HostPush", "handled the old way: ${e.javaClass.simpleName}")
        false
    }

    private fun handleOrThrow(context: Context, push: JSONObject): Boolean {
        if (!MultiHost.enabled(context)) return false
        val hosts = hostsOrNull(HostRegistry.of(context)) ?: return false
        val laptopId = knownLaptopId(context)
        if (shouldRelearn(push, hosts, laptop(context), laptopId)) relearn(context)  // for the next push
        val host = sender(push, hosts, laptop(context), laptopId) ?: return false
        val agent = IncomingActivity.nameOf(push.optString("agent", "handsfree"))
        val session = push.optString("session")
        when {
            push.has("text") -> {
                notify(context, "$agent on ${host.label}", push.optString("text"), chat(context, host, session), Messages.CHANNEL,
                    id = "${host.hostId} $session".hashCode())  // one per chat: a newer text replaces the last
                val tid = push.optString("id")
                if (tid.isNotEmpty()) post(host, "text-status", JSONObject().put("ids", listOf(tid).let { org.json.JSONArray(it) })
                    .put("status", "delivered"))  // one tick for the agent there; read is reported by its chat screen
            }
            push.optJSONObject("file") != null -> {
                val file = push.getJSONObject("file")
                val url = file.optString("url")
                val open = if (url.startsWith(host.baseUrl)) Intent(Intent.ACTION_VIEW, Uri.parse(url)) else chat(context, host, session)
                notify(context, "$agent on ${host.label} sent you ${file.optString("name", "a file")}",
                    push.optString("why").ifEmpty { "Tap to open it." }, open, Files.CHANNEL)
            }
            else -> {  // a ring: a real one, whose Answer calls this computer (RingService.answerIntent)
                val reply = Reply.valid(push.optString("reply"))
                Reply.send(reply, "rang")
                // On a call, another ring sounding (it keeps its record and its screen's Answer), or no agent to call: said
                if (CallService.Bus.service != null || Ringing.now(context) != null || !ChatKey.validSession(session)) {
                    notify(context, "$agent on ${host.label} wants you", push.optString("why").ifEmpty { "Open the chat to answer." },
                        chat(context, host, session), Messages.CHANNEL)
                } else {
                    val raw = push.optString("agent", "handsfree")
                    Calls.add(context, JSONObject().put("id", Calls.ringId(reply) ?: "ring-${System.currentTimeMillis()}").put("kind", "ring")
                        .put("agent", raw).put("session", session).put("why", push.optString("why")).put("host_id", host.hostId)
                        .put("outcome", if (reply == null) "rang" else "ringing"))
                    RingService.ringNow(context, raw, push.optString("why"), reply,
                        phrase = RingService.phraseFor(raw, push.optString("kind")), host = RingHost(host.hostId, session, host.label))
                }
            }
        }
        return true
    }

    /** Off the main thread (the hosts file, the self-test): the push address to every paired computer that takes it
     *  (signed). Never throws. */
    fun share(context: Context) {
        if (!context.getSharedPreferences("call", Context.MODE_PRIVATE).getBoolean(MultiHost.PREF, false)) return
        Thread {
            try {
                if (!MultiHost.enabled(context)) return@Thread
                val endpoint = context.getSharedPreferences("call", Context.MODE_PRIVATE).getString(RingService.ENDPOINT, null)
                    ?: return@Thread
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.TIRAMISU)
                    try { learnLaptopId(context) } catch (_: Exception) {}  // offline: what was learned before stands
                recipients(HostRegistry.of(context).all(), laptop(context), knownLaptopId(context))
                    .forEach { post(it, "ring-endpoint", JSONObject().put("endpoint", endpoint), wait = true) }
            } catch (e: Exception) {
                android.util.Log.w("HostPush", "push address not shared: ${e.javaClass.simpleName}")
            }
        }.start()
    }

    private fun post(host: Host, route: String, body: JSONObject, wait: Boolean = false) {
        val send = {
            try {
                val origin = host.baseUrl.trimEnd('/')
                Files.client.newCall(okhttp3.Request.Builder().url(host.baseUrl + route).header("Origin", origin)
                    .post(body.toString().toRequestBody("application/json".toMediaType())).build()).execute().close()
            } catch (_: Exception) {}  // offline: the next push, opening or pairing tries again
        }
        if (wait) send() else Thread { send() }.start()
    }

    private fun chat(context: Context, host: Host, session: String): Intent =
        (if (session.isEmpty()) HostChatActivity.openHost(context, host.hostId) else HostChatActivity.open(context, ChatKey(host.hostId, session)))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    private fun notify(context: Context, title: String, text: String, open: Intent, channel: String,
                       id: Int = (title + text + System.nanoTime()).hashCode()) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(channel,
            if (channel == Files.CHANNEL) "Files from your crew" else "Messages from your crew", NotificationManager.IMPORTANCE_HIGH))
        manager.notify(id, Notification.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_notify).setContentTitle(title).setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setContentIntent(PendingIntent.getActivity(context, id, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
            .setAutoCancel(true).build())
    }
}
