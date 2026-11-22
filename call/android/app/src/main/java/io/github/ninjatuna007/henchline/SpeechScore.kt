package io.github.ninjatuna007.henchline

/**
 * How a speech-to-text candidate scores on the user's own recordings (PhoneSpeechBench), and whether it meets the bar
 * set by today's laptop call path: Nemotron with hint words on the
 * laptop, 11.9 % word errors, 13 of 20 names, 0.15 s from the end of speech to text. Pure, for the JVM tests.
 */
object SpeechScore {
    /** The bar: names at least 13 of 20, word errors at most 12 %, end of speech to text at most 0.25 s (median). */
    const val MIN_NAMES = 13.0 / 20.0
    const val MAX_WER = 0.12
    const val MAX_WAIT_MS = 250L

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

    /** The names (hint words) said in ref, and how many of those hyp has: as whole words, case aside. */
    fun names(ref: String, hyp: String, names: List<String>): Pair<Int, Int> {
        val r = " " + words(ref).joinToString(" ") + " "
        val h = " " + words(hyp).joinToString(" ") + " "
        var said = 0
        var heard = 0
        for (name in names.map { words(it).joinToString(" ") }.filter { it.isNotEmpty() }.distinct()) {
            val whole = Regex("(?<= )" + Regex.escape(name) + "(?= )")  // a name said twice in a row counts twice
            val n = whole.findAll(r).count()
            if (n == 0) continue
            said += n
            heard += minOf(n, whole.findAll(h).count())
        }
        return said to heard
    }

    fun median(values: List<Long>): Long = values.sorted().let { if (it.isEmpty()) 0L else it[it.size / 2] }

    /** Why a candidate doesn't meet the bar; empty when it does. Jenny's speed alongside and the battery are read from
     *  the bench's own lines, against today's call, by whoever runs it. */
    fun shortfalls(wer: Double, namesSaid: Int, namesHeard: Int, waits: List<Long>): List<String> = buildList {
        if (wer > MAX_WER) add("word errors ${"%.1f".format(wer * 100)} % > ${"%.0f".format(MAX_WER * 100)} %")
        if (namesSaid > 0 && namesHeard.toDouble() / namesSaid < MIN_NAMES) add("names $namesHeard/$namesSaid < 13/20")
        val wait = median(waits)
        if (wait > MAX_WAIT_MS) add("median wait $wait ms > $MAX_WAIT_MS ms")
    }
}
