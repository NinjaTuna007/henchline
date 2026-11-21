package io.github.ninjatuna007.henchline

import android.app.Activity
import android.app.AlertDialog
import android.os.Build
import android.text.InputType
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.annotation.RequiresApi

/** Settings › Computers (multi-host preview): the paired computers, pairing another, removing one. Network work
 *  runs off the main thread; dialogs come back on it. Nothing here touches the single-laptop address or chats. */
@RequiresApi(MultiHost.MIN_SDK)
class HostsSheet(private val activity: Activity, private val onChanged: () -> Unit = {}) {
    private val registry = HostRegistry.of(activity)
    private val pairing = Pairing()
    private fun dp(v: Int) = (v * activity.resources.displayMetrics.density).toInt()

    fun show() {
        val hosts = registry.all()
        val rows = hosts.map { h ->
            h.label + "\n" + h.baseUrl.removePrefix("https://").trimEnd('/') +
                if (h.revocationPending) "\nRemoval pending: still paired there" else ""
        } + "Pair a computer…"
        AlertDialog.Builder(activity)
            .setTitle("Computers")
            .setItems(rows.toTypedArray()) { _, which -> if (which < hosts.size) details(hosts[which]) else askAddress() }
            .setNegativeButton("Done", null)
            .show()
    }

    private fun background(work: () -> Unit) = Thread {
        try { work() } catch (e: Exception) {
            val title = if (e is ClockSkew) "Check the phone's clock" else "Something went wrong"
            activity.runOnUiThread { tell(title, e.message ?: e.javaClass.simpleName) }
        }
    }.start()

    private fun tell(title: String, words: String, then: () -> Unit = {}) {
        AlertDialog.Builder(activity).setTitle(title).setMessage(words)
            .setPositiveButton("OK") { _, _ -> then() }.setOnCancelListener { then() }.show()
    }

    private fun box(hint: String, text: String = "", code: Boolean = false) = EditText(activity).apply {
        this.hint = hint; setText(text); setSingleLine()
        inputType = if (code) InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS or
            InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS else InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
    }

    private fun padded(vararg views: android.view.View) = LinearLayout(activity).apply {
        orientation = LinearLayout.VERTICAL; setPadding(dp(24), dp(8), dp(24), 0)
        views.forEach { addView(it) }
    }

