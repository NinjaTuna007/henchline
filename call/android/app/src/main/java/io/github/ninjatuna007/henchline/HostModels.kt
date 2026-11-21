package io.github.ninjatuna007.henchline

import android.net.Uri

/** A chat is named by its computer and its session there: two computers' "claude-1" are two chats. */
data class ChatKey(val hostId: String, val sessionId: String) {
    /** henchline://chat?host_id=<uuid>&session_id=<base64url of the UTF-8 session id>: navigation only. */
    fun link(): String = "henchline://chat?host_id=$hostId&session_id=" + Canonical.b64url(sessionId.toByteArray(Charsets.UTF_8))

    companion object {
        private val UUID_TEXT = Regex("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

        /** A session id as the host API accepts it: 1..256 characters, none of them control characters. */
        fun validSession(session: String) = session.length in 1..256 && session.none { it < ' ' || it == '\u007f' }

        /** The link's parts, or null for anything else. Untrusted: it only says which chat to open; the host must be
         *  one this phone paired, and nothing is sent because of the link. sessionId is null: that computer's list. */
        fun parse(hostId: String?, sessionB64: String?): Pair<String, String?>? {
            if (hostId == null || !UUID_TEXT.matches(hostId)) return null
            if (sessionB64.isNullOrEmpty()) return hostId to null
            val session = try { String(Canonical.unb64url(sessionB64), Charsets.UTF_8) } catch (e: IllegalArgumentException) { return null }
            if (!validSession(session) || Canonical.b64url(session.toByteArray(Charsets.UTF_8)) != sessionB64) return null
            return hostId to session
        }

        fun parse(uri: Uri?): Pair<String, String?>? =
            if (uri == null || uri.scheme != "henchline" || uri.host != "chat") null
            else parse(uri.getQueryParameter("host_id"), uri.getQueryParameter("session_id"))
    }
}
