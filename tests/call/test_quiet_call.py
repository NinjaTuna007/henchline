"""A text that goes unanswered escalates to a call; a quiet call asks after you, then calls back."""

import importlib.util
import unittest

from tests import sandbox  # first: a throwaway HOME
from voice.state import TEXTS, Sent, read_json, write_json

spec = importlib.util.spec_from_file_location("call_server_quiet", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

hf_spec = importlib.util.spec_from_file_location("handsfree_cli", sandbox.REPO / "handsfree/handsfree.py")
handsfree = importlib.util.module_from_spec(hf_spec)
hf_spec.loader.exec_module(handsfree)


class Replied(unittest.TestCase):
    def test_a_message_answers_that_agents_texts_only(self):
        TEXTS.mkdir(parents=True, exist_ok=True)
        write_json(TEXTS / "aaaa.json", {"id": "aaaa", "session": "claude-x", "status": "delivered"})
        write_json(TEXTS / "bbbb.json", {"id": "bbbb", "session": "codex-y", "status": "delivered"})
        Sent("claude", "texted", session="claude-x").save()  # any delivery of theirs to that agent
        self.assertTrue(read_json(TEXTS / "aaaa.json").get("replied"))
        self.assertFalse(read_json(TEXTS / "bbbb.json").get("replied"))
        self.assertTrue(handsfree.text_replied("aaaa"))
        self.assertFalse(handsfree.text_replied("bbbb"))
        self.assertFalse(handsfree.text_replied(""))


class Reminders(unittest.TestCase):
    """A text no agent sends (a timer, a script over ssh) is from Reminders, not a nameless "Agent"."""

    def test_a_named_script_gets_its_own_reminders_chat(self):
        self.assertEqual(handsfree.sender("Class reminders"), ("reminders", "reminders-class_reminders"))
        self.assertEqual(handsfree.sender(""), ("reminders", "reminders-reminders"))
        self.assertEqual(handsfree.sender("  !!  "), ("reminders", "reminders-reminders"))

    def test_with_no_agent_behind_it_a_text_is_kept_as_from_reminders(self):
        from unittest import mock
        TEXTS.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(handsfree, "caller", return_value=None), \
             mock.patch.object(handsfree, "identity", return_value=("agent", "agent-_tmp")), \
             mock.patch.object(handsfree, "in_call", return_value=False), \
             mock.patch.object(handsfree, "push", return_value=False):
            handsfree.text("class in 30 minutes")
            handsfree.text("dryer done", sent_by="Laundry")
        kept = sorted((read_json(p) for p in TEXTS.glob("*.json")), key=lambda r: r.get("at", 0))
        mine = [(r["agent"], r["session"]) for r in kept if r.get("words") in ("class in 30 minutes", "dryer done")]
        self.assertEqual(mine, [("reminders", "reminders-reminders"), ("reminders", "reminders-laundry")])


class RingAs(unittest.TestCase):
    def test_unknown_agent_is_not_rung(self):
        self.assertEqual(handsfree.ring("why", now=True, wait=False, as_session="claude-nobody"), 1)


class ScriptRing(unittest.TestCase):
    """A ring no agent placed (a script's reminder): the call says why it rang and whom the user's words go to,
    and that agent hears the reason with the first turn. Rings from agents go to the agent as before."""

    WHY = "Class in 30 minutes: Algebra B at 22:30"

    def setUp(self):
        from voice.state import CALL_HANGUP, CALL_RING_NOTE, CALL_RINGER
        for path in (CALL_HANGUP, CALL_RING_NOTE, CALL_RINGER, handsfree.RANG):
            path.unlink(missing_ok=True)

    def ring(self, speaker):
        from unittest import mock
        with mock.patch.object(handsfree, "caller", return_value=speaker), \
             mock.patch.object(handsfree, "in_call", return_value=False), \
             mock.patch.object(handsfree, "ring_henchline", return_value=True), \
             mock.patch.object(handsfree, "log"):
            self.assertEqual(handsfree.ring(self.WHY, now=True, wait=False, urgent=True), 0)
        from voice.state import CALL_RINGER
        return read_json(CALL_RINGER)

    def answer(self, reply, now, reachable=True):
        from unittest import mock
        said = mock.MagicMock()
        with mock.patch.object(server, "last_speaker", return_value=now), \
             mock.patch.object(server.routing, "reachable", return_value=reachable), \
             mock.patch.object(server.routing, "say", said), \
             mock.patch.object(server, "log"):
            server.route_to_ringer(reply)
        return said

    def test_the_ring_keeps_its_reason_when_no_agent_rang(self):
        ringer = self.ring(None)
        self.assertEqual((ringer["session"], ringer["why"]), ("", self.WHY))
        self.assertTrue(ringer["reply"].startswith("henchline-"))

    def test_an_agents_ring_is_unchanged(self):
        from voice.state import Speaker
        ringer = self.ring(Speaker("claude-ringer", "claude", "henchline"))
        self.assertEqual((ringer["session"], ringer["agent"]), ("claude-ringer", "claude"))
        self.assertNotIn("why", ringer)

    def test_answering_says_why_and_whom_the_words_go_to(self):
        from voice.state import CALL_HANGUP, CALL_RING_NOTE, Speaker
        reply = self.ring(None)["reply"]
        said = self.answer(reply, Speaker("claude-1", "claude", "henchline"))
        said.assert_called_once_with(f"Reminder: {self.WHY}. You're through to Claude, in henchline.")
        self.assertEqual(read_json(CALL_RING_NOTE), {"why": self.WHY})
        self.assertFalse(CALL_HANGUP.exists())

    def test_one_it_cant_type_to_yet_is_said_and_the_call_stays_up(self):
        from voice.state import CALL_HANGUP, CALL_RING_NOTE, Speaker
        reply = self.ring(None)["reply"]
        said = self.answer(reply, Speaker("claude-1", "claude", "henchline"), reachable=False)
        line = said.call_args.args[0]
        self.assertTrue(line.startswith(f"Reminder: {self.WHY}. "))
        self.assertIn("Claude, in henchline, but I can't type to it yet, so pick someone: say switch me to", line)
        self.assertEqual(read_json(CALL_RING_NOTE), {"why": self.WHY})  # for whoever they pick
        self.assertFalse(CALL_HANGUP.exists())

    def test_with_nobody_to_go_to_it_says_so_and_hangs_up(self):
        from voice.state import CALL_HANGUP, CALL_RING_NOTE
        reply = self.ring(None)["reply"]
        said = self.answer(reply, None)
        self.assertIn("Reminder: Class in 30 minutes", said.call_args.args[0])
        self.assertIn("hang up", said.call_args.args[0])
        said.return_value.wait.assert_called_once()  # queued before the hang-up is asked for
        self.assertTrue(CALL_HANGUP.exists())
        self.assertFalse(CALL_RING_NOTE.exists())

    def test_answering_an_agents_ring_still_goes_to_that_agent(self):
        from unittest import mock
        from voice.state import CALL_RING_NOTE, SPEAKERS, Speaker
        ringer = Speaker("claude-ringer", "claude", "henchline")
        SPEAKERS.mkdir(parents=True, exist_ok=True)
        ringer.save(SPEAKERS / "claude-ringer.json")
        reply = self.ring(ringer)["reply"]
        with mock.patch.object(server.routing, "reachable", return_value=True), \
             mock.patch.object(server.routing, "switch_to") as switch:
            said = self.answer(reply, None)
        switch.assert_called_once()
        self.assertEqual(switch.call_args.args[0].session, "claude-ringer")
        said.assert_not_called()
        self.assertFalse(CALL_RING_NOTE.exists())


class Nudges(unittest.TestCase):
    def test_two_nudges_then_call_back(self):
        self.assertEqual(len(server.NUDGES), 2)
        self.assertLess(server.NUDGE_AFTER, server.NUDGE_UNSURE)


if __name__ == "__main__":
    unittest.main()
