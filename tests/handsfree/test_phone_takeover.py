"""The laptop's ears stand aside while the phone hears the user itself (a bad line): no turn opens, and a turn the
phone took over midway is dropped, never delivered as a truncated half. A pause in the phone's audio alone only
holds the turn a moment: words are never lost to a pause the phone didn't take over."""

import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from tests import sandbox  # first: a throwaway HOME
import listener
import turn
from routing import Heard
from voice import state

assert sandbox.HOME  # imported for its effect

LOUD = (np.sin(np.arange(480) / 3) * 8000).astype(np.int16)  # a steady voice-loud frame: no pause ends the turn


class RecordGivesWay(unittest.TestCase):
    def setUp(self):
        state.STATE.mkdir(parents=True, exist_ok=True)
        for path in (state.CALL_GAP, state.CALL_EAR, state.CALL_EAR_SINCE, state.CALL_LAPTOP_HEARD):
            path.unlink(missing_ok=True)
            self.addCleanup(path.unlink, missing_ok=True)
        self.taker = object.__new__(turn.Turn)
        self.taker.cfg = SimpleNamespace(early_end=False)
        self.taker.live = None
        self.live = mock.Mock()  # this turn, heard as it is recorded
        self.taker.streamer = mock.Mock(begin=mock.Mock(return_value=self.live))
        self.taker.tapped = None
        self.taker.show = lambda mode: None
        self.reads = 0
        patches = [mock.patch.object(turn, "in_call", return_value=True),
                   mock.patch.object(turn, "ptt_busy", return_value=False),
                   mock.patch.object(turn, "get_speech_timestamps", return_value=[{"start": 0}]),
                   mock.patch.object(turn, "set_phase"),
                   mock.patch.object(turn, "release_phase"),
                   mock.patch.object(state, "in_call", return_value=True),
                   mock.patch.object(turn, "TAKEOVER_WAIT", 0.3)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def reader(self, at: int, then):
        def read():
            self.reads += 1
            if self.reads == at:
                then()
            return LOUD if self.reads < 400 else None
        return read

    @staticmethod
    def take_ears(words_back: float):
        """The phone takes the ears, with the words from `words_back` s ago."""
        def take():
            now = time.monotonic()
            state.CALL_EAR_SINCE.write_text(f"{now:.6f} {now - words_back:.6f}")
            state.CALL_EAR.write_text("phone")
        return take

    def test_the_phone_taking_the_ears_drops_the_turn_at_once(self):
        self.taker.read = self.reader(20, self.take_ears(60))  # it has the words from well before the turn began
        self.assertIsNone(self.taker.record([], -60.0, Heard.CALL))
        self.assertLess(self.reads, 20 + 2 * turn.GAP_CHECK)
        self.live.cancel.assert_called_once_with()
        self.assertIsNone(self.taker.live)
        turn.release_phase.assert_called_with(turn.BY_HANDSFREE)

    def test_the_phone_without_the_turns_start_leaves_it_here(self):
        """It took the ears midway, its words starting after this turn began: the half heard here is delivered."""
        self.taker.read = self.reader(20, self.take_ears(0))
        frames = self.taker.record([], -60.0, Heard.CALL)
        self.assertIsNotNone(frames)
        self.assertLess(self.reads, 20 + 2 * turn.GAP_CHECK)  # ended at once: no more audio comes from the phone
        self.live.cancel.assert_not_called()
        self.assertIs(self.taker.live, self.live)

    def test_a_kept_turn_marks_where_the_phone_starts_hearing(self):
        self.taker.read = self.reader(1000, lambda: None)
        began = time.monotonic()
        self.assertIsNotNone(self.taker.record([], -60.0, Heard.CALL))
        self.assertGreaterEqual(state.laptop_heard_until(), began)

    def test_a_dropped_turn_marks_nothing(self):
        self.taker.read = self.reader(20, self.take_ears(60))
        self.assertIsNone(self.taker.record([], -60.0, Heard.CALL))
        self.assertIsNone(state.laptop_heard_until())

    def test_an_older_phone_saying_only_that_it_took_the_ears(self):
        self.taker.read = self.reader(20, lambda: state.CALL_EAR.write_text("phone"))
        self.assertIsNotNone(self.taker.record([], -60.0, Heard.CALL))
        self.live.cancel.assert_not_called()

    def test_a_pause_alone_keeps_the_turn(self):
        """The phone's audio paused (an earbud switch), and the phone never said it took over: delivered here."""
        self.taker.read = self.reader(20, lambda: state.CALL_GAP.write_text(f"{time.monotonic():.3f}"))
        began = time.monotonic()
        self.assertIsNotNone(self.taker.record([], -60.0, Heard.CALL))
        self.assertGreaterEqual(self.reads, 400)  # recorded on through the pause
        self.assertGreaterEqual(time.monotonic() - began, 0.3)  # after waiting for the phone a moment
        self.live.cancel.assert_not_called()
        self.assertIs(self.taker.live, self.live)  # its words go on to be transcribed

    def test_a_pause_then_the_phone_says_it_took_over(self):
        def pause():
            state.CALL_GAP.write_text(f"{time.monotonic():.3f}")
            # took the ears with the words from before the turn, and maybe handed them back
            state.CALL_EAR_SINCE.write_text(f"{time.monotonic():.3f} {time.monotonic() - 60:.3f}")
        self.taker.read = self.reader(20, pause)
        self.assertIsNone(self.taker.record([], -60.0, Heard.CALL))
        self.live.cancel.assert_called_once_with()

    def test_a_pause_then_a_takeover_without_the_turns_start(self):
        def pause():
            state.CALL_GAP.write_text(f"{time.monotonic():.3f}")
            state.CALL_EAR_SINCE.write_text(f"{time.monotonic():.3f} {time.monotonic():.3f}")
        self.taker.read = self.reader(20, pause)
        self.assertIsNotNone(self.taker.record([], -60.0, Heard.CALL))
        self.live.cancel.assert_not_called()

    def test_the_phone_says_so_while_the_turn_waits(self):
        self.taker.read = self.reader(20, lambda: state.CALL_GAP.write_text(f"{time.monotonic():.3f}"))
        with mock.patch.object(turn, "phone_took_ears_since", side_effect=[False, False, True]):
            self.assertIsNone(self.taker.record([], -60.0, Heard.CALL))

    def test_a_takeover_from_before_the_turn_is_not_its_own(self):
        state.CALL_GAP.write_text(f"{time.monotonic() - 60:.3f}")
        state.CALL_EAR_SINCE.write_text(f"{time.monotonic() - 60:.3f} {time.monotonic() - 70:.3f}")
        self.taker.read = self.reader(1000, lambda: None)
        self.assertIsNotNone(self.taker.record([], -60.0, Heard.CALL))  # ran to the end of what was read

    def test_off_a_call_nothing_is_looked_at(self):
        turn.in_call.return_value = False
        state.CALL_GAP.write_text(f"{time.monotonic() + 60:.3f}")
        state.CALL_EAR.write_text("phone")
        self.taker.read = self.reader(1000, lambda: None)
        self.assertIsNotNone(self.taker.record([], -60.0, Heard.WOKE))


class ListenerStandsAside(unittest.TestCase):
    def setUp(self):
        state.STATE.mkdir(parents=True, exist_ok=True)
        state.CALL_PUSH.touch()  # even the talk button's turn is the phone's then
        self.addCleanup(state.CALL_PUSH.unlink, missing_ok=True)
        self.me = mock.Mock()
        self.me.play_started = 0.0
        self.me.preroll = mock.MagicMock()
        self.me.room = [-60.0] * 10

    def step(self, phone: bool):
        with mock.patch.object(listener, "in_call", return_value=True), \
                mock.patch.object(listener, "phone_listens", return_value=phone), \
                mock.patch.object(listener, "ptt_busy", return_value=False):
            return listener.Listener.step(self.me, LOUD, LOUD.astype(np.float32) / 32768)

    def test_no_turn_while_the_phone_listens(self):
        self.assertFalse(self.step(phone=True))
        self.me.turn.assert_not_called()
        self.me.show.assert_called_with(listener.Mode.WAITING)
        self.me.preroll.clear.assert_called_once_with()

    def test_the_talk_button_as_ever_while_the_laptop_listens(self):
        self.assertTrue(self.step(phone=False))
        self.assertIs(self.me.turn.call_args.args[1], Heard.PUSHED)


if __name__ == "__main__":
    unittest.main()
