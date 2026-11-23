"""handsfree/core/routing.py act_on_turn: what a finished turn's words do, whichever ear heard them."""

import time
import unittest
from unittest import mock

from tests import sandbox  # first: a throwaway HOME
import routing
import turn
from routing import Heard
from voice.desktop import Tab
from voice.state import Speaker

assert sandbox.HOME  # imported for its effect

CLAUDE = Speaker("claude-act", "claude", "henchline", Tab("tmux", "%3"))


class Acting(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.goodbye = mock.Mock()
        patches = {
            "say": mock.Mock(side_effect=lambda words, voice="": self.said.append(words) or self.goodbye),
            "deliver": mock.Mock(return_value=routing.Delivery.done()),
            "route": mock.Mock(return_value=True),
            "last_speaker": mock.Mock(return_value=CLAUDE),
            "set_target": mock.Mock(),
            "set_phase": mock.Mock(),
            "in_call": mock.Mock(return_value=True),
            "interrupt_waiting": mock.Mock(return_value=False),
            "clear_interrupt": mock.Mock(),
            "cuts_in": mock.Mock(return_value=False),
        }
        for name, fake in patches.items():
            patcher = mock.patch.object(routing, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fake = patches
        self.ding, self.off, self.echo, self.sent = mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock()
        routing.CALL_HANGUP.unlink(missing_ok=True)
        self.addCleanup(lambda: routing.CALL_HANGUP.unlink(missing_ok=True))

    def act(self, text, heard=Heard.CALL, last=None, **kwargs):
        return routing.act_on_turn(text, kwargs.pop("rich", text), heard, last, ding=self.ding, off=self.off,
                                   echo=self.echo, sent=self.sent, **kwargs)

    def test_plain_words_are_delivered_to_the_last_speaker(self):
        self.assertEqual(self.act("check the logs please", rich="check the *logs* please"), 0.0)
        self.fake["deliver"].assert_called_once_with("check the *logs* please", Heard.CALL, to=CLAUDE, urgent=False,
                                                     ear="")
        self.fake["set_target"].assert_called_once_with(True, CLAUDE.session)
        self.fake["set_phase"].assert_called_once_with(routing.Phase.IDLE, routing.BY_HANDSFREE)
        self.ding.assert_called_once_with()
        self.sent.assert_called_once_with(("check the *logs* please", Heard.CALL))
        self.assertEqual(self.said, [])

    def test_the_phone_ear_goes_along(self):
        self.act("check the logs", ear="phone")
        self.assertEqual(self.fake["deliver"].call_args.kwargs["ear"], "phone")

    def test_a_held_send_is_cleared_by_the_turn_it_waited_for(self):
        self.fake["interrupt_waiting"].return_value = True
        self.act("stop that")
        self.assertTrue(self.fake["deliver"].call_args.kwargs["urgent"])
        self.fake["clear_interrupt"].assert_called_once_with()

    def test_words_that_did_not_arrive(self):
        self.fake["deliver"].return_value = routing.Delivery.refused(routing.Refusal.NOBODY, "x")
        self.assertEqual(self.act("check the logs"), 0.0)
        self.ding.assert_not_called()
        self.sent.assert_not_called()

    def test_a_long_message_asking_for_time_keeps_listening_longer(self):
        long = "hold on, I need to look at the parser and the build and the tests before I answer that one"
        self.assertEqual(self.act(long), routing.THINK_WINDOW)
        self.fake["deliver"].assert_called_once()

    def test_give_me_a_minute(self):
        self.assertEqual(self.act("give me a minute"), routing.THINK_WINDOW)
        self.assertEqual(self.said, ["Take your time."])
        self.fake["deliver"].assert_not_called()
        self.fake["set_target"].assert_called_once_with(False, "")

    def test_switch(self):
        last = ("earlier words", Heard.CALL)
        self.assertEqual(self.act("switch me to Cursor", last=last), 0.0)
        self.fake["route"].assert_called_once_with("switch me to Cursor", last)
        self.ding.assert_called_once_with()
        self.fake["deliver"].assert_not_called()

    def test_a_switch_that_did_not_happen_does_not_ding(self):
        self.fake["route"].return_value = False
        self.assertEqual(self.act("who am I talking to"), 0.0)
        self.ding.assert_not_called()

    def test_hang_up_says_goodbye_then_ends_the_call(self):
        self.assertIsNone(self.act("hang up"))
        self.assertEqual(self.said, ["Bye! Hanging up."])
        for _ in range(100):
            if routing.CALL_HANGUP.exists():
                break
            time.sleep(0.02)
        self.assertTrue(routing.CALL_HANGUP.exists())
        self.goodbye.wait.assert_called_once_with(timeout=15)
        self.fake["deliver"].assert_not_called()

    def test_hang_up_off_a_call_is_a_message(self):
        self.fake["in_call"].return_value = False
        self.act("hang up", heard=Heard.WOKE)
        self.fake["deliver"].assert_called_once()
        self.assertFalse(routing.CALL_HANGUP.exists())

    def test_hands_free_off(self):
        self.assertEqual(self.act("hands-free off"), 0.0)
        self.off.assert_called_once_with()
        self.assertEqual(self.said, ["Hands-free off."])
        self.fake["deliver"].assert_not_called()

    def test_never_mind(self):
        self.assertEqual(self.act("never mind"), 0.0)
        self.assertEqual(self.said, [])
        self.fake["deliver"].assert_not_called()
        self.ding.assert_not_called()

    def test_nothing(self):
        self.assertIsNone(self.act("um"))
        self.assertIsNone(self.act(""))
        self.echo.assert_not_called()
        self.fake["deliver"].assert_not_called()

    def test_nothing_over_a_clip_was_our_echo(self):
        self.assertIsNone(self.act("uh", heard=Heard.OVER_YOU))
        self.echo.assert_called_once_with("echo cut a clip")


class TurnActs(unittest.TestCase):
    """The laptop's turn hands its words to act_on_turn with its own ding, stop, echo report and last message."""

    def test_wired_through(self):
        taker = object.__new__(turn.Turn)
        taker.cfg, taker.stop, taker.echo_slipped = object(), mock.Mock(), mock.Mock()
        taker.last_message = ("before", Heard.CALL)
        with mock.patch.object(routing, "deliver", return_value=routing.Delivery.done()) as deliver, \
                mock.patch.object(routing, "last_speaker", return_value=CLAUDE), \
                mock.patch.object(routing, "set_target"), mock.patch.object(routing, "set_phase"), \
                mock.patch.object(routing, "in_call", return_value=False), \
                mock.patch.object(routing, "say") as say, \
                mock.patch.object(turn.whisper, "ding") as ding:
            self.assertEqual(taker.act("check the logs", "check the *logs*", Heard.REPLY), 0.0)
            ding.assert_called_once_with(taker.cfg)
            self.assertEqual(taker.last_message, ("check the *logs*", Heard.REPLY))
            deliver.assert_called_once_with("check the *logs*", Heard.REPLY, to=CLAUDE, urgent=False, ear="")
            taker.act("turn off hands-free", "turn off hands-free", Heard.REPLY)
            taker.stop.set.assert_called_once_with()
            say.assert_called_once_with("Hands-free off.")
            self.assertIsNone(taker.act("um", "um", Heard.OVER_YOU))
            taker.echo_slipped.assert_called_once_with("echo cut a clip")


class PhoneEarsTag(unittest.TestCase):
    def deliver(self, heard, **kwargs):
        terms = mock.Mock()
        terms.agent_running.return_value = True
        terms.type_line.return_value = True
        with mock.patch.object(routing, "place", return_value=""), \
                mock.patch.object(routing, "desktop", return_value=mock.Mock(terminals=terms)), \
                mock.patch.object(routing, "in_call", return_value=True), \
                mock.patch.object(routing, "on_a_call", return_value=""), \
                mock.patch.object(routing, "is_command", return_value=False):
            self.assertTrue(routing.deliver_words("check the logs", heard, to=CLAUDE, **kwargs))
        return terms.type_line.call_args.args[1]

    def test_the_laptops_ears_as_before(self):
        self.assertEqual(self.deliver(Heard.CALL), "[hands-free · call] check the logs")
        self.assertEqual(self.deliver(Heard.PUSHED), "[hands-free · call, held the talk button] check the logs")

    def test_the_phones_ears(self):
        self.assertEqual(self.deliver(Heard.CALL, ear="phone"), "[hands-free · call · phone ears] check the logs")
        self.assertEqual(self.deliver(Heard.PUSHED, ear="phone"),
                         "[hands-free · call, held the talk button · phone ears] check the logs")


if __name__ == "__main__":
    unittest.main()
