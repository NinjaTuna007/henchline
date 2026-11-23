package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.security.MessageDigest

/** The download shared by the phone's voice and its speech model: the manifest it takes, when a model counts as
 *  ready, room on the phone, and files taken over from elsewhere on the phone without losing them. */
class ModelDownloadTest {
    private val root = java.nio.file.Files.createTempDirectory("model-download").toFile()

    @After fun clean() { root.deleteRecursively() }

    private fun sha(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private fun manifest(vararg files: Pair<String, ByteArray>) = JSONObject().put("version", "v1").put("files",
        org.json.JSONArray(files.map { (name, body) -> JSONObject().put("name", name).put("size", body.size).put("sha256", sha(body)) }))

    @Test fun theManifestsFilesAreTakenOnlyWhenEveryNameIsSafe() {
        val ok = ModelDownload.entries(manifest("a.onnx" to "abc".toByteArray(), "tokens.txt" to ByteArray(0)), PhoneAsr::safeName)!!
        assertEquals(listOf("a.onnx", "tokens.txt"), ok.map { it.name })
        assertEquals(3L, ok[0].size)
        assertNull(ModelDownload.entries(manifest("../secret" to ByteArray(1)), PhoneAsr::safeName))
        assertNull(ModelDownload.entries(manifest(".verified" to ByteArray(1)), PhoneAsr::safeName))
        assertNull(ModelDownload.entries(manifest("a" to ByteArray(1), "a" to ByteArray(2)), PhoneAsr::safeName))  // twice
        assertNull(ModelDownload.entries(JSONObject("{}"), PhoneAsr::safeName))
        assertNull(ModelDownload.entries(JSONObject("""{"files": []}"""), PhoneAsr::safeName))
        assertNull(ModelDownload.entries(JSONObject("""{"files": [{"name": "a", "size": 1, "sha256": "nothex"}]}"""), PhoneAsr::safeName))
        assertNull(ModelDownload.entries(JSONObject("""{"files": [{"name": "a", "sha256": "${"0".repeat(64)}"}]}"""), PhoneAsr::safeName))
    }

    @Test fun readyNeedsTheMarkerEveryRequiredFileAndTheirSizes() {
        val body = "model".toByteArray()
        assertFalse(ModelDownload.ready(root, listOf("m.onnx")))  // nothing yet
        File(root, "m.onnx").writeBytes(body)
        assertFalse(ModelDownload.ready(root, listOf("m.onnx")))  // the file, but never checked
        ModelDownload.marker(root).writeText(manifest("m.onnx" to body).toString())
        assertTrue(ModelDownload.ready(root, listOf("m.onnx")))
        assertFalse(ModelDownload.ready(root, listOf("m.onnx", "vad.onnx")))  // an older download lacks one
        File(root, "m.onnx").writeBytes("cut".toByteArray())
        assertFalse(ModelDownload.ready(root, listOf("m.onnx")))
        ModelDownload.marker(root).writeText("not json")
        assertFalse(ModelDownload.ready(root, emptyList()))
    }

    @Test fun roomForTheRestAndAMarginButNeverBelowTheFloor() {
        val gb = 1_000_000_000L
        assertTrue(ModelDownload.enoughSpace(free = 200_000_000, remaining = 64_000_000, minFree = 0))  // Jenny
        assertFalse(ModelDownload.enoughSpace(free = 100_000_000, remaining = 64_000_000, minFree = 0))
        assertFalse(ModelDownload.enoughSpace(free = gb - 1, remaining = 2_000_000, minFree = gb))  // the speech model
        assertTrue(ModelDownload.enoughSpace(free = gb, remaining = 2_000_000, minFree = gb))
        assertFalse(ModelDownload.enoughSpace(free = gb, remaining = 950_000_000, minFree = gb))
        assertEquals(653L, ModelDownload.mb(652_916_849))
        assertEquals(0L, ModelDownload.mb(0))
    }

    @Test fun aFileMatchesOnlyWithItsSizeAndHash() {
        val body = "encoder".toByteArray()
        val f = File(root, "e.onnx").apply { writeBytes(body) }
        val entry = ModelDownload.Entry("e.onnx", body.size.toLong(), sha(body))
        assertTrue(ModelDownload.matches(f, entry))
        assertFalse(ModelDownload.matches(f, entry.copy(sha256 = sha("other".toByteArray()))))
        assertFalse(ModelDownload.matches(File(root, "missing"), entry))
        f.writeBytes("encodex".toByteArray())  // same size, new bytes: hashed again (the time moved)
        f.setLastModified(f.lastModified() + 2000)
        assertFalse(ModelDownload.matches(f, entry))
    }

    @Test fun aCopyElsewhereIsTakenWithoutLosingIt() {
        val bench = File(root, "asr/nemotron").apply { mkdirs() }
        val copy = File(bench, "encoder.int8.onnx").apply { writeText("weights") }
        val model = File(root, "phone-asr/v1").apply { mkdirs() }
        val target = File(model, "encoder.int8.onnx")
        val record = File(model, ModelDownload.MOVED)
        val how = ModelDownload.take(copy, target, record, "encoder.int8.onnx")
        assertTrue(how in setOf("linked", "moved"))  // never a copy
        assertEquals("weights", target.readText())
        if (how == "linked") { assertEquals("weights", copy.readText()); assertFalse(record.exists()) }
        else assertTrue(record.readText().contains(copy.absolutePath))  // written before the move
        assertTrue(ModelDownload.giveBack(model))
        assertEquals("weights", copy.readText())  // still (or again) where the user put it
    }

    @Test fun nothingToTakeIsNotCopied() {
        val model = File(root, "phone-asr/v1").apply { mkdirs() }
        assertNull(ModelDownload.take(File(root, "missing"), File(model, "a"), File(model, ModelDownload.MOVED), "a"))
        assertFalse(File(model, "a").exists())
    }

    @Test fun movedFilesGoBackBeforeTheirFolderIsDeleted() {
        val bench = File(root, "asr/nemotron")
        val model = File(root, "phone-asr/v1").apply { mkdirs() }
        File(model, "encoder.int8.onnx").writeText("weights")
        File(model, "tokens.txt").writeText("fetched")
        File(model, ModelDownload.MOVED).writeText("encoder.int8.onnx\t${File(bench, "encoder.int8.onnx").absolutePath}\n" +
            "joiner.int8.onnx\t${File(bench, "joiner.int8.onnx").absolutePath}\nnonsense\n")  // joiner: recorded, never moved
        assertTrue(ModelDownload.giveBack(model))
        assertEquals("weights", File(bench, "encoder.int8.onnx").readText())  // its folder made again
        assertFalse(File(bench, "tokens.txt").exists())  // fetched files aren't the bench's
        assertFalse(File(bench, "joiner.int8.onnx").exists())
        assertTrue(ModelDownload.giveBack(File(root, "nothing-here")))  // no record: nothing to do
    }

    @Test fun aFileThatCantGoBackKeepsItsFolder() {
        File(root, "asr").writeText("a file where the folder was")  // the bench's folder can't be made again
        val model = File(root, "phone-asr/v1").apply { mkdirs() }
        File(model, "encoder.int8.onnx").writeText("weights")
        ModelDownload.marker(model).writeText("{}")
        File(model, ModelDownload.MOVED).writeText("encoder.int8.onnx\t${File(root, "asr/nemotron/encoder.int8.onnx").absolutePath}\n")
        assertFalse(ModelDownload.giveBack(model))
        assertFalse(PhoneAsr.clear(model))
        assertEquals("weights", File(model, "encoder.int8.onnx").readText())  // kept
        assertFalse(ModelDownload.marker(model).exists())  // no longer ready
        File(root, "asr").delete()
        assertTrue(PhoneAsr.clear(model))  // once it can go back, it does, and the folder goes
        assertEquals("weights", File(root, "asr/nemotron/encoder.int8.onnx").readText())
        assertFalse(model.exists())
    }

    @Test fun aFileAlreadyBackThereIsNeverOverwritten() {
        val bench = File(root, "asr/nemotron").apply { mkdirs() }
        File(bench, "tokens.txt").writeText("the user's")
        val model = File(root, "phone-asr/v1").apply { mkdirs() }
        File(model, "tokens.txt").writeText("another")
        File(model, ModelDownload.MOVED).writeText("tokens.txt\t${File(bench, "tokens.txt").absolutePath}\n")
        assertTrue(ModelDownload.giveBack(model))
        assertEquals("the user's", File(bench, "tokens.txt").readText())
    }

    @Test fun jennysMarkerWithTheEspeakPathsIsReady() {
        // A marker as Jenny's download keeps it: the manifest, espeak data at relative paths with "/"
        val names = listOf(PhoneVoice.ONNX, "tokens.txt", "MODEL_CARD") + PhoneVoice.ESPEAK
        val bodies = names.associateWith { "body of $it".toByteArray() }
        for ((name, body) in bodies) File(root, name).apply { parentFile?.mkdirs(); writeBytes(body) }
        ModelDownload.marker(root).writeText(JSONObject().put("version", PhoneVoice.MODEL).put("files",
            org.json.JSONArray(names.map { JSONObject().put("name", it).put("size", bodies[it]!!.size).put("sha256", sha(bodies[it]!!)) }))
            .toString())
        assertTrue(ModelDownload.ready(root, PhoneVoice.REQUIRED))
        File(root, "espeak-ng-data/lang/gmw/en-US").delete()
        assertFalse(ModelDownload.ready(root, PhoneVoice.REQUIRED))
    }
}
