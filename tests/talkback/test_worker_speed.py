"""Talkback starts sooner: a long first sentence is said in two pieces, the intro is rendered once, and the worker
keeps to the fastest cores."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tests import sandbox

spec = importlib.util.spec_from_file_location("talkback_worker", Path(sandbox.REPO) / "talkback/scripts/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class FirstPiece(unittest.TestCase):
    def test_a_short_sentence_is_said_whole(self):
        self.assertEqual(worker.first_piece("Done, the tests pass now."), ("Done, the tests pass now.", ""))

    def test_a_long_sentence_splits_after_eight_words(self):
        sentence = "I pushed the fix to the branch and the phone should pick it up on the next refresh"
        head, rest = worker.first_piece(sentence)
        self.assertEqual(head, "I pushed the fix to the branch and")
        self.assertEqual(f"{head} {rest}", sentence)

    def test_a_comma_in_reach_splits_there(self):
        head, rest = worker.first_piece("The tests pass and the build works, so you can try it on the phone tonight")
        self.assertEqual(head, "The tests pass and the build works,")
        self.assertTrue(rest.startswith("so you"))

    def test_a_one_word_lead_does_not_split_there(self):
        # "Done:" alone would leave a gap while the rest renders: it waits for a piece of some length
        head, _ = worker.first_piece("Done: the tests pass and I pushed the fix to the branch for you to try")
        self.assertEqual(len(head.split()), worker.FIRST_WORDS)

    def test_no_split_leaves_a_tiny_rest(self):
        head, rest = worker.first_piece("One two three four five six seven eight nine ten, eleven")
        self.assertGreaterEqual(len(rest.split()), 3)


class FastCores(unittest.TestCase):
    def cpus(self, clocks):
        root = Path(tempfile.mkdtemp())
        for n, clock in enumerate(clocks):
            (root / f"cpu{n}/cpufreq").mkdir(parents=True)
            (root / f"cpu{n}/cpufreq/cpuinfo_max_freq").write_text(str(clock))
        (root / "cpufreq").mkdir()  # not a cpu
        return root

    def test_the_top_tiers_within_ten_percent(self):
        root = self.cpus([5000000] * 4 + [4700000] * 8 + [3800000] * 8 + [2500000] * 2)
        self.assertEqual(worker.fast_cores(root), set(range(12)))

    def test_alike_cores_are_not_pinned(self):
        self.assertIsNone(worker.fast_cores(self.cpus([3000000] * 8)))

    def test_too_few_fast_cores_are_not_pinned(self):
        self.assertIsNone(worker.fast_cores(self.cpus([5000000] * 2 + [3000000] * 8)))

    def test_unreadable_clocks_are_not_pinned(self):
        root = Path(tempfile.mkdtemp())
        (root / "cpu0").mkdir()
        self.assertIsNone(worker.fast_cores(root))


class IntroCache(unittest.TestCase):
    def test_an_intro_is_rendered_once_per_voice(self):
        made = []

        class Kokoro:
            def create(self, text, voice, lang):
                made.append((text, voice))
                return np.ones(10, dtype="<f4"), 24000

        voice = worker.Voice.__new__(worker.Voice)
        voice._kokoro, voice.intros, voice.intros_lock = Kokoro(), {}, worker.threading.Lock()
        for _ in range(3):
            voice.render("Claude here, from henchline.", "af_heart", keep=True)
        voice.render("Claude here, from henchline.", "af_bella", keep=True)
        voice.render("Something else.", "af_heart")
        voice.render("Something else.", "af_heart")
        self.assertEqual(made.count(("Claude here, from henchline.", "af_heart")), 1)
        self.assertEqual(made.count(("Something else.", "af_heart")), 2)

    def test_the_cache_stays_small(self):
        class Kokoro:
            def create(self, text, voice, lang):
                return np.ones(1, dtype="<f4"), 24000

        voice = worker.Voice.__new__(worker.Voice)
        voice._kokoro, voice.intros, voice.intros_lock = Kokoro(), {}, worker.threading.Lock()
        for n in range(worker.INTROS + 5):
            voice.render(f"Agent{n} here, from x.", "af_heart", keep=True)
        self.assertEqual(len(voice.intros), worker.INTROS)


if __name__ == "__main__":
    unittest.main()
