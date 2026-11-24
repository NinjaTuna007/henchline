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
/** How a computer's chat is named and what it offers: pure, for the JVM tests. */
object HostChatRules {
    /** A timer's or a script's texts (handsfree text --from, or the computer's own notices): "reminders-<name>". */
    fun isReminders(sessionId: String) = sessionId.startsWith("reminders-")

    /** Who a Reminders chat is from: "Reminders" for the computer's own notices (reminders-reminders: a timer, the
     *  server, a text with no name), else the sender's name as handsfree text --from gave it (slugged lowercase there:
     *  "reminders-backup_job" is "Backup job"). */
    fun remindersFrom(sessionId: String): String {
        val from = sessionId.removePrefix("reminders-").replace('_', ' ').trim()
        return if (from.isEmpty() || from == "reminders") "Reminders" else from.replaceFirstChar { it.uppercase() }
    }

    /** As the laptop's chat list names it: a Reminders chat by its sender, else the name the computer gives, else
     *  its key. */
    fun name(sessionId: String, given: String = "") = if (isReminders(sessionId)) remindersFrom(sessionId) else given.ifEmpty { sessionId }

    fun title(sessionId: String?, label: String) = if (sessionId == null) label else "${name(sessionId)} · $label"

    /** Reminders take no replies and no calls: nothing reads them. */
    fun takesReplies(sessionId: String) = !isReminders(sessionId)
}

class HostChatActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || !MultiHost.enabled(this)) { finish(); return }
        val target = ChatKey.parse(intent.data)
            ?: ChatKey.parse(intent.getStringExtra(HOST), intent.getStringExtra(SESSION)?.let { Canonical.b64url(it.toByteArray()) })
        if (target == null) { finish(); return }
        Screen(this, target.first, target.second).start()
        // Answer on this computer's ring (RingService.answerIntent): the call, answering that ring. Only the app's own
        // Answer: never from a link (data), and only with the key and reply of the ring sounding now for this chat
        if (intent.hasExtra(ANSWERING)) {
            val reply = intent.getStringExtra(ANSWERING)?.ifEmpty { null }
            if (intent.data == null && target.second != null &&
                Ringing.mayAnswer(Ringing.now(this), target.first, target.second!!, reply, intent.getStringExtra(ANSWER_KEY) ?: ""))
                screen?.call(reply, answered = true)
        }
    }

    private val ticker = android.os.Handler(android.os.Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {  // while on screen: the agents' new texts show within a few seconds
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) screen?.refresh()
            seen()
            ticker.postDelayed(this, POLL_MS)
        }
    }

    /** On screen now: only then is a text drawn here reported read. */
    @Volatile private var resumed = false

    /** A call with this computer shows here (CallService keeps the laptop's screens out of it). */
    private val callWatcher: (String) -> Unit = { message ->
        runOnUiThread { if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) screen?.fromCall(message) }
    }

    override fun onResume() {
        super.onResume()
        resumed = true
        ticker.post(tick)
        CallService.Bus.hostListener = callWatcher
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) screen?.showCall()
        RemoteChatMotion.prepareClose(this, screen != null)  // a back swipe shows the close as it goes
    }

    /** This chat is open on screen: what it holds is read, for the home list's unread (HostLists). */
    private fun seen() {
        val shown = screen ?: return
        if (resumed && shown.sessionId != null) HostLists.markSeen(this, shown.hostId, shown.sessionId)
    }

    override fun onPause() {
        seen()  // the last look before leaving counts too
        resumed = false
        ticker.removeCallbacks(tick)
        if (CallService.Bus.hostListener === callWatcher) CallService.Bus.hostListener = null  // the call goes on
        super.onPause()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != MIC || Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (grantResults.firstOrNull() == android.content.pm.PackageManager.PERMISSION_GRANTED) screen?.call(pendingAnswer, pendingAnswered)
        else screen?.say("A call needs the microphone: allow it for Henchline in the phone's settings.")
    }

    /** Closing a chat that was drawn fades it and shrinks it over the list (RemoteChatMotion). */
    override fun finish() {
        val shown = screen != null
        RemoteChatMotion.beforeFinish(this, shown)
        super.finish()
        RemoteChatMotion.afterFinish(this, shown)
    }

    override fun onDestroy() {
        ticker.removeCallbacks(tick)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) screen?.close()
        super.onDestroy()
    }

    private var screen: Screen? = null
    private var pendingAnswer: String? = null  // the ring being answered while the microphone is asked for
    private var pendingAnswered = false

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
                text = HostChatRules.title(sessionId, host.label)
                textSize = 20f; setTextColor(activity.getColor(R.color.text)); typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
                setPadding(px(16), px(16), px(16), px(4)); maxLines = 2
            })
            root.addView(banner)
            if (sessionId != null) root.addView(callBar)
            root.addView(elsewhere)
            if (sessionId != null) { list.addView(events); list.addView(queue); list.addView(callLines); empty.visibility = View.GONE; list.addView(empty) }
            scroll.addView(list)
            root.addView(scroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
            // A timer's or a script's texts (Reminders) take no replies, as the laptop's: no Call, no message box
            if (sessionId != null && HostChatRules.takesReplies(sessionId)) root.addView(composer())
            activity.setContentView(root)
            Motion.rippleAll(root)  // its buttons and rows: a ripple; the filled ones press in
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

        private fun showChats(chats: List<ChatSummary>) {
            list.removeAllViews()
            if (chats.isEmpty()) list.addView(note("No agents are running on ${host.label} right now."))
            chats.forEach { c ->
                list.addView(TextView(activity).apply {
                    text = HostChatRules.name(c.sessionId, c.name); textSize = 16f; setTextColor(activity.getColor(R.color.text))
                    setPadding(px(4), px(14), px(4), px(14))
                    setOnClickListener { activity.startActivity(open(activity, ChatKey(host.hostId, c.sessionId))) }
                    Motion.ripple(this)
                })
            }
        }

        /** New events go where they belong among those drawn (nothing drawn is redrawn: a selection stays); then
         *  this phone's queue for what the host has no record of yet (waiting, failed, may not have gone). The view
         *  follows the newest only when it was already at the bottom (or on the first draw). */
        private fun draw(fresh: List<Pair<Int, org.json.JSONObject>>, rows: List<QueuedRow>) {
            val atBottom = !drawnOnce || !scroll.canScrollVertically(1)
            fresh.forEach { (at, e) ->
                // One view per event, always (the timeline's positions count them): an unknown kind is an empty one
                when (ChatEvents.lookOf(e)) {
                    EventLook.AGENT_TEXT -> {
                        events.addView(theirs(e.optString("agent").replaceFirstChar { it.uppercase() }.ifEmpty { "Agent" }, e.optString("text")), at)
                        drawnTexts += e.optString("event_id")
                    }
                    EventLook.MINE -> {
                        val text = e.optJSONObject("content")?.optString("text") ?: ""
                        events.addView(bubble(text, ChatEvents.mineStatus(e)), at)
                        onHost += e.optString("client_message_id")
                    }
                    // What was said on a call, as the computer kept it: drawn as the live lines are, in its place; the
                    // live copy of the same line (this screen's call) goes, so it isn't shown twice
                    EventLook.CALL_YOU, EventLook.CALL_THEM -> {
                        val from = e.optString("from"); val text = e.optString("text")
                        events.addView(if (ChatEvents.lookOf(e) == EventLook.CALL_YOU) yours(text, "said on the call")
                            else theirs(from.ifEmpty { "Agent" }, text), at)
                        dropLive(ChatEvents.callKey(from, text), ChatEvents.ofCall(e, CallService.Bus.hostLines.startedFor(hostId, sessionId ?: "")))
                    }
                    EventLook.NONE -> events.addView(View(activity).apply { visibility = View.GONE }, at)
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

        /** A line mirrored as yours: a helper's report or a reminder (Relayed) is drawn as from them, not you. */
        private fun yours(text: String, status: String): View =
            Relayed.of(text)?.let { theirs(it.label, it.text) } ?: bubble(text, status)

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

        // ---- a call with this computer's agent (CallService with HOST_ID): its bar, and what is said on it -------
        private val callState = TextView(activity).apply { textSize = 14f; setTextColor(activity.getColor(R.color.text)) }
        private fun callButton(label: String, action: String) = Button(activity).apply {
            text = label; isAllCaps = false; setTextColor(activity.getColor(R.color.navy)); minHeight = px(40)
            background = GradientDrawable().apply { setColor(activity.getColor(R.color.cyan)); cornerRadius = 20 * dp }
            setOnClickListener { activity.startService(Intent(activity, CallService::class.java).setAction(action)) }
        }
        private val talk = callButton("Talk", CallService.TALK)
        private val callBar = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; visibility = View.GONE
            setPadding(px(16), px(6), px(16), px(6)); setBackgroundColor(activity.getColor(R.color.surface))
            addView(callState, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(talk)
            addView(callButton("Hang up", CallService.STOP), LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT).apply { marginStart = px(8) })
        }
        private val callLines = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }  // this screen's call only
        private var shownSeq = -1  // main thread: the last call line drawn here
        // main thread: kept call lines drawn before their live copy came (a replay): each absorbs one live copy
        private val keptAhead = HashMap<String, Int>()

        /** A call line the computer kept is drawn: its live copy goes (the first one this screen shows), or, not shown
         *  yet and said on the latest call (ofLatest), the next one that comes is skipped. A line said twice is two
         *  lines either way; an older call's kept lines never hide a later call's ("yes", "okay"). */
        private fun dropLive(key: String, ofLatest: Boolean) {
            for (i in 0 until callLines.childCount) if (callLines.getChildAt(i).tag == key) { callLines.removeViewAt(i); return }
            if (ofLatest) keptAhead[key] = (keptAhead[key] ?: 0) + 1
        }

        fun say(words: String) = show(words)

        private fun ours() = CallService.Bus.service != null && CallService.Bus.host == hostId && CallService.Bus.hostSession == sessionId

        /** Elsewhere: a call with another chat (here or on another computer) is up; tapping goes there. */
        private val elsewhere = TextView(activity).apply {
            textSize = 13f; setTextColor(activity.getColor(R.color.cyan)); setPadding(px(16), px(6), px(16), px(6)); visibility = View.GONE
            setOnClickListener {
                val h = CallService.Bus.host ?: return@setOnClickListener
                activity.startActivity(open(activity, ChatKey(h, CallService.Bus.hostSession)))
            }
        }

        /** The bar's words on a call: and who is listening, the phone or this computer by its name. */
        private fun onACall() = "On a call · " + PhoneEar.listener(CallService.Bus.phoneEars,
            CallService.Bus.service?.computerName ?: HostPush.nameOr(host.label))

        /** The bar as the call is now (coming back to this screen mid-call). */
        fun showCall() {
            val other = CallService.Bus.host?.takeIf { CallService.Bus.service != null && !ours() }
            elsewhere.visibility = if (other != null) View.VISIBLE else View.GONE
            if (other != null) elsewhere.text = "On a call with ${CallService.Bus.hostSession}" +
                (if (other == hostId) "" else " on another computer") + ": tap to open it"
            if (sessionId == null) return
            // What was said while this screen was away (or before it was opened again): each line once, in order
            CallService.Bus.hostLines.of(hostId, sessionId).forEach { fromCall(it, replay = true) }
            CallService.Bus.hostLines.takeClosed(hostId, sessionId)?.let { show(it) }  // it ended while away
            callBar.visibility = if (ours()) View.VISIBLE else View.GONE
            if (ours()) callState.text = if (CallService.Bus.hostConnected) onACall() else "Connecting…"
        }

        /** Call this chat's agent on this computer (answering: the ring answered, which the computer routes to the
         *  agent that rang): one call at a time, the laptop's included. */
        fun call(answering: String? = null, answered: Boolean = answering != null) {
            if (sessionId != null && !HostChatRules.takesReplies(sessionId)) return  // no agent to talk to
            val session = sessionId ?: return  // a computer's chat list: nobody to call
            if (ours()) { showCall(); return }
            if (CallService.Bus.service != null) {
                if (answered) Ringing.end(activity)  // its ring stops: it can't be answered over this call
                show("Already on a call: hang up that one first."); return
            }
            if (activity.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                pendingAnswer = answering; pendingAnswered = answered
                activity.requestPermissions(arrayOf(android.Manifest.permission.RECORD_AUDIO), MIC); return
            }
            pendingAnswer = null; pendingAnswered = false
            // A computer makes no speech of its own: the phone speaks every reply with Jenny, so she must be here
            if (!PhoneVoice.ready(activity)) {
                if (answered) Ringing.end(activity)
                show(CallService.NEEDS_JENNY); return
            }
            if (answered) Ringing.end(activity)  // answered: the ring stops everywhere
            val prefs = activity.getSharedPreferences("call", Context.MODE_PRIVATE)
            callLines.removeAllViews(); shownSeq = -1
            activity.startForegroundService(Intent(activity, CallService::class.java)
                .putExtra(CallService.SERVER, host.baseUrl.trimEnd('/'))
                .putExtra(CallService.HOST_ID, host.hostId).putExtra(CallService.HOST_SESSION, session)
                .putExtra(CallService.CLEAR, prefs.getBoolean("clear", true))
                // A ring answered starts on the loudspeaker, as the laptop's does (the phone may be on the desk)
                .putExtra(CallService.SPEAKER, answered || prefs.getBoolean("speaker", true))
                .putExtra(IncomingActivity.REPLY, answering))
            callBar.visibility = View.VISIBLE; callState.text = "Connecting…"
        }

        /** What the call says (main thread). */
        fun fromCall(json: String, replay: Boolean = false) {
            val m = try { org.json.JSONObject(json) } catch (e: Exception) { return }
            // Only this chat's own call (replayed lines are this chat's already): another's only updates the note
            if (!replay && (m.optString(CallService.CALL_HOST) != hostId || m.optString(CallService.CALL_SESSION) != sessionId)) {
                showCall()
                if (m.has("closed")) elsewhere.visibility = View.GONE  // that other call is over
                return
            }
            if (m.has("closed")) CallService.Bus.hostLines.takeClosed(hostId, sessionId ?: "")  // seen here: not again
            when {
                m.has("opened") -> { callBar.visibility = View.VISIBLE; callState.text = onACall(); keptAhead.clear() }
                m.has("reconnecting") -> callState.text = if (m.optBoolean("reconnecting")) "Reconnecting…" else onACall()
                m.has("closed") -> { callBar.visibility = View.GONE; show(m.optString("closed")) }
                m.has("ears") -> if (CallService.Bus.hostConnected) callState.text = onACall()  // the ears switched
                m.has("talking") -> talk.text = if (m.optBoolean("talking")) "Send" else "Talk"
                m.has("note") -> show(m.optString("note"))
            }
            m.optJSONObject("chat")?.let { line ->
                val seq = m.optInt("seq", -1)
                if (seq <= shownSeq) return  // drawn already (told live, then replayed on coming back)
                shownSeq = seq
                val atBottom = !scroll.canScrollVertically(1)
                val key = ChatEvents.callKey(line.optString("from"), line.optString("text"))
                val ahead = keptAhead[key] ?: 0
                if (ahead > 0) { keptAhead[key] = ahead - 1; return }  // the computer's kept copy is drawn already
                callLines.addView((if (line.optString("from").equals("you", ignoreCase = true)) yours(line.optString("text"), "said on the call")
                    else theirs(line.optString("from").ifEmpty { "Agent" }, line.optString("text"))).apply { tag = key })
                if (callLines.childCount > CallService.HostLines.MAX) callLines.removeViewAt(0)  // as many as are kept
                empty.visibility = View.GONE
                if (atBottom) scroll.post { scroll.scrollTo(0, list.height) }
            }
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
                addView(Button(activity).apply {
                    text = "Call"; isAllCaps = false; setTextColor(activity.getColor(R.color.text)); minHeight = px(48)
                    background = GradientDrawable().apply { setStroke(px(1), activity.getColor(R.color.cyan)); cornerRadius = 22 * dp }
                    setOnClickListener { call() }
                }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, px(48)).apply { marginEnd = px(8) })
                addView(box, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                addView(send, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, px(48)).apply { marginStart = px(8) })
            }
        }
    }

    companion object {
        const val POLL_MS = 5000L
        const val PAGES_PER_LOOK = 10
        private const val MIC = 7
        const val HOST = "host_id"
        const val SESSION = "session_id"

        fun open(context: Context, key: ChatKey) = Intent(context, HostChatActivity::class.java)
            .putExtra(HOST, key.hostId).putExtra(SESSION, key.sessionId)

        const val ANSWERING = "answering"
        const val ANSWER_KEY = "answer_key"

        /** Answer on a ring from this computer: its chat, which calls it answering that ring (with the ring's key). */
        fun answer(context: Context, key: ChatKey, reply: String?, answerKey: String) = open(context, key)
            .putExtra(ANSWERING, reply ?: "").putExtra(ANSWER_KEY, answerKey)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)

        fun openHost(context: Context, hostId: String) = Intent(context, HostChatActivity::class.java).putExtra(HOST, hostId)
    }
}
