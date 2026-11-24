package io.github.ninjatuna007.henchline

import android.content.Context
import org.json.JSONObject

/**
 * Who hears the user on a call: the laptop (as always), or the phone itself with its own speech model (PhoneAsr)
 * when the line is bad. The user's setting, from Settings › Voices:
 *   AUTOMATIC  the laptop listens; the phone takes over while the line is bad and hands back when it recovers
 *   PHONE      the phone listens from the start of every call (needs the model)
 *   LAPTOP     the laptop only, as before
 * Stored here. Always phone is acted on by CallService with PhoneHearing; the rules a turn is heard by (pure, so they
 * are unit-tested against the laptop's own: tests/fixtures/phone-ears.json) are below.
 */
object PhoneEar {
    enum class Mode(val key: String, val label: String) {
        AUTOMATIC("automatic", "Automatic"),
        PHONE("phone", "Always phone"),
        LAPTOP("laptop", "Always computer");

        companion object {
            /** A stored value back to its mode; anything unknown is the default. */
            fun of(key: String?): Mode = entries.firstOrNull { it.key == key } ?: AUTOMATIC
        }
    }

    private const val PREF = "phone_ears"

    fun mode(context: Context): Mode = Mode.of(prefs(context).getString(PREF, null))
    fun setMode(context: Context, mode: Mode) = prefs(context).edit().putString(PREF, mode.key).apply()

    /** The mode a call works with: Always phone without the model is Automatic until it's downloaded. */
    fun effective(chosen: Mode, ready: Boolean): Mode = if (chosen == Mode.PHONE && !ready) Mode.AUTOMATIC else chosen

    /** The line under the setting in the Voices sheet. */
    fun line(chosen: Mode, ready: Boolean): String = when (effective(chosen, ready)) {
        Mode.AUTOMATIC -> if (ready) "The computer listens; when the line is bad, this phone does until it recovers."
            else "The computer listens. Download the speech model so this phone can listen when the line is bad."
        Mode.PHONE -> "This phone hears you on every call, and sends the computer your words."
        Mode.LAPTOP -> "Only the computer listens, as before: a bad line can cut your words."
    }

    /** Who is listening on the call, for the call bar and the chat's header: "Phone listening" while the phone
     *  hears the user itself, else the computer by its own name. */
    fun listener(phoneEars: Boolean, computer: String): String =
        if (phoneEars) "Phone listening" else "${HostPush.nameOr(computer).replaceFirstChar { it.uppercaseChar() }} listening"

    /** The talk button's colour (MainActivity.showMode's mode) on a call. The computer's hands-free mode says it while
     *  the computer hears the call; while the phone hears it, the computer's listener is idle ("waiting", grey), so
     *  the phone's own state is shown instead: red while it hears a turn (or the button is held), green while the
     *  line is open, grey only when the open line is off (only the held button gets in, as on the computer). */
    fun talkMode(computerMode: String, phoneEars: Boolean, hearing: Boolean, buttonOnly: Boolean): String = when {
        !phoneEars -> computerMode
        hearing -> "recording"
        buttonOnly -> "waiting"
        else -> "listening"
    }

    private fun prefs(context: Context) = context.getSharedPreferences("call", Context.MODE_PRIVATE)
}

/**
 * Our own words coming back: what the phone hears over talkback, told from the user by the laptop's rules
 * (handsfree/core/talkover.py word_keys / own_share / our_own_words, turn.py echo_turn).
 */
object OwnWords {
    const val MIN = 2  // words heard before telling our echo from the user
    const val OWN = 0.6  // share of what was heard that is the clip's own words, in order: our echo
    const val RUN = 3  // or at least this many of the clip's words, in order,
    const val SHARE = 0.3  // making up this share
    const val ECHO_TURN_MS = 3_000L  // a turn over a clip shorter than this, of the clip's own words, is our echo
    const val MOSTLY_OURS = 0.6  // or, longer, when at least this share of its words is the clip's
    private val NUMBERS = "zero one two three four five six seven eight nine ten eleven twelve".split(" ")
    private val WORD = Regex("[a-z0-9']+")

