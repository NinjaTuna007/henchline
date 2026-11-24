package io.github.ninjatuna007.henchline

import android.content.Context
import org.json.JSONObject
import java.io.File

/**
 * The phone's own speech-to-text model (the laptop's side: voice/phoneasr.py): the same streaming transducer the
 * laptop hears calls with, its word pieces for hint words, and a voice-activity model, fetched from the laptop with
 * ModelDownload into filesDir/phone-asr/<version>/ (about 664 MB; 1 GB free needed before it starts). It is only
 * stored here: what listens with it, and when, is the phone's ears (PhoneEar).
 *
 * A phone that already holds the model (pushed by adb for the speech bench, filesDir/asr/nemotron, or the external
 * files folder's asr/nemotron) has those files taken over when their SHA-256 is the laptop's: linked where Android
 * allows it, else moved within the app's own storage (and moved back on Delete), never copied or deleted; only
 * what's still missing comes down. Nothing is taken before the user agreed to the download.
 */
object PhoneAsr {
    // The model's name is the download's version, as the laptop names it: another version gets its own folder
    const val MODEL = "nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
    const val ENGINE = "sherpa-transducer"
    const val NAME = "Speech model"
    const val ABOUT_MB = 664  // shown before the manifest is fetched
    const val MIN_FREE = 1_000_000_000L
    const val BENCH = "asr/nemotron"  // where the bench's copy was put (PhoneSpeechBench), under either files folder
    val REQUIRED = listOf("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt", "bpe.vocab",
        "silero_vad.onnx")

    val download = ModelJob("henchline-ears-download")

    fun root(context: Context) = File(context.filesDir, "phone-asr")
    fun dir(context: Context) = File(root(context), MODEL)

    /** A plain file name from the laptop's manifest: no folders, nothing hidden. */
    fun safeName(name: String): Boolean =
        name.isNotEmpty() && name.length <= 100 && !name.contains('/') && !name.contains('\\') && !name.startsWith(".")

    /** Whether the laptop's manifest offers the model this app listens with; else why not. */
    fun offered(manifest: JSONObject): String? = when {
        manifest.optString("version") == MODEL && manifest.optString("engine") == ENGINE -> null
        else -> "The computer offers another speech model: update Henchline on both."
    }

    fun ready(context: Context): Boolean = ModelDownload.ready(dir(context), REQUIRED)

    fun sizeOnPhone(context: Context): Long = dir(context).walkTopDown().filter { it.isFile }.sumOf { it.length() }

    /** The hint words the laptop sent with the model (refreshed on each call later on). */
    fun hints(context: Context): List<String> = hints(try { JSONObject(ModelDownload.marker(dir(context)).readText()) } catch (_: Exception) { null })

    fun hints(manifest: JSONObject?): List<String> {
        val words = manifest?.optJSONArray("hints") ?: return emptyList()
        return (0 until words.length()).mapNotNull { words.optString(it).trim().takeIf { w -> w.isNotEmpty() && '\n' !in w } }
    }

    /** Where a copy of `name` may already be on this phone: the bench's folder, under the app's own files, then the
     *  external files folder (files adb pushed there may be unreadable to the app: those are simply fetched). */
    fun benchCopies(context: Context, name: String): List<File> =
        benchCopies(listOfNotNull(context.filesDir, try { context.getExternalFilesDir(null) } catch (_: Exception) { null }), name)

    fun benchCopies(bases: List<File>, name: String): List<File> =
        if (!safeName(name)) emptyList() else bases.map { File(it, "$BENCH/$name") }.filter { it.isFile && it.canRead() }

    /** Folders of other versions, gone once this one is whole. */
    fun stale(root: File): List<File> = root.listFiles()?.filter { it.isDirectory && it.name != MODEL } ?: emptyList()

    fun spec(context: Context) = ModelDownload.Spec(
        route = "/phone-asr", dir = dir(context), required = REQUIRED, safeName = ::safeName, offered = ::offered,
        notPrepared = "${HostPush.capitalName(context)} hasn't prepared the speech model yet (python -m voice.phoneasr prepare).",
        minFree = MIN_FREE, elsewhere = { benchCopies(context, it) },
        whole = { stale(root(context)).forEach { clear(it) } })

    /** This model's folders go; files moved in from the bench's folder go back there first, so they stay. False when
     *  one couldn't go back: its folder is kept (only its marker goes, so it no longer counts as ready). */
    fun delete(context: Context): Boolean {
        val all = root(context).listFiles()?.filter { it.isDirectory }?.map { clear(it) }?.all { it } ?: true
        if (all) root(context).deleteRecursively()
        return all
    }

    /** One model folder deleted once whatever was moved into it is back where it was; else only its marker. */
    fun clear(folder: File): Boolean {
        if (ModelDownload.giveBack(folder)) { folder.deleteRecursively(); return true }
        ModelDownload.marker(folder).delete()
        return false
    }
}