    private fun askAddress(typed: String = "") {
        val address = box("laptop.your-tailnet.ts.net", typed)
        AlertDialog.Builder(activity)
            .setTitle("Pair a computer")
            .setMessage("On the computer, run: voice-call pair\nIt shows its address, a fingerprint and a code.")
            .setView(padded(address))
            .setPositiveButton("Next") { _, _ ->
                val text = address.text.toString()
                background {
                    val candidate = try { pairing.lookUp(pairing.origin(text)) } catch (e: PairingError) {
                        activity.runOnUiThread { tell("Not found", e.message ?: "") { askAddress(text) } }
                        return@background
                    }
                    activity.runOnUiThread { askCode(candidate) }
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun askCode(candidate: Candidate) {
        if (registry.get(candidate.hostId) != null) {
            tell("Already paired", "${candidate.label} is already paired with this phone.")
            return
        }
        val code = box("ABCDE-FGHJK", code = true)
        val fingerprint = TextView(activity).apply {
            text = candidate.fingerprint; textSize = 20f; typeface = android.graphics.Typeface.MONOSPACE
            setTextColor(activity.getColor(R.color.text)); setPadding(0, dp(8), 0, dp(12))
        }
        AlertDialog.Builder(activity)
            .setTitle(candidate.label)
            .setMessage("Check that the computer shows this same fingerprint. If it doesn't, cancel: something else is answering at that address.")
            .setView(padded(fingerprint, code))
            .setPositiveButton("It matches: pair") { _, _ ->
                val typed = code.text.toString()
                background {
                    val existed = HostKeys.exists("host", candidate.hostId)  // a retry reuses the same key
                    val host = try {
                        pairing.claim(candidate, typed, { HostKeys.forAuthority("host", candidate.hostId) }, Build.MODEL ?: "Phone")
                    } catch (e: PairingError) {
                        // A key made for this try and never sent anywhere is dropped; one that may be paired is kept
                        if (!e.sent && !existed) HostKeys.delete("host", candidate.hostId)
                        activity.runOnUiThread { tell("Not paired", e.message ?: "") }
                        return@background
                    }
                    registry.put(host)
                    activity.runOnUiThread {
                        onChanged()
                        tell("Paired", "${host.label} is paired. Its chats come in a later update of this preview.") {
                            if (isTheLaptop(host)) offerImport(host) else show()
                        }
                    }
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun details(host: Host) {
        val words = "${host.baseUrl}\nFingerprint ${host.fingerprint}\nThis phone there: ${host.deviceId.take(8)}" +
            if (host.revocationPending) "\n\nRemoval is pending: the computer couldn't be reached, so this phone is still " +
                "authorized there. On the computer: voice-call devices revoke ${host.deviceId}" else ""
        AlertDialog.Builder(activity).setTitle(host.label).setMessage(words + "\n\n" + offers(host))
            .setPositiveButton(if (host.revocationPending) "Try again" else "Remove") { _, _ -> remove(host) }
            .apply {
                if (host.revocationPending) setNeutralButton("Forget anyway") { _, _ -> forgetAnyway(host) }
                else if (isTheLaptop(host) && Store(activity).use { it.migratedTo() } == null)
                    setNeutralButton("Copy chats") { _, _ -> offerImport(host) }
                else setNeutralButton("Check now") { _, _ -> check(host) }
            }
            .setNegativeButton("Close", null)
            .show()
    }

    /** The computer the single-laptop app talks to: its address is the old "server" setting. */
    private fun isTheLaptop(host: Host): Boolean {
        val server = activity.getSharedPreferences("call", android.content.Context.MODE_PRIVATE).getString("server", "") ?: ""
        return server.isNotEmpty() && runCatching { pairing.origin(server).toString() }.getOrNull() == host.baseUrl
    }

    /** Once, for the laptop: copy this phone's chats with it into the multi-host store, keyed by its host id. The old
     *  files are only read; the app keeps using them until the switch. Final once done. */
    private fun offerImport(host: Host) {
        Store(activity).use { store -> if (store.migratedTo() != null) { show(); return } }
        AlertDialog.Builder(activity)
            .setTitle("Bring your chats along?")
            .setMessage("${host.label} is the laptop this phone already talks to. Copy its chats, calls and unsent messages " +
                "on this phone into the new store for ${host.label}? Nothing is deleted or changed; the app keeps working as now.")
            .setPositiveButton("Copy") { _, _ -> background {
                val prefs = activity.getSharedPreferences("call", android.content.Context.MODE_PRIVATE).all
                val plan = LegacyImport.plan(activity.filesDir, prefs, host.hostId)
                val words = try {
                    Store(activity).use { it.apply(plan) }
                    val c = plan.counts
                    "Copied ${c["chats"]} chats (${c["lines"]} lines), ${c["calls"]} calls and ${c["outbox"]} unsent messages, " +
                        "and checked them." + if (plan.chats.any { it.session == null })
                        "\n\n${plan.chats.count { it.session == null }} chats couldn't be matched to their agent and keep their file name." else ""
                } catch (e: Store.MigrationRefused) {
                    "Not copied: ${e.message}."
                }
                activity.runOnUiThread { tell("Your chats", words) { show() } }
            } }
            .setNegativeButton("Not now") { _, _ -> show() }
            .show()
    }

    private fun offers(host: Host): String {
        val client = HostClient(host, NoKey)
        val names = listOf("chat", "calls", "files", "usage", "plans", "workflows", "delegated_agents", "notifications", "reminders")
        val (on, off) = names.partition { client.offers(it) }
        return "Offers: ${on.joinToString(", ").ifEmpty { "nothing yet" }}" +
            if (off.isNotEmpty()) "\nNot yet: ${off.joinToString(", ")}" else ""
    }

    /** A signed round trip: reachable, the clock agrees, and the latest signed description saved. */
    private fun check(host: Host) = background {
        val words = try {
            val fresh = HostClient(host, HostKeys.forAuthority("host", host.hostId)).refresh()
            registry.put(fresh)
            "Reachable, and this phone is still paired.\n\n" + offers(fresh)
        } catch (e: ClockSkew) {
            e.message ?: ""
        } catch (e: DescriptorRefused) {
            e.message ?: ""
        } catch (e: java.io.IOException) {
            "Couldn't reach ${host.label}: ${e.message ?: e.javaClass.simpleName}"
        }
        activity.runOnUiThread { onChanged(); tell(host.label, words) { show() } }
    }

    /** Revoke first, then forget: the key is deleted only once the computer no longer trusts it. */
    private fun remove(host: Host) = background {
        val revoked = HostClient(host, HostKeys.forAuthority("host", host.hostId)).revokeSelf()
        if (revoked) {
            HostKeys.delete("host", host.hostId)
            registry.remove(host.hostId)
        } else {
            registry.put(host.copy(revocationPending = true))
        }
        activity.runOnUiThread {
            onChanged()
            if (revoked) tell("Removed", "${host.label} no longer trusts this phone.") { show() }
            else tell("Couldn't reach ${host.label}", "It is still paired there. Henchline will ask again when you tap Try again, " +
                "or on the computer: voice-call devices revoke ${host.deviceId}") { show() }
        }
    }

    private fun forgetAnyway(host: Host) {
        AlertDialog.Builder(activity).setTitle("Forget without revoking?")
            .setMessage("${host.label} will still accept this phone's key until you run, on it:\nvoice-call devices revoke ${host.deviceId}")
            .setPositiveButton("Forget anyway") { _, _ ->
                HostKeys.delete("host", host.hostId)
                registry.remove(host.hostId)
                onChanged()
                show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }
}

/** For reading a host's descriptor only: never signs. */
private object NoKey : RequestKey {
    override val algorithm = "p256"
    override val publicKey = ""
    override fun sign(message: ByteArray): ByteArray = throw IllegalStateException("not for signing")
}
