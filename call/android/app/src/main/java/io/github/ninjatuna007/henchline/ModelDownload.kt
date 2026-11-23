package io.github.ninjatuna007.henchline

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean

/**
 * A model the phone fetches from the computer it calls (the phone's voice, the phone's own listening). The computer
 * lists each file's name, size and SHA-256 at `<route>/manifest` and serves it at `<route>/file/<name>`. Each file
 * comes down into "<name>.part", resumes with a Range after a cut, is checked against its hash and only then renamed
 * into place; once every file is whole the manifest is kept as the folder's ".verified" marker (ready()).
 *
 * Files already in the folder and whole are kept. A file the phone holds elsewhere with the same hash (the speech
 * model pushed for the bench) is taken in place of a download once the user agreed to it: linked, else moved (the
 * move recorded first, so it can be given back), never copied, never deleted.
 */
class ModelDownload(private val context: Context, private val spec: Spec,
                    private val progress: (done: Long, total: Long, name: String) -> Unit) {

    class Spec(
        val route: String,  // "/phone-voice": the manifest at <route>/manifest, the files at <route>/file/<name>
        val dir: File,  // where the files go
        val required: List<String>,  // what the marker must list before the model counts as ready
        val safeName: (String) -> Boolean,  // a name the manifest may use
        val offered: (JSONObject) -> String?,  // null when this app takes the manifest's model, else why not
        val notPrepared: String,  // said when the computer has no manifest
        val minFree: Long = 0,  // free space the phone must have before a download starts
        val elsewhere: (String) -> List<File> = { emptyList() },  // copies of a file the phone may already hold
        val whole: () -> Unit = {},  // after the marker is written (an earlier model's folder goes)
    )

    data class Entry(val name: String, val size: Long, val sha256: String)

    sealed class Outcome {
        object Done : Outcome()
        data class Failed(val why: String) : Outcome()
        /** On mobile data with `bytes` still to fetch: the user is asked first, then run(mobileOk = true). */
        data class AskMobile(val bytes: Long) : Outcome()
    }

    private val cancelled = AtomicBoolean(false)
    fun cancel() = cancelled.set(true)

    /** Fetches what's missing. mobileOk: the user agreed to mobile data. Without it, on a metered network the run
     *  stops before taking or fetching anything and says how much would go over it (AskMobile), and a download that
     *  finds itself off Wi-Fi midway stops until Wi-Fi is back (what's fetched is kept). Copies already on the phone
     *  cost nothing and are taken only once the download goes ahead. */
    fun run(mobileOk: Boolean = false): Outcome {
        val origin = Files.origin(context) ?: return Outcome.Failed("Set the laptop's address in Settings first.")
        val manifest = try {
            Files.client.newCall(okhttp3.Request.Builder().url("$origin${spec.route}/manifest")
                .header("Origin", origin).build()).execute().use {
                if (!it.isSuccessful) return Outcome.Failed(spec.notPrepared)
                JSONObject(it.body!!.string())
            }
        } catch (e: Exception) { return Outcome.Failed("Couldn't reach the laptop (is Tailscale on?)") }
        spec.offered(manifest)?.let { return Outcome.Failed(it) }
        val files = entries(manifest, spec.safeName) ?: return Outcome.Failed("The laptop named an odd file: stopped.")
        val folder = spec.dir.apply { mkdirs() }
        var have = 0L
        val missing = ArrayList<Entry>()
        val elsewhere = LinkedHashMap<Entry, File>()  // a matching copy on the phone, taken once the download goes ahead
        for (f in files) {
            if (cancelled.get()) return Outcome.Failed(CANCELLED)
            val target = File(folder, f.name)
            if (matches(target, f)) { have += f.size; continue }
            val copy = spec.elsewhere(f.name).firstOrNull { it.canonicalPath != target.canonicalPath && matches(it, f) }
            if (copy != null) elsewhere[f] = copy else missing += f
        }
        gate(folder, missing, mobileOk)?.let { return it }
        for ((f, copy) in elsewhere) {
            if (adopt(f, copy, File(folder, f.name))) have += f.size else missing += f  // a download instead
        }
        if (elsewhere.isNotEmpty()) gate(folder, missing, mobileOk)?.let { return it }  // one wasn't taken
        val total = files.sumOf { it.size }
        progress(have, total, "")
        marker(spec.dir).delete()
        var done = have
        for (f in missing) {
            fetch(origin, folder, f, done, total, mobileOk)?.let { return Outcome.Failed(it) }
            done += f.size
        }
        marker(spec.dir).writeText(manifest.toString())
        spec.whole()
        return Outcome.Done
    }

    /** Before anything is fetched: room for it, and Wi-Fi unless the user agreed to mobile data. null: go ahead. */
    private fun gate(folder: File, missing: List<Entry>, mobileOk: Boolean): Outcome? {
        if (missing.isEmpty()) return null
        val remaining = missing.sumOf { it.size - File(folder, "${it.name}.part").length().coerceIn(0, it.size) }
        if (!enoughSpace(folder.usableSpace, remaining, spec.minFree))
            return Outcome.Failed("Needs ${mb(maxOf(spec.minFree, remaining + MARGIN))} MB free on this phone: make room and try again.")
        if (!mobileOk && !PhoneVoice.unmetered(context)) return Outcome.AskMobile(remaining)
        return null
    }

    /** One file into "<name>.part" (resumed from what's there), checked, then put in place; null when it's whole. */
    private fun fetch(origin: String, folder: File, f: Entry, before: Long, total: Long, mobileOk: Boolean): String? {
        val target = File(folder, f.name)
        target.parentFile?.mkdirs()
        val part = File(folder, "${f.name}.part")
        val have = part.length().coerceAtMost(f.size)
        try {
            val request = okhttp3.Request.Builder().url("$origin${spec.route}/file/${f.name}").header("Origin", origin)
            if (have > 0) request.header("Range", "bytes=$have-")
            Files.client.newCall(request.build()).execute().use { answer ->
                if (!answer.isSuccessful) return "The laptop couldn't send ${f.name}."
                val append = have > 0 && answer.code == 206
                java.io.FileOutputStream(part, append).use { out ->
                    var got = if (append) have else 0L
                    val input = answer.body!!.byteStream()
                    val buffer = ByteArray(1 shl 16)
                    var checked = got
                    while (true) {
                        if (cancelled.get()) return CANCELLED
                        // Wi-Fi gone midway and mobile data never agreed to: stop, keeping what came (it resumes)
                        if (!mobileOk && got - checked >= RECHECK) { checked = got; if (!PhoneVoice.unmetered(context)) return WAITING_WIFI }
                        val n = input.read(buffer)
                        if (n < 0) break
                        out.write(buffer, 0, n)
                        got += n
                        progress(before + got, total, f.name)
                    }
                }
            }
        } catch (e: Exception) { return "The download broke off: try again to carry on." }
        if (part.length() != f.size || sha256(part) != f.sha256) { part.delete(); return "${f.name} didn't match the laptop's copy: try again." }
        if (!part.renameTo(target)) return "Couldn't keep ${f.name} on this phone."
        return null
    }

    /** A copy of this file the phone already holds, taken into `target` (linked, else moved with a record first). */
    private fun adopt(f: Entry, copy: File, target: File): Boolean {
        target.parentFile?.mkdirs()
        val taken = take(copy, target, File(spec.dir, MOVED), f.name)
        if (taken != null) android.util.Log.i("Henchline", "model download: ${f.name} taken from the phone ($taken), not fetched")
        return taken != null && matches(target, f)
    }

    companion object {
        const val CANCELLED = "Cancelled."
        const val WAITING_WIFI = "Waiting for Wi-Fi: the download carries on from here once you're back on it."
        const val RECHECK = 4_000_000L  // bytes between checks that the phone is still on Wi-Fi
        const val MARGIN = 100_000_000L  // free space kept beyond what a download needs
        private val hashes = ConcurrentHashMap<String, Triple<Long, Long, String>>()  // path → size, time, sha256

        const val MOVED = ".moved"  // "<name>\t<where it was>" per file moved in from elsewhere on the phone

        fun marker(dir: File) = File(dir, ".verified")

        /** Files moved in from elsewhere go back where they were (a folder is about to be deleted): a copy the user
         *  put on the phone outlives the download that took it. Never overwrites a file already back there. True when
         *  nothing recorded is left only here; false while one is (the folder must then be kept). */
        fun giveBack(dir: File): Boolean {
            val record = File(dir, MOVED)
            if (!record.exists()) return true
            val lines = try { record.readLines() } catch (_: Exception) { return false }
            var all = true
            for (line in lines) {
                val (name, was) = line.split('\t', limit = 2).takeIf { it.size == 2 } ?: continue
                val here = File(dir, name)
                val there = File(was)
                if (!here.isFile || there.exists()) continue  // never moved (the record comes first), or back already
                val back = there.parentFile?.let { it.isDirectory || it.mkdirs() } == true && here.renameTo(there)
                if (!back) all = false
            }
            return all
        }

        /** The manifest's files, or null when it has none or names one this model may not use. */
        fun entries(manifest: JSONObject, safeName: (String) -> Boolean): List<Entry>? {
            val files = manifest.optJSONArray("files") ?: return null
            val out = (0 until files.length()).map { i ->
                val f = files.optJSONObject(i) ?: return null
                val name = f.optString("name")
                val size = f.optLong("size", -1)
                val sha = f.optString("sha256")
                if (!safeName(name) || size < 0 || !Regex("[0-9a-f]{64}").matches(sha)) return null
                Entry(name, size, sha)
            }
            return out.takeIf { it.isNotEmpty() && it.map { e -> e.name }.toSet().size == it.size }
        }

        /** Downloaded and every file checked: the marker lists all of `required`, and each listed file has its size. */
        fun ready(dir: File, required: List<String>): Boolean {
            val kept = try { JSONObject(marker(dir).readText()) } catch (_: Exception) { return false }
            val files = entries(kept) { true } ?: return false
            if (!files.map { it.name }.containsAll(required)) return false
            return files.all { File(dir, it.name).length() == it.size }
        }

        /** Room for `remaining` more bytes and a margin, and never below `minFree`. */
        fun enoughSpace(free: Long, remaining: Long, minFree: Long): Boolean = free >= maxOf(minFree, remaining + MARGIN)

        fun mb(bytes: Long): Long = (bytes + 999_999) / 1_000_000

        /** The file is there, its size and SHA-256 the manifest's (hashed once per size and time). */
        fun matches(file: File, f: Entry): Boolean = file.isFile && file.length() == f.size && sha256(file) == f.sha256

        fun sha256(file: File): String {
            val key = file.absolutePath
            val known = hashes[key]
            val size = file.length(); val time = file.lastModified()
            if (known != null && known.first == size && known.second == time) return known.third
            val digest = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { input ->
                val buffer = ByteArray(1 shl 16)
                while (true) { val n = input.read(buffer); if (n < 0) break; digest.update(buffer, 0, n) }
            }
            return digest.digest().joinToString("") { "%02x".format(it) }.also { hashes[key] = Triple(size, time, it) }
        }

        /** `from` made available as `to` without deleting it: a hard link (both names stay), else a move on the same
         *  disk, written into `record` ("<name>\t<where it was>") before it happens, and not tried when that write
         *  fails. Never a copy (that would need room the space check didn't count): null, and it's downloaded. */
        fun take(from: File, to: File, record: File, name: String): String? {
            to.delete()
            try { java.nio.file.Files.createLink(to.toPath(), from.toPath()); return "linked" } catch (_: Exception) {}
            try { record.appendText("$name\t${from.absolutePath}\n") } catch (_: Exception) { return null }
            return if (from.renameTo(to)) "moved" else null
        }
    }
}

