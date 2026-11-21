package io.github.ninjatuna007.henchline

import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.ComponentName
import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Messages written while the laptop can't be reached (asleep, off Tailscale, on the way to work): kept here and sent
 * by themselves once it can be, oldest first, as a messaging app does (the user's idea, 2026-11-17). Meanwhile the
 * bubble shows a clock instead of a tick. Android runs OutboxJob whenever there is a network, and the app tries again
 * when it comes to the front and when the laptop pushes anything.
 */
object Outbox {
    private const val JOB = 7301

    private fun file(context: Context) = File(context.filesDir, "outbox.json")

    @Synchronized
    fun all(context: Context): List<JSONObject> = try {
        val list = JSONArray(file(context).readText())
        (0 until list.length()).map { list.getJSONObject(it) }
    } catch (_: Exception) {
        emptyList()
    }

    @Synchronized
    private fun save(context: Context, items: List<JSONObject>) {
        val aside = File(file(context).path + ".new")
        aside.writeText(JSONArray(items).toString())
        aside.renameTo(file(context))
    }

    @Synchronized
    fun add(context: Context, id: String, session: String, words: String) {
        save(context, all(context) + JSONObject().put("id", id).put("session", session).put("words", words)
            .put("at", System.currentTimeMillis()))
        schedule(context)
        watch(context)
    }

    private var watching: android.net.ConnectivityManager.NetworkCallback? = null

    /** Tailscale coming back on is a new network (a VPN) though the phone's own never dropped (2026-11-17: the job
     *  then sat out its backoff): each change of network tries at once, while anything waits. */
    @Synchronized
    fun watch(context: Context) {
        if (watching != null) return
        val net = context.applicationContext.getSystemService(android.net.ConnectivityManager::class.java) ?: return
        val app = context.applicationContext
        val callback = object : android.net.ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: android.net.Network) {
                Thread {
                    Thread.sleep(1500)  // the tunnel's routes settle first
                    val left = try { flush(app) } catch (_: Exception) { true }
                    if (!left) unwatch(app)
                }.start()
            }
        }
        try { net.registerDefaultNetworkCallback(callback); watching = callback } catch (_: Exception) {}
    }

    @Synchronized
    private fun unwatch(context: Context) {
        val callback = watching ?: return
        watching = null
        try { context.getSystemService(android.net.ConnectivityManager::class.java)?.unregisterNetworkCallback(callback) }
        catch (_: Exception) {}
    }

    @Synchronized
    private fun remove(context: Context, id: String) = save(context, all(context).filter { it.optString("id") != id })

    private val flushing = java.util.concurrent.atomic.AtomicBoolean()

    /** Sends what waits, oldest first; stops at the first that still can't reach the laptop. Off the main thread.
     *  Whether anything is left. */
    private val again = java.util.concurrent.atomic.AtomicBoolean()  // asked while one ran: it tries once more

    fun flush(context: Context): Boolean {
        if (!flushing.compareAndSet(false, true)) { again.set(true); return all(context).isNotEmpty() }
        try {
            while (true) {
                again.set(false)
                if (!flushOnce(context)) return false
                if (!again.get()) return true  // still away, and nothing new to try for
            }
        } finally {
            flushing.set(false)
        }
    }

    /** One pass, oldest first: whether anything is left. */
    private fun flushOnce(context: Context): Boolean {
        for (item in all(context)) {
            val id = item.optString("id")
            val session = item.optString("session")
            val words = item.optString("words")
            val sent = Messages.text(context, session, words, id)
            if (sent.offline) return true  // still away: the rest wait their turn behind it
            if (sent.note.isEmpty()) {
                Chats.setStatus(context, session, id, if (sent.queued) "sent" else "delivered")
            } else {
                Chats.setText(context, session, id, "$words  (${sent.note})")
                Chats.setStatus(context, session, id, "sent", force = true)
            }
            remove(context, id)
            CallService.Bus.listener?.invoke("""{"reload": true}""")
        }
        return false
    }

    /** Android tries again by itself as soon as there is a network (and keeps backing off while it can't reach). */
    fun schedule(context: Context) {
        val jobs = context.getSystemService(JobScheduler::class.java) ?: return
        try { jobs.schedule(JobInfo.Builder(JOB, ComponentName(context, OutboxJob::class.java))
            .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
            .setBackoffCriteria(30_000, JobInfo.BACKOFF_POLICY_LINEAR)
            .setPersisted(false)
            .build()) } catch (_: Exception) {}  // never lose the message over it: the app's opening flushes too
    }
}

/** The outbox's turn when there is a network: sent, or tried again later. */
class OutboxJob : JobService() {
    override fun onStartJob(params: JobParameters): Boolean {
        Thread {  // never a crash in the background: a failed run is tried again
            val left = try { Outbox.flush(this) } catch (e: Exception) { android.util.Log.w("Outbox", "flush", e); true }
            jobFinished(params, left)
        }.start()
        return true
    }

    override fun onStopJob(params: JobParameters): Boolean = true
}
