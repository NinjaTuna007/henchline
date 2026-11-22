package io.github.ninjatuna007.henchline

import android.content.Context
import android.os.Build

/**
 * Paired computers' signed descriptions kept fresh (multi-host preview): as the app comes to the front, each
 * computer not checked for EVERY_MS is asked again (the signed /api/v1/capabilities, checked as "Check now" checks
 * it: pinned key, never an older number). So a computer that starts taking signed requests on its own routes
 * (legacy_routes) gets them, and this phone's push address, without anyone tapping Check now. A failed look is
 * tried again after RETRY_MS. One pass at a time, off the main thread; nothing with multi-host off.
 */
object HostRefresh {
    const val EVERY_MS = 6 * 3600_000L
    const val RETRY_MS = 30 * 60_000L
    private const val PREFS = "host_refresh"  // host id -> when it is next due (ms)
    private val running = java.util.concurrent.atomic.AtomicBoolean()

    /** The computers due a look now: not being removed, and past their next time (never looked at: due). */
    fun due(hosts: List<Host>, next: Map<String, Long>, now: Long): List<Host> =
        hosts.filter { !it.revocationPending && now >= (next[it.hostId] ?: 0L) }

    /** Schedule entries for computers no longer paired: dropped, so one paired again later starts afresh. */
    fun stale(scheduled: Set<String>, hosts: List<Host>): Set<String> = scheduled - hosts.map { it.hostId }.toSet()

    /** A computer just paired (perhaps again, under the same id): due at once, whatever was scheduled before. */
    fun reset(context: Context, hostId: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove(hostId).apply()
    }

    enum class Look { KEPT, UNCHANGED, STALE, NO_KEY }

    /** One look at a computer (refresh: the signed descriptor request; it throws when the look fails), saved only if
     *  it still applies when it comes back (HostRegistry.replace): KEPT. UNCHANGED: nothing new. STALE: the computer
     *  changed meanwhile (removed, paired again, a newer description saved, its removal begun): nothing written.
     *  NO_KEY: this phone has no key for it: nothing asked, and none made. */
    fun look(host: Host, registry: HostRegistry, keyFor: (Host) -> RequestKey?, refresh: (Host, RequestKey) -> Host): Look {
        val key = keyFor(host) ?: return Look.NO_KEY
        val fresh = refresh(host, key)
        return when {
            fresh == host -> Look.UNCHANGED
            registry.replace(fresh) -> Look.KEPT
            else -> Look.STALE
        }
    }

    /** Whether a look's schedule may be written: the computer is still paired as it was looked at (not removed and
     *  paired again meanwhile, whose fresh schedule reset() made). */
    fun samePairing(registry: HostRegistry, host: Host): Boolean = registry.get(host.hostId)?.pairedAt == host.pairedAt

    /** When a computer is next due, after a look that worked or not. */
    fun nextAfter(worked: Boolean, now: Long): Long = now + if (worked) EVERY_MS else RETRY_MS

    fun soon(context: Context) {
        val app = context.applicationContext
        if (!app.getSharedPreferences("call", Context.MODE_PRIVATE).getBoolean(MultiHost.PREF, false)) return
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || !running.compareAndSet(false, true)) return
        Thread {
            try { pass(app) } catch (e: Exception) {
                android.util.Log.w("HostRefresh", "no look this time: ${e.javaClass.simpleName}")
            } finally { running.set(false) }
        }.start()
    }

    @androidx.annotation.RequiresApi(MultiHost.MIN_SDK)
    private fun pass(context: Context) {
        if (!MultiHost.enabled(context)) return
        val registry = HostRegistry.of(context)
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        val hosts = registry.all()
        stale(prefs.all.keys, hosts).takeIf { it.isNotEmpty() }?.let { gone ->
            prefs.edit().apply { gone.forEach { remove(it) } }.apply()
        }
        val next = prefs.all.mapNotNull { (k, v) -> (v as? Long)?.let { k to it } }.toMap()
        var changed = false
        for (host in due(hosts, next, now)) {
            val worked = try {
                val got = look(host, registry, { HostKeys.load("host", it.hostId) }) { h, key -> HostClient(h, key).refresh() }
                if (got == Look.KEPT) changed = true
                got != Look.NO_KEY
            } catch (e: Exception) { false }  // offline, clock, a refused description: kept as it was
            if (samePairing(registry, host)) prefs.edit().putLong(host.hostId, nextAfter(worked, now)).apply()
        }
        if (changed) HostPush.share(context)  // it may take signed requests, and so this phone's push address, now
    }
}
