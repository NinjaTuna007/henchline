package io.github.ninjatuna007.henchline

/**
 * How a speech-to-text candidate scores on the user's own recordings (PhoneSpeechBench), and whether it meets the bar
 * set by today's laptop call path: Nemotron with hint words on the
 * laptop, 11.9 % word errors, 13 of 20 names, 0.15 s from the end of speech to text. Pure, for the JVM tests.
 */
object SpeechScore {
    /** The bar: names at least 13 of 20, word errors at most 12 %, end of speech to text at most 0.25 s (median),
     *  decoding faster than live audio, and every take giving text. */
    const val MIN_NAMES = 13.0 / 20.0
    const val MAX_WER = 0.12
    const val MAX_WAIT_MS = 250L
    /** Compute over audio at most 0.8, so decoding keeps up with live audio with room to spare. */
    const val MAX_RTF = 0.8

    /** A recording set's references: one "file<TAB>what was said" per line; blank lines and # comments skipped. */
    fun refs(text: String): List<Pair<String, String>> = text.lineSequence()
        .map { it.trimEnd('\r') }
        .filter { it.isNotBlank() && !it.trimStart().startsWith("#") && '\t' in it }
        .map { it.substringBefore('\t').trim() to it.substringAfter('\t').trim() }
        .filter { it.first.isNotEmpty() }
        .toList()

    /** Words as compared: lower case, letters, digits and inner apostrophes only. */
    fun words(text: String): List<String> = text.lowercase()
        .replace(Regex("[^\\p{L}\\p{Nd}' ]+"), " ")
        .split(' ').map { it.trim('\'') }.filter { it.isNotEmpty() }

    /** Word edits (substitutions, insertions, deletions) turning ref into hyp. */
    fun edits(ref: List<String>, hyp: List<String>): Int {
        var row = IntArray(hyp.size + 1) { it }
        for (i in 1..ref.size) {
            val next = IntArray(hyp.size + 1)
            next[0] = i
            for (j in 1..hyp.size) {
                next[j] = minOf(row[j] + 1, next[j - 1] + 1, row[j - 1] + if (ref[i - 1] == hyp[j - 1]) 0 else 1)
            }
            row = next
        }
        return row[hyp.size]
    }

    /** Word error rate over a whole set: all edits over all reference words. */
    fun wer(pairs: List<Pair<String, String>>): Double {
        val total = pairs.sumOf { words(it.first).size }
        return if (total == 0) 0.0 else pairs.sumOf { edits(words(it.first), words(it.second)) }.toDouble() / total
    }

    /** The names (hint words) said in ref, and how many of those hyp has: as whole words, case aside. Where names
     *  overlap ("Claude" inside "Claude Code"), only the longest one counts there. */
    fun names(ref: String, hyp: String, names: List<String>): Pair<Int, Int> {
        val list = names.map { words(it).joinToString(" ") }.filter { it.isNotEmpty() }.distinct()
            .sortedByDescending { it.split(' ').size }
        val said = found(ref, list)
        val heard = found(hyp, list)
        return said.values.sum() to said.entries.sumOf { (name, n) -> minOf(n, heard[name] ?: 0) }
    }

    /** How many times each name is in the text as whole words, longest names first, each match taken out of the text
     *  so a shorter name inside it isn't counted again. A name said twice in a row counts twice. */
    private fun found(text: String, longestFirst: List<String>): Map<String, Int> {
        var t = " " + words(text).joinToString(" ") + " "
        val counts = LinkedHashMap<String, Int>()
        for (name in longestFirst) {
            val whole = Regex("(?<= )" + Regex.escape(name) + "(?= )")
            val n = whole.findAll(t).count()
            if (n == 0) continue
            counts[name] = n
            t = whole.replace(t, "\u0001")
        }
        return counts
    }

    /** The middle value; for an even count, the mean of the two middle ones (rounded down). */
    fun median(values: List<Long>): Long = values.sorted().let {
        when {
            it.isEmpty() -> 0L
            it.size % 2 == 1 -> it[it.size / 2]
            else -> (it[it.size / 2 - 1] + it[it.size / 2]) / 2
        }
    }

    /** The 90th percentile, nearest rank: the ceil(0.9 n)-th smallest (the 9th of 10, the max only below 10 values). */
    fun p90(values: List<Long>): Long = values.sorted().let { if (it.isEmpty()) 0L else it[(it.size * 9 + 9) / 10 - 1] }

    /** Why a candidate doesn't meet the bar; empty when it does. rtf is compute over audio (null: not measured, as for
     *  the phone's own recognizer); failed is the takes that gave no text at all (scored as all words missed). Jenny's
     *  speed alongside and the battery are read from the bench's own lines, against today's call, by whoever runs it. */
    fun shortfalls(wer: Double, namesSaid: Int, namesHeard: Int, waits: List<Long>, rtf: Double? = null, failed: Int = 0): List<String> = buildList {
        if (wer > MAX_WER) add("word errors ${"%.1f".format(wer * 100)} % > ${"%.0f".format(MAX_WER * 100)} %")
        if (namesSaid > 0 && namesHeard.toDouble() / namesSaid < MIN_NAMES) add("names $namesHeard/$namesSaid < 13/20")
        val wait = median(waits)
        if (wait > MAX_WAIT_MS) add("median wait $wait ms > $MAX_WAIT_MS ms")
        if (rtf != null && rtf > MAX_RTF) add("real-time factor ${"%.2f".format(rtf)} > $MAX_RTF: can't keep up with live audio")
        if (failed > 0) add("$failed take${if (failed == 1) "" else "s"} gave no text")
    }
}
