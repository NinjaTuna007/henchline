package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** A computer's chats named as the laptop's are, and its Reminders read-only. */
class HostChatRulesTest {
    @Test fun aScriptsOrTheComputersOwnTextsReadAsReminders() {
        assertEquals("Reminders · Desk", HostChatRules.title("reminders-reminders", "Desk"))  // its own notices
        assertEquals("Reminders · Desk", HostChatRules.title("reminders-desk", "Desk"))       // handsfree text --from Desk
        assertEquals("Reminders", HostChatRules.name("reminders-backup", "reminders-backup"))
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
