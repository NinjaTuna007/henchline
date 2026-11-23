package io.github.ninjatuna007.henchline

import android.app.Activity
import android.app.NotificationManager
import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject

/**
 * An agent is calling: its henchman, its name, a line of henchman-speak (spoken over its own ringtone, in
 * its talkback voice), why it rang, and Answer / Decline. Shown over the lock screen; unanswered, it gives
 * up after RingService.RING_SECONDS.
 */
class IncomingActivity : Activity() {

    private val main = Handler(Looper.getMainLooper())
    private var ring: MediaPlayer? = null
    private var reply: String? = null  // where the laptop hears how this ring went
    private var told = false  // the laptop was told how it ended: answered, declined or missed
    private var agent = "handsfree"
    private var why = ""
    private var phrase = 0  // which of the henchman's lines: "I need you" or "done" (RingService.phraseFor)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setShowWhenLocked(true)
        setTurnScreenOn(true)
        showing = java.lang.ref.WeakReference(this)
        agent = (intent.getStringExtra(AGENT) ?: "").takeIf { it in FLAVOURS } ?: "handsfree"
        why = intent.getStringExtra(WHY) ?: ""
        reply = intent.getStringExtra(REPLY)
        phrase = intent.getIntExtra(PHRASE, 0)
        val lines = phrasesOf(agent)

