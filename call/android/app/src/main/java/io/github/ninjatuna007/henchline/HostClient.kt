package io.github.ninjatuna007.henchline

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.security.SecureRandom
import java.util.UUID
import java.util.concurrent.TimeUnit

/** The phone's clock is too far from the computer's for a signed request (the host allows 120 s either way). */
class ClockSkew(val seconds: Long, label: String) : Exception(
    "This phone's clock is ${kotlin.math.abs(seconds)} s ${if (seconds > 0) "behind" else "ahead of"} $label's. " +
        "Turn on automatic date and time in the phone's settings (and check the computer's clock).")

/** A control for something the computer doesn't offer: disabled, never tried. */
class CapabilityMissing(val capability: String, label: String) : Exception("$label doesn't offer $capability.")

/** The computer answered with a description it didn't sign with its pinned key, an older one, or another host's. */
class DescriptorRefused(why: String) : Exception(why)

data class HttpAnswer(val code: Int, val body: String) {
    fun json(): JSONObject = try { JSONObject(body) } catch (e: Exception) { JSONObject() }
    val errorCode: String get() = json().optJSONObject("error")?.optString("code") ?: ""
}

/** How requests leave the phone; tests stand in a fake host. */
fun interface Http {
    fun send(method: String, url: HttpUrl, body: ByteArray?, headers: Map<String, String>): HttpAnswer
}

/** HTTPS with the platform's certificate checks, and no redirects: a signed request goes exactly where it was
 *  signed for, or nowhere. */
object OkHttpTransport : Http {
    private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(10, TimeUnit.SECONDS).readTimeout(30, TimeUnit.SECONDS).build()

    override fun send(method: String, url: HttpUrl, body: ByteArray?, headers: Map<String, String>): HttpAnswer {
        require(url.isHttps) { "a computer is only reached over https" }
        val request = Request.Builder().url(url).apply {
            headers.forEach { (k, v) -> header(k, v) }
            val payload = body?.toRequestBody("application/json".toMediaType())
            method(method, payload ?: if (method == "POST" || method == "PUT") ByteArray(0).toRequestBody() else null)
        }.build()
        client.newCall(request).execute().use { return HttpAnswer(it.code, it.body?.string() ?: "") }
    }
}

/** Signed requests to one paired computer (the host's v1 API). Multi-host only: Android 13+. */
@androidx.annotation.RequiresApi(MultiHost.MIN_SDK)
class HostClient(
    private val host: Host,
    private val key: RequestKey,
    private val http: Http = OkHttpTransport,
    private val clock: () -> Long = { System.currentTimeMillis() / 1000 },
) {
    /** segments: the path below the host's base URL, one element per segment, not yet encoded. What is signed is
     *  what OkHttp will send: the built URL's encodedPath and encodedQuery. */
    fun call(method: String, segments: List<String>, query: List<Pair<String, String>> = emptyList(),
             body: ByteArray? = null, requestId: String = UUID.randomUUID().toString()): HttpAnswer {
        val url = host.baseUrl.toHttpUrl().newBuilder().apply {
            segments.forEach { addPathSegment(it) }
            query.forEach { (k, v) -> addQueryParameter(k, v) }
        }.build()
        val sent = body ?: ByteArray(0)
        val timestamp = clock()
        val nonce = Canonical.b64url(ByteArray(16).also { SecureRandom().nextBytes(it) })
        val message = Canonical.requestBytes("1.0", host.hostId, "https", host.tlsAuthority, method,
            url.encodedPath, url.encodedQuery ?: "", sent, host.deviceId, timestamp, nonce, requestId)
        val headers = mapOf(
            "X-Henchline-Api-Version" to "1.0", "X-Henchline-Host" to host.hostId,
            "X-Henchline-Device" to host.deviceId, "X-Henchline-Timestamp" to timestamp.toString(),
            "X-Henchline-Nonce" to nonce, "X-Henchline-Request-Id" to requestId,
            "X-Henchline-Signature" to Canonical.b64url(key.sign(message)))
        val answer = http.send(method, url, body, headers)
        val error = answer.json().optJSONObject("error")
        if (answer.code == 401 && error?.optString("code") == "timestamp_out_of_window" && error.has("server_time")) {
            throw ClockSkew(error.getLong("server_time") - timestamp, host.label)
        }
        val named = answer.json().optString("host_id", "")
        if (answer.code in 200..299 && named.isNotEmpty() && named != host.hostId) {
            throw java.io.IOException("the computer answered as another host")  // never trusted, never shown
        }
        return answer
    }

    /** What the last verified descriptor offers. Unknown names are off: a capability is never inferred. */
    fun offers(capability: String): Boolean =
        try { JSONObject(host.descriptor).optJSONObject("capabilities")?.optBoolean(capability, false) ?: false } catch (e: Exception) { false }

    /** Before a control acts: CapabilityMissing when the computer doesn't offer it. */
    fun require(capability: String) {
        if (!offers(capability)) throw CapabilityMissing(capability, host.label)
    }

    /** The computer's current signed descriptor (GET /api/v1/capabilities): signed by the pinned key, for this host,
     *  and never an older sequence. A new address in it is accepted (it is signed by the pinned key). Returns the
     *  host to save. */
    fun refresh(): Host {
        val answer = call("GET", listOf("api", "v1", "capabilities"))
        if (answer.code != 200) throw java.io.IOException("${host.label} answered ${answer.code} ${answer.errorCode}")
        val descriptor = answer.json().optJSONObject("descriptor") ?: throw DescriptorRefused("No description in the answer.")
        if (!HostDescriptor.verify(descriptor, host.hostKey)) throw DescriptorRefused("${host.label}'s description isn't signed by its pinned key.")
        if (descriptor.optString("host_id") != host.hostId) throw DescriptorRefused("Another computer's description came back.")
        val before = try { JSONObject(host.descriptor).optLong("descriptor_seq", 0) } catch (e: Exception) { 0L }
        val seq = descriptor.optLong("descriptor_seq", 0)
        if (seq < before) throw DescriptorRefused("${host.label} sent an older description ($seq < $before): ignored.")
        return host.copy(label = descriptor.optString("label", host.label), baseUrl = descriptor.getString("base_url"),
            tlsAuthority = descriptor.getString("tls_authority"), descriptor = descriptor.toString())
    }

    /** Before this phone forgets the host: the host stops trusting this phone's key. True when it has (or already
     *  had); false when it couldn't be asked (offline), so the removal stays pending. */
    fun revokeSelf(): Boolean = try {
        val answer = call("DELETE", listOf("api", "v1", "devices", host.deviceId))
        // device_revoked: already revoked there. Unknown device: the host has no such device any more (removed by
        // its owner, or its database replaced): nothing there trusts this key either.
        answer.code == 200 || answer.errorCode == "device_revoked" ||
            (answer.code == 401 && answer.json().optJSONObject("error")?.optString("reason") == "unknown_device")
    } catch (e: java.io.IOException) {
        false
    }
}
