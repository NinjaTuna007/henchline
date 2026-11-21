package io.github.ninjatuna007.henchline

import org.json.JSONArray
import org.json.JSONObject
import java.math.BigInteger
import java.security.MessageDigest
import java.util.Base64

/** The bytes a host or the directory checks a signature over, exactly as the laptop's call/canonical.py makes them.
 *  tests/fixtures/host-signatures.json holds the shared vectors; CanonicalTest runs every one of them here.
 *
 *  The traps, each with a vector: empty query items are dropped and `key` alone is `key=`; a malformed `%` escape is
 *  an error, never passed through; escapes come out uppercase and unreserved bytes (A-Z a-z 0-9 - . _ ~) decoded;
 *  `+` is a literal plus (%2B), never a space; %2F stays inside its segment; pairs sort by encoded name, then value;
 *  the request id is lowercase hyphenated UUID text; the body hash is over the exact bytes sent; the last field ends
 *  with a newline too. Sign what OkHttp will actually send: its encodedPath and encodedQuery. */
object Canonical {
    const val PREFIX = "HENCHLINE-SIG-V1"
    const val PAIR_PREFIX = "HENCHLINE-PAIR-V1"
    private const val MAX_SAFE_INTEGER = 9007199254740991L  // 2^53 - 1
    private val BAD_ESCAPE = Regex("%(?![0-9A-Fa-f]{2})")
    private val B64URL = Regex("[A-Za-z0-9_-]+")
    private const val HEX = "0123456789ABCDEF"

    private fun unreserved(b: Int): Boolean =
        b in 'A'.code..'Z'.code || b in 'a'.code..'z'.code || b in '0'.code..'9'.code || b == '-'.code ||
            b == '.'.code || b == '_'.code || b == '~'.code

    /** One path segment or query name/value, percent-normalized. */
    fun component(value: String): String {
        require(!BAD_ESCAPE.containsMatchIn(value)) { "invalid percent encoding" }
        val raw = java.io.ByteArrayOutputStream()
        var i = 0
        while (i < value.length) {
            if (value[i] == '%') {
                raw.write(value.substring(i + 1, i + 3).toInt(16))
                i += 3
            } else {
                val end = if (Character.isHighSurrogate(value[i]) && i + 1 < value.length) i + 2 else i + 1
                raw.write(value.substring(i, end).toByteArray(Charsets.UTF_8))
                i = end
            }
        }
        val out = StringBuilder()
        for (byte in raw.toByteArray()) {
            val b = byte.toInt() and 0xff
            if (unreserved(b)) out.append(b.toChar()) else out.append('%').append(HEX[b shr 4]).append(HEX[b and 15])
        }
        return out.toString()
    }

    fun path(path: String): String {
        require(path.startsWith("/")) { "path must be absolute" }
        return path.split("/").joinToString("/") { component(it) }
    }

    fun query(query: String): String =
        query.split("&").filter { it.isNotEmpty() }.map { item ->
            val at = item.indexOf('=')
            if (at < 0) component(item) to "" else component(item.substring(0, at)) to component(item.substring(at + 1))
        }.sortedWith(compareBy({ it.first }, { it.second })).joinToString("&") { "${it.first}=${it.second}" }

    fun sha256Hex(data: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }

    private fun lines(fields: List<String>): ByteArray {
        require(fields.none { it.contains('\n') || it.contains('\r') }) { "canonical field contains a newline" }
        return (fields.joinToString("\n") + "\n").toByteArray(Charsets.UTF_8)
    }

    /** authorityId: the host_id for a host, the directory_id for the directory. deviceId: this phone's device id
     *  there (a host's own id when a host signs a heartbeat). */
    fun requestBytes(
        apiVersion: String, authorityId: String, scheme: String, authority: String, method: String,
        path: String, query: String, body: ByteArray, deviceId: String, timestamp: Long, nonce: String,
        requestId: String,
    ): ByteArray = lines(listOf(
        PREFIX, apiVersion, authorityId.lowercase(), scheme.lowercase(), authority.lowercase(), method.uppercase(),
        path(path), query(query), sha256Hex(body), deviceId, timestamp.toString(), nonce, requestId.lowercase(),
    ))

    /** Signed with a new key when claiming a pairing code: proof the phone holds it. publicKey: the base64url text
     *  exactly as sent in the claim. authorityId: the host_id, or the directory_id. */
    fun pairingBytes(authorityId: String, nonce: String, algorithm: String, publicKey: String): ByteArray =
        lines(listOf(PAIR_PREFIX, authorityId.lowercase(), nonce, algorithm, publicKey))

    fun b64url(data: ByteArray): String = Base64.getUrlEncoder().withoutPadding().encodeToString(data)

    /** Strict unpadded base64url: one spelling per byte string. */
    fun unb64url(value: String): ByteArray {
        require(B64URL.matches(value)) { "invalid base64url" }
        val data = try { Base64.getUrlDecoder().decode(value) } catch (e: IllegalArgumentException) {
            throw IllegalArgumentException("invalid base64url")
        }
        require(b64url(data) == value) { "invalid base64url" }
        return data
    }

    /** RFC 8785 (JCS) bytes of a parsed JSON value: what a host or the directory signed its descriptor over.
     *  Members sort by UTF-16 code units (Kotlin's own String order); floats and integers past 2^53 are refused. */
    fun jcs(value: Any?): ByteArray = StringBuilder().also { encode(value, it) }.toString().toByteArray(Charsets.UTF_8)

    private fun encode(value: Any?, out: StringBuilder) {
        when (value) {
            null, JSONObject.NULL -> out.append("null")
            is Boolean -> out.append(value)
            is Int, is Long, is Short, is Byte -> {
                val n = (value as Number).toLong()
                require(n in -MAX_SAFE_INTEGER..MAX_SAFE_INTEGER) { "integer too large to sign" }
                out.append(n)
            }
            is BigInteger -> {
                require(value.abs() <= BigInteger.valueOf(MAX_SAFE_INTEGER)) { "integer too large to sign" }
                out.append(value)
            }
            is Double, is Float, is java.math.BigDecimal -> throw IllegalArgumentException("floats are not signed")
            is String -> string(value, out)
            is JSONArray -> {
                out.append('[')
                for (i in 0 until value.length()) { if (i > 0) out.append(','); encode(value.get(i), out) }
                out.append(']')
            }
            is JSONObject -> {
                out.append('{')
                value.keys().asSequence().toList().sorted().forEachIndexed { i, key ->
                    if (i > 0) out.append(',')
                    string(key, out); out.append(':'); encode(value.get(key), out)
                }
                out.append('}')
            }
            else -> throw IllegalArgumentException("cannot sign a ${value::class.java.simpleName}")
        }
    }

    private fun string(value: String, out: StringBuilder) {
        out.append('"')
        var i = 0
        while (i < value.length) {
            val c = value[i]
            when {
                c == '"' -> out.append("\\\"")
                c == '\\' -> out.append("\\\\")
                c == '\b' -> out.append("\\b")
                c == '\u000C' -> out.append("\\f")
                c == '\n' -> out.append("\\n")
                c == '\r' -> out.append("\\r")
                c == '\t' -> out.append("\\t")
                c < ' ' -> out.append("\\u%04x".format(c.code))
                Character.isHighSurrogate(c) -> {
                    require(i + 1 < value.length && Character.isLowSurrogate(value[i + 1])) { "unpaired surrogate" }
                    out.append(c).append(value[i + 1]); i++
                }
                Character.isLowSurrogate(c) -> throw IllegalArgumentException("unpaired surrogate")
                else -> out.append(c)
            }
            i++
        }
        out.append('"')
    }
}
