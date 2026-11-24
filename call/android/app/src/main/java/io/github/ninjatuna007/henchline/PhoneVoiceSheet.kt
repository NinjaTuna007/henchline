package io.github.ninjatuna007.henchline

import android.app.Activity
import android.content.Context
import android.view.Gravity
import android.view.ViewGroup
import android.widget.CheckBox
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.TextView

/** The phone voice's download, one at a time for the whole app: the Voices sheet and this sheet both show it, and it
 *  goes on if the sheet that started it is closed. */
object JennyDownload {
    private val runner = ModelJob("henchline-jenny-download")
    const val ABOUT_MB = 64L  // her size, said before the laptop is asked (the bar then shows the exact size)
    val job: ModelDownload? get() = runner.job
    val percent: Int get() = runner.percent
    val bytes: Long get() = runner.bytes  // of size, both exact once the laptop's list is read
    val size: Long get() = runner.size
    val phase: DownloadPhase? get() = runner.phase
    val note: String? get() = runner.note  // why the last one stopped, until the next starts

    fun listen(listener: () -> Unit) = runner.listen(listener)
    fun forget(listener: () -> Unit) = runner.forget(listener)

    /** Fetch Jenny from the laptop. mobileOk: "over mobile data too" is ticked; without it the download waits for
     *  Wi-Fi, also when Wi-Fi drops midway. thenUse: once she's here, the agents' voices come from the phone (and a
     *  call that's up switches to her). */
    fun start(context: Context, thenUse: Boolean, mobileOk: Boolean) {
        val app = context.applicationContext
        runner.start(app, PhoneVoice.spec(app), mobileOk) {
            if (thenUse) {
                val service = CallService.Bus.service
                if (service != null && !service.otherHost) service.useVoice(true) else PhoneVoice.setEnabled(app, true)
            }
        }
    }

    fun cancel() = runner.cancel()
}

/** A sheet that shows the phone voice: the download card and the preview, the same in the Voices sheet and in
 *  "Speak on the phone". */
open class VoicePanel(activity: Activity) : BottomPanel(activity) {
    private var cardBar: DownloadBar? = null  // the card's progress, while a download runs (or waits for Wi-Fi)
    private var cardDrawnRunning = false
    private var redrawAll: () -> Unit = {}
    private val onDownload: () -> Unit = {
        activity.runOnUiThread {
            if (!isOpen) return@runOnUiThread
            val bar = cardBar?.takeIf { it.isAttachedToWindow && cardDrawnRunning }
            val running = JennyDownload.job != null
            val phase = barPhase(JennyDownload.phase, running, JennyDownload.bytes)
            when {
                bar != null && running && phase != null -> bar.show(phase, JennyDownload.bytes, JennyDownload.size)
                bar != null && !running && PhoneVoice.ready(activity) -> bar.finish { if (isOpen) redrawAll() }
                else -> redrawAll()
            }
        }
    }

    /** Redraws the whole sheet when a download starts or ends; progress only moves the card's bar. */
    protected fun followDownload(redraw: () -> Unit) {
        redrawAll = redraw
        JennyDownload.listen(onDownload)
    }

    override fun onClosed() = JennyDownload.forget(onDownload)  // however the sheet was closed

