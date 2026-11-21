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

/** Signed requests to one paired computer (the host's v1 API). */
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
        val named = answer.json().optString("host_id", "")
        if (answer.code in 200..299 && named.isNotEmpty() && named != host.hostId) {
            throw java.io.IOException("the computer answered as another host")  // never trusted, never shown
        }
        return answer
    }

    /** Before this phone forgets the host: the host stops trusting this phone's key. True when it has (or already
     *  had); false when it couldn't be asked (offline), so the removal stays pending. */
    fun revokeSelf(): Boolean = try {
        val answer = call("DELETE", listOf("api", "v1", "devices", host.deviceId))
        answer.code == 200 || answer.errorCode == "device_revoked"
    } catch (e: java.io.IOException) {
        false
    }
}