        val dp = resources.displayMetrics.density
        val screen = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setBackgroundColor(getColor(R.color.navy))
            setPadding((24 * dp).toInt(), (96 * dp).toInt(), (24 * dp).toInt(), (64 * dp).toInt())
        }
        screen.addView(TextView(this).apply {
            text = "${RingService.callerOf(this@IncomingActivity, agent, reply)} is calling"
            setTextColor(getColor(R.color.text))
            textSize = 28f
            gravity = Gravity.CENTER
        })
        val face = ImageView(this).apply { setImageResource(MainActivity.avatarOf(agent)) }
        screen.addView(face, LinearLayout.LayoutParams((220 * dp).toInt(), (220 * dp).toInt()).apply { topMargin = (32 * dp).toInt() })
        screen.addView(TextView(this).apply {  // the banner: what it is saying
            text = "“${lines.getOrElse(phrase) { lines[0] }}”"  // the line this ring says
            setTextColor(getColor(R.color.cyan))
            textSize = 20f
            gravity = Gravity.CENTER
            setPadding(0, (24 * dp).toInt(), 0, (8 * dp).toInt())
        })
        if (why.isNotEmpty()) screen.addView(TextView(this).apply {
            text = why
            setTextColor(getColor(R.color.muted))
            textSize = 15f
            gravity = Gravity.CENTER
        })
        screen.addView(android.view.View(this), LinearLayout.LayoutParams(1, 0, 1f))
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER }
        fun round(icon: Int, background: Int, what: String, then: (android.view.View) -> Unit) = ImageButton(this).apply {
            setImageResource(icon)
            setBackgroundResource(background)
            setColorFilter(getColor(android.R.color.white))
            contentDescription = what
            setOnClickListener { then(it) }
        }
        val decline = round(R.drawable.ic_call_end, R.drawable.round_hang, "Decline") {
            it.performHapticFeedback(RingMotion.declineHaptic(android.os.Build.VERSION.SDK_INT))
            tell("declined"); Ringing.end(this); stop()
        }
        buttons.addView(decline, LinearLayout.LayoutParams((76 * dp).toInt(), (76 * dp).toInt()).apply { marginEnd = (88 * dp).toInt() })
        val answer = round(R.drawable.ic_phone, R.drawable.round_call, "Answer") {
            it.performHapticFeedback(RingMotion.answerHaptic(android.os.Build.VERSION.SDK_INT))
            told = true  // the call tells the laptop the rest (answered, or couldn't connect)
            ended = true
            RingService.answerIntent(this, reply)?.let { startActivity(it) }  // the laptop's call, or the ringing computer's
            stop()
        }
        buttons.addView(answer, LinearLayout.LayoutParams((76 * dp).toInt(), (76 * dp).toInt()))
        screen.addView(buttons)
        setContentView(screen)
        if (RingMotion.enters(Motion.on(), savedInstanceState != null)) enter(face, listOf(decline, answer), dp)

        // The very sound the banner rings with (banner_mix.py: the tone, the phrases on its beat), so the two
        // never differ (2026-11-15: they said different phrases)
        ring = player(listOf("banner_${agent}_$phrase", "banner_$agent", "ring_$agent").map { resourceId(it) }.firstOrNull { it != 0 } ?: 0, loop = true,
            volume = loudness(), from = ::bannerPlace)
        // This screen rings now: the notification stays, silent, for Answer and Decline and to come back to (after
        // the screen's sound has started, so the banner's stops into it rather than before it)
        RingService.ringNow(this, agent, why, reply, quiet = true, seconds = maxOf(1, Ringing.secondsLeft(this)), phrase = phrase)
        main.postDelayed({ tell("missed"); Ringing.end(this); stop() }, maxOf(1, Ringing.secondsLeft(this)) * 1000)
    }

    private val springs = ArrayList<androidx.dynamicanimation.animation.SpringAnimation>()
    private var moving: List<android.view.View> = emptyList()

    /** Coming up ([RingMotion]): the henchman springs up from small as it fades in; the buttons rise 24 dp into
     *  place and fade in, Decline then Answer. Taps work throughout. */
    private fun enter(face: android.view.View, buttons: List<android.view.View>, dp: Float) {
        moving = buttons + face
        face.scaleX = RingMotion.AVATAR_FROM; face.scaleY = RingMotion.AVATAR_FROM; face.alpha = 0f
        Motion.spring(face, androidx.dynamicanimation.animation.DynamicAnimation.SCALE_X, 1f)?.let { springs.add(it) }
        Motion.spring(face, androidx.dynamicanimation.animation.DynamicAnimation.SCALE_Y, 1f)?.let { springs.add(it) }
        face.animate().alpha(1f).setDuration(Motion.MEDIUM).setInterpolator(Motion.standard).start()
        buttons.forEachIndexed { place, button ->
            button.translationY = RingMotion.SLIDE_DP * dp; button.alpha = 0f
            button.animate().translationY(0f).alpha(1f).setStartDelay(RingMotion.delayOf(place))
                .setDuration(RingMotion.BUTTON_MS).setInterpolator(Motion.emphasizedDecelerate).start()
        }
    }

    /** The coming up stopped where it is, everything in place and whole. */
    private fun settle() {
        springs.forEach { it.cancel() }; springs.clear()
        for (v in moving) { v.animate().cancel(); v.scaleX = 1f; v.scaleY = 1f; v.translationY = 0f; v.alpha = 1f }
        moving = emptyList()
    }

    /** In Do Not Disturb the ring plays as an alarm, and the alarm volume is usually far above the ringer's (15 of 15
     *  against 6 of 15 on the user's phone: the full screen blared, 2026-11-15): scaled down to the ringer's level. */
    // Set once, when the ring screen opens: Do Not Disturb or the ringer changed mid-ring (30 s) doesn't rescale it
    private fun loudness(): Float {
        if (!DoNotDisturb.on(this)) return 1f
        val audio = getSystemService(android.media.AudioManager::class.java)
        fun level(stream: Int) = audio.getStreamVolume(stream).toFloat() / audio.getStreamMaxVolume(stream).coerceAtLeast(1)
        val alarm = level(android.media.AudioManager.STREAM_ALARM)
        return if (alarm <= 0f) 1f else (level(android.media.AudioManager.STREAM_RING) / alarm).coerceIn(0.15f, 1f)
    }

    /** Where the banner's sound is now, to carry on from rather than start again (the user's wish, 2026-11-15):
     *  it began when the ring did and loops, so it is the time since then (player() takes it round the file's
     *  length). About 0 when this screen is the ring's first. */
    private fun bannerPlace(): Int {
        val started = (Ringing.now(this)?.optLong("until") ?: 0) - RingService.RING_SECONDS * 1000
        return if (started > 0) (System.currentTimeMillis() - started + LATENCY).toInt() else 0
    }

    /** A sound that won't load just isn't played: the screen, its buttons and the time-out go on. */
    private fun player(id: Int, loop: Boolean, volume: Float, from: () -> Int = { 0 }): MediaPlayer? {
        if (id == 0) return null
        val sound = MediaPlayer()
        return try {
            sound.apply {
                // In Do Not Disturb a ringtone is silenced: the ring plays as an alarm, which gets through
                val usage = if (DoNotDisturb.on(this@IncomingActivity)) AudioAttributes.USAGE_ALARM else AudioAttributes.USAGE_NOTIFICATION_RINGTONE
                setAudioAttributes(AudioAttributes.Builder().setUsage(usage)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                resources.openRawResourceFd(id).use { setDataSource(it.fileDescriptor, it.startOffset, it.length) }
                isLooping = loop
                setVolume(volume, volume)
                prepare()
                val place = from().coerceAtLeast(0) % duration.coerceAtLeast(1)
                if (place > 0) {  // a seek is asynchronous: start once it has landed, never heard jumping
                    setOnSeekCompleteListener { p -> if (p === ring) try { p.start() } catch (_: IllegalStateException) {} }
                    setOnErrorListener { p, _, _ ->  // a failed seek: ring from the top rather than not at all
                        if (p === ring) try { p.seekTo(0); p.start() } catch (_: IllegalStateException) {}
                        true
                    }
                    seekTo(place)
                } else start()
            }
        } catch (e: Exception) {
            android.util.Log.w("Henchline", "ring sound failed: ${e.message}")
            sound.release()
            null
        }
    }

    private fun stop() {  // safe to call more than once, and with no sound loaded
        main.removeCallbacksAndMessages(null)
        ring?.release()
        ring = null
        if (!isFinishing) finish()
    }

    /** Left without a button (back, home, another app, the power key): the call keeps ringing as a banner with
     *  Answer and Decline until it times out, as a phone call does (2026-11-15: leaving counted as declining,
     *  and then left nothing behind). On stop, which leaving always brings; a screen rebuilt (rotation, dark
     *  mode) doesn't stop this one (configChanges). */
    override fun onStop() {
        super.onStop()
        if (told || ended || Ringing.now(this) == null) return
        ended = true
        stop()
        RingService.ringNow(this, agent, why, reply, banner = true, seconds = maxOf(1, Ringing.secondsLeft(this)), phrase = phrase)
    }

    override fun onDestroy() {
        settle()  // nothing of the coming up outlives the screen
        stop()
        if (showing?.get() === this) showing = null
        super.onDestroy()
    }

    override fun onUserLeaveHint() {  // home or recents: leaving the ring, not answering it
        super.onUserLeaveHint()
        if (!isFinishing) finish()
    }

    private var ended = false  // stopped by Ringing.end: answered elsewhere, or over

    /** Ended from outside (answered from the app, or a call made by hand): no banner after it. */
    fun quietly() {
        ended = true
        told = true
        stop()
    }

    private fun tell(what: String) {
        if (told) return
        told = true
        Reply.send(reply, what)
        Calls.outcome(this, reply, what)
    }

    private fun resourceId(name: String) = resources.getIdentifier(name, "raw", packageName)

    private fun phrasesOf(agent: String): List<String> {
        val all = JSONObject(resources.openRawResource(R.raw.phrases).bufferedReader().readText())
        val lines = all.optJSONArray(agent) ?: all.getJSONArray("handsfree")
        return (0 until lines.length()).map { lines.getString(it) }
    }

    companion object {
        @Volatile var showing: java.lang.ref.WeakReference<IncomingActivity>? = null  // the ring screen, while up
        const val AGENT = "agent"
        const val WHY = "why"
        const val REPLY = "reply"
        const val PHRASE = "phrase"
        const val LATENCY = 150L  // ms: this screen's sound starts this much after the seek, so it seeks that far ahead
        const val SAY_GAP = 700L  // ms between the phrase ending and starting again
        val FLAVOURS = setOf("claude", "cursor", "codex", "handsfree")  // henchmen, tones and phrases there are

        fun nameOf(agent: String) = when (agent) {
            "claude" -> "Claude"
            "cursor" -> "Cursor"
            "codex" -> "Codex"
            "reminders" -> "Reminders"
            else -> "Your crew"
        }
    }
}
