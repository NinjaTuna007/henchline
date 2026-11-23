package io.github.ninjatuna007.henchline

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.os.BatteryManager
import android.os.Build
import java.io.File
import java.io.RandomAccessFile

/**
 * Debug builds only: speech-to-text candidates measured on this phone, on the user's own recordings, before any is
 * chosen for calls with another computer. Nothing here runs in a call or
 * ships: the receiver is disabled in release builds (the same placeholder as PhoneVoiceBench) and needs DUMP.
 *
 * Files (put there from the laptop, never bundled, never committed), all under one base folder: the app's own files
 * folder when it holds the set (filled with `adb exec-in run-as io.github.ninjatuna007.henchline tar -x -C files`, since
 * files adb pushes to the external folder belong to the shell and are often unreadable to the app), else the external
 * files folder (/sdcard/Android/data/io.github.ninjatuna007.henchline/files):
 *   asr/<model>/            a streaming transducer: encoder*.onnx, decoder*.onnx, joiner*.onnx (int8 preferred),
 *                           tokens.txt, and bpe.vocab for hint words (as the laptop's Nemotron has)
 *   asr-set/refs.tsv        one "take.wav<TAB>what was said" per line; the takes beside it: 16 kHz mono 16-bit WAV
 *   asr-set/names.txt       the hint words, one per line (also how names are scored)
 * From the laptop:
 *   adb shell am broadcast -n io.github.ninjatuna007.henchline/.PhoneSpeechBench --es model nemotron \
 *     [--ei threads 2] [--ez hint true] [--ef score 2.0] [--ez jenny true] [--ez boost true] [--ei loops 1] [--es set asr-set]
 *   (--es model android: the phone's own on-device recognizer, Android 13+, if it has one; --es lang en-US picks its
 *   language, asking the phone to download it first if it can)
 *   adb logcat -s Henchline | grep "speech bench"
 * Each take is fed in real time, each 0.1 s chunk once its audio would have been heard; "wait" is from when the
 * last chunk was due (the end of the take in real time, not when decoding let it be fed) to the final text (after 0.4 s
 * of silence and the end of input, as the laptop measured Nemotron). A take the phone's own recognizer gives no text
 * for (an error, or no answer in 30 s) counts as every word missed, and as a shortfall. jenny: Jenny renders over and over meanwhile (her
 * own bench lines show her speed alongside). boost: the call's performance hint session over the threads. loops: the
 * set again and again, with the battery's charge counter before and after (on battery only). Keep the app open, or
 * run it during a call, so the process isn't stopped mid-run.
 */
class PhoneSpeechBench : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE == 0) return
        val options = SpeechBench.Options(
            model = intent.getStringExtra("model")?.takeIf { Regex("[A-Za-z0-9._-]{1,64}").matches(it) } ?: return log("give --es model NAME"),
            threads = intent.getIntExtra("threads", 2).coerceIn(1, 8),
            hint = intent.getBooleanExtra("hint", false),
            score = intent.getFloatExtra("score", 2.0f).coerceIn(0f, 5f),
            jenny = intent.getBooleanExtra("jenny", false),
            boost = intent.getBooleanExtra("boost", false),
            loops = intent.getIntExtra("loops", 1).coerceIn(1, 50),
            set = intent.getStringExtra("set")?.takeIf { Regex("[A-Za-z0-9._-]{1,64}").matches(it) } ?: "asr-set",
            lang = benchLanguage(intent.getStringExtra("lang")))
        // A language it can't take is said, not quietly run as English
        intent.getStringExtra("lang")?.let { if (benchLanguage(it).isEmpty()) log("lang \"${it.take(40)}\" rejected (want e.g. en-US): English instead") }
        val app = context.applicationContext
        // Minutes, not seconds: past a receiver's time, on its own thread (the app kept open, or a call up)
        Thread({ try { SpeechBench.run(app, options) } catch (e: Throwable) { log("failed: $e") } }, "henchline-speech-bench").start()
    }
}

