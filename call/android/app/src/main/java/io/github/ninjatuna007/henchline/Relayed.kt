package io.github.ninjatuna007.henchline

/**
 * Words typed into an agent's terminal by Henchline, not by you: a helper's report ("[henchline · report] …",
 * "[henchline · report · from tests] …"), the brief a new helper starts with ("[henchline · deploy] …") or a
 * reminder ("[henchline · scheduled] …", "[henchline · scheduled · from claude in henchline] …"). The computer
 * mirrors whatever is typed into a chat as yours, so the phone tells these apart by their tag and draws them as
 * from that helper or Henchline, not with your face.
 */
data class Relayed(val kind: Kind, val sender: String, val text: String) {
    enum class Kind { REPORT, SCHEDULED, DEPLOY }

    /** The name over the bubble: the helper's (when the tag names it), else what it is. */
    val label: String
        get() = when (kind) {
            Kind.REPORT -> if (sender.isEmpty()) "Helper report" else "Report from ${sender.replaceFirstChar { it.uppercase() }}"
            Kind.DEPLOY -> if (sender.isEmpty()) "Brief from Henchline" else "Brief from ${sender.replaceFirstChar { it.uppercase() }}"
            Kind.SCHEDULED -> if (sender.isEmpty()) "Henchline reminder" else "Reminder from ${sender.replaceFirstChar { it.uppercase() }}"
        }

    /** Its face: a helper's in the crew hard hat (its own agent's when the tag names one), a reminder's clock. */
    val avatar: Int
        get() = when (kind) {
            Kind.SCHEDULED -> R.drawable.avatar_reminders
            Kind.DEPLOY -> if (sender.isEmpty()) R.drawable.avatar_handsfree else MainActivity.avatarOf(sender, "manager")
            Kind.REPORT -> MainActivity.avatarOf(sender.ifEmpty { "handsfree" }, "crew")
        }

    companion object {
        // The marks the computer puts before a mirrored line (typed in the terminal, heard on the call)
        private val LEAD = Regex("^\\s*(?:(?:⌨|🎙)\\uFE0F?\\s*)?")
        private val TAG = Regex("^\\[henchline\\s*·\\s*(report|scheduled|deploy)((?:\\s*·[^\\]]*)?)\\]\\s*", RegexOption.IGNORE_CASE)
        private val FROM = Regex("·\\s*from\\s+([^·\\]]+)", RegexOption.IGNORE_CASE)

        /** The line's sender and words without the tag, or null for an ordinary line of yours. */
        fun of(line: String): Relayed? {
            val rest = line.replaceFirst(LEAD, "")
            val tag = TAG.find(rest) ?: return null
            val kind = Kind.valueOf(tag.groupValues[1].uppercase())
            val sender = FROM.find(tag.groupValues[2])?.groupValues?.get(1)?.trim() ?: ""
            return Relayed(kind, sender, rest.substring(tag.range.last + 1).trim())
        }
    }
}
