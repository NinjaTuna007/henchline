package io.github.ninjatuna007.henchline

import android.content.Context
import android.os.Build
import okhttp3.Interceptor
import okhttp3.Request
import okhttp3.Response
import okio.HashingSink
import okio.blackholeSink
import okio.buffer
import org.json.JSONObject

/**
 * The app's requests to a computer's own routes (texts, agents, the call, shares...), signed for a paired
 * computer that offers "legacy_routes": it then knows this phone by its key, removing the phone cuts it off there,
 * and the computer may refuse anything unsigned (voice-call --signed-only). Every request still carries its Origin,
 * so a computer that doesn't check signatures sees it exactly as before.
 *
 * Installed on each OkHttp client that talks to a computer, as a network interceptor: each hop is signed for its own
 * address, so a redirect never carries a signature somewhere else. With multi-host off (or before Android 13), for any
 * address that isn't a paired computer offering it, or while that computer's removal is pending, a request goes out
 * unchanged. No dedupe on these routes: a retry is signed afresh, and runs again, as an unsigned retry always has.
 */
object LegacySigner : Interceptor {
    @Volatile private var context: Context? = null

    /** Once, as the app starts (HenchlineApp): where the paired computers are kept. */
    fun attach(context: Context) { this.context = context.applicationContext }

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val ctx = context
        if (ctx == null || Build.VERSION.SDK_INT < MultiHost.MIN_SDK || !MultiHost.enabled(ctx)) return chain.proceed(request)
        return chain.proceed(LegacySigning.signOrAsIs(request, HostRegistry.of(ctx).all(), keyFor = { host ->
            if (HostKeys.exists("host", host.hostId)) HostKeys.forAuthority("host", host.hostId) else null  // never a new key
        }))
    }
}

/** A request body that hashes itself for signing, instead of being written out an extra time: an upload, whose
 *  writing shows progress and whose source may not be readable twice. */
interface SelfHashing {
    /** Hex SHA-256 of exactly what writeTo will then send (or writeTo fails). */
    fun sha256Hex(): String
}

/** An upload that couldn't be made ready to sign (its hashing pass failed: no room for its copy, a broken pipe).
 *  It fails the request: never sent unsigned instead, since its source may already be used up. */
class UploadNotPrepared(why: String, cause: Throwable? = null) : java.io.IOException(why, cause)

/** The signing itself, apart from Android (tested on the JVM). */
object LegacySigning {
    /** The request signed, or as it was when signing it failed (logged): it then goes with its Origin, as before
     *  signing existed. Except an upload whose hashing pass failed: that fails (UploadNotPrepared). */
    fun signOrAsIs(request: Request, hosts: List<Host>, keyFor: (Host) -> RequestKey?,
                   clock: () -> Long = { System.currentTimeMillis() / 1000 }): Request = try {
        sign(request, hosts, keyFor, clock) ?: request
    } catch (e: UploadNotPrepared) {
        throw e
    } catch (e: Exception) {
        try { android.util.Log.w("LegacySigner", "not signed: ${e.javaClass.simpleName}") } catch (_: Throwable) {}
        request
    }

    /** Uploads the computer reads as they stream in: it checks them against the digest declared beside the signature. */
    val STREAMED = setOf("/share/file", "/voice-note")

    /** The request signed for the paired computer it goes to, or null when it isn't to be signed: not https, not to
     *  a paired computer offering legacy_routes, its removal pending, no key for it, the signed API's own paths
     *  (signed by HostClient), already signed, or a body that can be read only once (it is hashed first). */
    fun sign(request: Request, hosts: List<Host>, keyFor: (Host) -> RequestKey?,
             clock: () -> Long = { System.currentTimeMillis() / 1000 }): Request? {
        val url = request.url
        if (!url.isHttps || request.header("X-Henchline-Signature") != null) return null
        val path = url.encodedPath
        if (path.startsWith("/api/v1/") || path.startsWith("/.well-known/")) return null
        val origin = url.newBuilder().username("").password("").encodedPath("/").query(null).fragment(null).build().toString()
        val host = hosts.firstOrNull { it.baseUrl == origin } ?: return null
        if (host.revocationPending || !offersLegacyRoutes(host)) return null
        val body = request.body
        if (body != null && (body.isOneShot() || body.isDuplex())) return null
        val key = keyFor(host) ?: return null
        val digest = if (body == null) Canonical.sha256Hex(ByteArray(0))
        else if (body is SelfHashing) try { body.sha256Hex() } catch (e: Exception) {
            throw e as? UploadNotPrepared ?: UploadNotPrepared("couldn't prepare it to send (${e.message ?: e.javaClass.simpleName})", e)
        }
        else HashingSink.sha256(blackholeSink()).let { hashing ->
            val sink = hashing.buffer()
            body.writeTo(sink)
            sink.flush()
            hashing.hash.hex()
        }
        val headers = signedHeaders(host, key, request.method, url, digest, timestamp = clock())
        return request.newBuilder().apply {
            headers.forEach { (k, v) -> header(k, v) }
            if (request.method == "POST" && path in STREAMED) header("X-Henchline-Content-SHA256", digest)
        }.build()
    }

    private fun offersLegacyRoutes(host: Host): Boolean = try {
        JSONObject(host.descriptor).optJSONObject("capabilities")?.optBoolean("legacy_routes", false) ?: false
    } catch (e: Exception) { false }
}
