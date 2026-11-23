package io.github.ninjatuna007.henchline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The phone's speech bench scores candidates the way the laptop's were scored, against the same bar. */
class SpeechScoreTest {
    @Test fun referencesAreReadFromTheSetsListing() {
        assertEquals(listOf("a.wav" to "Ask Claude to run the tests.", "b.wav" to "Open tmux"),
            SpeechScore.refs("# the strict set\na.wav\tAsk Claude to run the tests.\r\n\nb.wav\t Open tmux \nno tab here\n"))
    }

    @Test fun wordErrorsCountEditsOverReferenceWords() {
        assertEquals(listOf("don't", "stop", "3"), SpeechScore.words("Don't STOP, 3!"))
        assertEquals(0, SpeechScore.edits(SpeechScore.words("run the tests"), SpeechScore.words("Run the tests.")))
        assertEquals(2, SpeechScore.edits(listOf("ask", "claude", "now"), listOf("ask", "cloud")))  // one wrong, one missing
        assertEquals(0.25, SpeechScore.wer(listOf("a b c d" to "a b c e")), 1e-9)
        assertEquals(0.0, SpeechScore.wer(emptyList()), 1e-9)
    }

    @Test fun namesAreCountedAsSaidAndAsHeard() {
        val names = listOf("Claude", "tmux", "Aquabase")
        assertEquals(2 to 1, SpeechScore.names("Ask Claude to open tmux", "ask cloud to open tmux", names))
        assertEquals(2 to 1, SpeechScore.names("Claude, Claude!", "claude", names))  // said twice, heard once
        assertEquals(0 to 0, SpeechScore.names("Run the tests", "run the tests", names))
        assertEquals(1 to 0, SpeechScore.names("On Aquabase", "on echo bay", names))  // the laptop's Parakeet miss
    }

    @Test fun overlappingNamesCountTheLongestOnly() {
        val names = listOf("Claude", "Claude Code", "Code")
        assertEquals(1 to 1, SpeechScore.names("Ask Claude Code", "ask claude code", names))  // not three names
        assertEquals(1 to 0, SpeechScore.names("Ask Claude Code", "ask claude", names))  // half the name isn't it
        assertEquals(2 to 2, SpeechScore.names("Claude, open Claude Code", "claude open claude code", names))
        assertEquals(2 to 2, SpeechScore.names("Claude Code Claude Code", "claude code claude code", names))
    }

    @Test fun aFailedTakeIsEveryWordMissed() {
        // scored as an empty hypothesis, so a set can't look better for the takes that gave nothing
        assertEquals(0.5, SpeechScore.wer(listOf("run the tests now" to "run the tests now", "open tmux please now" to "")), 1e-9)
        assertEquals(1 to 0, SpeechScore.names("Ask Claude", "", listOf("Claude")))
    }

    @Test fun medianAndP90() {
        assertEquals(0L, SpeechScore.median(emptyList()))
        assertEquals(2L, SpeechScore.median(listOf(3, 1, 2)))
        assertEquals(150L, SpeechScore.median(listOf(100, 200)))  // even: the mean of the two middle ones
        assertEquals(2L, SpeechScore.median(listOf(1, 2, 3, 4)))  // rounded down
        assertEquals(0L, SpeechScore.p90(emptyList()))
        assertEquals(9L, SpeechScore.p90((1L..10L).toList().shuffled()))  // the 9th of 10, not the max
        assertEquals(5L, SpeechScore.p90(listOf(1, 2, 3, 4, 5)))
        assertEquals(18L, SpeechScore.p90((1L..20L).toList()))
        assertEquals(7L, SpeechScore.p90(listOf(7)))
    }

    @Test fun theBarIsTodaysLaptopPath() {
        assertTrue(SpeechScore.shortfalls(0.119, 20, 13, listOf(150, 160, 170)).isEmpty())  // Nemotron on the laptop: meets it
        val below = SpeechScore.shortfalls(0.15, 20, 12, listOf(300, 300, 100))
        assertEquals(3, below.size)
        assertTrue(below[1].startsWith("names 12/20"))
        assertTrue(SpeechScore.shortfalls(0.10, 0, 0, emptyList()).isEmpty())  // no names in the set: not judged on them
    }

    @Test fun theBarNeedsLiveSpeedAndEveryTake() {
        assertTrue(SpeechScore.shortfalls(0.10, 20, 13, listOf(150), rtf = 0.8, failed = 0).isEmpty())
        val slow = SpeechScore.shortfalls(0.10, 20, 13, listOf(150), rtf = 0.95)
        assertEquals(1, slow.size)
        assertTrue(slow[0].startsWith("real-time factor 0.95"))
        assertTrue(SpeechScore.shortfalls(0.10, 20, 13, listOf(150), rtf = null).isEmpty())  // not measured: not judged
        val failed = SpeechScore.shortfalls(0.10, 20, 13, listOf(150), failed = 1)
        assertEquals(listOf("1 take gave no text"), failed)
    }
}
