package io.github.ninjatuna007.henchline

import android.content.Context
import android.os.Build

/** Henchline on more than one computer: off unless switched on, and only on Android 13+ (API 33), whose platform
 *  verifies the hosts' Ed25519 signatures. Older phones keep the single-laptop app with all of this hidden. */
object MultiHost {
    const val PREF = "multihost"
    const val MIN_SDK = 33

    fun available(): Boolean = Build.VERSION.SDK_INT >= MIN_SDK

    fun enabled(context: Context): Boolean =
        available() && context.getSharedPreferences("call", Context.MODE_PRIVATE).getBoolean(PREF, false)
}
