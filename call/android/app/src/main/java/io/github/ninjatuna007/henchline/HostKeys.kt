package io.github.ninjatuna007.henchline

import android.security.keystore.KeyGenParameterSpec
import androidx.annotation.RequiresApi
import android.security.keystore.KeyProperties
import org.json.JSONObject
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec

/** A key that signs this phone's requests to one host or one directory. */
interface RequestKey {
    val algorithm: String  // "p256", as the host and directory name it
    val publicKey: String  // base64url SubjectPublicKeyInfo DER, as sent when pairing
    fun sign(message: ByteArray): ByteArray
}

/** The phone's keys: one Android Keystore P-256 key per host (and per directory), never exported. P-256, not
 *  Ed25519: Keystore makes P-256 keys on every phone, Ed25519 only with newer secure hardware. The hosts' own
 *  Ed25519 keys are only ever verified here (HostDescriptor), with the platform's Ed25519 (Android 13+). */
object HostKeys {
    private const val STORE = "AndroidKeyStore"

    fun alias(kind: String, id: String): String = "henchline-$kind-${id.lowercase()}"

    /** That host's (or directory's) key: made on first use. Synchronized: two threads asking at once must not
     *  both generate the alias (the second would replace the key the first just paired with). */
    @Synchronized
    fun forAuthority(kind: String, id: String): RequestKey {
        val alias = alias(kind, id)
        val store = KeyStore.getInstance(STORE).apply { load(null) }
        if (!store.containsAlias(alias)) {
            KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, STORE).apply {
                initialize(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
                    .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .build())
            }.generateKeyPair()
        }
        val private = store.getKey(alias, null) as PrivateKey
        val public = store.getCertificate(alias).publicKey.encoded
        return P256Key(private, public)
    }

    fun exists(kind: String, id: String): Boolean =
        KeyStore.getInstance(STORE).apply { load(null) }.containsAlias(alias(kind, id))

    /** Forget that key (after the host acknowledged its revocation, or the user chose to forget it anyway). */
    fun delete(kind: String, id: String) {
        KeyStore.getInstance(STORE).apply { load(null) }.deleteEntry(alias(kind, id))
    }
}

/** A P-256 key: ECDSA with SHA-256, DER signature, as the host verifies it. */
class P256Key(private val private: PrivateKey, publicDer: ByteArray) : RequestKey {
    override val algorithm = "p256"
    override val publicKey: String = Canonical.b64url(publicDer)
    override fun sign(message: ByteArray): ByteArray =
        Signature.getInstance("SHA256withECDSA").run { initSign(private); update(message); sign() }
}

/** A host's (or the directory's) signed descriptor, checked against the key pinned when pairing. The platform has
 *  Ed25519 from Android 13: MultiHost is never available below it. */
@RequiresApi(MultiHost.MIN_SDK)
object HostDescriptor {
    private val ED25519_SPKI = byteArrayOf(0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00)

    /** The platform's provider: "Ed25519" on most, "EdDSA" on some (SelfTest checks whichever this phone has). */
    private fun <T> eitherName(get: (String) -> T): T = try { get("Ed25519") } catch (e: java.security.GeneralSecurityException) { get("EdDSA") }

    fun ed25519Verify(publicKey: ByteArray, signature: ByteArray, message: ByteArray): Boolean = try {
        require(publicKey.size == 32)
        val key = eitherName { KeyFactory.getInstance(it) }.generatePublic(X509EncodedKeySpec(ED25519_SPKI + publicKey))
        eitherName { Signature.getInstance(it) }.run { initVerify(key); update(message); verify(signature) }
    } catch (e: Exception) {
        false
    }

    /** True when descriptor's signature (over its RFC 8785 form without "signature") is by pinnedKey, which must
     *  also be the key it names (keyField: host_signing_key, or directory_signing_key). */
    fun verify(descriptor: JSONObject, pinnedKey: String, keyField: String = "host_signing_key"): Boolean = try {
        val unsigned = JSONObject(descriptor.toString()).apply { remove("signature") }
        descriptor.optString(keyField) == pinnedKey &&
            ed25519Verify(Canonical.unb64url(pinnedKey), Canonical.unb64url(descriptor.getString("signature")),
                Canonical.jcs(unsigned))
    } catch (e: Exception) {
        false
    }
}
