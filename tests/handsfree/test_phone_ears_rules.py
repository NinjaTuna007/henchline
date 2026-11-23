"""The phone's own ears end call turns and tell our echo by the laptop's rules (call/android PhoneEar.kt): the shared
fixture tests/fixtures/phone-ears.json is pinned here against the laptop's code, and on the JVM against the phone's."""

import json
import unittest
from types import SimpleNamespace

from tests import sandbox  # first: a throwaway HOME
import talkover
import turn
from sensing import FRAME, RATE
from voice.speech import nemotron

FIXTURE = json.loads((sandbox.REPO / "tests/fixtures/phone-ears.json").read_text())


def ms(frames: int) -> int:
    return round(frames * FRAME / RATE * 1000)


class Constants(unittest.TestCase):
    def test_the_phone_ends_turns_as_the_laptop_does_on_a_call(self):
        self.assertEqual(FIXTURE["constants"], {
            "call_end_ms": ms(turn.CALL_END_FRAMES), "call_early_end_ms": ms(turn.CALL_EARLY_END_FRAMES),
            "no_stop_ms": ms(turn.NO_STOP_FRAMES), "over_ms": ms(turn.OVER_FRAMES),
            "voice_gone_ms": round(turn.VOICE_GONE * 1000), "max_ms": turn.MAX_SECONDS * 1000,
            "end_db": turn.END_DB, "below_peak_db": turn.BELOW_PEAK,
            "echo_turn_ms": round(turn.ECHO_TURN_SECONDS * 1000), "mostly_ours": turn.MOSTLY_OURS,
            "own_words_min": talkover.OWN_WORDS_MIN, "own_words": talkover.OWN_WORDS,
            "own_run": talkover.OWN_RUN, "own_share": talkover.OWN_SHARE,
        })

    def test_the_same_words_lead_on(self):
        self.assertEqual(set(FIXTURE["trailing"]), turn.TRAILING)


class Vectors(unittest.TestCase):
    def test_our_own_words(self):
        for case in FIXTURE["own_words"]:
            with self.subTest(heard=case["heard"]):
                self.assertEqual(talkover.word_keys(case["heard"]), case["keys"])
                self.assertAlmostEqual(talkover.own_share(case["heard"], case["said"]), case["share"], places=5)
                self.assertEqual(talkover.our_own_words(case["heard"], case["said"]), case["ours"])
                self.assertEqual(turn.echo_turn(case["heard"], case["said"], 1.0), case["echo_short"])
                self.assertEqual(turn.echo_turn(case["heard"], case["said"], 5.0), case["echo_long"])

    def test_a_finished_sentence(self):
        for case in FIXTURE["sentence_done"]:
            fake = SimpleNamespace(live=SimpleNamespace(so_far=lambda said=case["said"]: said))
            with self.subTest(said=case["said"]):
                self.assertEqual(turn.Turn.sentence_done(fake, 0), case["done_at_once"])
                self.assertEqual(turn.Turn.sentence_done(fake, turn.NO_STOP_FRAMES), case["done_later"])

    def test_over(self):
        for case in FIXTURE["over"]:
            with self.subTest(said=case["said"]):
                self.assertEqual(bool(turn.OVER.search(case["said"].strip())), case["over"])

    def test_a_hint_word_said_once(self):
        for case in FIXTURE["tidy"]:
            with self.subTest(text=case["text"]):
                self.assertEqual(nemotron.tidy(case["text"], FIXTURE["hints"]), case["tidied"])


if __name__ == "__main__":
    unittest.main()
