package io.github.ninjatuna007.henchline

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioRecordingConfiguration
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.net.Uri
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.util.concurrent.LinkedBlockingDeque
import java.util.concurrent.TimeUnit

/**
 * The call: the phone's mic to the call server, the agents' voices back, as the web page does,
 * but in a foreground service, so it keeps going with the screen off or the app in the background.
 *
 * The server's protocol is the page's: binary frames are audio (16 kHz s16 out, 24 kHz s16 in),
 * text frames JSON. The phone's own voice-call audio mode cancels its echo, as the browser did.
 */
class CallService : Service() {

    /** What the screen shows: the service is the call, the activity only watches it. */
    object Bus {
        @Volatile var listener: ((String) -> Unit)? = null  // JSON from the server, or {"closed": reason}
        @Volatile var connected = false
        @Volatile var service: CallService? = null  // set from Call until the call ends: also "connecting"
        // The latest crew list and status: the server sends them only when they change, so a screen that
        // wasn't listening then (answering from the ring screen) showed "nobody has spoken" (2026-11-15)
        @Volatile var lastAgents: String? = null
        @Volatile var lastMode: String? = null
    }

    // Written on the main and OkHttp threads, read on the mic thread
    @Volatile private var socket: WebSocket? = null
    @Volatile private var recorder: Thread? = null
    @Volatile private var player: Thread? = null
    private var focus: AudioFocusRequest? = null
    // Clear voice (the default): the agents as media, not through the phone-call path, which sounded muffled and
    // tinny; the mic then asks for the phone's echo canceller itself. Tested 2026-11-15: a lot clearer, and no
    // echo of a 22 s clip came back. Off: the phone-call path, its routing and processing
    @Volatile var clear = true
        private set
    // Loudspeaker (the default) or the earpiece, as a phone call's speaker button (the user's wish, 2026-11-16:
    // "call is only ever on speaker"). The earpiece needs the phone-call path, so choosing it leaves clear sound;
    // earbuds or a headset, when there are some, win over both.
    @Volatile var speaker = true
        private set
    // Earbuds or a headset, when there are some: used unless the user picked the speaker or the earpiece over them
    @Volatile var useHeadset = true
        private set

