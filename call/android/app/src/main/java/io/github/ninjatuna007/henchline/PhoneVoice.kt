package io.github.ninjatuna007.henchline

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.PowerManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Talkback spoken by the phone itself (the laptop's side: voice/phonevoice.py). Over a call the laptop sends a
 * clip's words, already cut into pieces the way its own talkback cuts them (punctuation only), and the phone renders
 * them with the same Kokoro voices: sherpa-onnx's Kokoro v1.0 export carries the laptop's very voices file, a voice's
 * id is its place in the sorted names. Only words cross the network, so a dropped line in a lift loses nothing that
 * was already here, and a lost clip is simply sent again.
 *
 * Off unless the user turns "Speak on the phone" on and the model is here (about 360 MB, fetched from the laptop,
 * every file checked against the laptop's SHA-256). When the phone can't keep up (a piece late by more than a
 * second while the speaker ran dry), the battery is low or the phone is hot, it says so ({"tts": "fallback"}) and
 * the laptop streams its own audio for the rest of the call, as before.
 */
object PhoneVoice {
    const val MODEL = "kokoro-multi-lang-v1_0"
    const val ENGINE = "sherpa-kokoro"
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
    private const val SPEECH_CPS = 17.5f  // characters of text per second of speech, as the laptop estimates
    private const val LOW_BATTERY = 15
    // espeak-ng's English data under the model folder: Kokoro's fallback for words the lexicon lacks (without it
    // sherpa skipped "Here's" and "lived" outright, 2026-11-22). Exact relative paths, the only ones with a "/"
    val ESPEAK = listOf("espeak-ng-data/phontab", "espeak-ng-data/phonindex", "espeak-ng-data/phondata",
        "espeak-ng-data/intonations", "espeak-ng-data/en_dict", "espeak-ng-data/lang/gmw/en",
        "espeak-ng-data/lang/gmw/en-US", "espeak-ng-data/lang/gmw/en-GB-x-rp")
    // What a phone must have before it counts as ready: one that downloaded before the espeak data was offered fetches
    // just those files the next time (the others are kept, checked by size and hash)
    val REQUIRED = listOf("model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt") + ESPEAK

    /** A name the laptop's manifest may use: a plain file name, or one of the espeak data's fixed relative paths. */
    fun safeName(name: String): Boolean =
        name in ESPEAK || (name.isNotEmpty() && !name.contains('/') && !name.startsWith(".") && !name.contains(".."))

    fun enabled(context: Context) = prefs(context).getBoolean(PREF, false)
    fun setEnabled(context: Context, on: Boolean) = prefs(context).edit().putBoolean(PREF, on).apply()
    fun mobileData(context: Context) = prefs(context).getBoolean(PREF_MOBILE, false)
    fun setMobileData(context: Context, on: Boolean) = prefs(context).edit().putBoolean(PREF_MOBILE, on).apply()
    private fun prefs(context: Context) = context.getSharedPreferences("call", Context.MODE_PRIVATE)

    // --- the model files --------------------------------------------------------------------------------------

    fun dir(context: Context) = File(context.filesDir, "phone-voice/$MODEL")
    private fun marker(context: Context) = File(dir(context), ".verified")

    /** Downloaded and every file checked: the manifest it was checked against is kept as the marker. */
    fun ready(context: Context): Boolean {
        val kept = try { JSONObject(marker(context).readText()) } catch (_: Exception) { return false }
        val files = kept.optJSONArray("files") ?: return false
        val names = (0 until files.length()).map { files.getJSONObject(it).optString("name") }.toSet()
        if (!names.containsAll(REQUIRED)) return false  // an older download, from before the espeak data
        return (0 until files.length()).all { i ->
            val f = files.getJSONObject(i)
            File(dir(context), f.getString("name")).length() == f.getLong("size")
        }
    }

    fun sizeOnPhone(context: Context): Long = dir(context).walkTopDown().filter { it.isFile }.sumOf { it.length() }

    fun delete(context: Context) {
        release()
        dir(context).deleteRecursively()
    }

    /** May the download start on this network: Wi-Fi (unmetered) unless the user allowed mobile data. */
    fun networkAllows(context: Context): Boolean {
        if (mobileData(context)) return true
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
    }

    class Download(private val context: Context, private val progress: (done: Long, total: Long, name: String) -> Unit) {
        private val cancelled = AtomicBoolean(false)
        fun cancel() = cancelled.set(true)

        /** null when done; otherwise why not. Files already here and whole are kept (a cut download carries on). */
        fun run(): String? {
            val origin = Files.origin(context) ?: return "Set the laptop's address in Settings first."
            val manifest = try {
                Files.client.newCall(okhttp3.Request.Builder().url("$origin/phone-voice/manifest")
                    .header("Origin", origin).build()).execute().use {
                    if (!it.isSuccessful) return "The laptop hasn't prepared the voice yet (python -m voice.phonevoice prepare)."
                    JSONObject(it.body!!.string())
                }
            } catch (e: Exception) { return "Couldn't reach the laptop (is Tailscale on?)" }
            if (manifest.optString("version") != MODEL) return "The laptop offers another voice model: update the app."
            val files = manifest.getJSONArray("files")
            val total = (0 until files.length()).sumOf { files.getJSONObject(it).getLong("size") }
            val folder = dir(context).apply { mkdirs() }
            marker(context).delete()
            var done = 0L
            for (i in 0 until files.length()) {
                val f = files.getJSONObject(i)
                val name = f.getString("name")
                if (!safeName(name)) return "The laptop named an odd file: stopped."
                val target = File(folder, name)
                target.parentFile?.mkdirs()
                val size = f.getLong("size")
                if (target.length() == size && sha256(target) == f.getString("sha256")) { done += size; continue }
                val part = File(folder, "$name.part")
                val have = part.length().coerceAtMost(size)
                try {
                    val request = okhttp3.Request.Builder().url("$origin/phone-voice/file/$name").header("Origin", origin)
                    if (have > 0) request.header("Range", "bytes=$have-")
                    Files.client.newCall(request.build()).execute().use { answer ->
                        if (!answer.isSuccessful) return "The laptop couldn't send $name."
                        val append = have > 0 && answer.code == 206
                        java.io.FileOutputStream(part, append).use { out ->
                            var got = if (append) have else 0L
                            val input = answer.body!!.byteStream()
                            val buffer = ByteArray(1 shl 16)
                            while (true) {
                                if (cancelled.get()) return "Cancelled."
                                val n = input.read(buffer)
                                if (n < 0) break
                                out.write(buffer, 0, n)
                                got += n
                                progress(done + got, total, name)
                            }
                        }
                    }
                } catch (e: Exception) { return "The download broke off: try again to carry on." }
                if (sha256(part) != f.getString("sha256")) { part.delete(); return "$name didn't match the laptop's copy: try again." }
                part.renameTo(target)
                done += size
            }
            marker(context).writeText(manifest.toString())
            return null
        }
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(1 shl 16)
            while (true) { val n = input.read(buffer); if (n < 0) break; digest.update(buffer, 0, n) }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

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

    private fun measured(renderMs: Long, samples: Int) {
        if (samples <= 0) return
        val now = renderMs / 1000f / (samples.toFloat() / RATE)
        rtf = if (rtf == 0f) now else rtf * 0.7f + now * 0.3f
    }

    @Volatile private var tts: com.k2fsa.sherpa.onnx.OfflineTts? = null
    private val loadLock = Any()

    /** The model in memory (about 2.4 s the first time); held for the call. */
    fun load(context: Context): Boolean = synchronized(loadLock) {
        if (tts != null) return true
        if (!ready(context)) return false
        // ONNX's worker threads are made here and take this thread's priority: audio priority, a step under the call's mic and speaker so its network threads keep up
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO)
        val folder = dir(context).absolutePath
        return try {
            val kokoro = com.k2fsa.sherpa.onnx.OfflineTtsKokoroModelConfig(
                model = "$folder/model.onnx", voices = "$folder/voices.bin", tokens = "$folder/tokens.txt",
                lexicon = "$folder/lexicon-us-en.txt", lang = "en-us",
                dataDir = "$folder/espeak-ng-data")  // words the lexicon lacks are spoken through espeak, not skipped
            val config = com.k2fsa.sherpa.onnx.OfflineTtsConfig(
                model = com.k2fsa.sherpa.onnx.OfflineTtsModelConfig(kokoro = kokoro, numThreads = threads, provider = "cpu"))
            val engine = com.k2fsa.sherpa.onnx.OfflineTts(null, config)
            // A first render is slow (1.55 vs about 1.0 later, 2026-11-22): done here, not on the first reply
            val began = System.currentTimeMillis()
            val warm = engine.generate("Hi.", 3, 1.0f).samples
            measured(System.currentTimeMillis() - began, warm.size)
            log("loaded with $threads threads, warm-up rtf ${"%.2f".format(rtf)}")
            tts = engine
            true
        } catch (e: Throwable) {
            android.util.Log.w("Henchline", "phone voice: couldn't load the model: $e")
            false
        }
    }

    fun release() = synchronized(loadLock) { tts?.release(); tts = null }

    /** What the phone tells the laptop on connecting: it can speak talkback (only when switched on and here). */
    fun offer(context: Context): JSONObject? =
        if (enabled(context) && ready(context)) JSONObject().put("tts", JSONObject().put("engine", ENGINE).put("model", MODEL)
            .apply { if (rtf > 0f) put("rtf", rtf.toDouble()) })
        else null

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

    /** Debug builds only (PhoneVoiceBench): a separate engine with `threads`, `text` rendered 3 times, each run's
     *  render speed logged, so the app's speed can be compared with sherpa-onnx's own command-line build. */
    fun bench(context: Context, threads: Int, text: String) {
        if (!ready(context)) return log("bench: the voice isn't downloaded")
        val folder = dir(context).absolutePath
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO)
        val kokoro = com.k2fsa.sherpa.onnx.OfflineTtsKokoroModelConfig(
            model = "$folder/model.onnx", voices = "$folder/voices.bin", tokens = "$folder/tokens.txt",
            lexicon = "$folder/lexicon-us-en.txt", lang = "en-us", dataDir = "$folder/espeak-ng-data")
        val engine = com.k2fsa.sherpa.onnx.OfflineTts(null, com.k2fsa.sherpa.onnx.OfflineTtsConfig(
            model = com.k2fsa.sherpa.onnx.OfflineTtsModelConfig(kokoro = kokoro, numThreads = threads, provider = "cpu")))
        try {
            for (run in 1..3) {
                val began = System.currentTimeMillis()
                val samples = engine.generate(text, 3, 1.0f).samples
                val ms = System.currentTimeMillis() - began
                val audio = samples.size.toDouble() / RATE
                log("bench threads=$threads chars=${text.length} run=$run render_ms=$ms audio_s=${"%.2f".format(audio)} " +
                    "rtf=${"%.2f".format(if (audio > 0) ms / 1000.0 / audio else 0.0)}")
            }
        } finally { engine.release() }
    }

    /** One call's speaker: clips come from the laptop, audio goes to the call's own playback queue. */
    class Speaker(
        private val context: Context,
        private val enqueue: (ByteArray) -> Unit,  // 20 ms of 16-bit mono at 24 kHz into the call's speaker
        private val queued: () -> Int,  // chunks still waiting at the speaker
        private val send: (String) -> Unit,  // a message to the laptop
        private val played: () -> Long = { 0L },  // this phone's own chunks the speaker has taken, all told
        private val flush: () -> Unit = {},  // drop what's still queued at the speaker
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

        fun say(clip: JSONObject) { if (!fellBack) clips.offer(clip) else fallback(clip.optString("id"), "fell back earlier") }
        fun stop(id: String) { stopped.add(id) }
        fun stopAll() { clips.clear(); if (current.isNotEmpty()) stopped.add(current) }
        fun close() { running = false; stopAll(); thread.interrupt() }

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
            val sid = clip.optInt("sid", 3)
            // Rendered ahead of the one playing, on its own thread at audio priority
            val ready = LinkedBlockingQueue<Pair<Int, FloatArray>>(2)
            val over = AtomicBoolean(false)  // this clip is finished, cut or fallen back: the renderer stops
            val renderer = Thread({
                android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO)
                try { for (n in 0 until pieces.length()) {
                    if (over.get() || stopped.contains(id) || !running) break
                    val text = pieces.getString(n)
                    log("render $id #$n start (${text.length} chars)")
                    val began = System.currentTimeMillis()
                    val samples = engine.generate(text, sid, 1.0f).samples
                    val took = System.currentTimeMillis() - began
                    measured(took, samples.size)
                    log("render $id #$n done: ${samples.size} samples in $took ms, rtf ${"%.2f".format(
                        if (samples.isEmpty()) 0f else took / 1000f / (samples.size.toFloat() / RATE))} (running ${"%.2f".format(rtf)})")
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
                               System.currentTimeMillis() - gate < minOf(((needed - first) * 1000).toLong(), 2000L)) {
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

    /** Kokoro's quiet lead-in and tail cut (to EDGE) where pieces of one sentence join, as the laptop's trim(). */
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
