package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class RelayedTest {
    @Test fun aReportTypedInTheTerminalIsTheHelpers() {
        val r = Relayed.of("⌨ [henchline · report] tests pass, 12 files")!!
        assertEquals(Relayed.Kind.REPORT, r.kind)
        assertEquals("", r.sender)
        assertEquals("tests pass, 12 files", r.text)
        assertEquals("Helper report", r.label)
    }

    @Test fun aReportNamingItsHelper() {
        val r = Relayed.of("[henchline · report · from tests] done\nsecond line")!!
        assertEquals("tests", r.sender)
        assertEquals("done\nsecond line", r.text)
        assertEquals("Report from Tests", r.label)
    }

    @Test fun remindersWithAndWithoutTheirSetter() {
        val plain = Relayed.of("[henchline · scheduled] stand up")!!
        assertEquals(Relayed.Kind.SCHEDULED, plain.kind)
        assertEquals("Henchline reminder", plain.label)
        assertEquals("stand up", plain.text)
        val from = Relayed.of("🎙 [henchline · scheduled · from claude in henchline] check the build")!!
        assertEquals("claude in henchline", from.sender)
        assertEquals("Reminder from Claude in henchline", from.label)
        assertEquals("check the build", from.text)
    }

    @Test fun yourOwnWordsStayYours() {
        assertNull(Relayed.of("⌨ fix the tests"))
        assertNull(Relayed.of("[hands-free · call] hello"))
        assertNull(Relayed.of("please read [henchline · report] later"))
        assertNull(Relayed.of("[henchline · reported] no"))
    }

    @Test fun aNewHelpersBriefIsHenchlines() {
        val r = Relayed.of("⌨ [henchline · deploy] You were started by another agent. Report back.\n\nrun the tests")!!
        assertEquals(Relayed.Kind.DEPLOY, r.kind)
        assertEquals("Brief from Henchline", r.label)
        assertEquals("You were started by another agent. Report back.\n\nrun the tests", r.text)
        assertEquals("Brief from Claude", Relayed.of("[henchline · deploy · from claude] go")!!.label)
    }

    @Test fun theReportShapeTheComputerSends() {
        val r = Relayed.of("⌨ [henchline · report · from claude-1] all green")!!
        assertEquals(Relayed.Kind.REPORT, r.kind)
        assertEquals("claude-1", r.sender)
        assertEquals("Report from Claude-1", r.label)
        assertEquals("all green", r.text)
    }
}