    /** Words as the speech model and talkback may spell them differently: "5" and "five", "recognize" and
     *  "recognised" match on their number word and first four letters. */
    fun keys(text: String): List<String> = WORD.findAll(text.lowercase()).map { m ->
        val w = m.value
        (if (w.all { it in '0'..'9' }) w.toBigInteger().let { n -> if (n < NUMBERS.size.toBigInteger()) NUMBERS[n.toInt()] else w } else w).take(4)
    }.toList()

    /** How many of the heard words are the said ones, in order (longest common subsequence). */
    fun shared(heard: List<String>, said: List<String>): Int {
        val row = IntArray(said.size + 1)
        for (word in heard) {
            var prev = 0
            for (j in 1..said.size) {
                val was = row[j]
                row[j] = if (word == said[j - 1]) prev + 1 else maxOf(row[j], row[j - 1])
                prev = was
            }
        }
        return row[said.size]
    }

    /** The share of the heard words that are the clip's own, in order (0 with too few to tell). */
    fun share(heard: String, said: String): Double {
        val h = keys(heard); val s = keys(said)
        if (h.size < MIN || s.isEmpty()) return 0.0
        return shared(h, s).toDouble() / h.size
    }

    /** What was heard over a clip is mostly that clip's own words, in order: our echo. One word is too little. */
    fun ours(heard: String, said: String): Boolean {
        val h = keys(heard); val s = keys(said)
        if (h.size < MIN || s.isEmpty()) return false
        val n = shared(h, s)
        return n.toDouble() / h.size >= OWN || (n >= RUN && n.toDouble() / h.size >= SHARE)
    }

    /** A turn over a clip that was only our echo: the clip's own words, and short or mostly them. */
    fun echoTurn(text: String, clip: String, ms: Long): Boolean =
        ours(text, clip) && (ms < ECHO_TURN_MS || share(text, clip) >= MOSTLY_OURS)
}

/** When a call turn ends, by the laptop's call rules (handsfree/core/turn.py), and its words tidied as the laptop's
 *  speech model's are (voice/speech/nemotron.py tidy). */
object TurnRules {
    const val END_MS = 900  // CALL_END_FRAMES: quiet that ends a turn on a call
    const val EARLY_END_MS = 600  // CALL_EARLY_END_FRAMES: sooner, when the words so far end a sentence
    const val NO_STOP_MS = 300  // NO_STOP_FRAMES: its last full stop often doesn't come: then this much more
    const val OVER_MS = 600  // OVER_FRAMES: after this much quiet, a last word "over" ends it
    const val VOICE_GONE_MS = 1_000L  // VOICE_GONE: a second without speech ends it (steady hiss on the line)
    const val MAX_MS = 120_000L  // MAX_SECONDS
    const val END_DB = 6f  // at most this over the floor is quiet
    const val BELOW_PEAK_DB = 25f  // or this far under their loudest, for a noisy room
    val TRAILING = setOf("uh", "um", "er", "erm", "hmm", "and", "but", "or", "so", "because", "the", "a", "an", "to",
        "of", "with", "like", "if", "then", "that", "which", "is", "are", "my", "your", "for", "in", "on")
    val OVER = Regex("[\\s,.!?]*\\bover\\b[\\s.!?]*$", RegexOption.IGNORE_CASE)
    private val LETTERS = Regex("[a-z']+")

    /** The words so far end a sentence, not on a filler or a word that leads on; without a full stop, once the
     *  quiet has gone NO_STOP_MS past the shorter pause. */
    fun sentenceDone(soFar: String, longerMs: Int): Boolean {
        val said = soFar.trim()
        val words = LETTERS.findAll(said.lowercase()).map { it.value }.toList()
        if (words.isEmpty() || words.last() in TRAILING || OVER.containsMatchIn(said)) return false
        return said.last() in ".?!" || longerMs >= NO_STOP_MS
    }

