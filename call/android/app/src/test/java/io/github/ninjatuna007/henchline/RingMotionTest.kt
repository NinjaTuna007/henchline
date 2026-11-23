package io.github.ninjatuna007.henchline

import android.view.HapticFeedbackConstants
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RingMotionTest {
    @Test fun theButtonsRiseTwentyFourDpOneAfterTheOther() {
        assertEquals(24, RingMotion.SLIDE_DP)
        assertEquals(300L, RingMotion.BUTTON_MS)
        assertTrue(RingMotion.delayOf(1) > RingMotion.delayOf(0))
        assertEquals(RingMotion.STAGGER_MS, RingMotion.delayOf(1) - RingMotion.delayOf(0))
        assertEquals(RingMotion.BUTTONS_AFTER_MS, RingMotion.delayOf(-1))
    }

    @Test fun itComesInMovingOnlyWhenNewAndWithAnimationsOn() {
        assertTrue(RingMotion.enters(on = true, restored = false))
        assertFalse(RingMotion.enters(on = false, restored = false))
        assertFalse(RingMotion.enters(on = true, restored = true))  // made again: as it was, no second entrance
    }

    @Test fun answerConfirmsAndDeclineRejects() {
        assertEquals(HapticFeedbackConstants.CONFIRM, RingMotion.answerHaptic(30))
        assertEquals(HapticFeedbackConstants.REJECT, RingMotion.declineHaptic(34))
        assertEquals(HapticFeedbackConstants.VIRTUAL_KEY, RingMotion.answerHaptic(29))
        assertEquals(HapticFeedbackConstants.LONG_PRESS, RingMotion.declineHaptic(29))
        assertTrue(RingMotion.AVATAR_FROM in 0.5f..0.9f)
    }
}