    /** Earbuds or a wired headset the call could use now. */
    fun headsetAvailable(): Boolean = if (Build.VERSION.SDK_INT >= 31)
        audio.availableCommunicationDevices.any { it.type in HEADSETS }
    else audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS).any { it.type in HEADSETS || it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP }

    /** Where the call's sound is now, as the button shows it: "headset", "speaker" or "earpiece". */
    val where get() = if (useHeadset && headsetAvailable()) "headset" else if (speaker || clear) "speaker" else "earpiece"  // clear sound is never on the earpiece

    /** The route picked with earbuds connected: earbuds, speaker or earpiece. */
    fun setRoute(to: String): Boolean =
        if (to == "headset") moveTo(true, speaker) else moveTo(false, to == "speaker")

    /** The speaker button: loudspeaker or earpiece. */
    fun setSpeaker(on: Boolean): Boolean = moveTo(useHeadset, on)

    /** Clear or call sound stays as it is on the earbuds and the speaker; the earpiece is call sound only (the
     *  user's wish, 2026-11-16), so moving there from clear sound switches (false: still switching from a tap). */
    private fun moveTo(headset: Boolean, onSpeaker: Boolean): Boolean {
        val (wasHeadset, wasSpeaker) = useHeadset to speaker
        useHeadset = headset; speaker = onSpeaker
        if (clear && where == "earpiece") {
            if (switchSound(false)) return true
            useHeadset = wasHeadset; speaker = wasSpeaker
            return false
        }
        reroute()
        return true
    }

    /** The call path is routed again; clear sound's playing track is aimed again. */
    private fun reroute() {
        Thread { synchronized(audioLock) { if (running && !clear) route() } }.start()
        aimTrack()
        socket?.send(soundNote())  // the laptop's talk-over is careful on the loudspeaker: a speaker tap must reach it
    }

    @Volatile private var playing: AudioTrack? = null  // the sound playing now, to aim at a device

    /** Clear sound is media, which follows connected earbuds: when the user picked the speaker over them, it is
     *  aimed at the phone's loudspeaker instead (the user's report, 2026-11-16: speaker, then clear sound, and the
     *  sound stayed in the earbuds). Otherwise the phone picks, as for any media. The call path: route(). */
    private fun aimTrack() {
        val track = playing ?: return
        val loudspeaker = if (clear && !useHeadset) audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            .firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER } else null
        try { track.setPreferredDevice(loudspeaker) } catch (e: Exception) {}  // a released track: the next one aims
    }

    private var proximity: PowerManager.WakeLock? = null  // the screen off at the ear, on the earpiece
    private val generation = java.util.concurrent.atomic.AtomicInteger()  // the audio threads' round
    private val switching = java.util.concurrent.atomic.AtomicBoolean()  // a sound switch under way: taps wait
    private val audioLock = Any()  // starting the audio and switching its sound take turns
    private val attributes get() = if (clear) MEDIA else VOICE
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: android.net.wifi.WifiManager.WifiLock? = null  // Wi-Fi kept out of power save for the call
    // Talkback the phone speaks itself (PhoneVoice): only when "Speak on the phone" is on and the model is here
    private var phoneVoice: PhoneVoice.Speaker? = null
    @Volatile private var lastDrop = ""  // why the line last dropped: told to the laptop once it's back
    @Volatile private var running = false
    @Volatile private var link = 0  // which connection is the call's now: a dropped one's late words are ignored
    @Volatile private var lostAt = 0L  // when the connection dropped mid-call: reconnecting, for RECONNECT_FOR
    private var server = ""
    // One for the call and its reconnects. Pings every 5 s: a dropped connection is noticed in seconds, not half a
    // minute (the stall was heard as garbled catching-up, 2026-11-16)
    // OkHttp hangs up when one ping goes unanswered for a whole interval. At 5 s a phone locked for a while (Wi-Fi
    // power save, the VPN app throttled) lost the line every 10-60 s, and talkback with it (2026-11-21 19:12-19:18:
    // 13 drops). 15 s rides out those stalls; a line that is really dead still goes within 15-30 s and is redialled.
    private val client by lazy { OkHttpClient.Builder().pingInterval(15, TimeUnit.SECONDS).addNetworkInterceptor(LegacySigner).build() }
    @Volatile private var ended = false  // the screen was already told why the call ended
    private var reply: String? = null  // an answered ring's report-back address: told if the call can't connect
    @Volatile private var startedAt = 0L  // when the laptop answered: the call's length, for the history
    val callStartedAt get() = startedAt  // the call bar's timer counts from here, whichever screen shows it
    @Volatile var lastSession = ""  // the chat a typed line was last sent in (its ticks are kept there)
    private val heard = LinkedBlockingDeque<ByteArray>()  // the agents' voices, waiting to be played
    // The phone's own voice (PhoneVoice), queued apart and played first: PhoneVoice paces itself on this queue alone.
    // Sharing `heard` stalled it after one piece, the laptop's idle monitor keeping that queue full (2026-11-22)
    private val spoken = LinkedBlockingDeque<ByteArray>()
    // The phone voice's chunks gone from `spoken`, played or dropped, all told: PhoneVoice compares where each piece
    // ends with it, to tell the laptop on a fallback which pieces were really heard
    private val spokenTaken = java.util.concurrent.atomic.AtomicLong()

    /** Empty `spoken`, counting what goes as taken (so the pieces' ends stay comparable). */
    private fun clearSpoken() { val n = spoken.size; spoken.clear(); spokenTaken.addAndGet(n.toLong()) }
    private val audio by lazy { getSystemService(AudioManager::class.java) }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (intent?.action == TALK) {  // the notification's Talk / Send button (the lock screen's too)
            if (running) toggleTalk()
            return START_NOT_STICKY
        }
        goForeground()  // first: a service started for the foreground must say so within seconds
        val server = intent?.getStringExtra(SERVER)
        if (server == null) {
            if (!running) stopSelf()
            return START_NOT_STICKY
        }
        if (running) return START_NOT_STICKY  // already calling or connecting
        clear = intent.getBooleanExtra(CLEAR, true)
        speaker = intent.getBooleanExtra(SPEAKER, true)
        if (!speaker) clear = false  // the earpiece is only on the phone-call path
        reply = intent.getStringExtra(IncomingActivity.REPLY)
        Calls.outcome(this, reply, "answered")  // from the ring screen, its banner or the app: the user's tap
        running = true
        Bus.service = this
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "henchline:call").apply { acquire() }
        // A locked phone's Wi-Fi drops into power save and the call's socket stalls; low latency keeps it awake
        // (on newer Android only while the screen is on, so the longer ping tolerance carries the rest)
        wifiLock = try {
            getSystemService(android.net.wifi.WifiManager::class.java)
                .createWifiLock(android.net.wifi.WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "henchline:call")
                .apply { setReferenceCounted(false); acquire() }
        } catch (e: Exception) { null }
        if (PhoneVoice.enabled(this) && PhoneVoice.ready(this)) {
            phoneVoice = PhoneVoice.Speaker(this, enqueue = { spoken.offer(it) }, queued = { spoken.size },
                send = { socket?.send(it) }, played = { spokenTaken.get() }, flush = { clearSpoken() })
            Thread { PhoneVoice.load(this) }.start()  // about 2.4 s: ready before the first reply
        }
        applySound()
        connect(server)
        if (Build.VERSION.SDK_INT >= 31) {
            audio.addOnCommunicationDeviceChangedListener(mainExecutor, earbudsWatch)
            watching = true
        }
        devicesListed = false  // a reused service lists its devices again on registering
        audio.registerAudioDeviceCallback(devicesWatch, devicesHandler)
        audio.registerAudioRecordingCallback(recordingWatch, devicesHandler)
        try {
            listenToButtons()
        } catch (e: Exception) {  // the call goes on without the earbud button
            android.util.Log.w("Henchline", "no earbud button: ${e.message}")
        }
        return START_NOT_STICKY
    }

    // --- push-to-talk from earbuds and the notification ----------------------------------------------------

    @Volatile var talking = false  // a tap on the earbuds (or the notification's Talk) started a turn
        private set
    private var buttons: android.media.session.MediaSession? = null
    private val beeper by lazy { try { android.media.ToneGenerator(android.media.AudioManager.STREAM_MUSIC, 70) } catch (_: Exception) { null } }

    /** Earbuds' button (play/pause, or a headset's hook) while a call is up: one tap starts talking, the next
     *  sends; the phone can stay in a pocket. A media session that says it is playing gets those keys. */
    private fun listenToButtons() {
        buttons = android.media.session.MediaSession(this, "henchline-call").apply {
            setCallback(object : android.media.session.MediaSession.Callback() {
                override fun onMediaButtonEvent(intent: Intent): Boolean {
                    @Suppress("DEPRECATION")
                    val key = intent.getParcelableExtra<android.view.KeyEvent>(Intent.EXTRA_KEY_EVENT) ?: return false
                    if (key.keyCode !in TALK_KEYS) return false
                    if (key.action == android.view.KeyEvent.ACTION_DOWN && key.repeatCount == 0) toggleTalk()
                    return true
                }
            })
            setPlaybackState(android.media.session.PlaybackState.Builder()
                .setActions(android.media.session.PlaybackState.ACTION_PLAY_PAUSE or
                    android.media.session.PlaybackState.ACTION_PLAY or android.media.session.PlaybackState.ACTION_PAUSE)
                .setState(android.media.session.PlaybackState.STATE_PLAYING, 0, 1f).build())
            isActive = true
        }
    }

    /** A rising beep as talking starts, a falling one as it is sent: the earbuds, the notification, the screen's
     *  talk button alike. */
    fun beep(starting: Boolean) {
        beeper?.startTone(if (starting) android.media.ToneGenerator.TONE_PROP_ACK else android.media.ToneGenerator.TONE_PROP_PROMPT, 150)
    }

    @Volatile private var routedAt = 0L  // when route() last picked a device: its own changes settle meanwhile
    private var watching = false  // earbudsWatch registered (only once a call started)
    private var pressedAt = 0L  // the last press seen this way: a flaky call channel doesn't toggle talk on and on

    /** On call sound the earbuds' button sends "hang up", not play/pause, and the phone drops their call channel:
     *  the sound fell to the earpiece and the app never heard the press (the user's catch, 2026-11-16). That drop is
     *  taken as the press: push-to-talk, and the sound goes back to the earbuds. */
    private val earbudsWatch = AudioManager.OnCommunicationDeviceChangedListener { device ->
        if (!running || clear || !useHeadset || audio.mode != AudioManager.MODE_IN_COMMUNICATION) return@OnCommunicationDeviceChangedListener
        // The press drops the call to the earpiece (or nothing); the speaker is someone's choice, not the button
        if (device != null && device.type != AudioDeviceInfo.TYPE_BUILTIN_EARPIECE) return@OnCommunicationDeviceChangedListener
        val now = System.currentTimeMillis()
        if (!headsetAvailable() || now - routedAt < 1500 || now - pressedAt < 2500) return@OnCommunicationDeviceChangedListener
        pressedAt = now
        android.util.Log.i("Henchline", "earbuds' call channel dropped (their button): talk, and back to them")
        toggleTalk()
        Thread { synchronized(audioLock) { if (running && !clear) route() } }.start()
    }

    /** Start talking, or send what was said: as holding the talk button. */
    @Synchronized
    fun toggleTalk() {
        if (!Bus.connected) return  // the laptop isn't there yet: nothing to start or send
        talking = !talking
        socket?.send(org.json.JSONObject().put("push", talking).toString())
        beep(talking)
        Bus.listener?.invoke(org.json.JSONObject().put("talking", talking).toString())
        goForeground()  // the notification's button says Send while talking
    }

    override fun onDestroy() {
        running = false
        if (watching && Build.VERSION.SDK_INT >= 31) audio.removeOnCommunicationDeviceChangedListener(earbudsWatch)
        watching = false
        audio.unregisterAudioDeviceCallback(devicesWatch)
        audio.unregisterAudioRecordingCallback(recordingWatch)
        devicesHandler.removeCallbacksAndMessages(null)
        buttons?.release()
        buttons = null
        beeper?.release()
        socket?.close(1000, "hung up")
        socket = null
        recorder?.interrupt()
        player?.interrupt()
        heard.clear()
        clearSpoken()
        synchronized(audioLock) {  // after any sound switch under way, so it can't set call mode again after this
            focus?.let { audio.abandonAudioFocusRequest(it) }
            if (Build.VERSION.SDK_INT >= 31) audio.clearCommunicationDevice()
            audio.mode = AudioManager.MODE_NORMAL
        }
        wakeLock?.let { if (it.isHeld) it.release() }
        wifiLock?.let { try { if (it.isHeld) it.release() } catch (e: Exception) {} }
        wifiLock = null
        phoneVoice?.close()
        phoneVoice = null
        PhoneVoice.release()  // the model's few hundred MB go with the call
        nearEar(false)
        Bus.connected = false
        if (startedAt > 0) logCall()
        Bus.service = null
        Bus.lastAgents = null
        Bus.lastMode = null
        // Said once: the socket closing after this (onClosing, onFailure) would say it again (2026-11-15)
        if (!ended) Bus.listener?.invoke("""{"closed": "Call ended"}""")
        ended = true
        super.onDestroy()
    }

    fun send(json: String) {
        socket?.send(json)
    }

    // --- the mic kept alive: headsets coming and going, a silenced capture, a mic sending only zeros ----------
    // 2026-11-21: the earbuds went in their case and came out mid-call; the one AudioRecord made at the start went on
    // "recording" exact zeros, and every word after (push-to-talk too) reached the laptop as silence, for minutes.

    private val devicesHandler by lazy { android.os.Handler(mainLooper) }
    @Volatile private var micSession = 0  // the running recorder's audio session: its own recording config is found by it
    private var devicesListed = false  // the first onAudioDevicesAdded lists what is already there, not a change
    private val restartLock = Any()
    private var restartedAt = 0L  // when the audio was last started again this way
    private var restartGap = 0L  // how soon it may be again: 5 s, doubling while restarts keep coming, up to a minute
    private var restartStreak = 0
    @Volatile private var silentSpell = 0  // this spell of a silent mic: 0 none, 1 restarted, 2 told it didn't help
    private val afterDevices = Runnable { restartAudio("a headset came or went") }
    private val afterSilenced = Runnable { restartAudio("Android silenced the mic") }

    private val devicesWatch = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>) {
            if (!devicesListed) { devicesListed = true; return }
            headsetsChanged(added)
        }

        override fun onAudioDevicesRemoved(removed: Array<out AudioDeviceInfo>) = headsetsChanged(removed)
    }

    /** A burst of device changes (a headset brings its call and media devices, one by one) starts the audio again
     *  once, after it settles. */
    private fun headsetsChanged(devices: Array<out AudioDeviceInfo>) {
        if (!running || devices.none { it.type in HEADSETS || it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
                    it.type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES }) return
        devicesHandler.removeCallbacks(afterDevices)
        devicesHandler.postDelayed(afterDevices, DEVICES_SETTLE)
    }

    private val recordingWatch = object : AudioManager.AudioRecordingCallback() {
        override fun onRecordingConfigChanged(configs: List<AudioRecordingConfiguration>) {
            val ours = configs.firstOrNull { micSession != 0 && it.clientAudioSessionId == micSession } ?: return
            if (!running || !ours.isClientSilenced) return
            android.util.Log.i("Henchline", "call: Android silenced the call's mic")
            devicesHandler.removeCallbacks(afterSilenced)
            devicesHandler.postDelayed(afterSilenced, 500)
        }
    }

    /** The mic and speaker started again in the same sound, as a sound switch does; the connection stays. Never in
     *  a tight loop: at most every 5 s, the gap doubling while restarts keep coming (a mic another app holds). */
    private fun restartAudio(why: String): Boolean {
        if (!running || ended) return false
        val now = System.currentTimeMillis()
        synchronized(restartLock) {
            if (now - restartedAt < restartGap) {
                android.util.Log.i("Henchline", "call: not restarting the audio yet ($why)")
                return false
            }
            // The streak resets only after a quiet minute beyond the current gap, so a trigger that keeps firing
            // stays at the one-minute gap instead of cycling back to 5 s.
            restartStreak = if (now - restartedAt < restartGap + 60_000L) restartStreak + 1 else 0
            restartGap = minOf(60_000L, 5_000L shl minOf(restartStreak, 4))
            restartedAt = now
        }
        if (!switching.compareAndSet(false, true)) return false  // a sound switch is starting it again anyway
        android.util.Log.i("Henchline", "call: starting the audio again ($why)")
        Thread {
            try {
                synchronized(audioLock) {
                    val (mic, track) = recorder to player
                    generation.incrementAndGet()
                    mic?.join(1000)
                    track?.join(1000)
                    if (running) {
                        applySound()
                        startAudio()
                    }
                }
            } finally {
                switching.set(false)
            }
        }.start()
        return true
    }

    /** About MIC_DEAD_MS of exact zeros from the mic (called on the mic's thread): started again once; if that
     *  didn't help, said once; after that only as often as restartAudio allows. The laptop is told each step. */
    private fun micDead() {
        val spell = silentSpell
        socket?.send(org.json.JSONObject().put("note", "mic silent").put("restarted", spell > 0).toString())
        when (spell) {
            0 -> {
                silentSpell = 1
                Bus.listener?.invoke(org.json.JSONObject().put("note", "Your mic went silent: restarting it").toString())
                restartAudio("the mic sends only silence")
            }
            1 -> {
                silentSpell = 2
                Bus.listener?.invoke(org.json.JSONObject().put("note",
                    "Your mic is still silent. Try switching sound, or taking the earbuds out and back in.").toString())
                restartAudio("the mic still sends only silence")
            }
            // After that, zeros alone no longer restart: each restart also cuts the agent mid-sentence, and a phone
            // that gates a quiet room to exact zeros would be cut every few seconds. Device changes, Android's
            // silenced report and the user's sound switch still restart it; real sound ends the spell.
            else -> {}
        }
    }

    /** Clear or call sound, mid-call: the audio starts again in the other mode, the connection stays. */
    fun switchSound(toClear: Boolean): Boolean {
        if (toClear && where == "earpiece") return false  // the earpiece is call sound only
        if (!switching.compareAndSet(false, true)) return false  // a second quick tap: the first is still switching
        Thread {
            try {
                synchronized(audioLock) {
                    val (mic, track) = recorder to player
                    clear = toClear
                    generation.incrementAndGet()  // the running threads see this and stop
                    mic?.join(1000)
                    track?.join(1000)
                    if (running) {  // the call may have ended meanwhile: leave the phone's audio as onDestroy set it
                        applySound()
                        startAudio()
                    }
                }
            } finally {
                switching.set(false)
            }
        }.start()
        return true
    }


    /** The phone's audio for this sound: call mode and routing, or plain media; focus to match. */
    private fun applySound() {
        focus?.let { audio.abandonAudioFocusRequest(it) }
        if (clear) {
            if (Build.VERSION.SDK_INT >= 31) audio.clearCommunicationDevice()
            audio.mode = AudioManager.MODE_NORMAL
            nearEar(false)
        } else {
            audio.mode = AudioManager.MODE_IN_COMMUNICATION  // the phone's echo cancelling, and call routing
            route()
        }
        // Music and other sound pause or duck for the call, as for a phone call
        focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
            .setAudioAttributes(attributes).build().also { audio.requestAudioFocus(it) }
    }

    /** The connection dropped mid-call (walking about, Wi-Fi to mobile data; the user's catch, 2026-11-16: the
     *  call just ended in a pocket): connect again every 2 s, the audio still running, and the laptop hands the
     *  call over to the new connection. After RECONNECT_FOR, the call ends as before. */
    private fun reconnect() {
        val now = System.currentTimeMillis()
        if (lostAt == 0L) {
            lostAt = now
            android.util.Log.i("Henchline", "call: connection dropped (${lastDrop.ifEmpty { "no cause seen" }}), reconnecting")
            Bus.listener?.invoke("""{"reconnecting": true}""")
        }
        if (now - lostAt > RECONNECT_FOR) {
            fail("The connection dropped and didn't come back.")
            return
        }
        android.os.Handler(mainLooper).postDelayed({ if (running && !ended) connect(server, resume = true) }, 2000)
    }

    private fun connect(server: String, resume: Boolean = false) {
        this.server = server
        val me = ++link
        val base = Uri.parse(server)
        if (base.scheme != "https") {  // Android refuses plain http, and the call page needs https for the mic
            fail("The address must start with https:// (the laptop's `tailscale serve` name). Long-press Call.")
            return
        }
        val origin = "${base.scheme}://${base.authority}"  // the server lets only its own page's origin in
        val url = origin.replaceFirst("http", "ws") + "/call" + if (resume) "?resume=1" else ""
        val request = Request.Builder().url(url).header("Origin", origin).build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (me != link) { webSocket.close(1000, null); return }
                if (lostAt > 0) {  // back: the audio never stopped, the laptop kept the agent
                    lostAt = 0
                    android.util.Log.i("Henchline", "call: reconnected")
                    webSocket.send(soundNote())
                    offerPhoneVoice(webSocket)  // the laptop forgets the offer when the line drops
                    if (lastDrop.isNotEmpty()) {  // the laptop's log then says why the line went, not only that it did
                        webSocket.send(org.json.JSONObject().put("note", "dropped: $lastDrop").toString())
                        lastDrop = ""
                    }
                    if (talking) webSocket.send(org.json.JSONObject().put("push", true).toString())  // the laptop forgot
                    Bus.listener?.invoke("""{"reconnecting": false}""")
                    return
                }
                Bus.connected = true
                startedAt = System.currentTimeMillis()
                Calls.add(this@CallService, org.json.JSONObject().put("id", "call-$startedAt").put("kind", "call")
                    .put("at", startedAt).put("answering", Calls.ringId(reply) ?: ""))
                // Answering a ring: which one, so the laptop puts the call through to the agent that rang
                reply?.let { webSocket.send(org.json.JSONObject().put("answering", it.substringAfterLast('/')).toString()) }
                // Where the laptop can ring this phone later (`handsfree ring`), refreshed every call
                getSharedPreferences("call", MODE_PRIVATE).getString(RingService.ENDPOINT, null)?.let {
                    webSocket.send(org.json.JSONObject().put("ring_endpoint", it).toString())
                }
                offerPhoneVoice(webSocket)
                startAudio()
                Bus.listener?.invoke("""{"opened": true}""")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (me != link) return
                val message = try { org.json.JSONObject(text) } catch (_: Exception) { null }
                message?.optJSONObject("say")?.let { clip ->  // talkback the phone speaks itself (PhoneVoice)
                    phoneVoice?.say(clip) ?: webSocket.send(org.json.JSONObject().put("tts", "fallback")
                        .put("reason", "not speaking on the phone").put("id", clip.optString("id")).toString())
                    return
                }
                message?.optString("stop")?.takeIf { it.isNotEmpty() }?.let { phoneVoice?.stop(it); return }
                if (message?.optBoolean("flush") == true) {  // the user started talking: drop the talkback still queued
                    flushSpeaker()
                    return
                }
                if (message?.has("agents") == true) Bus.lastAgents = text
                else if (message?.has("mode") == true) Bus.lastMode = text
                // Kept here, by the call, whether or not the screen is showing: lines that came while another app
                // was in front were lost (2026-11-15); the screen draws them from the kept chat when it comes back
                message?.optJSONObject("chat")?.let { line ->
                    val mine = line.optString("from") == "you"
                    val keep = org.json.JSONObject().put("from", line.optString("from")).put("text", line.optString("text"))
                        .put("mine", mine)
                    if (mine) keep.put("status", "delivered")  // the laptop says so once it has typed your words in
                    line.optString("tid").takeIf { it.isNotEmpty() }?.let {  // an agent's text: on the phone now
                        keep.put("tid", it)
                        Messages.receipt(this@CallService, listOf(it), "delivered")
                    }
                    line.optJSONObject("file")?.let { keep.put("file", it) }
                    Chats.add(this@CallService, line.optString("session"), keep)
                    if (line.optString("from") != "you") Chats.readAll(this@CallService, line.optString("session"))
                }
                val file = message?.optJSONObject("chat")?.optJSONObject("file")
                if (file != null && Bus.listener == null) {  // the call screen isn't showing: don't let it pass unseen
                    val line = message.getJSONObject("chat")
                    Files.notify(this@CallService, line.optString("from").lowercase(), line.optString("text"),
                        file.optString("url"), file.optString("name", "file"),
                        file.optString("type", "application/octet-stream"), file.optLong("size"), line.optString("session"))
                }
                message?.optString("delivered")?.takeIf { it.isNotEmpty() }?.let { id ->  // typed into the agent
                    Chats.setStatus(this@CallService, message.optString("session").ifEmpty { lastSession }, id, "delivered")
                }
                Bus.listener?.invoke(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (me != link) return
                val chunk = bytes.toByteArray()
                if (spoken.isNotEmpty() && silent(chunk)) return  // the laptop's idle stream while the phone is speaking itself (after a fallback the laptop's pauses are kept)
                heard.offer(chunk)
                got.incrementAndGet()
                while (heard.size > MAX_BEHIND) { heard.poll(); dropped.incrementAndGet() }  // over a second behind: drop the oldest
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                android.util.Log.i("Henchline", "call: closing, code $code${if (reason.isNotEmpty()) " ($reason)" else ""}")
                if (me == link && running && !ended) lastDrop = "closed by the laptop, code $code${if (reason.isNotEmpty()) ": $reason" else ""}"
                webSocket.close(1000, null)
                if (me != link) return  // the dropped connection, closed by the laptop as the new one took over
                if (lostAt > 0 && running && !ended) { reconnect(); return }  // "busy": the old one not gone yet
                fail(reason.ifEmpty { "Call ended" })  // "busy: another call is up", or the server stopping
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                android.util.Log.i("Henchline", "call: socket failed: ${t.javaClass.simpleName}: ${t.message}" +
                    (response?.let { " (HTTP ${it.code})" } ?: ""))
                if (me != link) return
                lastDrop = "${t.javaClass.simpleName}: ${t.message ?: "no message"}"
                if (Bus.connected && running && !ended) { reconnect(); return }  // mid-call: try again
                // A self-signed certificate (call/server.py --host tailscale) fails here: the app trusts only
                // real ones, so it needs the `tailscale serve` address
                fail("Couldn't reach the call: ${t.message}")
            }
        })
    }

    private fun startAudio() = synchronized(audioLock) {
        val round = generation.get()
        // The server logs each sound, so turns can be compared by it (transcription, turn taking)
        socket?.send(soundNote())
        recorder = Thread {
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_AUDIO)  // not slowed when locked
            try {
                val size = maxOf(AudioRecord.getMinBufferSize(MIC_RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT), FRAME * 2 * 4)
                val mic = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, MIC_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, size)
                if (mic.state != AudioRecord.STATE_INITIALIZED) {
                    mic.release()
                    fail("The microphone isn't available (another app using it, or no permission).")
                    return@Thread
                }
                val buffer = ByteArray(FRAME * 2)
                val effects = if (clear) listOfNotNull(  // outside call mode, ask for them by name
                    if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(mic.audioSessionId) else null,
                    if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(mic.audioSessionId) else null,
                ).onEach { it.enabled = true } else emptyList()
                echoCanceller = if (clear) effects.any { it is AcousticEchoCanceler } else null
                socket?.send(soundNote())  // where it plays now (a restart or a sound switch may have moved it)
                if (clear) Bus.listener?.invoke(org.json.JSONObject().put("note",
                    "Clear voice: phone echo canceller ${if (effects.any { it is AcousticEchoCanceler }) "on" else "not available"}").toString())
                try {
                    mic.startRecording()
                    micSession = mic.audioSessionId
                    var zeroMs = 0L  // exact zeros in a row: a silenced capture, never a real room
                    while (running && generation.get() == round) {
                        val got = mic.read(buffer, 0, buffer.size)
                        if (got < 0) {
                            fail("The microphone stopped working (error $got).")
                            break
                        }
                        if (got > 0) {
                            if ((0 until got).all { buffer[it] == 0.toByte() }) {
                                zeroMs += got / 2 * 1000L / MIC_RATE
                            } else {
                                if (silentSpell > 0) android.util.Log.i("Henchline", "call: the mic is back")
                                zeroMs = 0
                                silentSpell = 0
                            }
                            if (zeroMs >= MIC_DEAD_MS && lostAt == 0L) {
                                zeroMs = 0  // counted afresh: the next step only after another MIC_DEAD_MS
                                micDead()
                            }
                        }
                        // Not while reconnecting: queued, it all arrived at once and was taken down late as the user's
                        if (got > 0 && lostAt == 0L) socket?.send(buffer.toByteString(0, got))
                    }
                } finally {
                    if (mic.recordingState == AudioRecord.RECORDSTATE_RECORDING) mic.stop()
                    effects.forEach { it.release() }
                    mic.release()
                }
            } catch (e: Exception) {  // never let the call take the app down with it
                fail("The microphone failed: ${e.message}")
            }
        }.apply { name = "mic"; start() }
        player = Thread {
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_AUDIO)  // not slowed when locked
            try {
                val track = AudioTrack.Builder()
                    .setAudioAttributes(attributes)
                    .setAudioFormat(AudioFormat.Builder().setSampleRate(SPEAKER_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .setBufferSizeInBytes(SPEAKER_RATE / 5 * 2)
                    .build()
                playing = track
                aimTrack()
                try {
                    track.play()
                    var reported = System.currentTimeMillis()
                    while (running && generation.get() == round) {
                        if (System.currentTimeMillis() - reported >= 2000) {  // what was played, for the laptop's log
                            reported = System.currentTimeMillis()
                            report(track)
                        }
                        val chunk = nextChunk() ?: continue
                        if (loud(chunk)) loudMs.addAndGet(chunk.size / 2 * 1000L / SPEAKER_RATE)
                        val written = track.write(chunk, 0, chunk.size)
                        if (written < 0) {
                            fail("The speaker stopped working (error $written).")
                            break
                        }
                    }
                } finally {
                    if (track.playState == AudioTrack.PLAYSTATE_PLAYING) track.stop()
                    track.release()
                    if (playing === track) playing = null
                }
            } catch (_: InterruptedException) {
            } catch (e: Exception) {
                fail("The speaker failed: ${e.message}")
            }
        }.apply { name = "speaker"; start() }
    }

    @Volatile private var echoCanceller: Boolean? = null  // the phone's canceller on the mic: null in call mode (built in)

    /** The sound mode, where it plays, and whether the phone's echo canceller is on, for the laptop: on the loudspeaker
     *  in clear sound, or without a canceller, our voice can reach this mic and talking over is judged with care. */
    private fun offerPhoneVoice(webSocket: WebSocket) {
        val voice = phoneVoice ?: return
        PhoneVoice.offer(this)?.let { webSocket.send(it.toString()) }
        voice.report().forEach { webSocket.send(it) }  // after a drop: how far each clip got while the line was down
    }

    private fun soundNote(): String = org.json.JSONObject().put("sound", if (clear) "clear" else "call").put("out", where)
        .apply { echoCanceller?.let { put("aec", it) } }.toString()

    /** The laptop heard the user start talking: what is still queued to play (up to a second, plus the speaker's
     *  own fifth of a second) is dropped, so a clip they cut stops at once here too (2026-11-21). */
    private fun flushSpeaker() {
        phoneVoice?.stopAll()  // the user is talking: a clip the phone speaks stops as the laptop's would
        val dropped = heard.size + spoken.size
        heard.clear()
        clearSpoken()
        playing?.let { track ->
            try {
                if (track.playState == AudioTrack.PLAYSTATE_PLAYING) {
                    track.pause()
                    track.flush()
                    track.play()
                }
            } catch (_: IllegalStateException) {}  // released by a sound switch just now: nothing left to drop
        }
        if (dropped > 0) android.util.Log.i("Henchline", "call: the user is talking, dropped $dropped queued chunks")
    }

    // What the phone played, told to the laptop every 2 s as the call page does: the laptop can't hear the phone,
    // so sound lost on the way (the screen off, audio paused, chunks dropped) shows only in its log this way
    // (2026-11-16: the agents went silent with the screen off, and nothing said why)
    private val got = java.util.concurrent.atomic.AtomicInteger()
    private val dropped = java.util.concurrent.atomic.AtomicInteger()
    private val loudMs = java.util.concurrent.atomic.AtomicLong()

    /** All zeros: a PipeWire monitor with nothing playing. */
    private fun silent(chunk: ByteArray): Boolean = chunk.all { it.toInt() == 0 }

    /** What the speaker plays next: the phone's own voice first, and for each of its chunks one quiet chunk of
     *  the laptop's goes (its idle monitor would otherwise build up behind); the laptop's loud ones are kept. */
    private fun nextChunk(): ByteArray? {
        spoken.poll()?.let { mine ->
            spokenTaken.incrementAndGet()
            heard.peek()?.let { theirs -> if (!loud(theirs) && heard.removeFirstOccurrence(theirs)) dropped.incrementAndGet() }
            return mine
        }
        return heard.poll(if (phoneVoice != null) 20L else 200L, TimeUnit.MILLISECONDS)
    }

    private fun loud(chunk: ByteArray): Boolean {
        var sum = 0L
        var i = 0
        while (i + 1 < chunk.size) {
            val v = (chunk[i].toInt() and 0xff) or (chunk[i + 1].toInt() shl 8)
            sum += v.toLong() * v
            i += 2
        }
        return chunk.size >= 2 && kotlin.math.sqrt(sum.toDouble() / (chunk.size / 2)) > 300  // above a quiet line
    }

    private fun report(track: AudioTrack) {
        val screenOn = getSystemService(PowerManager::class.java).isInteractive
        socket?.send(org.json.JSONObject().put("stats", org.json.JSONObject()
            .put("got", got.getAndSet(0)).put("dropped", dropped.getAndSet(0)).put("loud_ms", loudMs.getAndSet(0))
            .put("audio", if (track.playState == AudioTrack.PLAYSTATE_PLAYING) "running" else "stopped")
            .put("hidden", !screenOn)
            // Where the sound goes and how loud: audio lost after the speaker loop (a route, a muted stream) shows here
            .put("route", routeName(track.routedDevice?.type))
            .put("volume", audio.getStreamVolume(streamOf(track)))
            .put("volume_max", audio.getStreamMaxVolume(streamOf(track)))).toString())
    }

    private fun streamOf(track: AudioTrack): Int =
        if (track.audioAttributes.usage == android.media.AudioAttributes.USAGE_VOICE_COMMUNICATION) AudioManager.STREAM_VOICE_CALL
        else AudioManager.STREAM_MUSIC

    private fun routeName(type: Int?): String = when (type) {
        null -> "none"
        AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "speaker"
        AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "earpiece"
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth (call)"
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "bluetooth (media)"
        AudioDeviceInfo.TYPE_BLE_HEADSET -> "bluetooth LE"
        AudioDeviceInfo.TYPE_WIRED_HEADSET, AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "wired"
        AudioDeviceInfo.TYPE_USB_HEADSET -> "usb"
        else -> "device $type"
    }

    /** The call ended: its length in the history, and a line in the chat it ended on, as a messaging app shows one. */
    private fun logCall() {
        val seconds = (System.currentTimeMillis() - startedAt) / 1000
        // The chat of the agent the call was with (the laptop's last word on it), not the chat shown
        val withAgent = try { org.json.JSONObject(Bus.lastAgents ?: "{}").optString("current") } catch (_: Exception) { "" }
        val session = withAgent.ifEmpty { getSharedPreferences("call", MODE_PRIVATE).getString("current", "") ?: "" }
        Calls.update(this, "call-$startedAt") { it.put("seconds", seconds).put("session", session) }
        // As a messaging app shows a call: on the side of whoever started it (an answered ring: the agent's)
        Chats.add(this, session, org.json.JSONObject().put("from", "").put("note", true).put("mine", false)
            .put("text", "📞 Call · ${Calls.length(seconds)}")
            .put("call", org.json.JSONObject().put("dir", if (reply != null) "in" else "out").put("seconds", seconds)))
        startedAt = 0
    }

    /** End the call with a reason the screen shows; safe from any thread. */
    private fun fail(why: String) {
        if (ended) return
        ended = true
        android.util.Log.i("Henchline", "call ended: $why")  // the screen's note isn't kept: this is, in the phone's log
        if (!Bus.connected) {  // answered, but the laptop is out of reach
            Reply.send(reply, "unreachable")
            Calls.outcome(this, reply, "unreachable")
        }
        // Over before the screen hears it: the bar went on showing the call, and what it last said (the user's
        // catch, 2026-11-16: still "on a call" after hanging up)
        Bus.connected = false
        Bus.listener?.invoke(org.json.JSONObject().put("closed", why).toString())
        android.os.Handler(mainLooper).post { stopSelf() }
    }

    /** Earbuds or a headset when there are some, else the loudspeaker or the earpiece, as the speaker button says. */
    private fun route() {
        if (Build.VERSION.SDK_INT < 31) {
            @Suppress("DEPRECATION")
            audio.isSpeakerphoneOn = speaker
            nearEar(!speaker)
            return
        }
        val devices = audio.availableCommunicationDevices
        val preferred = (if (useHeadset) HEADSETS.toList() else emptyList()) +
            (if (speaker) AudioDeviceInfo.TYPE_BUILTIN_SPEAKER else AudioDeviceInfo.TYPE_BUILTIN_EARPIECE)
        val picked = preferred.firstNotNullOfOrNull { type -> devices.firstOrNull { it.type == type } }
        routedAt = System.currentTimeMillis()
        picked?.let { audio.setCommunicationDevice(it) }
        nearEar(picked?.type == AudioDeviceInfo.TYPE_BUILTIN_EARPIECE)
    }

    /** On the earpiece the screen goes off at the ear (the proximity sensor), so a cheek taps nothing. */
    private fun nearEar(on: Boolean) {
        if (on && proximity?.isHeld != true) {
            proximity = getSystemService(PowerManager::class.java)
                .takeIf { it.isWakeLockLevelSupported(PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK) }
                ?.newWakeLock(PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK, "henchline:ear")?.also { it.acquire(4 * 60 * 60 * 1000L) }
        } else if (!on) {
            proximity?.let { if (it.isHeld) it.release() }
            proximity = null
        }
    }

    private fun goForeground() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, "Call", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val hangUp = PendingIntent.getService(this, 1, Intent(this, CallService::class.java).setAction(STOP),
            PendingIntent.FLAG_IMMUTABLE)
        val talk = PendingIntent.getService(this, 2, Intent(this, CallService::class.java).setAction(TALK),
            PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notify)
            .setContentTitle(if (talking) "Henchline: talking… tap Send when done" else "Henchline: on a call with your crew")
            .setContentIntent(open)
            .setOngoing(true)
            .setVisibility(Notification.VISIBILITY_PUBLIC)  // its buttons on the lock screen too
            .addAction(Notification.Action.Builder(null, if (talking) "Send" else "Talk", talk).build())
            .addAction(Notification.Action.Builder(null, "Hang up", hangUp).build())
            .build()
        // Microphone for the mic; media playback for the agents' voices (clear sound plays as media)
        startForeground(1, notification,
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
    }

    companion object {
        const val SERVER = "server"
        const val CLEAR = "clear"
        const val SPEAKER = "speaker"
        // Earbuds and headsets the call uses before the phone's own speaker or earpiece, best first
        val HEADSETS = intArrayOf(AudioDeviceInfo.TYPE_BLE_HEADSET, AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
            AudioDeviceInfo.TYPE_WIRED_HEADSET, AudioDeviceInfo.TYPE_USB_HEADSET)
        const val STOP = "stop"
        const val RECONNECT_FOR = 30_000L  // ms a dropped call keeps trying to come back
        const val DEVICES_SETTLE = 1_500L  // ms after the last headset change before the audio starts again
        const val MIC_DEAD_MS = 3_000L  // ms of exact zeros before the mic is taken as silenced
        const val TALK = "talk"
        val TALK_KEYS = setOf(android.view.KeyEvent.KEYCODE_HEADSETHOOK, android.view.KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            android.view.KeyEvent.KEYCODE_MEDIA_PLAY, android.view.KeyEvent.KEYCODE_MEDIA_PAUSE)
        const val CHANNEL = "call"
        const val MIC_RATE = 16000  // what hands-free listens at
        const val SPEAKER_RATE = 24000  // Kokoro's own rate
        const val FRAME = MIC_RATE / 50  // 20 ms of samples per message, as the page sends
        const val MAX_BEHIND = 50  // chunks queued before the oldest are dropped: the server sends 20 ms, so a second
        val VOICE: AudioAttributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
        val MEDIA: AudioAttributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
    }
}
