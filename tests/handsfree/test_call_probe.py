"""An earbud tap while the phone is on a call (multipoint buds pause the laptop as the phone answers) sends nothing:
each tap asks the phone, only an app that said it understands the question is asked, and no answer lets taps work."""

import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from tests import sandbox  # first: a throwaway HOME
import callprobe
import listener
import turn
from routing import Heard
from voice import state

assert sandbox.HOME  # imported for its effect

ADDRESS = "https://ntfy.sh/up-test"
LOUD = (np.sin(np.arange(480) / 3) * 8000).astype(np.int16)  # a steady voice: no pause ends the turn


def answered(*messages):
    return lambda reply, since: set(messages)


class Gating(unittest.TestCase):
    def setUp(self):
        state.STATE.mkdir(parents=True, exist_ok=True)
        for path in (state.CALL_RING, state.CALL_RING_PROBES):
            path.unlink(missing_ok=True)
            self.addCleanup(path.unlink, missing_ok=True)
        self.push = mock.Mock(return_value=True)
        self.phone = callprobe.PhoneCalls(self.push, answered(), "https://ntfy.sh")

    def test_an_app_that_never_said_it_understands_is_never_asked(self):
        state.CALL_RING.write_text(ADDRESS)
        self.assertIsNone(self.phone.ask())
        self.push.assert_not_called()

    def test_the_flag_counts_only_for_the_address_it_came_with(self):
        state.CALL_RING.write_text("https://ntfy.sh/up-new")  # a reinstall's new address, said without the flag
        state.CALL_RING_PROBES.write_text(ADDRESS)
        self.assertFalse(self.phone.understood())

    def test_an_app_that_understands_is_asked(self):
        state.CALL_RING.write_text(ADDRESS)
        state.CALL_RING_PROBES.write_text(ADDRESS)
        with mock.patch.object(callprobe, "POLL_EVERY", 0), mock.patch.object(callprobe, "ANSWER_FOR", 0.05):
            probe = self.phone.ask()
            self.assertTrue(probe.done.wait(2))
        body = self.push.call_args.args[0].decode()
        self.assertIn('"probe": "call"', body)
        self.assertIn('"reply": "https://ntfy.sh/henchline-', body)
        self.assertEqual(self.push.call_args.kwargs, {"to": ADDRESS})

    def test_the_probe_goes_to_the_address_checked_not_one_swapped_in_after(self):
        state.CALL_RING.write_text(ADDRESS)
        state.CALL_RING_PROBES.write_text(ADDRESS)
        started = []
        with mock.patch.object(callprobe.threading, "Thread",
                               side_effect=lambda target, args, daemon: started.append((target, args)) or mock.Mock()):
            probe = self.phone.ask()
        state.CALL_RING.write_text("https://ntfy.sh/up-older-app")  # an older build's address, without the flag
        target, args = started[0]
        with mock.patch.object(callprobe, "POLL_EVERY", 0), mock.patch.object(callprobe, "ANSWER_FOR", 0.01), \
                mock.patch.object(callprobe, "log"):
            target(*args)
        self.assertTrue(probe.done.is_set())
        self.assertEqual(self.push.call_args.kwargs, {"to": ADDRESS})