/** One download at a time per model for the whole app: every sheet that shows it follows its progress, and it goes on
 *  when the sheet that started it is closed. */
class ModelJob(private val threadName: String) {
    @Volatile var job: ModelDownload? = null
        private set
    @Volatile var percent = 0
        private set
    @Volatile var note: String? = null  // why the last one stopped, until the next starts
        private set
    private val listeners = java.util.concurrent.CopyOnWriteArraySet<() -> Unit>()

    fun listen(listener: () -> Unit) { listeners.add(listener) }
    fun forget(listener: () -> Unit) { listeners.remove(listener) }
    private fun tell() = listeners.forEach { try { it() } catch (_: Throwable) {} }

    /** Runs the download on its own thread. mobileOk: the user agreed to mobile data. Otherwise, off Wi-Fi,
     *  askMobile(bytes) hears (on that thread) how much would go over it, so the user can be asked; nothing is taken
     *  or fetched until a start with mobileOk. Without askMobile the note says to wait for Wi-Fi. done after a
     *  whole one. */
    fun start(context: Context, spec: ModelDownload.Spec, mobileOk: Boolean, askMobile: ((Long) -> Unit)? = null,
              done: () -> Unit = {}) {
        if (job != null) return
        val download = ModelDownload(context.applicationContext, spec) { got, total, _ ->
            val now = if (total > 0) (got * 100 / total).toInt() else 0
            if (now != percent) { percent = now; tell() }
        }
        note = null; percent = 0
        job = download
        tell()
        Thread {
            val outcome = try { download.run(mobileOk) } catch (e: Throwable) {
                ModelDownload.Outcome.Failed("The download broke off: try again to carry on.")
            }
            job = null
            note = when (outcome) {
                is ModelDownload.Outcome.Failed -> outcome.why.takeIf { it != ModelDownload.CANCELLED }
                is ModelDownload.Outcome.AskMobile -> if (askMobile == null) ModelDownload.WAITING_WIFI else null
                else -> null
            }
            if (outcome == ModelDownload.Outcome.Done) try { done() } catch (_: Throwable) {}  // before the sheets redraw
            tell()
            if (outcome is ModelDownload.Outcome.AskMobile) askMobile?.invoke(outcome.bytes)
        }.apply { name = threadName; start() }
    }

    fun cancel() { job?.cancel() }
}
