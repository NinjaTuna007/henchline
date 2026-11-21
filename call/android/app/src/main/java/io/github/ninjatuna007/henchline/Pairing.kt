package io.github.ninjatuna007.henchline

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONObject
import java.util.UUID

class PairingError(message: String) : Exception(message)

/** A computer found at an address, before pairing: what the user checks against that computer's screen. */
data class Candidate(val origin: HttpUrl, val descriptor: JSONObject) {
    val hostId: String = descriptor.getString("host_id")
    val label: String = descriptor.getString("label")
    val hostKey: String = descriptor.getString("host_signing_key")
    val fingerprint: String = Canonical.fingerprint(Canonical.unb64url(hostKey))
}

/** Pairing with a computer by typing: its address, then (after comparing the fingerprint) the one-time code that
 *  `voice-call pair` shows there. Trust comes from that comparison: validated TLS alone pins nothing. */
@androidx.annotation.RequiresApi(MultiHost.MIN_SDK)
class Pairing(private val http: Http = OkHttpTransport) {
    /** "laptop.tailnet.ts.net" or a full https URL: the https origin, with a trailing slash. */
    fun origin(typed: String): HttpUrl {
        val text = typed.trim().let { if (it.contains("://")) it else "https://$it" }
        val url = text.toHttpUrlOrNull() ?: throw PairingError("That isn't an address.")
        if (!url.isHttps) throw PairingError("A computer is paired over https only.")
        return url.newBuilder().encodedPath("/").query(null).fragment(null).build()
    }

    fun lookUp(origin: HttpUrl): Candidate {
        val url = origin.newBuilder().addPathSegments(".well-known/henchline/v1/host").build()
        val answer = try { http.send("GET", url, null, emptyMap()) } catch (e: Exception) {
            throw PairingError("Couldn't reach ${origin.host}: ${e.message ?: e.javaClass.simpleName}")
        }
        if (answer.code != 200) throw PairingError("${origin.host} answered ${answer.code}: is Henchline's host API on there?")
        val descriptor = answer.json().optJSONObject("descriptor") ?: throw PairingError("No Henchline host at ${origin.host}.")
        val candidate = try { Candidate(origin, descriptor) } catch (e: Exception) {
            throw PairingError("${origin.host} sent an unreadable description.")
        }
        check(descriptor, candidate.hostKey, origin, null)
        return candidate
    }

    /** Claim the code with this phone's new key for that host; returns the paired host to save. */
    fun claim(candidate: Candidate, typedCode: String, key: RequestKey, phoneLabel: String): Host {
        val code = try { Canonical.pairingCode(typedCode) } catch (e: IllegalArgumentException) {
            throw PairingError("The code is the letters and digits the computer shows.")
        }
        val proof = key.sign(Canonical.pairingBytes(candidate.hostId, code, key.algorithm, key.publicKey))
        val body = JSONObject().put("nonce", code).put("algorithm", key.algorithm).put("public_key", key.publicKey)
            .put("proof", Canonical.b64url(proof)).put("label", phoneLabel).toString().toByteArray()
        val url = candidate.origin.newBuilder().addPathSegments("api/v1/pairing/claim").build()
        val answer = try { http.send("POST", url, body, emptyMap()) } catch (e: Exception) {
            throw PairingError("Couldn't reach ${candidate.origin.host}: ${e.message ?: e.javaClass.simpleName}")
        }
        when {
            answer.code == 201 -> {}
            answer.errorCode == "pairing_expired" -> throw PairingError("That code has expired or was used. Show a new one on the computer: voice-call pair")
            answer.errorCode == "rate_limited" -> throw PairingError("Too many tries just now. Wait a minute and try again.")
            else -> throw PairingError("Not paired (${answer.code} ${answer.errorCode}).")
        }
        val reply = answer.json()
        val deviceId = reply.optString("device_id")
        val descriptor = reply.optJSONObject("descriptor") ?: throw PairingError("The computer sent no description.")
        if (reply.optString("host_id") != candidate.hostId || runCatching { UUID.fromString(deviceId) }.isFailure ||
            deviceId != deviceId.lowercase()) throw PairingError("The computer's answer doesn't match it.")
        check(descriptor, candidate.hostKey, candidate.origin, candidate.hostId)
        return Host(candidate.hostId, descriptor.getString("label"), descriptor.getString("base_url"),
            descriptor.getString("tls_authority"), candidate.hostKey, deviceId, descriptor.toString(),
            System.currentTimeMillis())
    }

    /** Signed by the key, about the address it was fetched from, and (after pairing) the same host. */
    private fun check(descriptor: JSONObject, key: String, origin: HttpUrl, hostId: String?) {
        if (descriptor.optString("schema") != "henchline.host/v1") throw PairingError("Not a Henchline host description.")
        if (!HostDescriptor.verify(descriptor, key)) throw PairingError("The computer's description isn't signed by its key.")
        val authority = if (origin.port == 443) origin.host else "${origin.host}:${origin.port}"
        if (descriptor.optString("base_url") != origin.toString() || descriptor.optString("tls_authority") != authority.lowercase()) {
            throw PairingError("That computer says it lives at ${descriptor.optString("base_url")}, not ${origin}.")
        }
        if (hostId != null && descriptor.optString("host_id") != hostId) throw PairingError("Another computer answered.")
    }
}
