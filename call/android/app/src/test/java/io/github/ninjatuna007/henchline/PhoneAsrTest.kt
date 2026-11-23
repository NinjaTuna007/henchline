package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** The phone's speech model: the laptop's manifest it takes, the copy the bench already put on the phone, and the
 *  user's listening setting. */
class PhoneAsrTest {
    @Test fun theLaptopsManifestMustOfferThisModel() {
        assertNull(PhoneAsr.offered(JSONObject().put("version", PhoneAsr.MODEL).put("engine", PhoneAsr.ENGINE)))
        assertTrue(PhoneAsr.offered(JSONObject().put("version", "something-newer").put("engine", PhoneAsr.ENGINE)) != null)
        assertTrue(PhoneAsr.offered(JSONObject().put("version", PhoneAsr.MODEL).put("engine", "other")) != null)
        assertTrue(PhoneAsr.offered(JSONObject("{}")) != null)
    }

    @Test fun theLaptopsVersionAndFilesAreTheOnesItServes() {
        // voice/phoneasr.py: VERSION, ENGINE and FILES (MODEL_CARD comes too, but isn't needed to listen)
        assertEquals("nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25", PhoneAsr.MODEL)
        assertEquals("sherpa-transducer", PhoneAsr.ENGINE)
        assertEquals(setOf("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt", "bpe.vocab",
            "silero_vad.onnx"), PhoneAsr.REQUIRED.toSet())
        assertTrue(PhoneAsr.REQUIRED.all(PhoneAsr::safeName))
        assertTrue(PhoneAsr.safeName("MODEL_CARD"))
    }

    @Test fun onlyPlainNames() {
        for (bad in listOf("", "../x", "a/b", "a\\b", ".verified", ".moved", "x".repeat(101))) assertFalse(bad, PhoneAsr.safeName(bad))
    }

    @Test fun theBenchsCopyIsFoundInTheAppsFilesThenTheExternalOnes() {
        val root = java.nio.file.Files.createTempDirectory("phone-asr").toFile()
        try {
            val internal = File(root, "files"); val external = File(root, "external")
            assertTrue(PhoneAsr.benchCopies(listOf(internal, external), "encoder.int8.onnx").isEmpty())
            File(external, "asr/nemotron").mkdirs(); File(external, "asr/nemotron/encoder.int8.onnx").writeText("e")
            File(internal, "asr/nemotron").mkdirs(); File(internal, "asr/nemotron/encoder.int8.onnx").writeText("i")
            assertEquals(listOf("i", "e"), PhoneAsr.benchCopies(listOf(internal, external), "encoder.int8.onnx").map { it.readText() })
            assertTrue(PhoneAsr.benchCopies(listOf(internal), "../nemotron/encoder.int8.onnx").isEmpty())
            assertTrue(PhoneAsr.benchCopies(listOf(internal), "silero_vad.onnx").isEmpty())  // never put there: fetched
        } finally { root.deleteRecursively() }
    }

    @Test fun otherVersionsAreStale() {
        val root = java.nio.file.Files.createTempDirectory("phone-asr").toFile()
        try {
            File(root, PhoneAsr.MODEL).mkdirs(); File(root, "older").mkdirs(); File(root, "a-file").writeText("x")
            assertEquals(listOf("older"), PhoneAsr.stale(root).map { it.name })
        } finally { root.deleteRecursively() }
    }

    @Test fun hintWordsFromTheManifest() {
        assertEquals(listOf("Claude", "Henchline"),
            PhoneAsr.hints(JSONObject("""{"hints": ["Claude", " Henchline ", "", "two\nlines"]}""")))
        assertTrue(PhoneAsr.hints(JSONObject("{}")).isEmpty())
        assertTrue(PhoneAsr.hints(null as JSONObject?).isEmpty())
    }

    @Test fun theListeningSetting() {
        assertEquals(PhoneEar.Mode.AUTOMATIC, PhoneEar.Mode.of(null))
        assertEquals(PhoneEar.Mode.AUTOMATIC, PhoneEar.Mode.of("nonsense"))
        for (m in PhoneEar.Mode.entries) assertEquals(m, PhoneEar.Mode.of(m.key))
        assertEquals(listOf("Automatic", "Always phone", "Always laptop"), PhoneEar.Mode.entries.map { it.label })
        // Always phone waits for the model; the others stand as chosen
        assertEquals(PhoneEar.Mode.AUTOMATIC, PhoneEar.effective(PhoneEar.Mode.PHONE, ready = false))
        assertEquals(PhoneEar.Mode.PHONE, PhoneEar.effective(PhoneEar.Mode.PHONE, ready = true))
        assertEquals(PhoneEar.Mode.LAPTOP, PhoneEar.effective(PhoneEar.Mode.LAPTOP, ready = false))
        assertTrue(PhoneEar.line(PhoneEar.Mode.AUTOMATIC, ready = false).contains("Download"))
        assertEquals(PhoneEar.line(PhoneEar.Mode.AUTOMATIC, ready = false), PhoneEar.line(PhoneEar.Mode.PHONE, ready = false))
    }
}
