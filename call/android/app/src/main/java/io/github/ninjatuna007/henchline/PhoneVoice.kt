package io.github.ninjatuna007.henchline

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import android.os.PerformanceHintManager
import android.os.PowerManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Talkback spoken by the phone itself (the laptop's side: voice/phonevoice.py). Over a call the laptop sends a
 * clip's words, already cut into pieces the way its own talkback cuts them (punctuation only), with each piece's
 * phonemes, and the phone renders them with Piper's British English "Jenny" voice (VITS, one speaker, 22.05 kHz,
 * resampled to the call's 24 kHz): Kokoro was too slow here (1.12-1.34 s per second of speech on the cores a locked
 * call gets, Jenny 0.14, 2026-11-22). Only words cross the network, so a dropped line in a lift loses nothing that
 * was already here, and a lost clip is simply sent again.
 *
 * Off unless the user turns "Speak on the phone" on and the model is here (about 64 MB, fetched from the laptop,
 * every file checked against the laptop's SHA-256). When the phone can't keep up (a piece late by more than a
 * second while the speaker ran dry), the battery is low or the phone is hot, it says so ({"tts": "fallback"}) and
 * the laptop streams its own audio for the rest of the call, as before.
 */
object PhoneVoice {
    // The model's name is the download's version: a phone holding an earlier model (Kokoro's folder) fetches this one
    // afresh into its own folder, the earlier one is deleted once this one is whole (Download.run)
    const val MODEL = "vits-piper-en_GB-jenny_dioco-medium"
    const val ENGINE = "sherpa-vits"
    const val NAME = "Jenny (British English)"
    const val ONNX = "en_GB-jenny_dioco-medium.onnx"
    private const val SPEAKER = 0  // the model's one speaker
    private const val WARM_PH = "hˈaɪ."  // "Hi.", for the first render on loading
    private const val PREF = "phone_voice"  // the user's switch: speak on the phone
    private const val PREF_MOBILE = "phone_voice_mobile"  // download over mobile data too
    private const val FALLBACK_THREADS = 4  // when the phone's core speeds can't be read
    const val RATE = 24000
    private const val PAUSE = 0.25f  // as the laptop's talkback (talkback/scripts/worker.py): between sentences
    private const val JOIN = 0.05f  // between pieces of one sentence
    private const val EDGE = 0.035f  // kept beyond the first and last sound where pieces of one sentence join
    private const val QUIET = 0.005f  // below this share of the piece's peak counts as quiet
    private const val CHUNK = RATE / 50  // 20 ms, as the laptop streams
    private const val AHEAD_CHUNKS = 25  // at most half a second queued at the speaker: a cut stops it soon
    private const val STARVED_MS = 1000L  // the speaker ran dry this long waiting for a piece: a starve
    private const val STARVED_EARLY_MS = 2500L  // … within a clip's first 2 pieces or 5 s (the engine warming up)
    private const val STARVES = 2  // starves in one clip before the phone falls back (one is waited out)
    private const val HOLD_MS = 1000L  // at most this before a clip's first piece plays, waiting for its second (Kokoro needed 2 s)
    private const val SPEECH_CPS = 17.5f  // characters of text per second of speech, as the laptop estimates
    private const val LOW_BATTERY = 15
    private const val TARGET_RTF = 0.5f  // what the render threads' hint session asks for: render twice as fast as speech
    // espeak-ng's English data under the model folder, still fetched (and required, so a phone and laptop agree on
    // what "ready" means) though sherpa-onnx 2.0 ignores dataDir: the phone speaks the laptop's phonemes ("ph").
    // Exact relative paths, the only ones with a "/"
    val ESPEAK = listOf("espeak-ng-data/phontab", "espeak-ng-data/phonindex", "espeak-ng-data/phondata",
        "espeak-ng-data/intonations", "espeak-ng-data/en_dict", "espeak-ng-data/lang/gmw/en",
        "espeak-ng-data/lang/gmw/en-US", "espeak-ng-data/lang/gmw/en-GB-x-rp")
    // What a phone must have before it counts as ready
    val REQUIRED = listOf(ONNX, "tokens.txt") + ESPEAK

    /** A name the laptop's manifest may use: a plain file name, or one of the espeak data's fixed relative paths. */
    fun safeName(name: String): Boolean =
        name in ESPEAK || (name.isNotEmpty() && !name.contains('/') && !name.startsWith(".") && !name.contains(".."))

    fun enabled(context: Context) = prefs(context).getBoolean(PREF, false)
    fun setEnabled(context: Context, on: Boolean) = prefs(context).edit().putBoolean(PREF, on).apply()
    fun mobileData(context: Context) = prefs(context).getBoolean(PREF_MOBILE, false)
    fun setMobileData(context: Context, on: Boolean) = prefs(context).edit().putBoolean(PREF_MOBILE, on).apply()
    // The laptop's voice mode as the Voices sheet last saw it ("own", "one", "surprise"): Settings names it offline
    fun laptopMode(context: Context): String? = prefs(context).getString(PREF_LAPTOP_MODE, null)
    fun setLaptopMode(context: Context, mode: String) {
        if (laptopMode(context) != mode) prefs(context).edit().putString(PREF_LAPTOP_MODE, mode).apply()
    }
    private const val PREF_LAPTOP_MODE = "voices_mode"
    private fun prefs(context: Context) = context.getSharedPreferences("call", Context.MODE_PRIVATE)

    // --- the model files --------------------------------------------------------------------------------------

    private fun root(context: Context) = File(context.filesDir, "phone-voice")
    fun dir(context: Context) = File(root(context), MODEL)

    /** Folders of models this app no longer speaks with (Kokoro's, about 360 MB). */
    fun stale(context: Context): List<File> = stale(root(context))
    fun stale(root: File): List<File> = root.listFiles()?.filter { it.isDirectory && it.name != MODEL } ?: emptyList()
    fun staleSize(context: Context): Long = stale(context).sumOf { d -> d.walkTopDown().filter { it.isFile }.sumOf { it.length() } }

    /** Whether the laptop's manifest offers the model this app speaks with; else why not. */
    fun offered(manifest: JSONObject): String? = when {
        manifest.optString("version") == MODEL -> null
        manifest.optString("version").startsWith("kokoro") -> "The laptop still offers the earlier voice: update Henchline on the laptop."
        else -> "The laptop offers another voice model: update the app."
    }

    /** Downloaded and every file checked: the manifest it was checked against is kept as the marker (an older
     *  download, from before the espeak data, lacks some of REQUIRED). */
    fun ready(context: Context): Boolean = ModelDownload.ready(dir(context), REQUIRED)

    fun sizeOnPhone(context: Context): Long = dir(context).walkTopDown().filter { it.isFile }.sumOf { it.length() }

    fun delete(context: Context) {
        release()
        root(context).deleteRecursively()  // an earlier model's folder with it
    }

    /** May the download start on this network: Wi-Fi (unmetered) unless the user allowed mobile data. */
    fun networkAllows(context: Context): Boolean = mobileData(context) || unmetered(context)

    /** The phone's network is Wi-Fi or another that isn't metered. */
    fun unmetered(context: Context): Boolean {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
    }

    /** Jenny's download from the laptop (ModelDownload): her files, checked against the laptop's hashes; the
     *  earlier voice's folder goes once she's whole, not before, so a cut download leaves the phone no worse off. */
    fun spec(context: Context) = ModelDownload.Spec(
        route = "/phone-voice", dir = dir(context), required = REQUIRED, safeName = ::safeName, offered = ::offered,
        notPrepared = "The laptop hasn't prepared the voice yet (python -m voice.phonevoice prepare).",
        whole = { stale(context).forEach { it.deleteRecursively() } })

    // --- the engine -----------------------------------------------------------------------------------------

    /** Threads for the engine: the phone's fastest cores (the top two clock tiers; ONNX waits for its slowest
     *  thread, so a slow core holds every render back), else FALLBACK_THREADS. */
    val threads: Int by lazy {
        try {
            val speeds = (0 until 16).mapNotNull { File("/sys/devices/system/cpu/cpu$it/cpufreq/cpuinfo_max_freq")
                .takeIf { f -> f.canRead() }?.readText()?.trim()?.toLongOrNull() }
            val tiers = speeds.distinct().sortedDescending().take(2)
            speeds.count { it in tiers }.takeIf { it in 2..8 && it < speeds.size } ?: FALLBACK_THREADS
        } catch (_: Exception) { FALLBACK_THREADS }
    }

    /** Running render speed on this phone (render s per audio s), told to the laptop so it cuts pieces to fit. */
    @Volatile var rtf = 0f
        private set

    /** The call's rate from the engine's own (Jenny: 22.05 kHz), set when the model loads. */
    @Volatile private var resampler = Resampler(RATE, RATE)

    private fun measured(renderMs: Long, samples: Int) {
        if (samples <= 0) return
        val now = renderMs / 1000f / (samples.toFloat() / RATE)
        rtf = if (rtf == 0f) now else rtf * 0.7f + now * 0.3f
    }

    @Volatile private var tts: com.k2fsa.sherpa.onnx.OfflineTts? = null
    private val loadLock = Any()
    private val renderLock = Any()  // one render at a time on the engine: a call's clip and a preview take turns

    /** The model in memory; held for the call. */
    fun load(context: Context): Boolean = synchronized(loadLock) {
        if (tts != null) return true
        if (!ready(context)) return false
        // ONNX's worker threads are made here and take this thread's priority: audio priority, a step under the call's mic and speaker so its network threads keep up
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO)
        val folder = dir(context).absolutePath
        val before = tasks()
        return try {
            val engine = com.k2fsa.sherpa.onnx.OfflineTts(null, config(folder, threads))
            resampler = Resampler(engine.sampleRate(), RATE)
            // A first render is slow (Kokoro's: 1.55 vs about 1.0 later, 2026-11-22): done here, not on the first reply
            val began = System.currentTimeMillis()
            val warm = resampler.of(render(engine, "Hi.", WARM_PH, SPEAKER))
            measured(System.currentTimeMillis() - began, warm.size)
            engineThreads = spawned(before)
            log("loaded $MODEL (${engine.sampleRate()} Hz to $RATE) with $threads threads, warm-up rtf ${"%.2f".format(rtf)}, " +
                "engine threads ${engineThreads.joinToString(",")}; " +
                placement(intArrayOf(android.os.Process.myTid()) + engineThreads))
            tts = engine
            true
        } catch (e: Throwable) {
            android.util.Log.w("Henchline", "phone voice: couldn't load the model: $e")
            false
        }
    }

    // Lock order: loadLock, then renderLock (only here); a render never takes loadLock, so they can't deadlock. A
    // render under way finishes before the engine is freed, and one after finds tts changed and stops (rendered())
    fun release() = synchronized(loadLock) {
        synchronized(renderLock) { tts?.release(); tts = null; engineThreads = IntArray(0) }
    }

    /** One render on `engine` under renderLock, with the time it took (not counting the wait for the lock); throws
     *  if the engine was released meanwhile (never rendered on freed memory). */
    private fun rendered(engine: com.k2fsa.sherpa.onnx.OfflineTts, text: String, ph: String?, sid: Int): Pair<FloatArray, Long> =
        synchronized(renderLock) {
            check(tts === engine) { "the voice model was released" }
            val began = System.currentTimeMillis()
            render(engine, text, ph, sid) to System.currentTimeMillis() - began
        }

    /** Jenny's engine over the files in `folder`: the model, its symbols, and espeak's data (ignored by sherpa-onnx 2.0,
     *  which speaks only the phonemes it's given). */
    private fun config(folder: String, threads: Int): com.k2fsa.sherpa.onnx.OfflineTtsConfig {
        val vits = com.k2fsa.sherpa.onnx.OfflineTtsVitsModelConfig(
            model = "$folder/$ONNX", tokens = "$folder/tokens.txt", dataDir = "$folder/espeak-ng-data")
        return com.k2fsa.sherpa.onnx.OfflineTtsConfig(
            model = com.k2fsa.sherpa.onnx.OfflineTtsModelConfig(vits = vits, numThreads = threads, provider = "cpu"))
    }

    // --- the render threads' cores ----------------------------------------------------------------------------
    // In the background Android keeps an app to the little cores unless something says its work matters: a call's
    // foreground service moves the process to the foreground cpuset, and a performance hint session (below) asks the
    // scheduler to place and clock the render threads for a deadline. The log shows which held (adb logcat -s Henchline).

    /** ONNX's own worker threads, made inside the engine when it loads: they do most of a render's work. */
    @Volatile private var engineThreads = IntArray(0)

    internal fun tasks(): Set<Int> =
        try { File("/proc/self/task").list()?.mapNotNull { it.toIntOrNull() }?.toSet() ?: emptySet() } catch (_: Exception) { emptySet() }

    private fun comm(tid: Int): String? = try { File("/proc/self/task/$tid/comm").readText().trim() } catch (_: Exception) { null }

    /** Threads made since `before` by this thread: a native thread starts with its maker's name, so other threads the
     *  app made meanwhile are left out. */
    internal fun spawned(before: Set<Int>): IntArray {
        val mine = comm(android.os.Process.myTid()) ?: return IntArray(0)
        return (tasks() - before).filter { comm(it) == mine }.sorted().toIntArray()
    }

    /** The CPU a thread last ran on, from /proc/<pid>/task/<tid>/stat (field 39, counted after the name's ")"). */
    fun cpuOf(stat: String): Int? {
        val fields = stat.substringAfterLast(')').trim().split(' ')
        return fields.getOrNull(36)?.toIntOrNull()  // fields from the 3rd (state) on: the 39th is index 36
    }

    /** The cpuset in a /proc/…/cgroup listing ("4:cpuset:/foreground" → "/foreground"), or null. */
    fun cpusetOf(cgroup: String): String? =
        cgroup.lineSequence().map { it.split(':', limit = 3) }.firstOrNull { it.size == 3 && it[1].split(',').contains("cpuset") }?.get(2)

    /** Where these threads may and did run: the first one's cpuset and allowed CPUs, the process's importance (100 in
     *  front, 125 a foreground service), and the CPU each last ran on. Never throws. */
    fun placement(tids: IntArray): String = try {
        val first = tids.firstOrNull() ?: android.os.Process.myTid()
        val cpuset = try { cpusetOf(File("/proc/self/task/$first/cgroup").readText()) } catch (_: Exception) { null }
        val allowed = try { File("/proc/self/task/$first/status").readLines()
            .firstOrNull { it.startsWith("Cpus_allowed_list:") }?.substringAfter(':')?.trim() } catch (_: Exception) { null }
        val info = android.app.ActivityManager.RunningAppProcessInfo()
        android.app.ActivityManager.getMyMemoryState(info)
        val cpus = tids.joinToString(",") { tid ->
            (try { cpuOf(File("/proc/self/task/$tid/stat").readText()) } catch (_: Exception) { null })?.toString() ?: "?" }
        "cpuset=${cpuset ?: "?"} allowed=${allowed ?: "?"} importance=${info.importance} cpus=$cpus"
    } catch (e: Throwable) { "placement unknown (${e.javaClass.simpleName})" }

    /** A performance hint session (Android 12+) over the render threads: each piece's render time is reported against
     *  a target of TARGET_RTF times its expected speech, and the scheduler raises those threads' clocks and cores to
     *  meet it. Every call is a no-op where unsupported and never throws: the call goes on without it. */
    class Boost(private val context: Context) {
        private var session: PerformanceHintManager.Session? = null
        private var tids = IntArray(0)
        private var unsupported = false
        @Volatile private var closed = false

        /** What the last `start` left: "on", "off" (unsupported or refused) or "closed". */
        @Volatile var state = "off"
            private set

        /** Before a render on this thread, with ONNX's `workers`: (re)binds the session and sets the target. */
        @Synchronized fun start(expectedAudioS: Float, workers: IntArray = engineThreads) {
            if (closed || unsupported || Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return
            try {
                val want = intArrayOf(android.os.Process.myTid()) + workers
                val target = maxOf((expectedAudioS * TARGET_RTF * 1e9).toLong(), 20_000_000L)
                val current = session
                if (current != null && !want.contentEquals(tids)) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) current.setThreads(want)
                    else { current.close(); session = null }  // Android 12–13 can't change a session's threads
                    tids = want
                }
                if (session == null) {
                    val manager = context.getSystemService(PerformanceHintManager::class.java)
                    val made = manager?.createHintSession(want, target)
                    if (made == null) { unsupported = true; state = "off"; return }
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.VANILLA_ICE_CREAM) made.setPreferPowerEfficiency(false)
                    session = made
                    tids = want
                } else session?.updateTargetWorkDuration(target)
                state = "on"
            } catch (e: Throwable) {  // a thread that ended, a refused session: speak unboosted
                try { session?.close() } catch (_: Throwable) {}
                session = null
                state = "off"
                log("hint session failed: $e")
            }
        }

        /** After the render: how long it took. */
        @Synchronized fun done(tookMs: Long) {
            if (closed || Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return
            try { if (tookMs > 0) session?.reportActualWorkDuration(tookMs * 1_000_000L) } catch (_: Throwable) {}
        }

        @Synchronized fun close() {
            closed = true
            state = "closed"
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return
            try { session?.close() } catch (_: Throwable) {}
            session = null
        }
    }

    /** What the phone tells the laptop on connecting: it can speak talkback (only when switched on and here).
     *  `chosen`: the user switched to the phone mid-call (the laptop then says so in this voice). */
    /** required: a call with another computer, which always speaks through the phone (it makes no speech of its
     *  own, 2026-11-22): offered whatever the laptop-call setting, marked so the computer never answers with audio. */
    /** listening: the phone hears the user itself on this call (PhoneHearing): talkback is spoken here too, so it
     *  isn't streamed over the line either (the user's decision, 2026-11-23), whatever the setting. */
    fun offer(context: Context, chosen: Boolean = false, required: Boolean = false, listening: Boolean = false): JSONObject? =
        if ((required || listening || enabled(context)) && ready(context)) JSONObject().put("tts", JSONObject().put("engine", ENGINE).put("model", MODEL)
            .apply { if (rtf > 0f) put("rtf", rtf.toDouble()) }
            .apply { if (required) put("required", true) })
            .apply { if (chosen || required) put("chosen", true) }
        else null

    // The Voices sheet's preview: its phonemes made once on the laptop (python -m voice.phonevoice phonemes …), as
    // this engine speaks only phonemes
    const val PREVIEW = "Hello, I'm Jenny, speaking from your phone."
    private const val PREVIEW_PH = "həlˈəʊ, aɪm dʒˈɛnɪ, spˈiːkɪŋ fɹɒm jɔː fˈəʊn."

    /** The preview line as 16-bit PCM at RATE, or null when the model isn't here or won't load. Loads the model
     *  (about 2.4 s the first time); off a call the caller releases it again. Never throws. */
    fun previewPcm(context: Context): ByteArray? = try {
        if (!load(context)) null
        else tts?.let { engine -> pcm(resampler.of(rendered(engine, PREVIEW, PREVIEW_PH, SPEAKER).first), 0f) }
    } catch (e: Throwable) {
        android.util.Log.w("Henchline", "phone voice: preview failed: $e")
        null
    }

    /** Why the phone shouldn't take a clip now, or null. */
    private fun unfit(context: Context): String? {
        val battery = context.getSystemService(BatteryManager::class.java)
            ?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY) ?: 100
        val charging = context.getSystemService(BatteryManager::class.java)?.isCharging ?: false
        if (battery in 1 until LOW_BATTERY && !charging) return "battery $battery%"
        val power = context.getSystemService(PowerManager::class.java)
        if (power != null && power.currentThermalStatus >= PowerManager.THERMAL_STATUS_SEVERE) return "the phone is hot"
        return null
    }

    // --- speaking clips ---------------------------------------------------------------------------------------

    /** One piece's audio, at the engine's own rate. With the laptop's phonemes (`ph`: espeak-ng's IPA, as Piper makes
     *  it; the engine adds its start, end and padding marks, and skips, logging, any symbol the voice lacks) it speaks
     *  those: this engine has no text front end for a Piper voice. Without them, the words, which give it nothing to
     *  say. A piece with nothing to say (a bare tag) is silence. */
    fun render(engine: com.k2fsa.sherpa.onnx.OfflineTts, text: String, ph: String?, sid: Int): FloatArray {
        if (ph == null) return engine.generate(text, sid, 1.0f).samples
        if (ph.isBlank()) return FloatArray(0)
        return engine.generateWithConfig("", com.k2fsa.sherpa.onnx.GenerationConfig(
            sid = sid, speed = 1.0f, phonemeCodepoints = arrayOf(ph.codePoints().toArray()))).samples
    }

    /** The phonemes for piece `n`, or null when the laptop sent none (an older laptop) or a list that doesn't match. */
    fun phonemesOf(clip: JSONObject, n: Int, pieces: Int): String? {
        val ph = clip.optJSONArray("ph") ?: return null
        if (ph.length() != pieces) return null
        return ph.opt(n) as? String
    }

    /** Debug builds only (PhoneVoiceBench): a separate engine with `threads`, the laptop's phonemes `ph` for `text`
     *  rendered 3 times, each run's render speed logged, so the app's speed can be compared with sherpa-onnx's own
     *  command-line build. The downloaded voice, or the one in `folder` (its .onnx as ONNX, tokens.txt). */
    fun bench(context: Context, threads: Int, text: String, ph: String, hint: Boolean = false, folder: String? = null) {
        if (folder == null && !ready(context)) return log("bench: the voice isn't downloaded")
        val config = config(folder ?: dir(context).absolutePath, threads)
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO)
        val before = tasks()
        val engine = com.k2fsa.sherpa.onnx.OfflineTts(null, config)
        val rate = engine.sampleRate()
        // The bench's engine, not the call's: its threads are the ones to hint and watch
        val mine = spawned(before)
        val boost = if (hint) Boost(context) else null
        try {
            for (run in 1..3) {
                boost?.start(text.length / SPEECH_CPS, mine)
                val began = System.currentTimeMillis()
                val samples = render(engine, text, ph, SPEAKER)
                val ms = System.currentTimeMillis() - began
                boost?.done(ms)
                val audio = samples.size.toDouble() / rate
                log("bench model=$MODEL threads=$threads chars=${text.length} ph=${ph.length} hint=${boost?.state ?: "no"} run=$run " +
                    "render_ms=$ms audio_s=${"%.2f".format(audio)} rtf=${"%.2f".format(if (audio > 0) ms / 1000.0 / audio else 0.0)} " +
                    placement(intArrayOf(android.os.Process.myTid()) + mine))
            }
        } finally { boost?.close(); engine.release() }
    }

    /** One call's speaker: clips come from the laptop, audio goes to the call's own playback queue. */
    class Speaker(
        private val context: Context,
        private val enqueue: (ByteArray) -> Unit,  // 20 ms of 16-bit mono at 24 kHz into the call's speaker
        private val queued: () -> Int,  // chunks still waiting at the speaker
        private val send: (String) -> Unit,  // a message to the laptop
        private val played: () -> Long = { 0L },  // this phone's own chunks the speaker has taken, all told
        private val flush: () -> Unit = {},  // drop what's still queued at the speaker
        private val fellBackNow: () -> Unit = {},  // it just fell back by itself: the Voices sheet says so
    ) {
        // Where each piece of the clip being spoken ends, in chunks enqueued all told: a piece whose end the speaker
        // has passed has been heard. Said on a fallback, so the laptop goes on from the next piece, not from the
        // start of a sentence the user heard a moment ago (2026-11-22: a whole sentence was said twice)
        private val ends = java.util.concurrent.ConcurrentHashMap<Int, Long>()
        @Volatile private var enqueued = 0L

        /** The last piece of the clip whose audio has all left the speaker's queue; -1 for none. */
        private fun heard(): Int {
            val now = played()
            return ends.filterValues { it <= now }.keys.maxOrNull() ?: -1
        }
        private val clips = LinkedBlockingQueue<JSONObject>()
        @Volatile private var current = ""
        private val stopped = java.util.Collections.newSetFromMap(java.util.concurrent.ConcurrentHashMap<String, Boolean>())
        @Volatile private var running = true
        @Volatile private var fellBack = false
        private val boost = Boost(context)  // the render threads' hint session, for this call
        private val thread = Thread({
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_AUDIO)
            loop()
        }, "henchline-phone-voice").apply { isDaemon = true; start() }

        // The last ack of each recent clip: sent again when a dropped line comes back, so the laptop learns how far the
        // phone got while it couldn't hear (the phone goes on speaking through a drop: it has the words)
        private val acks = LinkedHashMap<String, JSONObject>()

        private fun ack(id: String, piece: Int? = null, done: Boolean = false) {
            val said = JSONObject().put("id", id)
            if (piece != null) said.put("piece", piece)
            if (done) said.put("done", true)
            if (rtf > 0f) said.put("rtf", rtf.toDouble())
            synchronized(acks) {
                acks.remove(id); acks[id] = said
                while (acks.size > 8) acks.remove(acks.keys.first())
            }
            send(JSONObject().put("said", said).toString())
        }

        /** How far each recent clip got, for the laptop after a reconnect. */
        fun report(): List<String> = synchronized(acks) { acks.values.map { JSONObject().put("said", it).toString() } }

        /** The id of the clip being spoken now; "" for none. */
        val speaking get() = current

        fun say(clip: JSONObject) { if (!fellBack) clips.offer(clip) else fallback(clip.optString("id"), "fell back earlier") }
        fun stop(id: String) { stopped.add(id) }
        fun stopAll() { clips.clear(); if (current.isNotEmpty()) stopped.add(current) }
        fun close() { running = false; stopAll(); thread.interrupt(); boost.close() }

        /** It fell back to the laptop's audio by itself this call (behind, hot, low battery). */
        val fellBehind get() = fellBack

        /** The user chose the phone again after a fallback: clips are taken again (the laptop is told by an offer). */
        fun tryAgain() { fellBack = false }

        private fun fallback(id: String, reason: String) {
            fellBack = true
            clips.clear()
            val message = JSONObject().put("tts", "fallback").put("reason", reason).put("id", id)
            if (id == current) {
                message.put("heard", heard())  // read before the flush: what was really heard
                flush()  // the laptop says the rest: nothing of the phone's left to play over it
            }
            send(message.toString())
            android.util.Log.i("Henchline", "phone voice: falling back to the laptop's audio ($reason)")
            try { fellBackNow() } catch (_: Throwable) {}
        }

        private fun loop() {
            while (running) {
                val clip = try { clips.poll(500, TimeUnit.MILLISECONDS) } catch (_: InterruptedException) { null } ?: continue
                val id = clip.optString("id")
                if (id.isEmpty() || stopped.remove(id)) continue
                val why = unfit(context)
                if (why != null) { fallback(id, why); continue }
                if (!load(context)) { fallback(id, "the voice model isn't loaded"); continue }
                current = id
                try { speak(clip, id) } catch (e: Throwable) { fallback(id, "rendering failed: ${e.javaClass.simpleName}") }
                current = ""
            }
        }

        private fun speak(clip: JSONObject, id: String) {
            val engine = tts ?: return fallback(id, "the voice model isn't loaded")
            ends.clear()
            val pieces = clip.optJSONArray("pieces") ?: JSONArray()
            val joined = clip.optJSONArray("joined").toIntSet()
            val sid = clip.optInt("sid", SPEAKER).takeIf { it in 0 until engine.numSpeakers() } ?: SPEAKER
            // Rendered ahead of the one playing, on its own thread at audio priority
            val ready = LinkedBlockingQueue<Pair<Int, FloatArray>>(2)
            val over = AtomicBoolean(false)  // this clip is finished, cut or fallen back: the renderer stops
            val renderer = Thread({
                android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO)
                try { for (n in 0 until pieces.length()) {
                    if (over.get() || stopped.contains(id) || !running) break
                    val text = pieces.getString(n)
                    val ph = phonemesOf(clip, n, pieces.length())
                    log("render $id #$n start (${text.length} chars${if (ph != null) ", ${ph.length} phonemes" else ""})")
                    boost.start(text.length / SPEECH_CPS)
                    val (raw, took) = rendered(engine, text, ph, sid)  // a preview's turn on the engine isn't counted
                    val samples = resampler.of(raw)
                    // Words alone (no phonemes from the laptop) give this voice nothing to say: the laptop says it
                    if (samples.isEmpty() && ph == null && text.any { it.isLetterOrDigit() }) error("no phonemes for #$n")
                    boost.done(took)
                    measured(took, samples.size)
                    log("render $id #$n done: ${samples.size} samples in $took ms, rtf ${"%.2f".format(
                        if (samples.isEmpty()) 0f else took / 1000f / (samples.size.toFloat() / RATE))} (running ${"%.2f".format(rtf)}) " +
                        "hint=${boost.state} ${placement(intArrayOf(android.os.Process.myTid()) + engineThreads)}")
                    val trimmed = trim(samples, lead = n in joined, tail = (n + 1) in joined)
                    val offered = System.currentTimeMillis()
                    while (!over.get() && running && !ready.offer(n to trimmed, 100, TimeUnit.MILLISECONDS)) Unit
                    log("$id #$n ready, waited ${System.currentTimeMillis() - offered} ms for room")
                } } catch (_: InterruptedException) {
                    log("render $id interrupted (${if (over.get()) "clip over" else "not over"})")
                } catch (e: Throwable) {  // a thread's uncaught error would end the whole app, the call with it
                    android.util.Log.w("Henchline", "phone voice: rendering failed: $e")
                }
            }, "henchline-phone-voice-render").apply { isDaemon = true; start() }
            val started = System.currentTimeMillis()
            var starves = 0
            try {
                for (n in 0 until pieces.length()) {
                    var waited = 0L
                    var starved = false
                    var next: Pair<Int, FloatArray>? = null
                    while (next == null) {
                        if (stopped.remove(id)) return
                        next = ready.poll(50, TimeUnit.MILLISECONDS)
                        if (next == null && !renderer.isAlive && ready.isEmpty()) return fallback(id, "rendering stopped")
                        if (next == null && n > 0 && queued() == 0) {  // the speaker ran dry waiting for this piece
                            waited += 50
                            val early = n < 2 || System.currentTimeMillis() - started < 5000
                            if (!starved && waited >= (if (early) STARVED_EARLY_MS else STARVED_MS)) {
                                starved = true
                                starves += 1
                                log("$id #$n starved ${waited} ms (${starves} of $STARVES)")
                                if (starves >= STARVES) return fallback(id, "fell behind by over a second, twice")
                            }
                        }
                    }
                    if (n == 0 && pieces.length() > 1) {
                        // Start only once what's ready covers rendering the next piece (or it's ready): a short first
                        // piece would otherwise run dry before the second is made
                        val first = next.second.size.toFloat() / RATE
                        val nextText = pieces.getString(1)
                        val needed = (if (rtf > 0f) rtf else 1f) * nextText.length / SPEECH_CPS
                        val gate = System.currentTimeMillis()
                        while (first < needed && ready.isEmpty() && renderer.isAlive &&
                               System.currentTimeMillis() - gate < minOf(((needed - first) * 1000).toLong(), HOLD_MS)) {
                            if (stopped.remove(id)) return
                            Thread.sleep(20)
                        }
                        val held = System.currentTimeMillis() - gate
                        if (held > 50) log("$id #0 held $held ms for #1 to be ready")
                    }
                    val gap = if (n == 0) 0f else if (n in joined) JOIN else PAUSE
                    val audio = pcm(next.second, gap)
                    ack(id, piece = n)
                    log("$id #$n playing, queued=${queued()}")
                    var at = 0
                    var told = false
                    while (at < audio.size) {
                        if (stopped.remove(id)) { log("$id stopped at #$n"); return }
                        val blocked = System.currentTimeMillis()
                        while (queued() > AHEAD_CHUNKS) {
                            if (stopped.remove(id)) { log("$id stopped at #$n"); return }
                            if (!told && System.currentTimeMillis() - blocked > 500) {
                                told = true
                                log("$id #$n blocked at speaker, queued=${queued()}")
                            }
                            Thread.sleep(10)
                        }
                        val end = minOf(audio.size, at + CHUNK * 2)
                        enqueue(audio.copyOfRange(at, end))
                        enqueued++
                        at = end
                    }
                    ends[n] = enqueued
                    log("$id #$n queued ${audio.size / (CHUNK * 2)} chunks")
                }
                while (queued() > 0 && !stopped.contains(id)) Thread.sleep(20)  // done when it has been heard
                ack(id, done = true)
                log("$id done")
            } finally {
                over.set(true)
                renderer.interrupt()
            }
        }
    }

    /** One line per step of a clip, so a stall shows where it happened (adb logcat -s Henchline). */
    private fun log(line: String) { try { android.util.Log.i("Henchline", "phone voice: $line") } catch (_: RuntimeException) {} }

    // --- shared with the laptop's rules (tests/fixtures/phone-voice-trim.json) ---------------------------------

    /** The voice's quiet lead-in and tail cut (to EDGE) where pieces of one sentence join, as the laptop's trim(). */
    fun trim(samples: FloatArray, lead: Boolean, tail: Boolean): FloatArray {
        if (samples.isEmpty() || !(lead || tail)) return samples
        var peak = 0f
        for (s in samples) peak = maxOf(peak, kotlin.math.abs(s))
        val level = maxOf(peak * QUIET, 1e-5f)
        var first = -1
        var last = -1
        for (i in samples.indices) if (kotlin.math.abs(samples[i]) > level) { if (first < 0) first = i; last = i }
        if (first < 0) return samples
        val keep = (RATE * EDGE).toInt()
        val start = if (lead) maxOf(0, first - keep) else 0
        val end = if (tail) minOf(samples.size, last + 1 + keep) else samples.size
        return samples.copyOfRange(start, end)
    }

    /** 16-bit little-endian PCM, with `gap` seconds of silence in front. */
    fun pcm(samples: FloatArray, gap: Float): ByteArray {
        val silence = (RATE * gap).toInt()
        val out = ByteArray((silence + samples.size) * 2)
        for (i in samples.indices) {
            val v = (samples[i].coerceIn(-1f, 1f) * 32767f).toInt()
            out[(silence + i) * 2] = (v and 0xff).toByte()
            out[(silence + i) * 2 + 1] = ((v shr 8) and 0xff).toByte()
        }
        return out
    }

    private fun JSONArray?.toIntSet(): Set<Int> = if (this == null) emptySet() else (0 until length()).map { getInt(it) }.toSet()
}
