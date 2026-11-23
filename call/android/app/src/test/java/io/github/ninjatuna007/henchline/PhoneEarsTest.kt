package io.github.ninjatuna007.henchline

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The phone's own ears end turns and tell our echo by the laptop's rules (tests/fixtures/phone-ears.json, pinned
 *  against the laptop's code by tests/handsfree/test_phone_ears_rules.py), and keep each turn until it's answered. */
class PhoneEarsTest {
    private val fixture = JSONObject(javaClass.classLoader!!.getResource("phone-ears.json")!!.readText())

    @Test fun constantsAreTheLaptops() {
        val c = fixture.getJSONObject("constants")
        assertEquals(c.getInt("call_end_ms"), TurnRules.END_MS)
        assertEquals(c.getInt("call_early_end_ms"), TurnRules.EARLY_END_MS)
        assertEquals(c.getInt("no_stop_ms"), TurnRules.NO_STOP_MS)
        assertEquals(c.getInt("over_ms"), TurnRules.OVER_MS)
        assertEquals(c.getLong("voice_gone_ms"), TurnRules.VOICE_GONE_MS)
        assertEquals(c.getLong("max_ms"), TurnRules.MAX_MS)
        assertEquals(c.getDouble("end_db"), TurnRules.END_DB.toDouble(), 0.0)
        assertEquals(c.getDouble("below_peak_db"), TurnRules.BELOW_PEAK_DB.toDouble(), 0.0)
        assertEquals(c.getLong("echo_turn_ms"), OwnWords.ECHO_TURN_MS)
        assertEquals(c.getDouble("mostly_ours"), OwnWords.MOSTLY_OURS, 0.0)
        assertEquals(c.getInt("own_words_min"), OwnWords.MIN)
        assertEquals(c.getDouble("own_words"), OwnWords.OWN, 0.0)
        assertEquals(c.getInt("own_run"), OwnWords.RUN)
        assertEquals(c.getDouble("own_share"), OwnWords.SHARE, 0.0)
        val trailing = fixture.getJSONArray("trailing")
        assertEquals((0 until trailing.length()).map { trailing.getString(it) }.toSet(), TurnRules.TRAILING)
    }

    @Test fun ourOwnWordsAsTheLaptopTellsThem() {
        val cases = fixture.getJSONArray("own_words")
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val heard = c.getString("heard"); val said = c.getString("said")
            val keys = c.getJSONArray("keys")
            assertEquals(heard, (0 until keys.length()).map { keys.getString(it) }, OwnWords.keys(heard))
            assertEquals(heard, c.getDouble("share"), OwnWords.share(heard, said), 1e-5)
            assertEquals(heard, c.getBoolean("ours"), OwnWords.ours(heard, said))
            assertEquals(heard, c.getBoolean("echo_short"), OwnWords.echoTurn(heard, said, 1_000))
            assertEquals(heard, c.getBoolean("echo_long"), OwnWords.echoTurn(heard, said, 5_000))
        }
    }

    @Test fun sentencesOverAndTidyAsTheLaptops() {
        val sentences = fixture.getJSONArray("sentence_done")
        for (i in 0 until sentences.length()) {
            val c = sentences.getJSONObject(i)
            assertEquals(c.getString("said"), c.getBoolean("done_at_once"), TurnRules.sentenceDone(c.getString("said"), 0))
            assertEquals(c.getString("said"), c.getBoolean("done_later"), TurnRules.sentenceDone(c.getString("said"), TurnRules.NO_STOP_MS))
        }
        val over = fixture.getJSONArray("over")
        for (i in 0 until over.length()) {
            val c = over.getJSONObject(i)
            assertEquals(c.getString("said"), c.getBoolean("over"), TurnRules.endsWithOver(c.getString("said")))
        }
        val hints = fixture.getJSONArray("hints").let { a -> (0 until a.length()).map { a.getString(it) } }
        val tidy = fixture.getJSONArray("tidy")
        for (i in 0 until tidy.length()) {
            val c = tidy.getJSONObject(i)
            assertEquals(c.getString("tidied"), TurnRules.tidy(c.getString("text"), hints))
        }
    }

    // --- the endpointer, on scripted frames -------------------------------------------------------------------

    private class Run(val e: Endpointer = Endpointer(20), var words: String = "") {
        var at = 0L
        val steps = ArrayList<Pair<Long, Endpointer.Step>>()

        /** `ms` of frames at `db`, voiced or not; each step that isn't None kept with its time. */
        fun frames(ms: Int, db: Float, voice: Boolean): Run {
            repeat(ms / 20) {
                val step = e.frame(at, db, voice) { words }
                if (step != Endpointer.Step.None) steps += at to step
                at += 20
            }
            return this
        }

        fun quiet(ms: Int) = frames(ms, QUIET, false)
        fun talk(ms: Int) = frames(ms, LOUD, true)
        fun opened() = steps.map { it.second }.filterIsInstance<Endpointer.Step.Open>()
        fun closed() = steps.map { it.second }.filterIsInstance<Endpointer.Step.Close>()
        fun closedAt() = steps.first { it.second is Endpointer.Step.Close }.first
    }

    @Test fun aTurnOpensOnVoiceWithItsLeadIn() {
        val r = Run().quiet(1000).talk(400)
        val open = r.opened().single()
        assertEquals(1000L, open.voiceMs)
        assertEquals(1000L - Endpointer.PREROLL_MS, open.fromMs)
        assertFalse(open.pushed)
        assertEquals(1000L + Endpointer.OPEN_MS - 20, r.steps.single().first)  // after OPEN_MS of voice
    }

    @Test fun aTurnAlreadyUnderWayWhenHearingBeginsIsNotTakenUpMidway() {
        val r = Run().talk(3000)
        assertTrue(r.opened().isEmpty())
        r.quiet(400).talk(400)
        assertEquals(1, r.opened().size)
    }

    @Test fun aScrapOfVoiceOpensNothing() {
        assertTrue(Run().quiet(1000).talk(100).quiet(500).talk(100).opened().isEmpty())
    }

    @Test fun aPauseEndsIt() {
        val r = Run(words = "and then the").quiet(1000).talk(2000).quiet(2000)
        assertEquals(Endpointer.End.PAUSE, r.closed().single().end)
        assertEquals(3000L + TurnRules.END_MS - 20, r.closedAt())
    }

    @Test fun aFinishedSentenceEndsItSooner() {
        val r = Run(words = "Run the tests.").quiet(1000).talk(2000).quiet(2000)
        assertEquals(Endpointer.End.FINISHED, r.closed().single().end)
        assertEquals(3000L + TurnRules.EARLY_END_MS - 20, r.closedAt())
    }

    @Test fun withoutAFullStopALittleLater() {
        val r = Run(words = "This is a test").quiet(1000).talk(2000).quiet(2000)
        assertEquals(Endpointer.End.FINISHED, r.closed().single().end)
        assertEquals(3000L + TurnRules.EARLY_END_MS + TurnRules.NO_STOP_MS - 20, r.closedAt())
    }

    @Test fun overEndsIt() {
        val r = Run(words = "that's all for now, over").quiet(1000).talk(2000).quiet(2000)
        assertEquals(Endpointer.End.OVER, r.closed().single().end)
        assertEquals(3000L + TurnRules.OVER_MS - 20, r.closedAt())
    }

    @Test fun steadyHissWithoutVoiceEndsIt() {
        val r = Run(words = "and").quiet(1000).talk(1000).frames(3000, LOUD, false)
        assertEquals(Endpointer.End.VOICELESS, r.closed().single().end)
        assertEquals(2000L + TurnRules.VOICE_GONE_MS - 20, r.closedAt())  // a second after the last voice ended
    }

    @Test fun aLongTurnIsCutAtTheLimit() {
        val r = Run(words = "and").quiet(1000).talk(130_000)
        assertEquals(Endpointer.End.LIMIT, r.closed().first().end)
        assertEquals(1000L + TurnRules.MAX_MS - 20, r.closedAt())
    }

    @Test fun theButtonHoldsATurnThroughPauses() {
        val r = Run(words = "Run the tests.").quiet(1000)
        val open = r.e.press(r.at) as Endpointer.Step.Open
        assertTrue(open.pushed)
        r.talk(1000).quiet(5000)
        assertTrue(r.closed().isEmpty())
        assertEquals(Endpointer.End.BUTTON, (r.e.release() as Endpointer.Step.Close).end)
        assertEquals(Endpointer.Step.None, r.e.release())
    }

    @Test fun theButtonDuringATurnHoldsThatTurn() {
        val r = Run().quiet(1000).talk(400)
        val open = r.e.press(r.at) as Endpointer.Step.Open
        assertTrue(open.already)
        assertEquals(1000L, open.voiceMs)
        r.quiet(3000)
        assertTrue(r.closed().isEmpty())
    }

    @Test fun openLineOffLetsOnlyTheButtonIn() {
        val r = Run()
        r.e.buttonOnly = true
        r.quiet(1000).talk(2000)
        assertTrue(r.opened().isEmpty())
        assertTrue((r.e.press(r.at) as Endpointer.Step.Open).pushed)
    }

    @Test fun aDroppedTurnLetsTheNextOneOpen() {
        val r = Run().quiet(1000).talk(400)
        r.e.drop()
        assertFalse(r.e.open)
        r.quiet(400).talk(400)
        assertEquals(2, r.opened().size)
    }

    @Test fun aNoisyRoomStillEnds() {
        // The floor is the room's (-40 dB, not the -70 of a quiet one): back down to it is quiet, even with a voice
        val r = Run(words = "and").frames(5000, -40f, false).talk(2000).frames(1000, -40f, true)
        assertEquals(Endpointer.End.PAUSE, r.closed().single().end)
        assertEquals(7000L + TurnRules.END_MS - 20, r.closedAt())
    }

    @Test fun aCutArmsHearingAtOnce() {
        val r = Run()
        r.e.arm()  // the server's cut: what follows the laptop's last turn is never one of its own
        r.talk(400)
        assertEquals(1, r.opened().size)
    }

    // --- the backlog: frames trimmed, the talk button never ------------------------------------------------------

    private fun Backlog.drain(): List<Backlog.Item> = generateSequence { poll(0) }.toList()

    @Test fun theButtonIsNeverTrimmedAway() {
        val b = Backlog()
        b.frame(ShortArray(320), 0L, 100L)
        b.press(true)
        b.frame(ShortArray(320), 20L, 100L)
        b.press(false)
        var gone = 0
        for (at in 40L..1000L step 20) gone += b.frame(ShortArray(320), at, 100L)
        assertEquals(45, gone)  // only frames went: 51 made, the last 100 ms (6 frames) kept
        val left = b.drain()
        assertEquals(listOf(true, false), left.mapNotNull { it.press })
        assertEquals(listOf(900L, 920L, 940L, 960L, 980L, 1000L), left.filter { it.pcm != null }.map { it.atMs })
    }

    @Test fun whatTheLaptopDeliveredIsDroppedUnheard() {
        val b = Backlog()
        for (at in 0L..180L step 20) b.frame(ShortArray(320), at, 10_000L)
        b.press(true)
        assertEquals(0L, b.oldest())
        assertEquals(5, b.dropBefore(100L))  // 0..80: each over by 100
        assertEquals(100L, b.oldest())
        val left = b.drain()
        assertEquals(listOf(100L, 120L, 140L, 160L, 180L), left.filter { it.pcm != null }.map { it.atMs })
        assertEquals(listOf(true), left.mapNotNull { it.press })
        assertNull(b.oldest())
    }

    // --- turns kept until answered, and talkback's words --------------------------------------------------------

    @Test fun aTurnIsKeptUntilTheServerAnswersIt() {
        val turns = HeardTurns("ear-1")
        val first = JSONObject(turns.add("check the logs", pushed = false, saidAt = 1000L, late = false)).getJSONObject("heard")
        val second = JSONObject(turns.add("over", pushed = true, saidAt = 2000L, late = true)).getJSONObject("heard")
        assertEquals("ear-1-0", first.getString("id"))
        assertEquals("ear-1-1", second.getString("id"))
        assertTrue(second.getBoolean("pushed")); assertTrue(second.getBoolean("late"))
        assertEquals(2000L, second.getLong("said_at"))
        assertEquals(2, turns.unanswered().size)
        assertNull(turns.answered(JSONObject().put("delivered", "typed-7")))  // a typed line's: not ours
        assertEquals("ear-1-1", turns.answered(JSONObject().put("note", "Not sent").put("unsent", "ear-1-1")))
        assertEquals(listOf("ear-1-0"), turns.unanswered().map { JSONObject(it).getJSONObject("heard").getString("id") })
        assertEquals("ear-1-0", turns.answered(JSONObject().put("taken", "ear-1-0")))
        assertNull(turns.answered(JSONObject().put("delivered", "ear-1-0")))  // answered twice: once is enough
        assertEquals(0, turns.size())
    }

    @Test fun talkbacksWordsLingerAMomentAfterItStops() {
        val clips = OwnClips(tailMs = 2000)
        assertEquals("", clips.text(0))
        clips.set("laptop", "the tests passed", 0)
        clips.set("phone", "hello there", 100)
        val both = clips.text(500)
        assertTrue("the tests passed" in both && "hello there" in both)
        clips.set("laptop", null, 1000)
        clips.set("phone", null, 1000)
        assertEquals("the tests passed hello there".length, clips.text(2900).length)
        assertEquals("", clips.text(3100))
        clips.set("laptop", "", 3200)  // a clip with no words is none
        assertEquals("", clips.text(3200))
    }

    companion object {
        const val QUIET = -70f
        const val LOUD = -20f
    }
}
