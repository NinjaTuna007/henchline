package io.github.ninjatuna007.henchline

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.IBinder
import android.provider.OpenableColumns
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * What was shared to Henchline, taken to the laptop's call server (any size: a long video takes a while, with
 * its progress in a notification), and then the agent told where it is. Over Tailscale, as the calls are.
 */
class UploadService : Service() {

    private val client = OkHttpClient.Builder().readTimeout(120, TimeUnit.SECONDS).writeTimeout(120, TimeUnit.SECONDS)
        .addNetworkInterceptor(LegacySigner).build()
    private val manager by lazy { getSystemService(NotificationManager::class.java) }
    private var busy = 0  // shares being sent: the service ends when none are

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "Sharing with your crew", NotificationManager.IMPORTANCE_LOW))
        startForeground(PROGRESS, progress("Sharing with your crew…", 0, 0), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        if (intent == null) {
            if (busy == 0) stopSelf()
            return START_NOT_STICKY
        }
        busy++
        val uris = intent.clipData?.let { clip -> (0 until clip.itemCount).map { clip.getItemAt(it).uri } } ?: emptyList()
        val names = intent.getStringArrayListExtra(NAMES) ?: arrayListOf()
        Thread {
            if (intent.getBooleanExtra(INTO_CHAT, false)) try {
                intoChat(uris, names, intent.getStringExtra(TEXT) ?: "", intent.getStringExtra(NOTE) ?: "",
                    intent.getStringExtra(SESSION) ?: "")
                CallService.Bus.listener?.invoke("""{"reload": true}""")  // the app, if open, shows it at once
            } catch (e: Exception) {
                android.util.Log.w("Henchline", "share not put in the chat: ${e.message}")
            }
            val result = try {
                share(uris, names, intent.getStringExtra(TEXT) ?: "", intent.getStringExtra(NOTE) ?: "",
                    intent.getStringExtra(SESSION) ?: "", intent.getBooleanExtra(SNAP, false))
            } catch (e: Exception) {
                "Not shared: ${e.message ?: "the laptop couldn't be reached"} (is Tailscale on?)"
            }
            CallService.Bus.listener?.invoke(JSONObject().put("note", result).toString())  // on the call screen too
            manager.notify(DONE + startId, Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("Henchline").setContentText(result)
                .setStyle(Notification.BigTextStyle().bigText(result)).setAutoCancel(true).build())
            android.os.Handler(mainLooper).post { if (--busy == 0) stopSelf() }
        }.start()
        return START_NOT_STICKY
    }

    /** Each file up, then the agent told; what to say about it. */
    /** The share in that agent's chat on the phone, a line a file (a picture with its small copy): made here, where
     *  the read grant lasts the whole upload (the share screen's ends as it closes). The only line it gets: the
     *  laptop adds none for a share. */
    private fun intoChat(uris: List<Uri>, names: List<String>, text: String, note: String, session: String) {
        val said = note.trim()
        if (uris.isEmpty()) Chats.add(this, session, JSONObject().put("from", "you").put("mine", true)
            .put("text", if (said.isEmpty()) text.take(300) else "${text.take(300)}\n$said"))
        uris.forEachIndexed { i, uri ->
            val name = names.getOrNull(i) ?: "file"
            val line = JSONObject().put("from", "you").put("mine", true)
                .put("text", "📎 $name" + if (i == uris.size - 1 && said.isNotEmpty()) "\n$said" else "")
            Files.keepThumb(this, uri)?.let { line.put("thumb", it) }
            Chats.add(this, session, line)
        }
    }

    private fun share(uris: List<Uri>, names: List<String>, text: String, note: String, session: String,
                      snap: Boolean = false): String {
        val origin = Files.origin(this) ?: return "Set the laptop's address in Henchline first."
        val saved = JSONArray()
        val failed = mutableListOf<String>()  // the others still go, and the agent hears about those that arrived
        uris.forEachIndexed { i, uri ->
            val name = names.getOrNull(i) ?: "file"
            val answer = try {
                post("$origin/share/file?name=${URLEncoder.encode(name, "UTF-8")}", origin, Streamed(uri, name, i + 1, uris.size))
            } catch (e: Exception) {
                JSONObject().put("note", e.message ?: "no connection")
            }
            answer.optString("saved").ifEmpty { null }?.let { saved.put(it) }
                ?: failed.add("$name (${answer.optString("note", "not kept")})")
        }
        if (uris.isNotEmpty() && saved.length() == 0) return "Not shared: ${failed.joinToString("; ")}"
        manager.notify(PROGRESS, progress("Telling your crew…", 0, 0))
        if (snap) {  // the call's camera: kept on the laptop for the next turn spoken, not told now
            val kept = post("$origin/share/snap", origin, JSONObject().put("files", saved).put("session", session)
                .toString().toRequestBody("application/json".toMediaTypeOrNull()))
            return if (kept.has("waiting")) "Photo with you: it goes with what you say next."
            else "Photo saved on the laptop, but: ${kept.optString("note")}"
        }
        val told = post("$origin/share/tell", origin, JSONObject().put("files", saved).put("text", text).put("note", note)
            .put("session", session).toString().toRequestBody("application/json".toMediaTypeOrNull()))
        val what = if (uris.isEmpty()) "that" else if (saved.length() == 1 && uris.size == 1) names.firstOrNull() ?: "the file"
            else "${saved.length()} files"
        val missing = if (failed.isEmpty()) "" else " Not sent: ${failed.joinToString("; ")}."
        return (if (told.has("to")) "Shared $what with ${told.getString("to").substringBefore(",")}."
        else "Saved $what on the laptop (Downloads/Henchline), but: ${told.optString("note")}") + missing
    }

    private fun post(url: String, origin: String, body: RequestBody): JSONObject =
        client.newCall(Request.Builder().url(url).header("Origin", origin).post(body).build()).execute().use { answer ->
            val text = answer.body?.string() ?: ""
            try { JSONObject(text) } catch (_: Exception) { JSONObject().put("note", "the laptop said ${answer.code}") }
        }

    /** A shared file read straight from the app that shared it, never copied on the phone first. */
    private inner class Streamed(val uri: Uri, val name: String, val n: Int, val of: Int) : RequestBody() {
        private val size = try {
            contentResolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use {
                if (it.moveToFirst() && !it.isNull(0)) it.getLong(0) else -1L
            } ?: -1L
        } catch (_: Exception) { -1L }

        override fun contentType() = (contentResolver.getType(uri) ?: "application/octet-stream").toMediaTypeOrNull()
        override fun contentLength() = size

        override fun writeTo(sink: BufferedSink) {
            val input = contentResolver.openInputStream(uri) ?: throw IllegalStateException("can't read $name")
            input.use {
                val buffer = ByteArray(1 shl 16)
                var sent = 0L
                var shown = 0L
                while (true) {
                    val got = it.read(buffer)
                    if (got < 0) break
                    sink.write(buffer, 0, got)
                    sent += got
                    if (sent - shown > 1 shl 20) {  // a megabyte at a time is enough for the bar
                        shown = sent
                        val title = if (of > 1) "Sharing $name ($n of $of)" else "Sharing $name"
                        manager.notify(PROGRESS, progress(title, sent, size))
                    }
                }
            }
        }
    }

    private fun progress(title: String, sent: Long, size: Long): Notification =
        Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_notify).setContentTitle(title)
            .setContentText(if (size > 0) "${Files.sizeText(sent)} of ${Files.sizeText(size)}" else Files.sizeText(sent))
            .setProgress(if (size > 0) 1000 else 0, if (size > 0) (sent * 1000 / size).toInt() else 0, size <= 0)
            .setOngoing(true).build()

    companion object {
        /** What to share: the files' read permission goes with them to the service. */
        fun intent(context: android.content.Context, uris: List<Uri>, names: List<String>, text: String, note: String,
                   session: String): Intent {
            val upload = Intent(context, UploadService::class.java).putStringArrayListExtra(NAMES, ArrayList(names))
                .putExtra(TEXT, text).putExtra(NOTE, note).putExtra(SESSION, session)
            if (uris.isNotEmpty()) {
                upload.clipData = android.content.ClipData.newRawUri("", uris[0]).apply { uris.drop(1).forEach { addItem(android.content.ClipData.Item(it)) } }
                upload.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            return upload
        }

        const val NAMES = "names"
        const val SNAP = "snap"  // from the call's camera button: kept for the next turn, not told at once
        const val INTO_CHAT = "into_chat"  // a share from another app: its lines in the chat come from here
        const val TEXT = "text"
        const val NOTE = "note"
        const val SESSION = "session"
        const val CHANNEL = "share"
        const val PROGRESS = 10
        const val DONE = 100
    }
}
