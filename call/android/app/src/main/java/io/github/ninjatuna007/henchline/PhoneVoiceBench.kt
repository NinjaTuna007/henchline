package io.github.ninjatuna007.henchline

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo

/**
 * Debug builds only: the phone voice's render speed, measured inside the app, to compare with sherpa-onnx's own
 * command-line build (the app ran about twice as slow in a call, 2026-11-22). From the laptop:
 *   adb shell am broadcast -n io.github.ninjatuna007.henchline/.PhoneVoiceBench --ei threads 4 [--es text "…" --es ph "…"] [--ez hint true]
 *     [--es dir /data/user/0/…/files/some-folder]  (another copy of the voice: its .onnx, tokens.txt)
 * (`ph`: the laptop's phonemes for the text, as a call sends them: python -m voice.phonevoice phonemes "…")
 *   adb logcat -s Henchline | grep "phone voice: bench"
 * (`hint`: render under the same performance hint session a call's speaker uses. Each line ends with where the threads
 *  ran: the cpuset, the CPUs allowed, the process's importance, the CPU each thread last ran on. A broadcast to a
 *  backgrounded app measures the background; one sent during a call measures the call's foreground service)
 * The receiver is disabled in release builds (manifest placeholder) and needs DUMP, which adb's shell holds and apps
 * don't; it does nothing unless the app is debuggable.
 */
class PhoneVoiceBench : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE == 0) return
        val threads = intent.getIntExtra("threads", 4).coerceIn(1, 8)
        val text = intent.getStringExtra("text")?.take(2000) ?: TEXT
        val ph = intent.getStringExtra("ph")?.take(2000) ?: if (text == TEXT) PH else null
        if (ph == null) { android.util.Log.i("Henchline", "phone voice: bench: a text needs its ph"); return }
        val hint = intent.getBooleanExtra("hint", false)
        val dir = intent.getStringExtra("dir")
        val pending = goAsync()
        Thread {
            try { PhoneVoice.bench(context.applicationContext, threads, text, ph, hint, dir) } finally { pending.finish() }
        }.start()
    }

    private companion object {
        const val TEXT = "Once there was a small robot henchman with one goggle eye, who lived in a laptop by the window."
        const val PH = "wˈʌns ðeəwˌɒz ɐ smˈɔːl ɹˈəʊbɒt hˈɛntʃmən wɪð wˈɒn ɡˈɒɡəl ˈaɪ, hˌuː lˈɪvd ɪn ɐ lˈæptɒp baɪ ðə wˈɪndəʊ."
    }
}
