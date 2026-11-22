package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** A computer's chats named as the laptop's are, and its Reminders read-only. */
class HostChatRulesTest {
    @Test fun aComputersOwnNoticesReadAsRemindersAndAScriptsByItsName() {
        assertEquals("Reminders · Desk", HostChatRules.title("reminders-reminders", "Desk"))  // its own notices
        assertEquals("Aquabase · Desk", HostChatRules.title("reminders-aquabase", "Desk"))    // handsfree text --from Aquabase
        assertEquals("Backup job", HostChatRules.name("reminders-backup_job", "reminders-backup_job"))
        assertEquals("Reminders", HostChatRules.remindersFrom("reminders-"))
        assertEquals("Nightly.sh", HostChatRules.remindersFrom("reminders-nightly.sh"))
    }

    @Test fun otherChatsKeepTheirNameOrKey() {
        assertEquals("Desk", HostChatRules.title(null, "Desk"))
        assertEquals("cursor-1 · Desk", HostChatRules.title("cursor-1", "Desk"))
        assertEquals("Cursor, henchline", HostChatRules.name("cursor-1", "Cursor, henchline"))
        assertEquals("remindersx", HostChatRules.name("remindersx"))  // only the reminders- keys
    }

    @Test fun remindersTakeNoRepliesOrCalls() {
        assertFalse(HostChatRules.takesReplies("reminders-reminders"))
        assertTrue(HostChatRules.takesReplies("cursor-1"))
    }
}
