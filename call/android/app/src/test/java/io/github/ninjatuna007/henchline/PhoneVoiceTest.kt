package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
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

    @Test fun onlyTheEspeakDataMayHaveAPath() {
        // Still required, though sherpa-onnx 2.0 ignores it (the laptop's phonemes cover missing words)
        assertTrue("espeak-ng-data/en_dict" in PhoneVoice.REQUIRED)
        assertTrue(PhoneVoice.safeName("espeak-ng-data/lang/gmw/en-US"))
        assertTrue(PhoneVoice.safeName("model.onnx"))
        assertFalse(PhoneVoice.safeName("espeak-ng-data/../model.onnx"))
        assertFalse(PhoneVoice.safeName("espeak-ng-data/other"))
        assertFalse(PhoneVoice.safeName("../secret"))
        assertFalse(PhoneVoice.safeName(".verified"))
        assertFalse(PhoneVoice.safeName(""))
    }

    @Test fun eachPieceTakesItsOwnPhonemesWhenTheLaptopSentThem() {
        val clip = JSONObject("""{"pieces": ["Here's one,", "and more."], "ph": ["hˈɪɹz wˈʌn,", "ænd mˈɔːɹ."]}""")
        assertEquals("hˈɪɹz wˈʌn,", PhoneVoice.phonemesOf(clip, 0, 2))
        assertEquals("ænd mˈɔːɹ.", PhoneVoice.phonemesOf(clip, 1, 2))
    }

    @Test fun anOlderLaptopOrAMismatchedListMeansTheWords() {
        assertNull(PhoneVoice.phonemesOf(JSONObject("""{"pieces": ["One."]}"""), 0, 1))
        assertNull(PhoneVoice.phonemesOf(JSONObject("""{"pieces": ["One.", "Two."], "ph": ["wˈʌn."]}"""), 0, 2))
        assertNull(PhoneVoice.phonemesOf(JSONObject("""{"pieces": ["One."], "ph": [3]}"""), 0, 1))
    }

    @Test fun readsTheCpuAThreadLastRanOn() {
        // a name with spaces and a ")" must not shift the fields
        val stat = "1234 (render) x) S 1 1 0 0 -1 4194624 10 0 0 0 5 3 0 0 16 -4 9 0 100 0 0 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 6 0 0 0 0 0"
        assertEquals(6, PhoneVoice.cpuOf(stat))
        assertNull(PhoneVoice.cpuOf("1234 (short) S 1"))
    }

    @Test fun readsTheCpuset() {
        assertEquals("/background", PhoneVoice.cpusetOf("5:schedtune:/\n4:cpuset:/background\n0::/uid_10234/pid_99"))
        assertEquals("/top-app", PhoneVoice.cpusetOf("3:cpu,cpuset:/top-app"))
        assertNull(PhoneVoice.cpusetOf("0::/uid_10234/pid_99"))
    }
}