    fun endsWithOver(soFar: String): Boolean = OVER.containsMatchIn(soFar.trim())

    /** A hint word said once, not repeated: boosted, it can be written again in the silence around the words. */
    fun tidy(text: String, hints: List<String>): String {
        var out = text
        for (word in hints) {
            if (word.isEmpty()) continue
            out = Regex("\\b(${Regex.escape(word)})(?:[\\s,.]+\\1\\b)+", RegexOption.IGNORE_CASE).replace(out) { it.groupValues[1] }
        }
        return out.trim()
    }
}

/**
 * When the phone's turns open and end, frame by frame (20 ms of the call's mic), on audio time: a turn opens on
 * voice (Silero) with a little lead-in, and ends by the laptop's call rules (TurnRules). The talk button holds a turn
 * open until it is let go; "open line off" (button only) lets nothing else in. Pure: driven by the caller's frames,
 * its words so far asked for only when a rule needs them.
 */
class Endpointer(private val frameMs: Int = 20) {
    enum class End(val why: String) {
        PAUSE("pause"), FINISHED("a finished sentence"), OVER("over"), VOICELESS("no voice for a second"),
        LIMIT("limit"), BUTTON("the button")
    }

    sealed interface Step {
        object None : Step
        /** A turn opened: its audio is from `fromMs` (the lead-in before the voice), its voice from `voiceMs`.
         *  `already`: the talk button pressed during a turn already open, which it now holds. */
        data class Open(val fromMs: Long, val voiceMs: Long, val pushed: Boolean, val already: Boolean = false) : Step
        data class Close(val end: End) : Step
    }

    var open = false; private set
    var pushed = false; private set
    var openedAt = 0L; private set  // audio time the turn opened at (its voice)
    @Volatile var buttonOnly = false
    private var floor = Float.NaN  // the room's level between turns
    private var armed = false  // quiet seen since hearing began: a turn already under way then is not taken up midway
    private var quietSince = -1L  // armed after QUIET_TO_ARM of no voice
    private var voicedMs = 0
    private var voiceStart = 0L
    private var quietMs = 0
    private var peak = -120f
    private var voiceAt = 0L

    fun frame(atMs: Long, db: Float, voice: Boolean, words: () -> String): Step {
        if (!open) {
            floor = when {
                floor.isNaN() || db < floor -> db
                else -> floor + (db - floor) * FLOOR_RISE
            }
            if (!voice) {
                voicedMs = 0
                if (quietSince < 0) quietSince = atMs
                if (atMs + frameMs - quietSince >= QUIET_TO_ARM) armed = true
                return Step.None
            }
            quietSince = -1
            if (voicedMs == 0) voiceStart = atMs
            voicedMs += frameMs
            if (!armed || buttonOnly || voicedMs < OPEN_MS) return Step.None
            begin(voiceStart, pushed = false)
            return Step.Open(voiceStart - PREROLL_MS, voiceStart, pushed = false)
        }
        if (atMs + frameMs - openedAt >= TurnRules.MAX_MS) return close(End.LIMIT)
        if (pushed) return Step.None  // the button decides when it ends, not a pause
        peak = maxOf(peak, db)
        val wasQuiet = quietMs
        quietMs = if (db < maxOf(floor + TurnRules.END_DB, peak - TurnRules.BELOW_PEAK_DB)) quietMs + frameMs else 0
        if (wasQuiet < TurnRules.OVER_MS && quietMs >= TurnRules.OVER_MS && TurnRules.endsWithOver(words())) return close(End.OVER)
        // From the shorter pause on, looked at again now and then: the full stop can come a moment late
        val past = quietMs - TurnRules.EARLY_END_MS
        if (past >= 0 && past % RECHECK_MS < frameMs && TurnRules.sentenceDone(words(), past)) return close(End.FINISHED)
        if (quietMs >= TurnRules.END_MS) return close(End.PAUSE)
        if (voice) voiceAt = atMs
        else if (atMs + frameMs - voiceAt > TurnRules.VOICE_GONE_MS) return close(End.VOICELESS)
        return Step.None
    }

