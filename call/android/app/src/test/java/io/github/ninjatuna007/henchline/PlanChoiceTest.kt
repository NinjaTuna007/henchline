package io.github.ninjatuna007.henchline

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Which of a chat's plans the Plan tab shows: the chat's latest, or the user's pick until the chat moves on. */
class PlanChoiceTest {
    private fun answer(showing: String, vararg files: String) = JSONObject().put("file", showing)
        .put("plans", JSONArray().apply { files.forEach { put(JSONObject().put("file", it).put("name", it)) } })

    @Test fun theFilesComeInTheLaptopsOrder() {
        assertEquals(listOf("b", "a"), PlanChoice.files(answer("b", "b", "a")))
        assertEquals(emptyList<String>(), PlanChoice.files(JSONObject().put("file", "a")))  // an older laptop
    }

    @Test fun pickingThePlanTheChatIsOnIsNoPick() {
        assertNull(PlanChoice.pick(listOf("new", "old"), "new"))
        assertEquals(PlanPick("old", "new"), PlanChoice.pick(listOf("new", "old"), "old"))
        assertNull(PlanChoice.pick(listOf("new", "old"), "stranger"))
        assertNull(PlanChoice.pick(emptyList(), "old"))
    }

    @Test fun aPickHoldsWhileTheChatStaysOnTheSamePlan() {
        val pick = PlanPick("old", "new")
        assertEquals(pick, PlanChoice.settle(answer("old", "new", "old"), pick))
        assertNull(PlanChoice.settle(answer("new", "new", "old"), null))
    }

    @Test fun theChatTouchingAnotherPlanIsFollowed() {
        val pick = PlanPick("old", "new")
        assertNull(PlanChoice.settle(answer("old", "third", "new", "old"), pick))  // a newer plan
        assertNull(PlanChoice.settle(answer("old", "old", "new"), pick))  // the picked one itself: it is the chat's now
    }

    @Test fun aGonePlanDropsThePick() {
        val pick = PlanPick("old", "new")
        assertNull(PlanChoice.settle(answer("new", "new"), pick))
        assertNull(PlanChoice.settle(JSONObject().put("note", PlanChoice.NOT_ITS_OWN), pick))
    }

    @Test fun anErrorOrAnOlderLaptopLeavesThePickAlone() {
        val pick = PlanPick("old", "new")
        assertEquals(pick, PlanChoice.settle(JSONObject().put("note", "Couldn't reach the laptop (is Tailscale on?)"), pick))
        assertEquals(pick, PlanChoice.settle(JSONObject().put("file", "new").put("todos", JSONArray()), pick))
    }

    @Test fun anAnswerToAnAskFromBeforeThePickChangedIsIgnored() {
        val pick = PlanPick("old", "new")
        // the 5 s poll went without the pick; the user picked meanwhile: its answer (the latest) is not shown
        assertEquals(PlanChoice.Verdict.IGNORE, PlanChoice.verdict("", pick, answer("new", "new", "old")))
        // and one that carried a pick since cleared
        assertEquals(PlanChoice.Verdict.IGNORE, PlanChoice.verdict("old", null, answer("old", "new", "old")))
        assertEquals(PlanChoice.Verdict.SHOW, PlanChoice.verdict("old", pick, answer("old", "new", "old")))
        assertEquals(PlanChoice.Verdict.SHOW, PlanChoice.verdict("", null, answer("new", "new", "old")))
        assertEquals(PlanChoice.Verdict.ASK_AGAIN, PlanChoice.verdict("old", pick, answer("old", "third", "new", "old")))
        assertEquals(PlanChoice.Verdict.SHOW, PlanChoice.verdict("old", pick, JSONObject().put("note", "refused (502).")))
    }

    @Test fun picksAreKeptNewestLastAndTheOldestGoFirst() {
        var saved = JSONArray()
        for (i in 1..4) saved = PlanChoice.put(saved, "chat-$i", PlanPick("p$i", "l$i"), keep = 3)
        assertNull(PlanChoice.find(saved, "chat-1"))
        assertEquals(PlanPick("p2", "l2"), PlanChoice.find(saved, "chat-2"))
        saved = PlanChoice.put(saved, "chat-2", PlanPick("q2", "l2"), keep = 3)  // picked again: now the newest
        saved = PlanChoice.put(saved, "chat-5", PlanPick("p5", "l5"), keep = 3)
        assertNull(PlanChoice.find(saved, "chat-3"))
        assertEquals(PlanPick("q2", "l2"), PlanChoice.find(saved, "chat-2"))
        saved = PlanChoice.put(saved, "chat-2", null, keep = 3)
        assertNull(PlanChoice.find(saved, "chat-2"))
        assertEquals(2, saved.length())
    }
}
