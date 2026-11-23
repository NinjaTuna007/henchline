package io.github.ninjatuna007.henchline

import android.content.Context
import org.json.JSONObject
import android.os.SystemClock
import java.io.File

/**
 * The phone's own ears on a call (Settings › Voices › Always phone): the call's mic frames heard here with the
 * phone's speech model (PhoneAsr: the laptop's own streaming transducer, as PhoneSpeechBench runs it) and Silero
 * VAD, turns opened and ended by the laptop's call rules (Endpointer), and each turn's words sent to the call server
 * as {"heard"}; the laptop then stops hearing the line.
 *
 * Until take(), frames are only kept (the last RING_MS): what the phone then hears starts at the oldest of them, which
 * it tells the server ({"ear": "phone", "from_ms"}), so the laptop drops only the turns the phone surely has. The
 * server answers with where the laptop's last delivered turn ended ({"ear_cut"}): the frames before it are dropped
 * unheard, so a turn the laptop delivered isn't delivered again. Times here are SystemClock.elapsedRealtime() (the
 * clock "from_ms" and "t" are in); only "said_at" is the wall clock.
 * Words never go to the log here: counts, durations and reasons only. Audio stays in RAM.
 */
class PhoneHearing(
    private val context: Context,
    private val send: (String) -> Unit,  // to the call server
    private val ownText: (Long) -> String,  // talkback playing at that time (elapsedRealtime), or a moment before
    private val userTalks: () -> Unit,  // the user talks over talkback: what is queued to play is dropped
    private val lineDown: () -> Boolean,  // the connection is down now: a turn ending meanwhile is sent late
    private val failed: () -> Unit,  // hearing stopped on an error: the laptop must hear the user again
    private val turns: HeardTurns,  // the call's, kept by CallService: they outlive these ears if they fail
) {
    private val items = Backlog()
    private val lock = Any()  // loading, taking and closing take turns
    @Volatile private var recognizer: com.k2fsa.sherpa.onnx.OnlineRecognizer? = null
    @Volatile private var vad: com.k2fsa.sherpa.onnx.Vad? = null
    @Volatile private var worker: Thread? = null
    @Volatile private var closed = false
    @Volatile var taken = false; private set
    private var hints: List<String> = emptyList()
    private val endpointer = Endpointer(FRAME_MS)

    var buttonOnly: Boolean
        get() = endpointer.buttonOnly
        set(v) { endpointer.buttonOnly = v }

    // The turn being heard (worker thread only, but for `hearing`, read when a dropped line comes back)
    private var stream: com.k2fsa.sherpa.onnx.OnlineStream? = null
    private var turnFrom = 0L  // when its voice began (or the button was pressed)
    private var turnPushed = false
    private var turnUsers = false  // told to be the user's by new words over talkback: never dropped as echo
    private var turnClip = ""  // talkback's words over which the turn began: echo is told from the user by them
    private var suspect = false  // over talkback, not yet told to be the user's: no {"hearing"} sent yet
    private var lookedAt = 0L  // audio ms since the turn opened at which its words were last compared with the clip
    @Volatile private var hearing = false  // {"hearing": true} sent for the turn open now
    private val preroll = ArrayDeque<Pair<Long, FloatArray>>()
    private val window = FloatArray(VAD_WINDOW)
    private var windowAt = 0
    private var voice = false
    private var lastAt = 0L
    @Volatile private var pressAt = 0L  // a press made before hearing began: acted on at its frame
    // The server's cut ({"ear_cut"}): frames before it were the laptop's, delivered. Waited for before hearing begins
    @Volatile private var cut: Long? = null
    private val cutTold = java.util.concurrent.CountDownLatch(1)

    /** The recognizer and VAD loaded (a few seconds, about 1.2 GB): false if they won't. On a thread of its own. */
    fun load(): Boolean {
        val dir = PhoneAsr.dir(context)
        val began = System.currentTimeMillis()
        var asr: com.k2fsa.sherpa.onnx.OnlineRecognizer? = null
        try {
            hints = PhoneAsr.hints(context)
            val vocab = File(dir, "bpe.vocab")
            val hintFile = if (hints.isNotEmpty() && vocab.isFile) File(context.cacheDir, "phone-ears-hints.txt")
                .apply { writeText(hints.joinToString("\n", postfix = "\n")) } else null
            // As the bench measured it on calls (PhoneSpeechBench), and as the laptop decodes
            val config = com.k2fsa.sherpa.onnx.OnlineRecognizerConfig(
                featConfig = com.k2fsa.sherpa.onnx.FeatureConfig(sampleRate = RATE, featureDim = 80, dither = 0f),
                modelConfig = com.k2fsa.sherpa.onnx.OnlineModelConfig(
                    transducer = com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig(
                        File(dir, "encoder.int8.onnx").path, File(dir, "decoder.int8.onnx").path, File(dir, "joiner.int8.onnx").path),
                    tokens = File(dir, "tokens.txt").path, numThreads = THREADS, provider = "cpu",
                    modelingUnit = if (hintFile != null) "bpe" else "", bpeVocab = if (hintFile != null) vocab.path else ""),
                enableEndpoint = false,  // turns are ended here, by the laptop's rules
                decodingMethod = "modified_beam_search", maxActivePaths = 4,
                hotwordsFile = hintFile?.path ?: "", hotwordsScore = HINT_SCORE)
            val made = com.k2fsa.sherpa.onnx.OnlineRecognizer(null, config)
            asr = made
            val silero = com.k2fsa.sherpa.onnx.Vad(null, com.k2fsa.sherpa.onnx.VadModelConfig(
                sileroVadModelConfig = com.k2fsa.sherpa.onnx.SileroVadModelConfig(
                    model = File(dir, "silero_vad.onnx").path, threshold = VAD_THRESHOLD, windowSize = VAD_WINDOW),
                sampleRate = RATE, numThreads = 1, provider = "cpu"))
            synchronized(lock) {
                if (closed) { made.release(); silero.release(); asr = null; return false }
                recognizer = made; vad = silero
            }
            log("loaded in ${System.currentTimeMillis() - began} ms, ${hints.size} hint words")
            return true
        } catch (e: Throwable) {  // a phone short of memory: the laptop goes on listening
            log("wouldn't load: ${e.javaClass.simpleName}")
            try { asr?.release() } catch (_: Throwable) {}  // the recognizer made before the VAD failed: not kept
            return false
        }
    }

    val loaded get() = recognizer != null

    /** A frame of the call's mic (16-bit, 16 kHz), from the mic's thread; `atMs`: when it began (elapsedRealtime). */
    fun hear(buffer: ByteArray, size: Int, atMs: Long) {
        if (closed) return
        val pcm = ShortArray(size / 2) { i -> ((buffer[2 * i].toInt() and 0xff) or (buffer[2 * i + 1].toInt() shl 8)).toShort() }
        // Only the last RING_MS until it hears; after, only a phone far behind loses its oldest (never a press)
        val gone = items.frame(pcm, atMs, if (taken) MOST_BEHIND_MS else RING_MS)
        if (taken) dropped += gone
    }

    @Volatile private var dropped = 0

    /** The talk button pressed (true) or let go, from any thread. */
    fun press(on: Boolean) { if (taken && !closed) items.press(on) }

    /**
     * Start hearing the user: from the oldest frame kept (what the laptop may not have delivered yet). Returns that
     * time (elapsedRealtime), for the server. `pressedAt`: when the talk button held now was pressed, else null:
     * the laptop's held turn from then on is dropped, so the phone's starts there too (or at its oldest frame).
     * Only once loaded.
     */
    fun take(pressedAt: Long?): Long = synchronized(lock) {
        check(recognizer != null && !closed)
        val from = items.oldest() ?: SystemClock.elapsedRealtime()
        taken = true
        if (pressedAt != null) pressAt = maxOf(pressedAt, 1L)
        worker = Thread({ run() }, "henchline-ears").apply { start() }
        log("hearing the user, from ${SystemClock.elapsedRealtime() - from} ms back")
        from
    }

    /** The server's {"ear_cut"}: where the laptop's last delivered turn ended (this clock), or null: none. Only the
     *  first counts (it answers each {"ear"}, also on reconnecting). */
    fun cutAt(ms: Long?) {
        if (cutTold.count == 0L) return
        cut = ms
        cutTold.countDown()
    }

    /** A dropped line is back, and the turns not answered yet were sent again (CallService): the turn being heard
     *  now is said again, after them, so their answers don't end its hold on talkback. */
    fun resendHearing() {
        if (hearing) send(JSONObject().put("hearing", true).toString())
    }

    /** The call ended: hearing stops, the models go (on the worker as it ends, else here). */
    fun close() {
        val running = synchronized(lock) {
            closed = true
            worker
        }
        if (running != null) { running.interrupt(); return }
        synchronized(lock) { release() }
    }

    private fun release() {
        try { stream?.release() } catch (_: Throwable) {}
        stream = null
        try { recognizer?.release() } catch (_: Throwable) {}
        try { vad?.release() } catch (_: Throwable) {}
        recognizer = null; vad = null
    }

    private fun run() {
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO)
        try {
            // What the laptop already delivered isn't heard again: wait for the server's cut (an answer away)
            val told = try { cutTold.await(CUT_WAIT_MS, java.util.concurrent.TimeUnit.MILLISECONDS) } catch (_: InterruptedException) { false }
            cut?.let { at ->
                items.dropBefore(at).let { if (it > 0) log("skipped ${it * FRAME_MS} ms the laptop delivered") }
                endpointer.arm()  // what follows the laptop's last turn is never one of its own
            }
            if (!told) log("no cut from the server in time: hearing from the oldest audio kept")
            while (!closed) {
                val item = try { items.poll(200) } catch (_: InterruptedException) { null } ?: continue
                when {
                    item.press == true -> opened(endpointer.press(lastAt))
                    item.press == false -> { pressAt = 0L; closedBy(endpointer.release()) }
                    item.pcm != null -> frame(item.pcm, item.atMs)
                }
                if (dropped > 0) { log("fell behind: dropped ${dropped} frames"); dropped = 0 }
            }
        } catch (e: Throwable) {  // never let it take the call down; nobody would hear the user: the laptop does again
            log("stopped: ${e.javaClass.simpleName}")
            if (!closed) try { failed() } catch (_: Throwable) {}
        } finally {
            synchronized(lock) { release() }
        }
    }

    private fun frame(pcm: ShortArray, atMs: Long) {
        lastAt = atMs
        if (pressAt in 1..atMs + FRAME_MS) { pressAt = 0L; opened(endpointer.press(atMs)) }
        val samples = FloatArray(pcm.size) { pcm[it] / 32768f }
        val silero = vad ?: return
        for (s in samples) {  // Silero looks at 32 ms windows: a frame's verdict is the latest window's
            window[windowAt++] = s
            if (windowAt == VAD_WINDOW) {
                voice = silero.compute(window.copyOf()) > VAD_THRESHOLD
                windowAt = 0
            }
        }
        preroll.addLast(atMs to samples)
        while (preroll.size > PREROLL_FRAMES) preroll.removeFirst()
        stream?.let { s -> s.acceptWaveform(samples, RATE); decode(s) }
        val step = endpointer.frame(atMs, level(samples), voice) { soFar() }
        when (step) {
            is Endpointer.Step.Open -> opened(step)
            is Endpointer.Step.Close -> closedBy(step)
            else -> if (suspect && stream != null) lookAgain(atMs)
        }
    }

    private fun decode(s: com.k2fsa.sherpa.onnx.OnlineStream) {
        val asr = recognizer ?: return
        while (asr.isReady(s)) asr.decode(s)
    }

    private fun soFar(): String = stream?.let { s -> recognizer?.getResult(s)?.text } ?: ""

    private fun opened(step: Endpointer.Step) {
        if (step !is Endpointer.Step.Open) return
        if (step.already) {  // the button pressed during a turn: it is the user's now, and held
            turnPushed = true
            confirm("the button")
            return
        }
        val asr = recognizer ?: return
        val s = asr.createStream("")
        for ((at, samples) in preroll) if (at >= step.fromMs) s.acceptWaveform(samples, RATE)
        decode(s)
        stream = s
        turnFrom = step.voiceMs
        turnPushed = step.pushed
        turnUsers = false
        lookedAt = 0L
        turnClip = ownText(step.voiceMs)
        suspect = false
        if (step.pushed || turnClip.isEmpty()) confirm(null) else suspect = true
    }

    /** The turn is the user's: talkback over it stops, and the server holds talkback for it. */
    private fun confirm(why: String?) {
        val over = turnClip.isNotEmpty() || ownText(lastAt).isNotEmpty()
        if (over) userTalks()
        suspect = false
        if (!hearing) {
            hearing = true
            send(JSONObject().put("hearing", true).toString())
        }
        if (over && why != null) log("talking over talkback ($why)")
    }

    /** A voice over talkback, every LOOK_MS: the clip's own words coming back are echo; new words are the user. */
    private fun lookAgain(atMs: Long) {
        val since = atMs - endpointer.openedAt
        if (since - lookedAt < LOOK_MS) return
        lookedAt = since
        val clip = listOf(turnClip, ownText(atMs)).filter { it.isNotEmpty() }.distinct().joinToString(" ")
        val words = soFar()
        when {
            OwnWords.keys(words).size >= OwnWords.MIN -> if (!OwnWords.ours(words, clip)) { turnUsers = true; confirm("new words") }
            since >= UNSURE_MS -> confirm("a voice too long to be a scrap")
        }
    }

    private fun closedBy(step: Endpointer.Step) {
        if (step !is Endpointer.Step.Close) return
        val s = stream ?: return
        stream = null
        val asr = recognizer
        val text = try {
            if (asr == null) "" else {
                s.acceptWaveform(FloatArray(TAIL), RATE)  // as the laptop's turns get: the last word comes through
                s.inputFinished()
                decode(s)
                TurnRules.tidy(asr.getResult(s).text.trim(), hints)
            }
        } finally { s.release() }
        val ms = (lastAt - turnFrom).coerceAtLeast(0)
        // As the laptop: only a turn that began over talkback can be its echo, never one held with the button, nor
        // one its new words already showed to be the user's
        val clip = if (turnClip.isEmpty() || turnPushed || turnUsers) ""
            else listOf(turnClip, ownText(lastAt)).filter { it.isNotEmpty() }.distinct().joinToString(" ")
        val words = OwnWords.keys(text).size
        when {
            text.isEmpty() || (clip.isNotEmpty() && OwnWords.echoTurn(text, clip, ms)) -> {
                log("turn of ${ms} ms, ended by ${step.end.why}: ${if (text.isEmpty()) "no words" else "our own talkback coming back"}")
                if (hearing) send(JSONObject().put("hearing", false).toString())
            }
            else -> {
                if (suspect) confirm(null)  // new words at the very end of a short turn over talkback
                val late = lineDown()
                send(turns.add(text, turnPushed, wallClock(turnFrom), late))
                log("heard a turn: $words words, ${ms} ms, ended by ${step.end.why}${if (turnPushed) ", held the talk button" else ""}" +
                    if (late) ", the line down" else "")
            }
        }
        hearing = false
        suspect = false
        turnPushed = false
    }

    /** This clock's time on the wall clock (epoch ms), for "said_at". */
    private fun wallClock(at: Long): Long = System.currentTimeMillis() - (SystemClock.elapsedRealtime() - at)

    private fun level(samples: FloatArray): Float {
        var sum = 0.0
        for (s in samples) sum += s * s
        return (20 * kotlin.math.log10(kotlin.math.sqrt(sum / maxOf(1, samples.size)) + 1e-6)).toFloat()
    }

    private fun log(line: String) { try { android.util.Log.i("Henchline", "phone ears: $line") } catch (_: RuntimeException) {} }

    companion object {
        const val RATE = 16000
        const val FRAME_MS = 20
        const val RING_MS = 10_000L  // audio kept before the phone hears: its takeover starts from the oldest
        const val MOST_BEHIND_MS = 30_000L  // hearing this far behind, the oldest frames go
        const val THREADS = 2
        const val HINT_SCORE = 2.0f
        const val VAD_WINDOW = 512  // Silero's 32 ms at 16 kHz
        const val VAD_THRESHOLD = 0.5f
        const val TAIL = 6400  // 0.4 s of silence after the last word, as the laptop's speech model gets
        const val LOOK_MS = 600L  // voice over talkback before its words are compared with the clip's
        const val CUT_WAIT_MS = 3_000L  // for the server's cut, before hearing anyway (a duplicate beats words lost)
        const val UNSURE_MS = 2_000L  // a voice this long over talkback with too few words to tell: the user's
        val PREROLL_FRAMES = (Endpointer.PREROLL_MS / FRAME_MS + Endpointer.OPEN_MS / FRAME_MS + 2).toInt()
    }
}
