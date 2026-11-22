package io.github.ninjatuna007.henchline

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * One whole piece of audio from `from` Hz to `to` Hz (the phone voice: 22.05 kHz into the call's 24 kHz). A
 * polyphase windowed-sinc filter: every output sample falls at one of `to / gcd` fixed fractions between two input
 * samples, so each fraction's weights are worked out once. The low-pass sits just under the lower rate's Nyquist
 * (10.1 kHz here: nothing speech needs is lost, no image above it is made), 2 x TAPS input samples per output.
 * A piece starts and ends in quiet, so the edges are padded with silence.
 */
class Resampler(val from: Int, val to: Int) {
    private val up: Int
    private val down: Int
    private val weights: Array<FloatArray>  // per output phase: the weights of input samples base-TAPS+1 … base+TAPS

    init {
        require(from > 0 && to > 0)
        val g = gcd(from, to)
        up = to / g
        down = from / g
        val cutoff = 0.5 * CUTOFF * minOf(1.0, to.toDouble() / from)  // cycles per input sample
        weights = Array(if (from == to) 0 else up) { phase ->
            val frac = phase.toDouble() / up
            val w = DoubleArray(2 * TAPS) { i ->
                val x = frac + TAPS - 1 - i  // how far output time lies past input sample i of the window
                val sinc = if (x == 0.0) 1.0 else sin(2 * PI * cutoff * x) / (2 * PI * cutoff * x)
                val window = if (kotlin.math.abs(x) >= TAPS) 0.0
                    else 0.42 + 0.5 * cos(PI * x / TAPS) + 0.08 * cos(2 * PI * x / TAPS)  // Blackman
                sinc * window
            }
            val sum = w.sum()  // each phase passes a steady level unchanged
            FloatArray(w.size) { (w[it] / sum).toFloat() }
        }
    }

    /** How many samples `n` input samples become. */
    fun length(n: Int): Int = if (from == to) n else ((n.toLong() * up + down / 2) / down).toInt()

    fun of(samples: FloatArray): FloatArray {
        if (from == to) return samples
        val out = FloatArray(length(samples.size))
        for (j in out.indices) {
            val at = j.toLong() * down
            val base = (at / up).toInt()
            val w = weights[(at % up).toInt()]
            val first = base - TAPS + 1
            var acc = 0f
            val lo = maxOf(0, -first)
            val hi = minOf(w.size, samples.size - first)
            for (i in lo until hi) acc += w[i] * samples[first + i]
            out[j] = acc
        }
        return out
    }

    companion object {
        const val TAPS = 16  // input samples each side of an output sample
        const val CUTOFF = 0.92  // the pass band's edge, as a share of the lower rate's Nyquist

        private tailrec fun gcd(a: Int, b: Int): Int = if (b == 0) a else gcd(b, a % b)
    }
}