    /** Jenny's download: her size and source, "over mobile data too", and Download; while it runs (or waits for
     *  Wi-Fi) its bar, with Cancel under it. thenUse: once she's here the agents' voices come from the phone. */
    protected fun downloadCard(thenUse: Boolean): LinearLayout = LinearLayout(activity).apply {
        orientation = LinearLayout.VERTICAL
        background = rounded(colour(R.color.surface_high), 18f); setPadding(px(16), px(16), px(16), px(16))
        val running = JennyDownload.job != null
        val justStopped = cardDrawnRunning && !running
        cardDrawnRunning = running
        val phase = barPhase(JennyDownload.phase, running, JennyDownload.bytes)
        val grow = cardBar == null  // the sheet's first bar grows in; one redrawn (it paused, say) stays where it was
        cardBar = phase?.let { DownloadBar(activity).apply {
            show(it, JennyDownload.bytes, JennyDownload.size, grow, justPaused = justStopped)
        } }
        cardBar?.let { addView(it) }
        val old = PhoneVoice.staleSize(activity) / 1_000_000
        val status = TextView(activity).apply {
            textSize = 14f; setTextColor(colour(R.color.text))
            text = JennyDownload.note ?: ("Jenny · about ${JennyDownload.ABOUT_MB} MB, from ${HostPush.computerName(activity)}" +
                if (old > 0) " (the earlier voice's $old MB go once she's here)" else "")
            if (phase != null) visibility = android.view.View.GONE  // the bar says it
        }
        if (!running) addView(status, wide(if (phase != null) 10 else 0))
        val mobile = CheckBox(activity).apply {
            text = "Download over mobile data too"; textSize = 13f; setTextColor(colour(R.color.muted))
            isChecked = VoiceSource.mobileTicked(PhoneVoice.mobileData(activity), CallService.Bus.service != null,
                !PhoneVoice.unmetered(activity))
            minimumHeight = px(48)
            setOnCheckedChangeListener { _, on -> PhoneVoice.setMobileData(activity, on) }
        }
        if (running) {
            addView(quiet("Cancel") { JennyDownload.cancel() }, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(6) })
            return@apply
        }
        addView(mobile, wide(if (phase != null) 12 else 8))
        addView(primary("Download Jenny").apply {
            setOnClickListener {
                if (JennyDownload.job != null) JennyDownload.cancel()
                else if (!mobile.isChecked && !PhoneVoice.unmetered(activity)) {
                    status.visibility = android.view.View.VISIBLE
                    status.text = "On Wi-Fi only: connect to Wi-Fi, or tick \"over mobile data too\"."
                } else JennyDownload.start(activity, thenUse, mobileOk = mobile.isChecked)
            }
        }, wide(10, px(52)))
    }

    /** Jenny's preview line, made on this phone: into the call when one is up, else on the media speaker. Loading her
     *  takes about 2.4 s the first time. done(true) as it starts, done(false) when she isn't here or wouldn't speak. */
    protected fun previewJenny(done: (Boolean) -> Unit) {
        Thread {
            val pcm = PhoneVoice.previewPcm(activity)
            val service = CallService.Bus.service
            activity.runOnUiThread { done(pcm != null) }  // as it starts playing
            if (pcm != null) {
                if (service != null) service.playPreview(pcm)
                else try { playLocally(pcm) } catch (e: Exception) { android.util.Log.w("Henchline", "phone voice: preview: $e") }
            }
            if (CallService.Bus.service == null) PhoneVoice.release()  // off a call her memory goes again
        }.apply { name = "henchline-jenny-preview"; start() }
    }

    private fun playLocally(pcm: ByteArray) {
        val track = android.media.AudioTrack.Builder()
            .setAudioAttributes(android.media.AudioAttributes.Builder().setUsage(android.media.AudioAttributes.USAGE_MEDIA)
                .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(android.media.AudioFormat.Builder().setEncoding(android.media.AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(PhoneVoice.RATE).setChannelMask(android.media.AudioFormat.CHANNEL_OUT_MONO).build())
            .setTransferMode(android.media.AudioTrack.MODE_STATIC).setBufferSizeInBytes(pcm.size).build()
        try {
            track.write(pcm, 0, pcm.size)
            track.play()
            Thread.sleep(pcm.size / 2 * 1000L / PhoneVoice.RATE + 200)
        } finally { track.release() }
    }
}

/** "Speak on the phone": the voice model's download (from the laptop, checked file by file), the switch, and room to
 *  delete it again. The switch is the same choice as the Voices sheet's "Laptop | Phone" (PhoneVoice.enabled). */
class PhoneVoiceSheet(activity: Activity, private val changed: () -> Unit = {}) : VoicePanel(activity) {
    private var body: LinearLayout? = null

    fun show() {
        val sheet = open("Speak on the phone", "Talkback in ${PhoneVoice.NAME}'s voice, made on this phone")
        val content = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        body = content
        sheet.addView(content, flexible(4))
        followDownload { draw() }
        draw()
        sheet.addView(quiet("Close") { close() }, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }

    private fun draw() {
        changed()  // the settings row and the Voices sheet say what this sheet now stands at
        val content = body ?: return
        content.removeAllViews()
        val ready = PhoneVoice.ready(activity)
        content.addView(TextView(activity).apply {
            val computer = HostPush.computerName(activity)
            text = "On a call, $computer sends the words and the phone says them itself, in one British voice for " +
                "every agent (not $computer's voices), so a weak signal (a lift, " +
                "a train) cuts no words off. It uses this phone's processor while it speaks; if the phone falls " +
                "behind, gets hot or runs low on battery, $computer's audio takes over by itself."
            textSize = 13f; setTextColor(colour(R.color.muted))
        }, wide(0))
        if (ready) content.addView(TextView(activity).apply {
            textSize = 14f; setTextColor(colour(R.color.text))
            text = "${PhoneVoice.NAME} · ready · ${PhoneVoice.sizeOnPhone(activity) / 1_000_000} MB on this phone"
        }, wide(14))
        val service = CallService.Bus.service
        content.addView(LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; minimumHeight = px(48)
            addView(TextView(activity).apply {
                text = "Speak on the phone"; textSize = 15f
                setTextColor(colour(if (ready) R.color.text else R.color.muted))
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(Switch(activity).apply {
                isChecked = ready && PhoneVoice.enabled(activity); isEnabled = ready && service?.otherHost != true
                setOnCheckedChangeListener { _, on ->
                    if (service != null && !service.otherHost) service.useVoice(on) else PhoneVoice.setEnabled(activity, on)
                    changed()
                }
            })
        }, wide(8))
        if (!ready) content.addView(downloadCard(thenUse = false), wide(10))
        else if (service?.otherHost != true) content.addView(quiet("Delete it from this phone") {  // not while she speaks a call
            if (service != null && !service.otherHost && PhoneVoice.enabled(activity)) service.useVoice(false)
            PhoneVoice.setEnabled(activity, false)
            Thread { PhoneVoice.delete(activity); activity.runOnUiThread { draw() } }.start()
        }, wide(10))
    }
}
