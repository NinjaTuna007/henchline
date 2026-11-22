package io.github.ninjatuna007.henchline

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Files the agents send (`handsfree send` on the laptop): shown in the call's chat, or as a notification off a
 * call. Opening one saves it to Downloads/Henchline, so it stays on the phone, and hands it to whatever app
 * opens that type. Pictures are previewed in the chat.
 */
object Files {
    const val CHANNEL = "files"
    const val URL = "file_url"
    const val NAME = "file_name"
    const val TYPE = "file_type"
    const val SIZE = "file_size"
    const val SESSION = "file_session"

    val client: OkHttpClient = OkHttpClient.Builder().readTimeout(60, TimeUnit.SECONDS).addNetworkInterceptor(LegacySigner).build()

    /** The laptop's address as Henchline knows it, e.g. https://laptop.tailnet.ts.net */
    fun origin(context: Context): String? {
        val server = context.getSharedPreferences("call", Context.MODE_PRIVATE).getString("server", "") ?: ""
        val uri = Uri.parse(server)
        return if (uri.scheme == "https" && !uri.authority.isNullOrEmpty()) "https://${uri.authority}" else null
    }

    /** A link from the laptop: /files/… from the call, or a whole address from a push. Only the laptop's own
     *  address is followed, so a push can't point the phone anywhere else. */
    fun resolve(context: Context, url: String): String? {
        val origin = origin(context) ?: return null
        return when {
            url.startsWith("/files/") -> origin + url
            url.startsWith("$origin/files/") -> url
            else -> null
        }
    }

    /** Opened from its copy in Downloads/Henchline when it is there already (the user's catch, 2026-11-16: every
     *  tap downloaded it again), else saved there first and then opened; `note` hears how it went (on the main
     *  thread). size: the file's size, to know it again among copies saved before this was remembered. */
    fun open(context: Context, url: String, name: String, type: String, size: Long = 0, session: String = "",
             note: (String) -> Unit) {
        val address = resolve(context, url) ?: return note("That file isn't from your laptop's address, so I didn't open it.")
        val main = android.os.Handler(context.mainLooper)
        Thread {
            val kept = savedCopy(context, address, name, size)
            if (kept == null) main.post { note("Downloading $name…") }
            val saved = kept ?: try {
                download(context, address, name, type).also { remember(context, address, it) }
            } catch (e: Exception) {
                main.post { note("Couldn't download $name: ${e.message ?: "no connection"} (is Tailscale on?)") }
                return@Thread
            }
            Media.record(context, saved, address, name, session)  // which chat it came from
            main.post {
                try {
                    context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(saved, type)
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
                    if (kept == null) note("Saved to Downloads/Henchline: $name")
                } catch (_: ActivityNotFoundException) {
                    note("Saved to Downloads/Henchline: $name (no app here opens $type)")
                }
            }
        }.start()
    }

    /** That file in Downloads/Henchline, saved there first if it isn't yet (off the main thread): for sharing it. */
    fun keep(context: Context, url: String, name: String, type: String, size: Long = 0, session: String = ""): Uri {
        val address = resolve(context, url) ?: throw IllegalStateException("not from your laptop's address")
        val saved = savedCopy(context, address, name, size)
            ?: download(context, address, name, type).also { remember(context, address, it) }
        Media.record(context, saved, address, name, session)
        return saved
    }

    private fun remember(context: Context, address: String, saved: Uri) =
        context.getSharedPreferences("downloads", Context.MODE_PRIVATE).edit().putString(address, saved.toString()).apply()

