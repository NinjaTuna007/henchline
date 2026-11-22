package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The phone trims and joins pieces exactly as the laptop's talkback does (tests/fixtures/phone-voice-trim.json). */
class PhoneVoiceTest {
    private val fixture = JSONObject(javaClass.classLoader!!.getResource("phone-voice-trim.json")!!.readText())

    /** The fixture's synthetic piece, built the same way: silence, a soft consonant, a tone, a soft one, silence. */
    private fun piece(): FloatArray {
        val rate = fixture.getInt("rate")
        val out = ArrayList<Float>()
        fun silence(s: Double) = repeat((rate * s).toInt()) { out.add(0f) }
        fun tone(amp: Double, hz: Double, s: Double) =
            repeat((rate * s).toInt()) { i -> out.add((amp * kotlin.math.sin(2 * Math.PI * hz * i / rate)).toFloat()) }
        silence(0.08); tone(0.003, 3000.0, 0.03)
        val toneLen = (rate * 0.4).toInt()
        repeat(toneLen) { i -> out.add((0.5 * kotlin.math.sin(2 * Math.PI * 220 * i / rate)).toFloat()) }
        tone(0.003, 3000.0, 0.03); silence(0.2)
        return out.toFloatArray()
    }

    @Test fun trimKeepsTheLaptopsSpanAtEveryJoin() {
        val samples = piece()
        assertEquals(fixture.getInt("length"), samples.size)
        val cases = fixture.getJSONArray("cases")
        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            val out = PhoneVoice.trim(samples, case.getBoolean("lead"), case.getBoolean("tail"))
            val start = (0..samples.size - out.size).first { s -> out.indices.all { out[it] == samples[s + it] } }
            // the same samples to within two (float rounding at the threshold)
            assertTrue("start ${case}: $start", kotlin.math.abs(start - case.getInt("start")) <= 2)
            assertTrue("length ${case}: ${out.size}", kotlin.math.abs(out.size - case.getInt("length")) <= 2)
        }
    }

    @Test fun silenceIsLeftAlone() {
        val zeros = FloatArray(100)
        assertEquals(fixture.getJSONArray("silent").getJSONObject(0).getInt("length"), PhoneVoice.trim(zeros, true, true).size)
    }

    @Test fun pcmIsLittleEndianWithTheGapInFront() {
        val bytes = PhoneVoice.pcm(floatArrayOf(1f, -1f, 0.5f), gap = 1f / PhoneVoice.RATE)  // one sample of silence
        assertArrayEquals(byteArrayOf(0, 0, 0xff.toByte(), 0x7f, 0x01, 0x80.toByte(), 0xff.toByte(), 0x3f), bytes)
    }
}
