"""Talkback the phone speaks itself: the call's offer and fallback, clips and cuts sent once, the phone's acks."""

import tempfile
import unittest
from pathlib import Path

from tests import sandbox  # first: a throwaway HOME
from voice import phonevoice

assert sandbox.HOME  # imported for its effect


class Offer(unittest.TestCase):
    def tearDown(self):
        phonevoice.call_over()

    def test_a_capable_phone_renders_until_it_falls_back(self):
        self.assertFalse(phonevoice.phone_renders())  # no offer: the laptop streams, as before
        phonevoice.offer({"tts": {"engine": "sherpa-kokoro", "model": phonevoice.VERSION}})
        self.assertTrue(phonevoice.phone_renders())
        phonevoice.offer({"tts": "fallback", "reason": "battery 12%"})
        self.assertFalse(phonevoice.phone_renders())

    def test_another_model_or_engine_is_not_taken(self):
        phonevoice.offer({"tts": {"engine": "sherpa-kokoro", "model": "kokoro-v0_19"}})
        self.assertFalse(phonevoice.phone_renders())
        phonevoice.offer({"tts": {"engine": "piper", "model": phonevoice.VERSION}})
        self.assertFalse(phonevoice.phone_renders())

    def test_the_call_ending_clears_the_offer_and_clips(self):
        phonevoice.offer({"tts": {"engine": "sherpa-kokoro", "model": phonevoice.VERSION}})
        phonevoice.send("c1", "claude", "af_heart", 3, ["Hello."], set(), [0])
        phonevoice.call_over()
        self.assertFalse(phonevoice.phone_renders())
        self.assertEqual(phonevoice.outgoing(), [])


class Clips(unittest.TestCase):
    def tearDown(self):
        phonevoice.call_over()

    def test_a_clip_is_sent_once_and_a_cut_after_it(self):
        phonevoice.send("c1", "claude", "af_heart", 3, ["Done.", "Here is more,", "and the rest."], {2}, [0, 1, 1])
        first = phonevoice.outgoing()
        self.assertEqual(first[0]["say"]["pieces"], ["Done.", "Here is more,", "and the rest."])
        self.assertEqual(first[0]["say"]["joined"], [2])
        self.assertEqual(first[0]["say"]["sid"], 3)
        phonevoice.stop("c1")
        self.assertEqual(phonevoice.outgoing(), [{"stop": "c1"}])
        self.assertEqual(phonevoice.outgoing(), [])  # each once

    def test_a_clip_taken_back_before_it_went_out_is_never_sent(self):
        phonevoice.send("c6", "claude", "af_heart", 3, ["One."], set(), [0])
        phonevoice.stop("c6")
        self.assertEqual(phonevoice.outgoing(), [])  # no say, and no stop for something the phone never had
        self.assertFalse((phonevoice.PHONE_SAY / "c6.stop").exists())  # consumed: cancel() stops waiting

    def test_acks_move_progress_forward_only(self):
        phonevoice.send("c2", "claude", "af_heart", 3, ["One.", "Two.", "Three."], set(), [0, 1, 2])
        phonevoice.said({"said": {"id": "c2", "piece": 1}})
        self.assertEqual(phonevoice.progress("c2"), {"spoken": 2, "done": False, "fallback": ""})
        phonevoice.said({"said": {"id": "c2", "piece": 0}})  # a late ack doesn't move it back
        self.assertEqual(phonevoice.progress("c2")["spoken"], 2)
        phonevoice.said({"said": {"id": "c2", "done": True}})
        self.assertTrue(phonevoice.progress("c2")["done"])

    def test_a_fallback_names_the_clip_it_stopped(self):
        phonevoice.offer({"tts": {"engine": "sherpa-kokoro", "model": phonevoice.VERSION}})
        phonevoice.send("c3", "claude", "af_heart", 3, ["One."], set(), [0])
        phonevoice.offer({"tts": "fallback", "reason": "fell behind 1.2 s", "id": "c3"})
        self.assertEqual(phonevoice.progress("c3")["fallback"], "fell behind 1.2 s")

    def test_cancel_passes_the_stop_on_before_the_files_go(self):
        import threading
        phonevoice.send("c4", "claude", "af_heart", 3, ["One."], set(), [0])
        phonevoice.outgoing()  # sent
        seen = []
        server = threading.Thread(target=lambda: [seen.extend(phonevoice.outgoing()) or __import__("time").sleep(0.05)
                                                  for _ in range(10)], daemon=True)
        server.start()
        phonevoice.cancel("c4")
        server.join(2)
        self.assertIn({"stop": "c4"}, seen)
        self.assertFalse((phonevoice.PHONE_SAY / "c4.json").exists())

    def test_cancel_with_no_call_to_pass_it_on_gives_up_and_cleans(self):
        phonevoice.send("c5", "claude", "af_heart", 3, ["One."], set(), [0])
        phonevoice.cancel("c5", wait=0.1)
        self.assertEqual(list(phonevoice.PHONE_SAY.glob("c5*")), [])

    def test_bad_ids_and_unknown_clips_are_ignored(self):
        phonevoice.said({"said": {"id": "../escape", "piece": 0}})
        phonevoice.said({"said": {"id": "never-sent", "piece": 0}})
        self.assertFalse((phonevoice.PHONE_SAY / "never-sent.progress").exists())
        self.assertEqual(phonevoice.progress("never-sent"), {"spoken": 0, "done": False, "fallback": ""})


class Model(unittest.TestCase):
    def test_manifest_hashes_each_file_and_serves_only_those(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(phonevoice.manifest(root))  # not prepared yet
            for name in phonevoice.FILES:
                (root / name).write_bytes(name.encode())
            found = phonevoice.manifest(root)
            self.assertEqual([f["name"] for f in found["files"]], list(phonevoice.FILES))
            self.assertEqual(found["files"][0]["size"], len(phonevoice.FILES[0]))
            self.assertEqual(len(found["files"][0]["sha256"]), 64)
            self.assertEqual(phonevoice.manifest(root), found)  # from the kept hashes the second time
            (root / "secret.txt").write_text("x")
            self.assertIsNone(phonevoice.model_file("secret.txt", root))
            self.assertIsNone(phonevoice.model_file("../model.onnx", root))
            self.assertEqual(phonevoice.model_file("tokens.txt", root), root / "tokens.txt")

    def test_voice_ids_are_places_in_the_sorted_names(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            voices = Path(tmp) / "voices.npz"
            np.savez(voices, bf_alice=np.zeros(2), af_heart=np.zeros(2), af_bella=np.zeros(2))
            self.assertEqual(phonevoice.voice_id("af_bella", voices), 0)
            self.assertEqual(phonevoice.voice_id("af_heart", voices), 1)
            self.assertEqual(phonevoice.voice_id("bf_alice", voices), 2)
            self.assertIsNone(phonevoice.voice_id("nobody", voices))