    /** The copy of that file already in Downloads/Henchline, if it is still there and readable: the one saved for
     *  this very link, else (saved before links were remembered) the newest there with its name and exact size. */
    private fun savedCopy(context: Context, address: String, name: String, size: Long): Uri? {
        val resolver = context.contentResolver
        fun readable(uri: Uri) = try { resolver.openFileDescriptor(uri, "r")?.use { true } ?: false } catch (_: Exception) { false }
        context.getSharedPreferences("downloads", Context.MODE_PRIVATE).getString(address, null)?.let { kept ->
            val uri = Uri.parse(kept)
            if (readable(uri)) return uri
        }
        if (size <= 0) return null
        // Only copies saved before links were remembered: a new file that happens to share an old one's name and
        // size must never open the old copy (DATE_ADDED is in seconds)
        val prefs = context.getSharedPreferences("downloads", Context.MODE_PRIVATE)
        val since = prefs.getLong("remembering_since", 0L).takeIf { it > 0 }
            ?: (System.currentTimeMillis() / 1000).also { prefs.edit().putLong("remembering_since", it).apply() }
        return try {
            // Its name, or Android's "name (1).pdf" when that name was taken: the exact size says it is this file
            val dot = name.lastIndexOf('.').takeIf { it > 0 } ?: name.length
            val renamed = name.substring(0, dot).replace("%", "\\%").replace("_", "\\_") + " (%)" +
                name.substring(dot).replace("%", "\\%").replace("_", "\\_")
            resolver.query(MediaStore.Downloads.EXTERNAL_CONTENT_URI, arrayOf(MediaStore.Downloads._ID),
                "(${MediaStore.Downloads.DISPLAY_NAME} = ? OR ${MediaStore.Downloads.DISPLAY_NAME} LIKE ? ESCAPE '\\') AND " +
                    "${MediaStore.Downloads.SIZE} = ? AND " +
                    "${MediaStore.Downloads.RELATIVE_PATH} LIKE ? AND ${MediaStore.Downloads.DATE_ADDED} < ?",
                arrayOf(name, renamed, size.toString(), "${Environment.DIRECTORY_DOWNLOADS}/Henchline%", since.toString()),
                "${MediaStore.Downloads.DATE_ADDED} DESC")?.use { rows ->
                if (!rows.moveToFirst()) null
                else android.content.ContentUris.withAppendedId(MediaStore.Downloads.EXTERNAL_CONTENT_URI, rows.getLong(0))
                    .takeIf { readable(it) }?.also { remember(context, address, it) }
            }
        } catch (_: Exception) {
            null
        }
    }

    /** Named by the link's SHA-256: two links never share a file (its copy may be saved under the other's name). */
    fun cacheFile(context: Context, address: String) = File(context.cacheDir, "previews/" +
        java.security.MessageDigest.getInstance("SHA-256").digest(address.toByteArray()).joinToString("") { "%02x".format(it) })

