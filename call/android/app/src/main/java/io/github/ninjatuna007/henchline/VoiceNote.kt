package io.github.ninjatuna007.henchline

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * A voice message off a call (the user's wish, 2026-11-16): held mic button records, letting go sends the WAV to
 * the laptop, which transcribes it with Whisper and types it to the chat's agent. 16 kHz mono, as Whisper hears.
 */
class VoiceNote {
    private var record: AudioRecord? = null
    private var reader: Thread? = null
    private val pcm = ByteArrayOutputStream()
    @Volatile private var going = false
    val started = System.currentTimeMillis()

    @SuppressLint("MissingPermission")  // asked for before recording (MainActivity)
    fun start(): Boolean {
        val size = maxOf(AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT), RATE)
        val recorder = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, size)
        } catch (_: Exception) { return false }
        if (recorder.state != AudioRecord.STATE_INITIALIZED) { recorder.release(); return false }
        record = recorder
        going = true
        recorder.startRecording()
        reader = Thread {
            val chunk = ByteArray(size)
            while (going) {
                val n = recorder.read(chunk, 0, chunk.size)
                if (n > 0) synchronized(pcm) { if (pcm.size() < MAX) pcm.write(chunk, 0, n) else going = false }
            }
        }.also { it.start() }
        return true
    }

    /** Stops; the recording as a WAV, or null when cancelled. */
    fun stop(keep: Boolean): ByteArray? {
        going = false
        reader?.join(500)
        record?.run { try { stop() } catch (_: Exception) {}; release() }
        record = null
        if (!keep) return null
        val data = synchronized(pcm) { pcm.toByteArray() }
        val head = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray()); putInt(36 + data.size); put("WAVE".toByteArray())
            put("fmt ".toByteArray()); putInt(16); putShort(1); putShort(1); putInt(RATE); putInt(RATE * 2)
            putShort(2); putShort(16)
            put("data".toByteArray()); putInt(data.size)
        }
        return head.array() + data
    }

    companion object {
        const val RATE = 16000
        const val LONGEST = 300  // seconds: then it is sent (MainActivity's clock)
        private const val MAX = RATE * 2 * LONGEST  // the laptop takes up to 20 MiB

        /** Sends it to the laptop for that agent and waits for what was heard: ("", words) once typed to the
         *  agent, or (why not, "") when it failed. Off the main thread. */
        fun send(context: Context, session: String, wav: ByteArray): Pair<String, String> = try {
            val origin = Files.origin(context) ?: throw IllegalStateException("no computer address set")
            val nid = Files.client.newCall(Request.Builder().url("$origin/voice-note?session=" + android.net.Uri.encode(session))
                .header("Origin", origin).post(wav.toRequestBody("audio/wav".toMediaType())).build()).execute().use { answer ->
                val body = try { JSONObject(answer.body?.string() ?: "") } catch (_: Exception) { JSONObject() }
                body.optString("id").ifEmpty { throw IllegalStateException(body.optString("note", "${HostPush.computerName(context)} said ${answer.code}")) }
            }
            var result: Pair<String, String>? = null
            val until = System.currentTimeMillis() + 180_000  // Whisper is busy with a call turn at worst
            while (result == null && System.currentTimeMillis() < until) {
                Thread.sleep(1000)
                result = try {
                    Files.client.newCall(Request.Builder().url("$origin/voice-note/$nid").header("Origin", origin).build())
                        .execute().use { answer ->
                            val body = JSONObject(answer.body?.string() ?: "{}")
                            when (body.optString("status")) {
                                "done" -> "" to body.optString("text")
                                "failed" -> body.optString("note", "it couldn't be heard").ifEmpty { "it couldn't be heard" } to ""
                                else -> null
                            }
                        }
                } catch (_: Exception) { null }  // a blip: ask again
            }
            result ?: ("${HostPush.computerName(context)} hasn't transcribed it yet" to "")
        } catch (e: Exception) {
            "Not sent: ${e.message ?: "${HostPush.computerName(context)} couldn't be reached"} (is Tailscale on?)" to ""
        }
    }
}
