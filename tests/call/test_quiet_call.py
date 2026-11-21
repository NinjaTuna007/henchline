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


class Nudges(unittest.TestCase):
    def test_two_nudges_then_call_back(self):
        self.assertEqual(len(server.NUDGES), 2)
        self.assertLess(server.NUDGE_AFTER, server.NUDGE_UNSURE)


if __name__ == "__main__":
    unittest.main()