    private fun download(context: Context, url: String, name: String, type: String): Uri {
        val resolver = context.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, name)
            put(MediaStore.Downloads.MIME_TYPE, type)
            put(MediaStore.Downloads.RELATIVE_PATH, "${Environment.DIRECTORY_DOWNLOADS}/Henchline")
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: throw IllegalStateException("no room in Downloads")
        try {
            val fetched = cacheFile(context, url)
            if (fetched.exists()) {  // fetched already for its preview: copied, not fetched again
                resolver.openOutputStream(uri)!!.use { out -> fetched.inputStream().use { it.copyTo(out) } }
            } else client.newCall(Request.Builder().url(url).build()).execute().use { answer ->
                if (!answer.isSuccessful) throw IllegalStateException(
                    if (answer.code == 404) "the laptop no longer has it (kept a week)" else "the laptop said ${answer.code}")
                resolver.openOutputStream(uri)!!.use { out -> answer.body!!.byteStream().copyTo(out) }
            }
        } catch (e: Exception) {
            resolver.delete(uri, null, null)
            throw e
        }
        resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
        return uri
    }

    /** The file itself in the app's cache, fetched once (a screen rebuilt doesn't fetch it again), or null. */
    fun fetched(context: Context, address: String): File? {
        val cached = cacheFile(context, address)
        if (cached.exists()) return cached
        cached.parentFile?.mkdirs()
        client.newCall(Request.Builder().url(address).build()).execute().use { answer ->
            if (!answer.isSuccessful) return null
            val part = File(cached.path + "." + java.util.UUID.randomUUID() + ".part")  // two fetches never share one
            try {
                part.outputStream().use { out -> answer.body!!.byteStream().copyTo(out) }
                part.renameTo(cached)
            } finally {
                part.delete()  // a dropped connection leaves no half file behind (after a rename it's already gone)
            }
        }
        return cached.takeIf { it.exists() }
    }

    /** A small picture for the chat, or null. */
    fun preview(context: Context, url: String, widthPx: Int): Bitmap? {
        val address = resolve(context, url) ?: return null
        try {
            val cached = fetched(context, address) ?: return null
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(cached.path, bounds)
            var sample = 1
            while (bounds.outWidth / (sample * 2) >= widthPx) sample *= 2
            // Not only by width: a tall page (1920 × 20000) would decode to 150 MB, more than a screen can draw
            while (bounds.outWidth / sample > 4096 || bounds.outHeight / sample > 4096 ||
                   (bounds.outWidth.toLong() / sample) * (bounds.outHeight.toLong() / sample) > 16_000_000L) sample *= 2
            return BitmapFactory.decodeFile(cached.path, BitmapFactory.Options().apply { inSampleSize = sample })
        } catch (_: Exception) {
            return null
        } catch (_: OutOfMemoryError) {  // a picture too big even so: not shown, never a crash
            return null
        }
    }

    /** A PDF's first page as a picture widthPx wide, and how many pages it has, for its card in the chat; null
     *  when it can't be fetched or read. Fetched once into the cache, which opening it later copies from. */
    fun pdfPreview(context: Context, url: String, widthPx: Int): Pair<Bitmap, Int>? {
        val address = resolve(context, url) ?: return null
        return try {
            val file = fetched(context, address) ?: return null
            android.os.ParcelFileDescriptor.open(file, android.os.ParcelFileDescriptor.MODE_READ_ONLY).use { fd ->
                android.graphics.pdf.PdfRenderer(fd).use { pdf ->
                    if (pdf.pageCount == 0) return null
                    pdf.openPage(0).use { page ->
                        val height = (widthPx.toLong() * page.height / page.width.coerceAtLeast(1)).toInt().coerceIn(1, widthPx * 2)
                        val bitmap = Bitmap.createBitmap(widthPx, height, Bitmap.Config.ARGB_8888)
                        bitmap.eraseColor(android.graphics.Color.WHITE)  // pages are paper: transparent would show black
                        page.render(bitmap, null, null, android.graphics.pdf.PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                        bitmap to pdf.pageCount
                    }
                }
            }
        } catch (_: Exception) {
            null
        } catch (_: OutOfMemoryError) {
            null
        }
    }

    /** An agent sent a file while no call was up, or while the call screen wasn't showing. */
    fun notify(context: Context, agent: String, words: String, url: String, name: String, type: String, size: Long,
               session: String = "") {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "Files from your crew", NotificationManager.IMPORTANCE_HIGH))
        // Files from one agent within a minute are one notification, as a messaging app's ("Claude sent you 5
        // pictures"), buzzing once (the user's wish, 2026-11-16: five pictures were five notifications)
        val kept = context.getSharedPreferences("file_notices", Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        val joined = now - kept.getLong("$agent.at", 0) < 60_000
        val count = if (joined) kept.getInt("$agent.count", 1) + 1 else 1
        val pictures = (!joined || kept.getBoolean("$agent.pictures", true)) && type.startsWith("image/")
        val id = if (joined) kept.getInt("$agent.id", url.hashCode()) else url.hashCode()
        kept.edit().putLong("$agent.at", now).putInt("$agent.count", count).putBoolean("$agent.pictures", pictures)
            .putInt("$agent.id", id).apply()
        val who = IncomingActivity.nameOf(agent)
        val open = if (count == 1) Intent(context, MainActivity::class.java).setAction(MainActivity.OPEN_FILE)
            .putExtra(URL, url).putExtra(NAME, name).putExtra(TYPE, type).putExtra(SIZE, size).putExtra(SESSION, session)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        else Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)  // the chat, with its album
        val title = if (count == 1) "$who sent you $name" else "$who sent you $count ${if (pictures) "pictures" else "files"}"
        val text = words.ifEmpty { if (count == 1) "Tap to open it." else "Tap to see them." }
        manager.notify(id, Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_notify)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setNumber(count)
            .setOnlyAlertOnce(true)
            .setContentIntent(PendingIntent.getActivity(context, id, open,
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
            .setAutoCancel(true)
            .build())
    }

    /** A small copy of a picture you send (shared or attached), kept with the chats: the chat shows it later, when
     *  the app that lent the picture may have taken it back. The path, or null for anything but a picture. */
    fun keepThumb(context: Context, uri: Uri): String? {
        if (context.contentResolver.getType(uri)?.startsWith("image/") != true) return null
        return try {
            val picture = context.contentResolver.loadThumbnail(uri, android.util.Size(640, 640), null)
            val out = File(context.filesDir, "chats/thumbs/" + java.util.UUID.randomUUID() + ".jpg")
            out.parentFile?.mkdirs()
            out.outputStream().use { picture.compress(Bitmap.CompressFormat.JPEG, 85, it) }
            out.path
        } catch (_: Exception) {
            null
        }
    }

    /** A shared or picked file's name, as the app it came from gives it. */
    fun nameOf(context: Context, uri: Uri): String = try {
        context.contentResolver.query(uri, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
            if (it.moveToFirst()) it.getString(0) else null
        }
    } catch (_: Exception) {
        null
    } ?: uri.lastPathSegment?.substringAfterLast('/') ?: "file"

    fun sizeText(size: Long): String = when {
        size < 1024 -> "$size bytes"
        size < 1024 * 1024 -> "%.1f KB".format(size / 1024.0)
        size < 1024L * 1024 * 1024 -> "%.1f MB".format(size / 1024.0 / 1024)
        else -> "%.1f GB".format(size / 1024.0 / 1024 / 1024)
    }
}