class Answers(unittest.TestCase):
    def ask(self, replies, push=True, phone=None):
        phone = phone or callprobe.PhoneCalls(mock.Mock(return_value=push), replies, "https://ntfy.sh")
        probe = callprobe.Probe()
        with mock.patch.object(callprobe, "POLL_EVERY", 0), mock.patch.object(callprobe, "ANSWER_FOR", 0.05), \
                mock.patch.object(callprobe, "log"):
            phone.wait_for(probe, ADDRESS)
        self.assertTrue(probe.done.is_set())
        return phone, probe

    def test_on_call_keeps_taps_off_a_while(self):
        phone, probe = self.ask(answered("on-call"))
        self.assertEqual(probe.answer, callprobe.ON_CALL)
        self.assertTrue(phone.on_call())

    def test_free_lets_taps_through_again(self):
        phone, probe = self.ask(answered("free"))
        phone.until = time.monotonic() + 100  # an earlier "on-call"
        with mock.patch.object(callprobe, "POLL_EVERY", 0), mock.patch.object(callprobe, "log"):
            phone.wait_for(probe, ADDRESS)
        self.assertEqual(probe.answer, callprobe.FREE)
        self.assertFalse(phone.on_call())

    def test_no_answer_or_no_push_leaves_it_open(self):
        for phone, probe in (self.ask(answered()), self.ask(answered("rang")), self.ask(answered("on-call"), push=False)):
            self.assertEqual(probe.answer, "")
            self.assertFalse(phone.on_call())

    def test_a_probe_unanswered_long_ago_is_waited_for_again(self):
        phone = callprobe.PhoneCalls(lambda *a, **k: True, lambda *a: set(), "https://ntfy.sh")
        phone.silent_at = time.monotonic()
        self.assertTrue(phone.unanswered)
        phone.silent_at = time.monotonic() - callprobe.SILENT_FOR - 1  # the phone may be back, and on a call
        self.assertFalse(phone.unanswered)

    def test_an_unanswered_probe_means_the_next_isnt_waited_for_until_one_is_answered(self):
        state.STATE.mkdir(parents=True, exist_ok=True)
        for path in (state.CALL_RING, state.CALL_RING_PROBES):
            path.write_text(ADDRESS)
            self.addCleanup(path.unlink, missing_ok=True)
        phone, _ = self.ask(answered())
        self.assertTrue(phone.unanswered)
        with mock.patch.object(callprobe.threading, "Thread"):
            self.assertFalse(phone.ask().expected)
        phone.replies = answered("free")  # back on the net
        self.ask(phone.replies, phone=phone)
        with mock.patch.object(callprobe.threading, "Thread"):
            self.assertTrue(phone.ask().expected)

    def test_never_rings_or_marks_a_ring(self):
        rang = state.STATE / "call.rang"  # handsfree.py's RANG: the ring rate limit
        state.STATE.mkdir(parents=True, exist_ok=True)
        for path in (state.CALL_RINGER, state.CALL_CHECK, rang):
            path.unlink(missing_ok=True)
        push = mock.Mock(return_value=True)
        self.ask(None, phone=callprobe.PhoneCalls(push, answered("on-call"), "https://ntfy.sh"))
        push.assert_called_once()
        body = json.loads(push.call_args.args[0])
        self.assertEqual(set(body), {"probe", "reply"})  # no agent, why or kind: nothing a ring is made of
        for path in (state.CALL_RINGER, state.CALL_CHECK, rang):
            self.assertFalse(path.exists(), path)


