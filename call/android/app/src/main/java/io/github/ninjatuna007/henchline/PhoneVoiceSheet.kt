package io.github.ninjatuna007.henchline

import android.app.Activity
import android.view.Gravity
import android.view.ViewGroup
import android.widget.CheckBox
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.TextView

/** "Speak on the phone": the voice model's download (from the laptop, checked file by file), the switch, and room to
 *  delete it again. While it is off or missing, calls stream the laptop's audio as always. */
class PhoneVoiceSheet(activity: Activity, private val changed: () -> Unit = {}) : BottomPanel(activity) {
    @Volatile private var download: PhoneVoice.Download? = null
    private var status: TextView? = null
    private var body: LinearLayout? = null

    fun show() {
        val sheet = open("Speak on the phone", "Talkback in the same voices, made on this phone")
        val content = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        body = content
        sheet.addView(content, flexible(4))
        draw()
        sheet.addView(quiet("Close") { download?.cancel(); close() }, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { gravity = Gravity.END; topMargin = px(8) })
    }

    private fun draw() {
        changed()  // the settings row says On or Off as this sheet now stands (it showed Off after a switch on)
        val content = body ?: return
        content.removeAllViews()
        val ready = PhoneVoice.ready(activity)
        content.addView(TextView(activity).apply {
            text = "On a call, the laptop sends the words and the phone says them itself, so a weak signal (a lift, " +
                "a train) cuts no words off. It uses this phone's processor while it speaks; if the phone falls " +
                "behind, gets hot or runs low on battery, the laptop's audio takes over by itself."
            textSize = 13f; setTextColor(colour(R.color.muted))
        }, wide(0))
        status = TextView(activity).apply {
            textSize = 14f; setTextColor(colour(R.color.text))
            text = if (ready) "Ready · ${PhoneVoice.sizeOnPhone(activity) / 1_000_000} MB on this phone"
                   else "Not downloaded yet · about 360 MB, from your laptop"
        }
        content.addView(status, wide(14))
        content.addView(LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; minimumHeight = px(48)
            addView(TextView(activity).apply {
                text = "Speak on the phone"; textSize = 15f
                setTextColor(colour(if (ready) R.color.text else R.color.muted))
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(Switch(activity).apply {
                isChecked = ready && PhoneVoice.enabled(activity); isEnabled = ready
                setOnCheckedChangeListener { _, on -> PhoneVoice.setEnabled(activity, on); changed() }
            })
        }, wide(8))
        content.addView(TextView(activity).apply {
            text = "Takes effect from the next call."
            textSize = 12f; setTextColor(colour(R.color.muted))
        }, wide(0))
        if (!ready) {
            content.addView(CheckBox(activity).apply {
                text = "Download over mobile data too"; textSize = 13f; setTextColor(colour(R.color.muted))
                isChecked = PhoneVoice.mobileData(activity); minimumHeight = px(48)
                setOnCheckedChangeListener { _, on -> PhoneVoice.setMobileData(activity, on) }
            }, wide(10))
            content.addView(primary(if (download != null) "Cancel download" else "Download").apply {
                setOnClickListener { if (download != null) download?.cancel() else start() }
            }, wide(10, px(52)))
        } else {
            content.addView(quiet("Delete it from this phone") {
                PhoneVoice.setEnabled(activity, false)
                Thread { PhoneVoice.delete(activity); activity.runOnUiThread { draw() } }.start()
            }, wide(10))
        }
    }

    private fun start() {
        if (!PhoneVoice.networkAllows(activity)) {
            status?.text = "On Wi-Fi only: connect to Wi-Fi, or tick \"over mobile data too\"."
            return
        }
        val job = PhoneVoice.Download(activity) { done, total, name ->
            activity.runOnUiThread {
                if (isOpen) status?.text = "Downloading… ${if (total > 0) done * 100 / total else 0}% · $name"
            }
        }
        download = job
        draw()
        Thread {
            val why = job.run()
            download = null
            activity.runOnUiThread {
                if (!isOpen) return@runOnUiThread
                draw()
                if (why != null) status?.text = why
            }
        }.start()
    }
}
