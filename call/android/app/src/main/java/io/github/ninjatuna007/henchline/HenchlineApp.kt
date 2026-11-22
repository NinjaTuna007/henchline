package io.github.ninjatuna007.henchline

import android.app.Application

/** The app's process starting: the request signer learns where the paired computers are kept (it does nothing
 *  unless multi-host is on). */
class HenchlineApp : Application() {
    override fun onCreate() {
        super.onCreate()
        LegacySigner.attach(this)
    }
}