class TappedTurn(unittest.TestCase):
    def setUp(self):
        self.taker = object.__new__(turn.Turn)
        self.taker.cfg = SimpleNamespace(early_end=False)
        self.taker.live = None
        self.live = mock.Mock()
        self.taker.streamer = mock.Mock(begin=mock.Mock(return_value=self.live))
        self.taker.tapped = None
        self.taker.show = lambda mode: None
        self.taker.probe = callprobe.Probe()
        self.reads = 0
        patches = [mock.patch.object(turn, "in_call", return_value=False),
                   mock.patch.object(turn, "ptt_busy", return_value=False),
                   mock.patch.object(turn, "set_phase"), mock.patch.object(turn, "release_phase"),
                   mock.patch.object(turn, "log"), mock.patch.object(turn, "PROBE_GRACE", 0.2)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def reader(self, at: int = 0, then=lambda: None, frames: int = 40):
        def read():
            self.reads += 1
            if self.reads == at:
                then()
            return LOUD if self.reads <= frames else None
        return read

    def answer(self, what: str):
        def set_it():
            self.taker.probe.answer = what
            self.taker.probe.done.set()
        return set_it

    def dropped_quietly(self):
        turn.log.assert_any_call(turn.HANDSFREE_LOG, "earbud tap during a phone call: turn dropped")
        turn.release_phase.assert_called_with(turn.BY_HANDSFREE)
        self.live.cancel.assert_called_once_with()
        self.assertTrue(self.taker.dropped)

    def test_on_call_while_recording_stops_and_drops_it(self):
        self.taker.read = self.reader(5, self.answer("on-call"), frames=1000)
        self.assertIsNone(self.taker.record([], -60.0, Heard.WOKE))
        self.assertLess(self.reads, 10)  # stopped recording at once
        self.dropped_quietly()

    def test_on_call_in_the_grace_after_recording_drops_it(self):
        self.taker.read = self.reader()
        threading.Timer(0.05, self.answer("on-call")).start()
        self.assertIsNone(self.taker.record([], -60.0, Heard.WOKE))
        self.dropped_quietly()

    def test_a_dropped_turn_sends_nothing_and_opens_no_window(self):
        self.taker.read = self.reader(1, self.answer("on-call"))
        self.taker.transcribe = mock.Mock()
        self.assertIsNone(self.taker.take([], -60.0, Heard.WOKE))  # None: no window after it
        self.taker.transcribe.assert_not_called()

    def test_free_sends_as_ever(self):
        self.taker.read = self.reader(1, self.answer("free"))
        self.assertEqual(len(self.taker.record([], -60.0, Heard.WOKE)), 40)
        self.assertFalse(self.taker.dropped)

    def test_a_phone_that_left_the_last_probe_unanswered_isnt_waited_for(self):
        self.taker.read = self.reader()
        self.taker.probe = callprobe.Probe(expected=False)
        began = time.monotonic()
        with mock.patch.object(turn, "PROBE_GRACE", 5.0):
            self.assertEqual(len(self.taker.record([], -60.0, Heard.WOKE)), 40)
        self.assertLess(time.monotonic() - began, 1.0)

    def test_no_answer_in_time_sends_as_ever(self):
        self.taker.read = self.reader()
        began = time.monotonic()
        self.assertEqual(len(self.taker.record([], -60.0, Heard.WOKE)), 40)
        self.assertLess(time.monotonic() - began, 1.0)  # only the short grace, not the probe's whole wait
        self.assertFalse(self.taker.dropped)


class TapWhileOnCall(unittest.TestCase):
    def setUp(self):
        self.ear = SimpleNamespace(phone=mock.Mock(), turns=SimpleNamespace(tapped=None, probe=None),
                                   button=object(), room=[], turn=mock.Mock())
        self.ear.phone.ask.return_value = probe = callprobe.Probe()
        self.probe = probe

    def tap_while_blocked(self, answer):
        self.ear.phone.on_call.return_value = True
        if answer is not None:
            threading.Timer(0.05, lambda: (setattr(self.probe, "answer", answer), self.probe.done.set())).start()
        with mock.patch.object(listener, "log") as log, mock.patch.object(listener, "BLOCKED_TAP_WAIT", 0.5):
            listener.Listener.earbud_tap(self.ear, LOUD.astype(np.float32) / 32768)
        self.ear.phone.ask.assert_called_once_with()  # its answer refreshes the block, or clears it
        return log

    def test_a_recent_on_call_ignores_the_tap_but_still_asks(self):
        for answer in ("on-call", None):  # still on it, or no answer in time
            with self.subTest(answer=answer):
                self.ear.phone.ask.reset_mock()
                self.ear.turn.reset_mock()
                self.ear.phone.ask.return_value = self.probe = callprobe.Probe()
                log = self.tap_while_blocked(answer)
                self.ear.turn.assert_not_called()
                log.assert_called_once_with(listener.HANDSFREE_LOG, "earbud tap ignored: the phone is on a call")

    def test_a_call_that_ended_lets_the_tap_through(self):
        self.tap_while_blocked("free")
        self.ear.turn.assert_called_once()

    def test_otherwise_the_tap_records_with_its_probe(self):
        self.ear.phone.on_call.return_value = False
        seen = []
        self.ear.turn.side_effect = lambda *a: seen.append(self.ear.turns.probe)
        with mock.patch.object(listener, "log"):
            listener.Listener.earbud_tap(self.ear, LOUD.astype(np.float32) / 32768)
        self.assertEqual(seen, [self.probe])
        self.assertIsNone(self.ear.turns.probe)  # and let go after it

    def test_without_a_phone_to_ask_taps_work_as_ever(self):
        self.ear.phone = None
        with mock.patch.object(listener, "log"):
            listener.Listener.earbud_tap(self.ear, LOUD.astype(np.float32) / 32768)
        self.ear.turn.assert_called_once()


class WindowAfterACall(unittest.TestCase):
    """A turn dropped as the phone's call leaves no window: talk in the room then is the call's."""

    def setUp(self):
        state.STATE.mkdir(parents=True, exist_ok=True)
        self.addCleanup(state.LISTEN.unlink, missing_ok=True)
        self.ear = SimpleNamespace(cfg=None, echo_watch=mock.Mock(), window=listener.Window(), gate=mock.Mock(),
                                   preroll=mock.Mock(), turns=SimpleNamespace(take=lambda *a: None, during_call=False))
        self.ear.window.open(60, Heard.FOLLOW_ON)
        state.LISTEN.write_text(str(time.time() + 60))

    def run_turn(self):
        with mock.patch.object(listener.whisper, "ding"):
            listener.Listener.turn(self.ear, [], Heard.WOKE, -60.0)

    def test_a_call_drop_closes_the_window(self):
        self.ear.turns.during_call = True
        self.run_turn()
        self.assertFalse(self.ear.window.is_open())
        self.assertFalse(state.LISTEN.exists())

    def test_a_noise_keeps_the_window_they_had(self):
        self.run_turn()
        self.assertTrue(self.ear.window.is_open())
        self.assertTrue(state.LISTEN.exists())


if __name__ == "__main__":
    unittest.main()