    /** The talk button pressed at `atMs`: a turn held open until it is let go (the one open now, if any). */
    fun press(atMs: Long): Step {
        if (open) {
            if (pushed) return Step.None
            pushed = true
            return Step.Open(openedAt - PREROLL_MS, openedAt, pushed = true, already = true)
        }
        begin(atMs, pushed = true)
        return Step.Open(atMs - PREROLL_MS, atMs, pushed = true)
    }

    /** The talk button let go: its turn ends. */
    fun release(): Step = if (open && pushed) close(End.BUTTON) else Step.None

    /** Hearing starts where the laptop's own turns ended (the server's cut): a voice there is a new turn, not one
     *  the laptop has. */
    fun arm() { armed = true }

    /** The open turn dropped without a step (our own echo): the next voice opens another. */
    fun drop() { open = false; pushed = false; voicedMs = 0 }

    private fun begin(atMs: Long, pushed: Boolean) {
        open = true; this.pushed = pushed; openedAt = atMs
        quietMs = 0; peak = -120f; voiceAt = atMs; voicedMs = 0
    }

    private fun close(end: End): Step {
        open = false; pushed = false; voicedMs = 0; quietSince = -1
        return Step.Close(end)
    }

    companion object {
        const val OPEN_MS = 200  // voice in a row that opens a turn
        const val PREROLL_MS = 300L  // audio kept before the voice began, as the laptop's lead-in
        // ms without voice, once hearing begins, before a turn may open: one under way already then is the laptop's
        // (it began before the phone's words did). Kept under the server's EAR_FROM_SLACK (250 ms), so a voice
        // starting late enough for the laptop to drop its turn is always one the phone opens
        const val QUIET_TO_ARM = 100
        const val RECHECK_MS = 100  // how often the words so far are looked at once the shorter pause has passed
        const val FLOOR_RISE = 0.002f  // per frame between turns: the floor follows a louder room in ten seconds or so
    }
}

/** The turns the phone heard, each kept until the call server answers it ({"delivered"}, {"taken"} or
 *  {"note", "unsent"}): sent again, oldest first, when a dropped line comes back (the server acts on an id once). */
class HeardTurns(private val prefix: String) {
    private val waiting = LinkedHashMap<String, String>()
    private var next = 0

    /** The {"heard": …} message for a turn, kept until answered. said_at: epoch ms its voice began. late: said while
     *  the line was down (words only, never acted on as a command). */
    @Synchronized fun add(text: String, pushed: Boolean, saidAt: Long, late: Boolean): String {
        val id = "$prefix-${next++}"
        val message = JSONObject().put("heard", JSONObject().put("id", id).put("text", text).put("pushed", pushed)
            .put("said_at", saidAt).put("late", late)).toString()
        waiting[id] = message
        return message
    }

    /** One of ours answered: its id (forgotten now), else null. */
    @Synchronized fun answered(message: JSONObject): String? {
        for (key in ANSWERS) {
            val id = message.optString(key).takeIf { message.has(key) && it.isNotEmpty() } ?: continue
            if (waiting.remove(id) != null) return id
        }
        return null
    }

    /** Still waiting for an answer, oldest first. */
    @Synchronized fun unanswered(): List<String> = waiting.values.toList()

    @Synchronized fun size(): Int = waiting.size

    companion object { val ANSWERS = listOf("delivered", "taken", "unsent") }
}

/** What talkback says now, or said a moment ago, by source (the laptop's clip, the phone's own voice): its words may
 *  still come back through the mic for TAIL_MS after it stops. */
class OwnClips(private val tailMs: Long = TAIL_MS) {
    private class Clip(var text: String, var stoppedAt: Long)  // stoppedAt -1: playing now
    private val clips = HashMap<String, Clip>()

