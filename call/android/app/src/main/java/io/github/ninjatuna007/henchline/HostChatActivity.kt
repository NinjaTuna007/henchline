package io.github.ninjatuna007.henchline

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.annotation.RequiresApi

/**
 * A paired computer's chats over the signed v1 API (multi-host preview): its list of chats, or one chat with what
 * this phone sent there and what became of each message (waiting, delivered, failed, may not have gone).
 * Opened from Settings › Computers or by henchline://chat?host_id=…&session_id=… , which only says what to open:
 * opening a chat sends what is already queued for that computer (sooner than otherwise), never anything new.
 * Messages here go through that computer's own queue (HostOutbox) and nowhere else; the laptop's usual chat
 * screens keep their own outbox: each message has exactly one way out.
 */
class HostChatActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || !MultiHost.enabled(this)) { finish(); return }
        val target = ChatKey.parse(intent.data)
            ?: ChatKey.parse(intent.getStringExtra(HOST), intent.getStringExtra(SESSION)?.let { Canonical.b64url(it.toByteArray()) })
        if (target == null) { finish(); return }
        Screen(this, target.first, target.second).start()
    }

    override fun onResume() {
        super.onResume()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) screen?.refresh()
    }

    override fun onDestroy() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) screen?.close()
        super.onDestroy()
    }

    private var screen: Screen? = null

    @RequiresApi(MultiHost.MIN_SDK)
    private inner class Screen(val activity: Activity, val hostId: String, val sessionId: String?) {
        private val registry = HostRegistry.of(activity)
        private val store = Store(activity)
        private val dp = activity.resources.displayMetrics.density
        private fun px(v: Int) = (v * dp).toInt()
        private val list = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL; setPadding(px(16), px(8), px(16), px(8)) }
        private val banner = TextView(activity).apply {
            textSize = 13f; setTextColor(activity.getColor(R.color.muted)); setPadding(px(16), px(6), px(16), px(6)); visibility = View.GONE
        }
        private lateinit var host: Host

        fun start() {
            host = registry.get(hostId) ?: run {
                store.close()
                Toast.makeText(activity, "That computer isn't paired with this phone.", Toast.LENGTH_LONG).show()
                activity.finish(); return
            }
            screen = this
            val root = LinearLayout(activity).apply {
                orientation = LinearLayout.VERTICAL; setBackgroundColor(activity.getColor(R.color.navy))
                fitsSystemWindows = true
            }
            root.addView(TextView(activity).apply {
                text = if (sessionId == null) host.label else "$sessionId · ${host.label}"
                textSize = 20f; setTextColor(activity.getColor(R.color.text)); typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
                setPadding(px(16), px(16), px(16), px(4)); maxLines = 2
            })
            root.addView(banner)
            root.addView(ScrollView(activity).apply { addView(list) }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
            if (sessionId != null) root.addView(composer())
            activity.setContentView(root)
            refresh()
        }

        fun close() = store.close()

        private fun client() = HostClient(host, HostKeys.forAuthority("host", host.hostId))
        private fun chat() = HostChat(client(), host.hostId)

        private fun background(work: () -> Unit) = Thread { try { work() } catch (e: Exception) { show(explain(e)) } }.start()

        private fun explain(e: Exception): String = when (e) {
            is ClockSkew, is CapabilityMissing, is DescriptorRefused -> e.message ?: ""
            is java.io.IOException -> "Couldn't reach ${host.label}: ${e.message ?: "offline"}. Messages wait here and go when it's back."
            else -> e.message ?: e.javaClass.simpleName
        }

        private fun show(words: String?) = activity.runOnUiThread {
            banner.text = words ?: ""; banner.visibility = if (words.isNullOrEmpty()) View.GONE else View.VISIBLE
        }

        fun refresh() {
            if (!::host.isInitialized) return
            background {
                host = registry.get(hostId) ?: return@background
                if (sessionId == null) {
                    val chats = chat().chats()
                    activity.runOnUiThread { showChats(chats) }
                    show(null)
                } else {
                    val result = HostOutbox.flushHost(store, host) { chat() }  // what waits goes first
                    val events = try { allEvents() } catch (e: Exception) { show(explain(e)); null }
                    activity.runOnUiThread { showChat(events ?: emptyList()) }
                    if (events != null) show(result?.stoppedBy?.let { "Waiting to send: $it" })
                }
            }
        }

        private fun allEvents(): List<org.json.JSONObject> {
            val out = ArrayList<org.json.JSONObject>()
            var cursor: String? = null
            repeat(10) {
                val (page, next) = chat().events(sessionId!!, cursor)
                out += page
                cursor = next ?: return out
            }
            return out
        }

        private fun showChats(chats: List<HostChat.Summary>) {
            list.removeAllViews()
            if (chats.isEmpty()) list.addView(note("No agents are running on ${host.label} right now."))
            chats.forEach { c ->
                list.addView(TextView(activity).apply {
                    text = c.name.ifEmpty { c.sessionId }; textSize = 16f; setTextColor(activity.getColor(R.color.text))
                    setPadding(px(4), px(14), px(4), px(14))
                    setOnClickListener { activity.startActivity(open(activity, ChatKey(host.hostId, c.sessionId))) }
                })
            }
        }

        /** What this phone sent there over v1: the host's record where it has one, this phone's queue for the rest
         *  (waiting, failed, may not have gone). The agents' replies come to this screen in a later update. */
        private fun showChat(events: List<org.json.JSONObject>) {
            list.removeAllViews()
            val recorded = events.associateBy { it.optString("client_message_id") }
            val rows = store.rows(host.hostId, sessionId!!)
            val shown = HashSet<String>()
            events.forEach { e ->
                val text = e.optJSONObject("content")?.optString("text") ?: ""
                list.addView(bubble(text, if (e.optString("state") == "delivered") "✓ delivered" else "not delivered: ${e.optString("note")}"))
                shown += e.optString("client_message_id")
            }
            rows.filter { it.clientMessageId !in shown }.forEach { r ->
                val status = when (r.state) {
                    "waiting" -> "⏱ waiting to send"
                    "failed" -> "not sent: ${r.note}"
                    "maybe" -> "may not have gone: ${r.note}"
                    else -> if (recorded.containsKey(r.clientMessageId)) "✓ delivered" else "✓ sent"
                }
                list.addView(bubble(r.text, status))
            }
            if (events.isEmpty() && rows.isEmpty()) list.addView(note("Nothing sent to this chat from this phone yet."))
        }

        private fun note(words: String) = TextView(activity).apply {
            text = words; textSize = 14f; setTextColor(activity.getColor(R.color.muted)); setPadding(px(4), px(16), px(4), px(16))
        }

        private fun bubble(text: String, status: String) = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL; gravity = Gravity.END
            addView(TextView(activity).apply {
                this.text = text; textSize = 15f; setTextColor(activity.getColor(R.color.text))
                background = GradientDrawable().apply { setColor(activity.getColor(R.color.mine)); cornerRadius = 16 * dp }
                setPadding(px(12), px(8), px(12), px(8))
            }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END })
            addView(TextView(activity).apply {
                this.text = status; textSize = 12f; setTextColor(activity.getColor(R.color.muted)); setPadding(0, px(2), px(4), px(10))
            }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END })
        }

        private fun composer(): View {
            val box = EditText(activity).apply {
                hint = "Message"; setTextColor(activity.getColor(R.color.text)); setHintTextColor(activity.getColor(R.color.muted))
                maxLines = 6
            }
            val send = Button(activity).apply {
                text = "Send"; isAllCaps = false; setTextColor(activity.getColor(R.color.navy))
                background = GradientDrawable().apply { setColor(activity.getColor(R.color.cyan)); cornerRadius = 22 * dp }
                minHeight = px(48)
            }
            send.setOnClickListener {
                val text = box.text.toString().trim()
                if (text.isEmpty()) return@setOnClickListener
                box.setText("")
                background {
                    HostOutbox.enqueue(store, host, sessionId!!, text)  // queued first: it survives a crash or no network
                    refresh()
                }
            }
            return LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
                setPadding(px(12), px(8), px(12), px(12)); setBackgroundColor(activity.getColor(R.color.surface))
                addView(box, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                addView(send, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, px(48)).apply { marginStart = px(8) })
            }
        }
    }

    companion object {
        const val HOST = "host_id"
        const val SESSION = "session_id"

        fun open(context: Context, key: ChatKey) = Intent(context, HostChatActivity::class.java)
            .putExtra(HOST, key.hostId).putExtra(SESSION, key.sessionId)

        fun openHost(context: Context, hostId: String) = Intent(context, HostChatActivity::class.java).putExtra(HOST, hostId)
    }
}
