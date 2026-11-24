package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Test

/** Who the call screen says is listening, and the talk button's colour on the phone's own ears. */
class CallEarsLookTest {
    @Test fun theListenerIsNamed() {
        assertEquals("Phone listening", PhoneEar.listener(true, "aquabox"))
        assertEquals("Aquabox listening", PhoneEar.listener(false, "aquabox"))
        assertEquals("Your computer listening", PhoneEar.listener(false, HostPush.UNNAMED))
        assertEquals("Your computer listening", PhoneEar.listener(false, " "))  // an unlabelled paired one too
    }

    @Test fun theComputersModeWhileItHears() {
        assertEquals("waiting", PhoneEar.talkMode("waiting", phoneEars = false, hearing = true, buttonOnly = false))
        assertEquals("transcribing", PhoneEar.talkMode("transcribing", phoneEars = false, hearing = false, buttonOnly = false))
    }

    @Test fun thePhonesOwnStateWhileItHears() {
        // The computer's listener sits idle ("waiting") then: never a permanent grey
        assertEquals("listening", PhoneEar.talkMode("waiting", phoneEars = true, hearing = false, buttonOnly = false))
        assertEquals("recording", PhoneEar.talkMode("waiting", phoneEars = true, hearing = true, buttonOnly = false))
        assertEquals("recording", PhoneEar.talkMode("waiting", phoneEars = true, hearing = true, buttonOnly = true))
        assertEquals("waiting", PhoneEar.talkMode("listening", phoneEars = true, hearing = false, buttonOnly = true))
    }

    @Test fun theScreensHoldAndTheToggleDontUndoEachOther() {
        val hold = TalkHold()
        assertEquals(true, hold.toggle(true, 100))  // the earbuds start a turn: one press
        assertEquals(null, hold.screen(true, 200))  // the screen held too: no second press
        assertEquals(null, hold.screen(false, 300))  // let go: the earbuds' turn goes on
        assertEquals(true, hold.held)
        assertEquals(100L, hold.since)
        assertEquals(false, hold.toggle(false, 400))  // the earbuds end it: one release
        assertEquals(false, hold.held)
        assertEquals(null, hold.since)
    }

    @Test fun anEarbudTapDuringAScreenHoldDoesntEndIt() {
        val hold = TalkHold()
        assertEquals(true, hold.screen(true, 50))
        assertEquals(null, hold.toggle(true, 60))
        assertEquals(null, hold.toggle(false, 70))
        assertEquals(50L, hold.since)
        assertEquals(false, hold.screen(false, 80))
        assertEquals(null, hold.screen(false, 90))  // a second release changes nothing
    }
}