    /** `key` plays `text` now, or (null) nothing from that source since `atMs`. */
    @Synchronized fun set(key: String, text: String?, atMs: Long) {
        val clip = clips[key]
        when {
            text != null && text.isNotBlank() -> if (clip == null) clips[key] = Clip(text, -1) else { clip.text = text; clip.stoppedAt = -1 }
            clip != null && clip.stoppedAt < 0 -> clip.stoppedAt = atMs
        }
    }

    /** Every clip playing at `atMs` or stopped within the tail before it, joined; "" for none. */
    @Synchronized fun text(atMs: Long): String =
        clips.values.filter { it.stoppedAt < 0 || atMs - it.stoppedAt <= tailMs }.joinToString(" ") { it.text }

    companion object { const val TAIL_MS = 2_000L }
}

/**
 * What the phone's ears have yet to hear: the call's mic frames (stamped with SystemClock.elapsedRealtime() at
 * their start) and the talk button's presses and releases, in the order they came. Only frames are ever trimmed: a
 * press or a release is never lost to a backlog, however far behind. Thread-safe: the mic adds, the ears take.
 */
class Backlog {
    class Item(val pcm: ShortArray?, val atMs: Long, val press: Boolean? = null)

    private val items = java.util.concurrent.LinkedBlockingDeque<Item>()

    /** A frame, keeping only frames within `keepMs` of it; the number of frames dropped to keep to that. */
    fun frame(pcm: ShortArray, atMs: Long, keepMs: Long): Int {
        items.offer(Item(pcm, atMs))
        var dropped = 0
        val each = items.iterator()
        while (each.hasNext()) {
            val item = each.next()
            if (item.pcm == null) continue  // the button's: always kept
            if (atMs - item.atMs <= keepMs) break
            each.remove()
            dropped++
        }
        return dropped
    }

    fun press(on: Boolean) { items.offer(Item(null, 0L, press = on)) }

    /** The oldest frame's time; null for none. */
    fun oldest(): Long? = items.firstOrNull { it.pcm != null }?.atMs

    /** Frames from before `cutMs` dropped (the laptop delivered what was said then); how many. */
    fun dropBefore(cutMs: Long): Int {
        var dropped = 0
        val each = items.iterator()
        while (each.hasNext()) {
            val item = each.next()
            if (item.pcm == null) continue
            if (item.atMs + PhoneHearing.FRAME_MS > cutMs) break
            each.remove()
            dropped++
        }
        return dropped
    }

    fun poll(ms: Long): Item? = items.poll(ms, java.util.concurrent.TimeUnit.MILLISECONDS)
    fun size() = items.size
}

/**
 * The talk button held for the phone's own ears, from two places that must not undo each other: the earbuds' or the
 * notification's toggle (CallService.talking, which the notification's Talk/Send follows) and the call screen's
 * hold. The ears get one press when either starts and one release when both have ended; a turn is held from the
 * earlier of the two. Times are elapsedRealtime.
 */
class TalkHold {
    private var toggleSince: Long? = null
    private var screenSince: Long? = null

    val held: Boolean @Synchronized get() = toggleSince != null || screenSince != null

    /** When the hold now began (the earlier source), or null when nothing holds it. */
    val since: Long? @Synchronized get() = listOfNotNull(toggleSince, screenSince).minOrNull()

    /** The toggle on or off: the ears' new press (true/false) when the combined hold changed, else null. */
    @Synchronized fun toggle(on: Boolean, at: Long): Boolean? = change { toggleSince = if (on) toggleSince ?: at else null }

    /** The screen's hold on or off: as toggle. */
    @Synchronized fun screen(on: Boolean, at: Long): Boolean? = change { screenSince = if (on) screenSince ?: at else null }

    private inline fun change(set: () -> Unit): Boolean? {
        val before = held
        set()
        return held.takeIf { it != before }
    }
}
