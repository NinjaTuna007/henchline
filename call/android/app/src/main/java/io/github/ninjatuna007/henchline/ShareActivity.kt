package io.github.ninjatuna007.henchline

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.EditText
import android.widget.LinearLayout

/**
 * Henchline in the phone's Share menu: any file (pictures, videos, audio, zips, documents…) or text from any
 * app, for the agent you are talking to. You can add a few words; then UploadService takes it to the laptop
 * and that agent is told where it landed.
 */
class ShareActivity : Activity() {
    private var sheet: Sheet? = null

    private var back: PredictiveBack? = null

    @Deprecated("the platform Activity's back handling, kept for Android 10-13; on 14 and later PredictiveBack calls it")
    override fun onBackPressed() {
        // The sheet slides away first, then the screen goes
        if (sheet?.isOpen == true) sheet?.close() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    /** Android 14 and later: a back swipe narrows the sheet as it goes (only while the sheet is open; otherwise the
     *  system's own back). Back going ahead is onBackPressed, as before. */
    private fun followBack() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE) return
        back = PredictiveBack(onBackInvokedDispatcher,
            wanted = { sheet?.isOpen == true && !keyboardUp(window.decorView) },
            begin = { sheet?.backFrame?.backPreview() },
            surfaceNow = { sheet?.backFrame },
            invoke = { @Suppress("DEPRECATION") onBackPressed() }).also { it.watch(window.decorView) }
    }

    override fun onDestroy() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) back?.release()
        back = null
        super.onDestroy()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        @Suppress("DEPRECATION")  // the typed forms need Android 13; this app runs from 10
        val uris = when (intent.action) {
            Intent.ACTION_SEND -> listOfNotNull(intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE -> intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM) ?: emptyList()
            else -> emptyList()
        }
        val text = if (uris.isEmpty()) intent.getStringExtra(Intent.EXTRA_TEXT) ?: "" else ""
        if (uris.isEmpty() && text.isBlank()) return finish()
        if (Files.origin(this) == null) {
            AlertDialog.Builder(this).setTitle("Henchline").setMessage("Set ${HostPush.computerName(this)}'s address in Henchline first.")
                .setPositiveButton("OK", null).setOnDismissListener { finish() }.show()
            return
        }
        val names = uris.map { Files.nameOf(this, it) }
        val crew = crew()
        if (crew.isEmpty()) {  // never on a call yet: nobody to pick from
            AlertDialog.Builder(this).setTitle("Henchline")
                .setMessage("Call your crew once from Henchline, so it knows who's in it, then share again.")
                .setPositiveButton("OK", null).setOnDismissListener { finish() }.show()
            return
        }
        sheet = Sheet(this, uris, names, text, crew, current().first.takeIf { s -> crew.any { it.first == s } } ?: crew[0].first)
            .also { it.show() }
        followBack()
    }

    /** The crew to pick from: on a call, as the laptop lists it now; otherwise as it was on the last call. */
    private fun crew(): List<Pair<String, String>> {
        val raw = CallService.Bus.lastAgents?.let { org.json.JSONObject(it).optJSONArray("agents")?.toString() }
            ?: getSharedPreferences("call", MODE_PRIVATE).getString("crew", "[]")
        return try {
            val list = org.json.JSONArray(raw)
            (0 until list.length()).map { list.getJSONObject(it).getString("session") to list.getJSONObject(it).getString("name") }
        } catch (_: Exception) { emptyList() }
    }

    /** The agent whose chat Henchline shows (kept across restarts): a share goes to that one, never elsewhere. */
    private fun current(): Pair<String, String> {
        val prefs = getSharedPreferences("call", MODE_PRIVATE)
        val session = prefs.getString("current", "") ?: ""
        val name = prefs.getString("current_name", "")?.substringBefore(",")?.ifEmpty { null } ?: "your agent"
        return session to name
    }
}
