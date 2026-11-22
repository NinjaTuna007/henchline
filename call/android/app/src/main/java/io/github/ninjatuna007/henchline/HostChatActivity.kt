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

    private val ticker = android.os.Handler(android.os.Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {  // while on screen: the agents' new texts show within a few seconds
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) screen?.refresh()
            ticker.postDelayed(this, POLL_MS)
        }
    }

    /** On screen now: only then is a text drawn here reported read. */
    @Volatile private var resumed = false

    override fun onResume() {
        super.onResume()
        resumed = true
        ticker.post(tick)
    }

    override fun onPause() {
        resumed = false
        ticker.removeCallbacks(tick)
        super.onPause()
    }

    override fun onDestroy() {
        ticker.removeCallbacks(tick)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) screen?.close()
        super.onDestroy()
    }

    private var screen: Screen? = null

    /** The screen's network and store work runs on one worker thread, one piece at a time: a look at the host
     *  (send what waits, fetch what's new) never overlaps a send's enqueue or a receipt, and the store is closed
     *  on that same thread after whatever was already running. Views are touched on the main thread only. */
    @RequiresApi(MultiHost.MIN_SDK)
    private inner class Screen(val activity: Activity, val hostId: String, val sessionId: String?) {
        private val registry = HostRegistry.of(activity)
        private val store = Store(activity)
        private val worker = java.util.concurrent.Executors.newSingleThreadExecutor()
        @Volatile private var closed = false
        private val dp = activity.resources.displayMetrics.density
        private fun px(v: Int) = (v * dp).toInt()
        private val list = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL; setPadding(px(16), px(8), px(16), px(8)) }
        private val events = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }  // one view per event held
        private val queue = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }   // what the host has no record of yet
        private val empty = note("Nothing here yet.")
        private val scroll = ScrollView(activity)
        private val banner = TextView(activity).apply {
            textSize = 13f; setTextColor(activity.getColor(R.color.muted)); setPadding(px(16), px(6), px(16), px(6)); visibility = View.GONE
        }
        @Volatile private lateinit var host: Host
        private val lookQueued = java.util.concurrent.atomic.AtomicBoolean()
        private val timeline = ChatTimeline()  // worker thread only
        private val drawnTexts = LinkedHashSet<String>()  // main thread: agents' texts drawn here
        private val receipted = HashSet<String>()  // worker thread only: reported read from this screen
        private val onHost = HashSet<String>()     // main thread: client message ids the host has recorded
        private var queueShown: List<Triple<String, String, String>>? = null  // main thread
        private var drawnOnce = false              // main thread

        fun start() {
            host = registry.get(hostId) ?: run {
                close()
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
            if (sessionId != null) { list.addView(events); list.addView(queue); empty.visibility = View.GONE; list.addView(empty) }
            scroll.addView(list)
            root.addView(scroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
            if (sessionId != null) root.addView(composer())
            activity.setContentView(root)
            refresh()
        }

        /** Leaving: a message queued here still goes (one last send of what waits, after any enqueue already
         *  on the worker), then the store closes. */
        fun close() {
            closed = true
            try {
                worker.execute {
                    try {
                        if (sessionId != null && ::host.isInitialized && store.waiting(host.hostId).isNotEmpty())
                            HostOutbox.flushHost(store, host) { chat() }
                    } catch (e: Exception) {}
                    store.close()
                }
            } catch (e: java.util.concurrent.RejectedExecutionException) {}
            worker.shutdown()
        }

        private fun client() = HostClient(host, HostKeys.forAuthority("host", host.hostId))
        private fun chat() = HostChat(client(), host.hostId)

        /** On the worker, unless the screen is gone by the time it gets there (always: run even so, before the
         *  store closes, as a message being queued must be). */
        private fun work(always: Boolean = false, job: () -> Unit) = try {
            worker.execute { if (always || !closed) try { job() } catch (e: Exception) { show(explain(e)) } }
        } catch (e: java.util.concurrent.RejectedExecutionException) {}

        private fun ui(job: () -> Unit) = activity.runOnUiThread { if (!closed && !activity.isDestroyed) job() }

        private fun explain(e: Exception): String = when (e) {
            is ClockSkew, is CapabilityMissing, is DescriptorRefused -> e.message ?: ""
            is java.io.IOException -> "Couldn't reach ${host.label}: ${e.message ?: "offline"}. Messages wait here and go when it's back."
            else -> e.message ?: e.javaClass.simpleName
        }

        private fun show(words: String?) = ui {
            banner.text = words ?: ""; banner.visibility = if (words.isNullOrEmpty()) View.GONE else View.VISIBLE
        }

        /** A look at the host soon. One already waiting to start covers this call too: it starts after whatever
         *  asked (a send enqueues on the worker first), so nothing queued waits for the next tick. */
        fun refresh() {
            if (closed || !::host.isInitialized || !lookQueued.compareAndSet(false, true)) return
            work { lookQueued.set(false); look() }
        }

        private fun look() {
            host = registry.get(hostId) ?: return
            if (sessionId == null) {
                val chats = chat().chats()
                ui { showChats(chats) }
                show(null)
                return
            }
            val result = HostOutbox.flushHost(store, host) { chat() }  // what waits goes first
            // Only what follows where this screen got to (a little before it: a text filed late), each event once
            val fresh = ArrayList<Pair<Int, org.json.JSONObject>>()
            val trouble = try {
                var cursor = timeline.resumeFrom()
                for (i in 0 until PAGES_PER_LOOK) {  // a long history comes in over several looks, oldest first
                    val (page, next) = chat().events(sessionId, cursor)
                    fresh += timeline.add(page)
                    timeline.pageEnded(next)
                    cursor = next ?: break
                }
                null
            } catch (e: Exception) { explain(e) }
            val rows = store.rows(host.hostId, sessionId)
            ui {
                draw(fresh, rows)
                // After drawing, and only while on screen: what is reported read was in front of the user. Texts
                // drawn while away are reported by the first look after coming back.
                if (resumed) drawnTexts.toList().let { drawn -> work { readReceipts(drawn) } }
            }
            show(trouble ?: result?.stoppedBy?.let { "Waiting to send: $it" })
            if (trouble == null && timeline.catchingUp) refresh()  // the rest of a long history now, not next tick
        }

        /** The agents' texts drawn here while the screen was on, reported read in the host's chunks; each chunk
         *  recorded once the host answered (a failed one is tried again on the next look). */
        private fun readReceipts(drawn: List<String>) {
            val unread = drawn.filter { it !in receipted }
            for (chunk in unread.chunked(HostChat.RECEIPT_IDS)) {
                try { chat().receipt(sessionId!!, chunk) } catch (e: Exception) { return }
                receipted += chunk
            }
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

        /** New events go where they belong among those drawn (nothing drawn is redrawn: a selection stays); then
         *  this phone's queue for what the host has no record of yet (waiting, failed, may not have gone). The view
         *  follows the newest only when it was already at the bottom (or on the first draw). */
        private fun draw(fresh: List<Pair<Int, org.json.JSONObject>>, rows: List<QueuedRow>) {
            val atBottom = !drawnOnce || !scroll.canScrollVertically(1)
            fresh.forEach { (at, e) ->
                if (e.optString("kind") == "agent_text") {
                    events.addView(theirs(e.optString("agent").replaceFirstChar { it.uppercase() }.ifEmpty { "Agent" }, e.optString("text")), at)
                    drawnTexts += e.optString("event_id")
                } else {
                    val text = e.optJSONObject("content")?.optString("text") ?: ""
                    events.addView(bubble(text, if (e.optString("state") == "delivered") "✓ delivered" else "not delivered: ${e.optString("note")}"), at)
                    onHost += e.optString("client_message_id")
                }
            }
            val waiting = rows.filter { it.clientMessageId !in onHost }
            val summary = waiting.map { Triple(it.clientMessageId, it.state, it.note) }
            if (summary != queueShown) {  // redrawn only when it changed
                queueShown = summary
                queue.removeAllViews()
                waiting.forEach { r ->
                    val status = when (r.state) {
                        "waiting" -> "⏱ waiting to send"
                        "failed" -> "not sent: ${r.note}"
                        "maybe" -> "may not have gone: ${r.note}"
                        else -> "✓ sent"
                    }
                    queue.addView(bubble(r.text, status))
                }
            }
            empty.visibility = if (events.childCount == 0 && queue.childCount == 0) View.VISIBLE else View.GONE
            drawnOnce = true
            if (atBottom && (fresh.isNotEmpty() || summary.isNotEmpty())) scroll.post { scroll.scrollTo(0, list.height) }  // not fullScroll: it takes focus from the box
        }

        private fun note(words: String) = TextView(activity).apply {
            text = words; textSize = 14f; setTextColor(activity.getColor(R.color.muted)); setPadding(px(4), px(16), px(4), px(16))
        }

        /** An agent's text: on the left, its name above. */
        private fun theirs(name: String, text: String) = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL; gravity = Gravity.START
            addView(TextView(activity).apply {
                this.text = name; textSize = 12f; setTextColor(activity.getColor(R.color.muted)); setPadding(px(4), px(6), 0, px(2))
            })
            addView(TextView(activity).apply {
                this.text = text; textSize = 15f; setTextColor(activity.getColor(R.color.text)); setTextIsSelectable(true)
                background = GradientDrawable().apply { setColor(activity.getColor(R.color.surface)); cornerRadius = 16 * dp }
                setPadding(px(12), px(8), px(12), px(8))
            }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = px(10) })
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
                work(always = true) {
                    HostOutbox.enqueue(store, host, sessionId!!, text)  // queued first: it survives a crash or no network
                    refresh()  // a look starts after this, never before it
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
        const val POLL_MS = 5000L
        const val PAGES_PER_LOOK = 10
        const val HOST = "host_id"
        const val SESSION = "session_id"

        fun open(context: Context, key: ChatKey) = Intent(context, HostChatActivity::class.java)
            .putExtra(HOST, key.hostId).putExtra(SESSION, key.sessionId)

        fun openHost(context: Context, hostId: String) = Intent(context, HostChatActivity::class.java).putExtra(HOST, hostId)
    }
}
