"""Talkback starts sooner: a long first sentence is said in two pieces, the intro is rendered once, and the worker
keeps to the fastest cores."""

import importlib.util
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np

from tests import sandbox

spec = importlib.util.spec_from_file_location("talkback_worker", Path(sandbox.REPO) / "talkback/scripts/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class FirstPiece(unittest.TestCase):
    def test_a_short_sentence_is_said_whole(self):
        self.assertEqual(worker.first_piece("Done, the tests pass now."), ("Done, the tests pass now.", ""))

    def test_a_long_sentence_splits_at_its_first_punctuation(self):
        head, rest = worker.first_piece("The tests pass and the build works, so you can try it on the phone tonight")
        self.assertEqual(head, "The tests pass and the build works,")
        self.assertEqual(rest, "so you can try it on the phone tonight")

    def test_without_punctuation_it_is_said_whole(self):
        sentence = "The tests pass and the build works so you can try it on the phone tonight"
        self.assertEqual(worker.first_piece(sentence), (sentence, ""))  # never "between my | first"

    def test_a_one_word_lead_does_not_split_there(self):
        head, _ = worker.first_piece("Done: the tests pass and I pushed the fix to the branch, for you to try")
        self.assertEqual(head, "Done: the tests pass and I pushed the fix to the branch,")


class Pieces(unittest.TestCase):
    def test_words_are_unchanged_when_joined(self):
        sentences = ["Done.", "Here is a much longer second sentence, that takes a while to render; properly and fully."]
        self.assertEqual(" ".join(worker.pieces(sentences).pieces), " ".join(sentences))  # hands-free's echo match

    def test_never_cut_without_punctuation(self):
        plan = worker.pieces(["Got it.", "Tell me if you still hear a pause between my first and second sentence."])
        self.assertEqual(plan.pieces, ["Got it.", "Tell me if you still hear a pause between my first and second sentence."])
        self.assertIn(1, plan.late)  # the first piece may wait for it a little: a pause before speaking, not in it

    def test_a_long_sentence_after_a_short_one_is_cut_at_its_commas(self):
        plan = worker.pieces(["Okay.", "The reviewer passed it, so I committed and pushed the change, and the phone "
                                       "will get it on the next install."])
        self.assertEqual(plan.pieces[1], "The reviewer passed it,")
        for piece in plan.pieces[1:-1]:
            self.assertIn(piece[-1], worker.CUTS)  # every cut at punctuation
        self.assertNotIn(1, plan.joined)  # a new sentence: the normal pause
        self.assertEqual(plan.joined, set(range(2, len(plan.pieces))))  # the rest of it: short joins
        self.assertEqual(plan.sentence, [0] + [1] * (len(plan.pieces) - 1))

    def test_whole_when_there_is_audio_enough_ahead(self):
        first = "I looked at the logs, and the test runs on the laptop, the phone and the server without trouble."
        plan = worker.pieces([first, "Short, but cuttable."])
        self.assertEqual(plan.pieces[-1], "Short, but cuttable.")  # made long before the first sentence ends

    def test_never_cut_inside_a_tag(self):
        words = "Well, [clear throat] that part, the long one, is done now".split()
        for cut in worker.cuts(words):
            self.assertEqual(" ".join(words[:cut]).count("["), " ".join(words[:cut]).count("]"))
        self.assertNotIn(2, worker.cuts("I think [clear throat] it works now fine".split()))

    def test_the_intro_comes_first_and_is_its_own_sentence(self):
        plan = worker.pieces(["The tests pass and the build works, so you can try it on the phone tonight."],
                             intro="Claude here, from henchline.")
        self.assertEqual(plan.pieces[0], "Claude here, from henchline.")
        self.assertEqual(plan.sentence[0], -1)
        self.assertEqual(" ".join(plan.pieces[1:]), "The tests pass and the build works, so you can try it on the phone "
                                                     "tonight.")

    def test_a_cut_resumes_from_the_start_of_its_sentence(self):
        sentences = ["Okay.", "The reviewer passed it, so I committed and pushed the change, and the phone will get "
                              "it on the next install.", "Tell me when."]
        plan = worker.pieces(sentences, intro="Claude here, from henchline.")
        mid = next(i for i, s in enumerate(plan.sentence) if s == 1 and i in plan.joined)  # a later piece of sentence 1
        self.assertEqual(plan.left(mid, sentences), sentences[1:])  # whole sentences, not leftover pieces
        self.assertEqual(plan.left(0, sentences), sentences)  # cut in the intro: everything again
        self.assertEqual(plan.left(len(plan.pieces), sentences), [])

    def test_empty(self):
        self.assertEqual(worker.pieces([]).pieces, [])


class RenderSpeed(unittest.TestCase):
    def test_a_busy_laptop_plans_slower_renders(self):
        cores = os.cpu_count() or 1
        with mock.patch.object(worker.os, "getloadavg", return_value=(0.6 * cores, 0, 0)):
            self.assertEqual(worker.render_rtf(), worker.RENDER_RTF_BUSY)
        with mock.patch.object(worker.os, "getloadavg", return_value=(0.1 * cores, 0, 0)):
            self.assertEqual(worker.render_rtf(), worker.RENDER_RTF)
        with mock.patch.object(worker.os, "getloadavg", side_effect=OSError):
            self.assertEqual(worker.render_rtf(), worker.RENDER_RTF)

    def test_slower_renders_cut_sooner(self):
        sentences = ["Sure, here it is.", "I looked into the slow start, and the first piece is rendered alone, so "
                                          "speech starts sooner."]
        quick, slow = worker.pieces(sentences, rtf=0.3), worker.pieces(sentences, rtf=0.9)
        self.assertEqual(" ".join(quick.pieces), " ".join(slow.pieces))
        self.assertGreater(len(slow.pieces), len(quick.pieces))  # a busy laptop needs the comma cut


class Trim(unittest.TestCase):
    def test_quiet_edges_are_cut_where_pieces_join(self):
        rate = worker.RATE
        quiet, loud = np.zeros(int(0.2 * rate), "<f4"), np.full(int(0.5 * rate), 0.5, "<f4")
        samples = np.concatenate([quiet, loud, quiet])
        both = worker.trim(samples, lead=True, tail=True)
        self.assertAlmostEqual(len(both) / rate, 0.5 + 2 * worker.EDGE, places=2)
        self.assertEqual(len(worker.trim(samples, lead=False, tail=False)), len(samples))  # sentence edges kept
        only_tail = worker.trim(samples, lead=False, tail=True)
        self.assertAlmostEqual(len(only_tail) / rate, 0.2 + 0.5 + worker.EDGE, places=2)

    def test_soft_sounds_at_the_edges_are_kept(self):
        rate = worker.RATE
        soft = np.full(int(0.08 * rate), 0.004, "<f4")  # a soft "s" before the comma: about -42 dB of a 0.5 peak
        samples = np.concatenate([np.zeros(int(0.2 * rate), "<f4"), np.full(int(0.5 * rate), 0.5, "<f4"), soft,
                                  np.zeros(int(0.2 * rate), "<f4")])
        trimmed = worker.trim(samples, lead=True, tail=True)
        self.assertAlmostEqual(len(trimmed) / rate, 0.5 + 0.08 + 2 * worker.EDGE, places=2)

    def test_silence_and_empty_are_left_alone(self):
        self.assertEqual(len(worker.trim(np.zeros(100, "<f4"), True, True)), 100)
        self.assertEqual(len(worker.trim(np.zeros(0, "<f4"), True, True)), 0)


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
