"""A clip the phone speaks itself: the words go out as pieces, the phone's acks end it, a cut sends a stop, and a
fallback puts what's left back for the laptop to say."""

import importlib.util
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import sandbox
from voice import phonevoice, spool
from voice.state import SPOOL

spec = importlib.util.spec_from_file_location("talkback_worker_phone", Path(sandbox.REPO) / "talkback/scripts/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class FakeVoice:
    skip = False


def clip_file(text: str) -> tuple[spool.Clip, Path]:
    clip = spool.Clip.of_text(text, agent="claude", session="claude-x", voice="af_heart")
    SPOOL.mkdir(parents=True, exist_ok=True)
    path = SPOOL / f"{time.time_ns()}-1.json"
    clip.save(path)
    return clip, path


def phone(script):
    """Plays the phone: waits for the clip to go out, then runs `script(say)` (acks, fallback) in the background."""
    def run():
        until = time.time() + 5
        while time.time() < until:
            out = phonevoice.outgoing()
            for message in out:
                if "say" in message:
                    script(message["say"])
                    return
            time.sleep(0.02)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class PhonePlay(unittest.TestCase):
    def setUp(self):
        phonevoice.offer({"tts": {"engine": "sherpa-kokoro", "model": phonevoice.VERSION}})
        patches = [mock.patch.object(worker, "in_call", return_value=True),
                   mock.patch.object(worker, "mic_busy", return_value=False),
                   mock.patch.object(worker.phonevoice, "voice_id", return_value=3)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        phonevoice.call_over()
        for path in SPOOL.glob("*.json"):
            path.unlink()

    def test_the_phone_gets_the_words_and_its_acks_finish_the_clip(self):
        clip, path = clip_file("Done. The tests pass now.")
        seen = []

        def acks(say):
            seen.append(say)
            for n in range(len(say["pieces"])):
                phonevoice.said({"said": {"id": say["id"], "piece": n}})
            phonevoice.said({"said": {"id": say["id"], "done": True}})

        thread = phone(acks)
        worker.phone_play(FakeVoice(), clip, path, "")
        thread.join(2)
        self.assertEqual(" ".join(seen[0]["pieces"]), "Done. The tests pass now.")
        self.assertEqual(seen[0]["sid"], 3)
        self.assertFalse(path.exists())  # spoken: nothing kept
        self.assertFalse(worker.PLAYING.exists())

    def test_a_fallback_puts_the_rest_back_for_the_laptop(self):
        clip, path = clip_file("First sentence here. Second sentence here. Third one too.")

        def falls_back(say):
            phonevoice.said({"said": {"id": say["id"], "piece": 1}})  # the second piece had started
            phonevoice.offer({"tts": "fallback", "reason": "fell behind", "id": say["id"]})

        thread = phone(falls_back)
        worker.phone_play(FakeVoice(), clip, path, "")
        thread.join(2)
        kept = spool.Clip.load(path)
        self.assertEqual(kept.sentences, ["Second sentence here.", "Third one too."])  # from the sentence it reached
        self.assertFalse(phonevoice.phone_renders())  # the rest of the call streams the laptop's audio

    def test_talking_over_it_sends_a_stop(self):
        clip, path = clip_file("One sentence. Another sentence.")
        stops = []

        def started(say):
            phonevoice.said({"said": {"id": say["id"], "piece": 0}})
            busy.set()
            until = time.time() + 2
            while time.time() < until and not stops:
                stops.extend(m["stop"] for m in phonevoice.outgoing() if "stop" in m)
                time.sleep(0.02)

        busy = threading.Event()
        with mock.patch.object(worker, "mic_busy", side_effect=lambda: busy.is_set()):
            thread = phone(started)
            worker.phone_play(FakeVoice(), clip, path, "")
            thread.join(3)
        self.assertEqual(stops, [path.name])
        self.assertEqual(spool.Clip.load(path).sentences, ["One sentence.", "Another sentence."])  # said again later