private fun log(line: String) { try { android.util.Log.i("Henchline", "speech bench: $line") } catch (_: RuntimeException) {} }

/** --es lang as given when it looks like a language tag ("en", "en-US"); "" (the default English) when absent or not. */
fun benchLanguage(raw: String?): String = raw?.takeIf { Regex("[a-z]{2,3}(-[A-Z]{2})?").matches(it) } ?: ""

object SpeechBench {
    data class Options(val model: String, val threads: Int, val hint: Boolean, val score: Float, val jenny: Boolean,
                       val boost: Boolean, val loops: Int, val set: String, val lang: String = "")

    private class Take(val name: String, val ref: String, val pcm: FloatArray)

    private class Heard(val text: String, val waitMs: Long, val computeMs: Long, val failed: Boolean = false)

    fun run(context: Context, o: Options) {
        // The app's own files first (filled with `adb exec-in run-as … tar -x`: files adb pushes to the external
        // folder belong to the shell and are often unreadable to the app), else the external files folder
        val base = context.filesDir.takeIf { File(it, o.set).isDirectory }
            ?: context.getExternalFilesDir(null) ?: return log("no external files folder")
        val setDir = File(base, o.set)
        val takes = SpeechScore.refs(File(setDir, "refs.tsv").takeIf { it.exists() }?.readText() ?: return log("no ${o.set}/refs.tsv"))
            .mapNotNull { (file, ref) -> wav16k(File(setDir, file))?.let { Take(file, ref, it) } ?: run { log("skipped $file: not 16 kHz mono 16-bit"); null } }
        if (takes.isEmpty()) return log("no takes")
        val names = File(setDir, "names.txt").takeIf { it.exists() }?.readLines()?.map { it.trim() }?.filter { it.isNotEmpty() } ?: emptyList()
        val battery = context.getSystemService(BatteryManager::class.java)
        val charging = battery?.isCharging ?: false
        val chargeBefore = battery?.getLongProperty(BatteryManager.BATTERY_PROPERTY_CHARGE_COUNTER) ?: 0L
        val began = System.currentTimeMillis()
        val rssBefore = memory("VmRSS")
        val renderJenny = java.util.concurrent.atomic.AtomicBoolean(o.jenny)
        val jenny = if (!o.jenny) null else Thread({
            while (renderJenny.get()) try { PhoneVoice.bench(context, 2, JENNY_TEXT, JENNY_PH, hint = o.boost) } catch (_: Throwable) { break }
        }, "henchline-speech-bench-jenny").apply { start() }
        val results = ArrayList<Pair<Take, Heard>>()
        try {
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO)
            if (o.model == "android") {
                if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return log("android: needs Android 13")
                val asr = OnDevice(context, if (o.hint) names else emptyList(), o.lang)
                if (!asr.available) return log("android: no on-device recognizer on this phone")
                if (!asr.ready()) return  // its English model isn't on the phone yet: asked for, try again once it is
                repeat(o.loops) {
                    for (t in takes) {
                        val heard = asr.hear(t.pcm) ?: Heard("", 0L, 0L, failed = true)  // scored as all words missed
                        results += t to heard
                        line(o, t, heard)
                    }
                }
            } else {
                // Nemotron's files may have been taken over by the app's own speech model (PhoneAsr moves them
                // where it can't link them): the bench then reads them there, whole download or not
                val dir = File(base, "asr/${o.model}").takeIf { d -> d.listFiles()?.any { it.name.startsWith("encoder") } == true }
                    ?: PhoneAsr.dir(context).takeIf { o.model == "nemotron" && File(it, "encoder.int8.onnx").isFile }
                    ?: File(base, "asr/${o.model}")
                val before = PhoneVoice.tasks()
                val recognizer = transducer(dir, o, names, base) ?: return
                val workers = PhoneVoice.spawned(before)
                val boost = if (o.boost) PhoneVoice.Boost(context) else null
                try {
                    repeat(o.loops) {
                        for (t in takes) {
                            boost?.start(t.pcm.size / 16000f, workers)
                            val heard = feed(recognizer, t.pcm)
                            boost?.done(heard.computeMs)
                            results += t to heard
                            line(o, t, heard, PhoneVoice.placement(intArrayOf(android.os.Process.myTid()) + workers))
                        }
                    }
                } finally { boost?.close(); recognizer.release() }
            }
        } finally {
            renderJenny.set(false)
            jenny?.join(30_000)
        }
        val minutes = (System.currentTimeMillis() - began) / 60_000.0
        val chargeAfter = battery?.getLongProperty(BatteryManager.BATTERY_PROPERTY_CHARGE_COUNTER) ?: 0L
        val chargingAfter = battery?.isCharging ?: false
        val wer = SpeechScore.wer(results.map { it.first.ref to it.second.text })
        var said = 0; var heard = 0
        for ((t, h) in results) SpeechScore.names(t.ref, h.text, names).let { said += it.first; heard += it.second }
        val failed = results.count { it.second.failed }
        val waits = results.filter { !it.second.failed }.map { it.second.waitMs }  // a failed take has no wait
        val audio = results.sumOf { it.first.pcm.size.toDouble() / 16000 }
        val compute = results.sumOf { it.second.computeMs } / 1000.0
        // The phone's own recognizer computes in another process: its compute isn't seen here
        val rtf = if (o.model == "android" || audio <= 0) null else compute / audio
        val shortfalls = SpeechScore.shortfalls(wer, said, heard, waits, rtf, failed)
        log("summary model=${o.model} threads=${o.threads} hint=${o.hint} score=${o.score} jenny=${o.jenny} boost=${o.boost} " +
            "takes=${results.size} failed=$failed wer=${"%.1f".format(wer * 100)}% names=$heard/$said " +
            "wait_median_ms=${SpeechScore.median(waits)} wait_p90_ms=${SpeechScore.p90(waits)} " +
            "wait_max_ms=${waits.maxOrNull() ?: 0} rtf=${rtf?.let { "%.2f".format(it) } ?: "n/a"} " +
            "rss_before_kb=$rssBefore rss_peak_kb=${memory("VmHWM")} minutes=${"%.1f".format(minutes)} " +
            when {
                charging != chargingAfter -> "battery=charging_changed(not measured)"
                charging -> "battery=charging(not measured)"
                else -> "battery_uah=${chargeBefore - chargeAfter}"
            } + " " +
            if (shortfalls.isEmpty()) "MEETS THE BAR (check Jenny's lines and the battery against today's call)" else "BELOW THE BAR: ${shortfalls.joinToString("; ")}")
    }

    /** One take's numbers (no words: the recordings and what was said stay on the phone). */
    private fun line(o: Options, t: Take, h: Heard, where: String = "") {
        log(("take model=${o.model} file=${t.name} audio_s=${"%.2f".format(t.pcm.size / 16000.0)} " +
            (if (h.failed) "FAILED(no text) " else "wait_ms=${h.waitMs} ") +
            "compute_ms=${h.computeMs} errors=${SpeechScore.edits(SpeechScore.words(t.ref), SpeechScore.words(h.text))}/" +
            "${SpeechScore.words(t.ref).size} $where").trim())
    }

    /** A streaming transducer from that folder (int8 files preferred), hint words from names.txt when asked. */
    private fun transducer(dir: File, o: Options, names: List<String>, base: File): com.k2fsa.sherpa.onnx.OnlineRecognizer? {
        fun pick(prefix: String) = dir.listFiles()?.filter { it.name.startsWith(prefix) && it.name.endsWith(".onnx") }
            ?.minByOrNull { if ("int8" in it.name) 0 else 1 }
        val encoder = pick("encoder"); val decoder = pick("decoder"); val joiner = pick("joiner")
        val tokens = File(dir, "tokens.txt")
        if (encoder == null || decoder == null || joiner == null || !tokens.exists()) { log("${o.model}: needs encoder, decoder, joiner and tokens.txt"); return null }
        val vocab = File(dir, "bpe.vocab")
        val hints = if (o.hint && names.isNotEmpty() && vocab.exists()) File(base, "asr-hotwords.txt").apply { writeText(names.joinToString("\n", postfix = "\n")) } else null
        if (o.hint && hints == null) log("${o.model}: no hint words (needs ${o.set}/names.txt and bpe.vocab): measured without")
        val config = com.k2fsa.sherpa.onnx.OnlineRecognizerConfig(
            featConfig = com.k2fsa.sherpa.onnx.FeatureConfig(sampleRate = 16000, featureDim = 80, dither = 0f),
            modelConfig = com.k2fsa.sherpa.onnx.OnlineModelConfig(
                transducer = com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig(encoder.path, decoder.path, joiner.path),
                tokens = tokens.path, numThreads = o.threads, provider = "cpu",
                modelingUnit = if (hints != null) "bpe" else "", bpeVocab = if (hints != null) vocab.path else ""),
            enableEndpoint = false,
            decodingMethod = "modified_beam_search", maxActivePaths = 4,  // as the laptop decodes
            hotwordsFile = hints?.path ?: "", hotwordsScore = o.score)
        val loaded = System.currentTimeMillis()
        return try {
            com.k2fsa.sherpa.onnx.OnlineRecognizer(null, config).also {
                log("${o.model}: loaded in ${System.currentTimeMillis() - loaded} ms (encoder ${encoder.name}, ${encoder.length() / 1_000_000} MB)")
            }
        } catch (e: Throwable) { log("${o.model}: wouldn't load: $e"); null }
    }

    /** One take, fed in real time 0.1 s at a time, each chunk once its audio would have been heard; then 0.4 s of
     *  silence and the end of input, decoded to the end. The wait counts from when the last chunk was due, so decoding
     *  slower than live audio shows in it. */
    private fun feed(recognizer: com.k2fsa.sherpa.onnx.OnlineRecognizer, pcm: FloatArray): Heard {
        val stream = recognizer.createStream("")
        var compute = 0L
        try {
            val start = System.nanoTime()
            var at = 0
            var due = start
            while (at < pcm.size) {
                val end = minOf(pcm.size, at + CHUNK)
                due = start + end * NS_PER_SAMPLE  // when the chunk's last sample would have been heard
                val now = System.nanoTime()
                if (due > now) Thread.sleep((due - now) / 1_000_000, ((due - now) % 1_000_000).toInt())
                stream.acceptWaveform(pcm.copyOfRange(at, end), 16000)
                val t0 = System.nanoTime()
                while (recognizer.isReady(stream)) recognizer.decode(stream)
                compute += System.nanoTime() - t0
                at = end
            }
            val fed = System.nanoTime()
            stream.acceptWaveform(FloatArray(TAIL), 16000)
            stream.inputFinished()
            while (recognizer.isReady(stream)) recognizer.decode(stream)
            val done = System.nanoTime()
            compute += done - fed
            return Heard(recognizer.getResult(stream).text.trim(), (done - due) / 1_000_000, compute / 1_000_000)
        } finally { stream.release() }
    }

    /** A 16 kHz mono 16-bit WAV's samples, or null for anything else. */
    private fun wav16k(file: File): FloatArray? = try {
        RandomAccessFile(file, "r").use { f ->
            val head = ByteArray(12).also { f.readFully(it) }
            if (String(head, 0, 4) != "RIFF" || String(head, 8, 4) != "WAVE") return null
            var rate = 0; var channels = 0; var bits = 0
            while (f.filePointer < f.length()) {
                val id = ByteArray(4).also { f.readFully(it) }.let { String(it) }
                val size = Integer.reverseBytes(f.readInt())
                when (id) {
                    "fmt " -> {
                        val fmt = ByteArray(size).also { f.readFully(it) }
                        channels = (fmt[2].toInt() and 0xff) or ((fmt[3].toInt() and 0xff) shl 8)
                        rate = (fmt[4].toInt() and 0xff) or ((fmt[5].toInt() and 0xff) shl 8) or ((fmt[6].toInt() and 0xff) shl 16)
                        bits = (fmt[14].toInt() and 0xff) or ((fmt[15].toInt() and 0xff) shl 8)
                    }
                    "data" -> {
                        if (rate != 16000 || channels != 1 || bits != 16) return null
                        val bytes = ByteArray(size).also { f.readFully(it) }
                        return FloatArray(size / 2) { i -> ((bytes[2 * i].toInt() and 0xff) or (bytes[2 * i + 1].toInt() shl 8)).toShort() / 32768f }
                    }
                    else -> f.seek(f.filePointer + size + (size and 1))
                }
            }
            null
        }
    } catch (e: Exception) { null }

    /** A line of /proc/self/status in kB (VmRSS now, VmHWM the peak). */
    private fun memory(key: String): Long = try {
        File("/proc/self/status").readLines().firstOrNull { it.startsWith("$key:") }?.split(Regex("\\s+"))?.getOrNull(1)?.toLong() ?: 0L
    } catch (_: Exception) { 0L }

    /** The phone's own on-device recognizer (Android 13+), fed the take through EXTRA_AUDIO_SOURCE in real time: the
     *  session ends when that audio is closed. Driven on the main thread, as SpeechRecognizer must be. Null when the
     *  take gives no text: an error, no answer in 30 s, or the recognizer no longer taking the audio. */
    @androidx.annotation.RequiresApi(Build.VERSION_CODES.TIRAMISU)
    private class OnDevice(private val context: Context, private val hints: List<String>, private val asked: String = "") {
        private var language = "en-US"  // the installed English one, once ready() has looked
        private val main = android.os.Handler(android.os.Looper.getMainLooper())
        val available = android.speech.SpeechRecognizer.isOnDeviceRecognitionAvailable(context)

        /** Whether its English model is installed; if the phone could get it, asks for the download (once). */
        @androidx.annotation.RequiresApi(Build.VERSION_CODES.TIRAMISU)
        fun ready(): Boolean {
            val done = java.util.concurrent.CountDownLatch(1)
            var installed = false
            var checker: android.speech.SpeechRecognizer? = null  // touched on the main thread only
            fun release() = main.post { try { checker?.destroy() } catch (_: Throwable) {}; checker = null }
            main.post {
                try {
                    val r = android.speech.SpeechRecognizer.createOnDeviceSpeechRecognizer(context).also { checker = it }
                    val ask = android.content.Intent(android.speech.RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                        .putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE, "en-US")
                    r.checkRecognitionSupport(ask, context.mainExecutor, object : android.speech.RecognitionSupportCallback {
                        override fun onSupportResult(support: android.speech.RecognitionSupport) {
                            val have = support.installedOnDeviceLanguages
                            // en-US when it is there, so runs on one phone stay comparable; else any English
                            // --es lang: that one only (downloaded if it can be); else en-US when it is there, so runs on one
                            // phone stay comparable, else any English
                            val english = if (asked.isNotEmpty()) have.firstOrNull { it == asked }
                                else have.firstOrNull { it == "en-US" } ?: have.firstOrNull { it.startsWith("en") }
                            val gettable = support.supportedOnDeviceLanguages.filter {
                                if (asked.isNotEmpty()) it == asked else it.startsWith("en") }
                            installed = english != null
                            if (english != null) language = english
                            log("android: installed=$have pending=${support.pendingOnDeviceLanguages} downloadable=$gettable")
                            when {
                                english != null -> { log("android: using $english"); release() }
                                gettable.isEmpty() -> { log("android: no ${asked.ifEmpty { "English" }} model on this phone, none to download"); release() }
                                else -> {
                                    val wanted = gettable.firstOrNull { it == asked.ifEmpty { "en-US" } } ?: gettable.first()
                                    try {
                                        r.triggerModelDownload(Intent(android.speech.RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                                            .putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE, wanted))
                                        log("android: ${asked.ifEmpty { "no English model" }} not installed yet; asked for $wanted, run again once it is in")
                                    } catch (e: Throwable) { log("android: download of $wanted not started: $e") }
                                    main.postDelayed({ release() }, 2000)
                                }
                            }
                            done.countDown()
                        }
                        override fun onError(error: Int) { log("android: support check error $error"); release(); done.countDown() }
                    })
                } catch (e: Throwable) { log("android: support check failed: $e"); release(); done.countDown() }
            }
            if (!done.await(10, java.util.concurrent.TimeUnit.SECONDS)) {
                log("android: support check timed out")
                release()
                return false
            }
            return installed
        }

        fun hear(pcm: FloatArray): Heard? {
            val (read, write) = android.os.ParcelFileDescriptor.createPipe()
            val done = java.util.concurrent.CountDownLatch(1)
            var text: String? = null  // both written before done counts down, read after it
            var answeredAt = 0L
            var recognizer: android.speech.SpeechRecognizer? = null
            // Our copy of the read end, closed once the recognizer has its own (it is ready for speech) or the take
            // is over: held open, a recognizer that stopped reading would leave the writes waiting for ever
            val readClosed = java.util.concurrent.atomic.AtomicBoolean(false)
            fun closeRead() { if (readClosed.compareAndSet(false, true)) try { read.close() } catch (_: Exception) {} }
            var began = false; var ended = false; var partials = 0; var loudest = -100f; var options = 0
            var lastPartial = ""
            main.post {
                try {
                    recognizer = android.speech.SpeechRecognizer.createOnDeviceSpeechRecognizer(context).apply {
                        setRecognitionListener(object : android.speech.RecognitionListener {
                            override fun onResults(results: android.os.Bundle?) {
                                answeredAt = System.nanoTime()
                                val all = results?.getStringArrayList(android.speech.SpeechRecognizer.RESULTS_RECOGNITION)
                                options = all?.size ?: 0
                                // This recognizer, fed from a pipe, can end with an empty final and its words only in
                                // the partials: the last partial is then what it heard
                                text = all?.firstOrNull()?.takeIf { it.isNotBlank() } ?: lastPartial
                                done.countDown()
                            }
                            // An error after partials carried words (7, no match) is scored like an empty final: with the
                            // last partial, so failed takes don't depend on which of the two this recognizer ends with
                            override fun onError(error: Int) {
                                log("android: error $error")
                                if (lastPartial.isNotBlank()) { answeredAt = System.nanoTime(); text = lastPartial }
                                done.countDown()
                            }
                            // The request (and the pipe in it) is only sent once the service is bound: by now it has it
                            override fun onReadyForSpeech(params: android.os.Bundle?) { closeRead() }
                            override fun onBeginningOfSpeech() { began = true }
                            override fun onRmsChanged(rmsdB: Float) { loudest = maxOf(loudest, rmsdB) }
                            override fun onBufferReceived(buffer: ByteArray?) {}
                            override fun onEndOfSpeech() { ended = true }
                            override fun onPartialResults(partialResults: android.os.Bundle?) {
                                partials++
                                partialResults?.getStringArrayList(android.speech.SpeechRecognizer.RESULTS_RECOGNITION)
                                    ?.firstOrNull()?.takeIf { it.isNotBlank() }?.let { lastPartial = it }
                            }
                            override fun onEvent(eventType: Int, params: android.os.Bundle?) {}
                        })
                        startListening(Intent(android.speech.RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                            .putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE_MODEL, android.speech.RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                            .putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE, language)
                            .putExtra(android.speech.RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                            .putExtra(android.speech.RecognizerIntent.EXTRA_AUDIO_SOURCE, read)
                            .putExtra(android.speech.RecognizerIntent.EXTRA_AUDIO_SOURCE_CHANNEL_COUNT, 1)
                            .putExtra(android.speech.RecognizerIntent.EXTRA_AUDIO_SOURCE_ENCODING, android.media.AudioFormat.ENCODING_PCM_16BIT)
                            .putExtra(android.speech.RecognizerIntent.EXTRA_AUDIO_SOURCE_SAMPLING_RATE, 16000)
                            .apply { if (hints.isNotEmpty()) putStringArrayListExtra(android.speech.RecognizerIntent.EXTRA_BIASING_STRINGS, ArrayList(hints)) })
                    }
                } catch (e: Throwable) { log("android: couldn't start: $e"); done.countDown() }
            }
            var writeFailed = false
            var due = System.nanoTime()  // a take with no samples: its audio was due at once
            try {
                // Non-blocking writes: a full pipe nobody reads ends the take instead of hanging the bench
                android.system.Os.fcntlInt(write.fileDescriptor, android.system.OsConstants.F_SETFL, android.system.OsConstants.O_NONBLOCK)
                val start = System.nanoTime()
                var at = 0
                while (at < pcm.size && done.count > 0) {
                    val end = minOf(pcm.size, at + CHUNK)
                    due = start + end * NS_PER_SAMPLE  // when the chunk's last sample would have been heard
                    val now = System.nanoTime()
                    if (due > now) Thread.sleep((due - now) / 1_000_000)
                    val bytes = ByteArray((end - at) * 2)
                    for (i in at until end) {
                        val s = (pcm[i] * 32767f).toInt().coerceIn(-32768, 32767)
                        bytes[2 * (i - at)] = s.toByte(); bytes[2 * (i - at) + 1] = (s shr 8).toByte()
                    }
                    var off = 0
                    val stuckAt = System.nanoTime() + STUCK_NS
                    while (off < bytes.size) {
                        val n = try { android.system.Os.write(write.fileDescriptor, bytes, off, bytes.size - off) }
                            catch (e: android.system.ErrnoException) { if (e.errno == android.system.OsConstants.EAGAIN) 0 else throw e }
                        off += n
                        if (n == 0) {
                            if (done.count == 0L) break
                            if (System.nanoTime() > stuckAt) throw java.io.IOException("the recognizer stopped taking the audio")
                            Thread.sleep(10)
                        }
                    }
                    at = end
                }
            } catch (e: Exception) { log("android: feeding failed: $e"); writeFailed = true }
            try { write.close() } catch (_: Exception) {}  // the end of the audio: the session ends
            // Still waited on after a failed write: a recognizer that closes its end once it has enough audio (or at
            // its own silence endpoint) gives its text a moment later. Only no text at all is a failed take.
            val answered = done.await(if (writeFailed) 5L else 30L, java.util.concurrent.TimeUnit.SECONDS)
            main.post { try { recognizer?.destroy() } catch (_: Throwable) {} }
            closeRead()
            // Numbers only, never the words: how the session went when a take comes back empty
            log("android: began=$began ended=$ended partials=$partials loudest_db=$loudest options=$options " +
                "words=${text?.split(Regex("\\s+"))?.count { it.isNotBlank() } ?: -1}")
            if (!answered || text == null) return null
            return Heard(text!!.trim(), maxOf(0L, answeredAt - due) / 1_000_000, 0L)
        }
    }

    private const val CHUNK = 1600  // 0.1 s at 16 kHz
    private const val NS_PER_SAMPLE = 62_500L  // 1 s / 16000
    private const val STUCK_NS = 5_000_000_000L  // a write that can't go on for this long ends the take as failed
    private const val TAIL = 6400  // 0.4 s of silence after the last word, as the laptop's Nemotron gets
    private const val JENNY_TEXT = "Once there was a small robot henchman with one goggle eye, who lived in a laptop by the window."
    private const val JENNY_PH = "wˈʌns ðeəwˌɒz ɐ smˈɔːl ɹˈəʊbɒt hˈɛntʃmən wɪð wˈɒn ɡˈɒɡəl ˈaɪ, hˌuː lˈɪvd ɪn ɐ lˈæptɒp baɪ ðə wˈɪndəʊ."
}
