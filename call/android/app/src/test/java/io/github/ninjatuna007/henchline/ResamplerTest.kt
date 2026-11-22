package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin

/** Jenny's 22.05 kHz into the call's 24 kHz: the right length, the same sound, no jumps. */
class ResamplerTest {
    private val jenny = Resampler(22050, 24000)

    private fun tone(hz: Double, rate: Int, n: Int, amp: Double = 0.5) =
        FloatArray(n) { (amp * sin(2 * PI * hz * it / rate)).toFloat() }

    @Test fun lengthFollowsTheRatio() {
        assertEquals(24000, jenny.length(22050))
        assertEquals(160, jenny.length(147))
        assertEquals(0, jenny.length(0))
        assertEquals(1, jenny.length(1))
        for (n in listOf(1, 2, 100, 4321, 43264)) {
            assertEquals(jenny.length(n), jenny.of(FloatArray(n)).size)
            assertTrue(abs(jenny.length(n) - n * 24000.0 / 22050) <= 0.5)
        }
        assertEquals(0, jenny.of(FloatArray(0)).size)
    }

    @Test fun theSameRateIsLeftAlone() {
        val same = Resampler(24000, 24000)
        val samples = floatArrayOf(0.1f, -0.2f, 0.3f)
        assertSame(samples, same.of(samples))
        assertEquals(3, same.length(3))
    }

    @Test fun speechTonesComeOutAsTheSameTonesAtTheNewRate() {
        for (hz in listOf(150.0, 1000.0, 4000.0, 8000.0)) {
            val out = jenny.of(tone(hz, 22050, 22050))
            val expected = tone(hz, 24000, out.size)
            var worst = 0f
            for (j in 200 until out.size - 200) worst = maxOf(worst, abs(out[j] - expected[j]))  // away from the padded edges
            assertTrue("$hz Hz off by $worst", worst < 0.01f)
        }
    }

    @Test fun aSteadyLevelStaysAndNothingJumps() {
        val out = jenny.of(FloatArray(5000) { 0.5f })
        for (j in 100 until out.size - 100) assertEquals(0.5f, out[j], 1e-4f)
        // a 1 kHz tone moves at most 2 pi f A / rate a sample: no seams between the filter's phases
        val wave = jenny.of(tone(1000.0, 22050, 10000))
        val limit = (2 * PI * 1000 * 0.5 / 24000 * 1.05).toFloat()
        for (j in 100 until wave.size - 100) assertTrue("step at $j", abs(wave[j] - wave[j - 1]) <= limit)
    }

    @Test fun aboveTheOldNyquistNothingIsMade() {
        // a tone near the input's top (10 kHz) passes; the image a naive stretch makes above 11 kHz doesn't appear
        val out = jenny.of(tone(10000.0, 22050, 22050))
        val image = 22050 - 10000.0  // where linear interpolation leaves an image: 12.05 kHz
        var re = 0.0; var im = 0.0
        for (j in 500 until out.size - 500) { re += out[j] * kotlin.math.cos(2 * PI * image * j / 24000); im += out[j] * sin(2 * PI * image * j / 24000) }
        val level = kotlin.math.sqrt(re * re + im * im) * 2 / (out.size - 1000)
        assertTrue("image level $level", level < 0.01)
    }
}
