package io.github.ninjatuna007.henchline

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo

/**
 * Debug builds only: the phone voice's render speed, measured inside the app, to compare with sherpa-onnx's own
 * command-line build (the app ran about twice as slow in a call, 2026-11-22). From the laptop:
 *   adb shell am broadcast -n io.github.ninjatuna007.henchline/.PhoneVoiceBench --ei threads 4 --es text "…"
 *   adb logcat -s Henchline | grep "phone voice: bench"
 * The receiver is disabled in release builds (manifest placeholder) and needs DUMP, which adb's shell holds and apps
 * don't; it does nothing unless the app is debuggable.
 */
class PhoneVoiceBench : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE == 0) return
        val threads = intent.getIntExtra("threads", 4).coerceIn(1, 8)
        val text = intent.getStringExtra("text")?.take(2000)
            ?: "Once there was a small robot henchman with one goggle eye, who lived in a laptop by the window."
        val pending = goAsync()
        Thread {
            try { PhoneVoice.bench(context.applicationContext, threads, text) } finally { pending.finish() }
        }.start()
    }
}
