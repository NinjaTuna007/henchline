package io.github.ninjatuna007.henchline

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** One computer this phone is paired with. Its signing key is pinned from the pairing (and the fingerprint the
 *  user compared); the descriptor is the last one it signed. revocationPending: the user removed it while it
 *  was unreachable, so this phone is still authorized there and keeps its key until the host acknowledges. */
data class Host(
    val hostId: String,
    val label: String,
    val baseUrl: String,
    val tlsAuthority: String,
    val hostKey: String,
    val deviceId: String,
    val descriptor: String,
    val pairedAt: Long,
    val revocationPending: Boolean = false,
) {
    val fingerprint: String get() = Canonical.fingerprint(Canonical.unb64url(hostKey))

    fun toJson(): JSONObject = JSONObject().put("host_id", hostId).put("label", label).put("base_url", baseUrl)
        .put("tls_authority", tlsAuthority).put("host_key", hostKey).put("device_id", deviceId)
        .put("descriptor", descriptor).put("paired_at", pairedAt).put("revocation_pending", revocationPending)

    companion object {
        fun fromJson(o: JSONObject) = Host(o.getString("host_id"), o.getString("label"), o.getString("base_url"),
            o.getString("tls_authority"), o.getString("host_key"), o.getString("device_id"), o.getString("descriptor"),
            o.getLong("paired_at"), o.optBoolean("revocation_pending"))
    }
}

/** The paired computers, in one small file written whole (temp file, then rename). Order: as paired. */
class HostRegistry(private val file: File) {
    @Synchronized
    fun all(): List<Host> = try {
        val array = JSONArray(file.readText())
        (0 until array.length()).map { Host.fromJson(array.getJSONObject(it)) }
    } catch (e: java.io.FileNotFoundException) {
        emptyList()
    }

    fun get(hostId: String): Host? = all().firstOrNull { it.hostId == hostId }

    @Synchronized
    fun put(host: Host) = write(all().filter { it.hostId != host.hostId } + host)

    @Synchronized
    fun remove(hostId: String) = write(all().filter { it.hostId != hostId })

    private fun write(hosts: List<Host>) {
        val temp = File(file.parentFile, "${file.name}.tmp")
        temp.writeText(JSONArray().apply { hosts.forEach { put(it.toJson()) } }.toString())
        if (!temp.renameTo(file)) throw java.io.IOException("could not save $file")
    }

    companion object {
        fun of(context: Context) = HostRegistry(File(context.filesDir, "hosts.json"))
    }
}
