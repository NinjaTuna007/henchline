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

    def test_a_fallback_goes_on_from_the_piece_after_the_last_one_played(self):
        clip, path = clip_file("First sentence here. Second sentence here. Third one too.")

        def falls_back(say):
            phonevoice.said({"said": {"id": say["id"], "piece": 2}})  # started the third piece (rendering ahead)
            # but only the first two had played: nothing heard is said again
            phonevoice.offer({"tts": "fallback", "reason": "fell behind", "id": say["id"], "heard": 1})

        thread = phone(falls_back)
        worker.phone_play(FakeVoice(), clip, path, "")
        thread.join(2)
        self.assertEqual(spool.Clip.load(path).sentences, ["Third one too."])

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


class Timeout(unittest.TestCase):
    """The phone never starts a clip: the laptop takes it back, but tells the phone to stop first."""

    def setUp(self):
        phonevoice.offer({"tts": {"engine": "sherpa-kokoro", "model": phonevoice.VERSION}})
        for p in [mock.patch.object(worker, "in_call", return_value=True),
                  mock.patch.object(worker, "mic_busy", return_value=False),
                  mock.patch.object(worker, "PHONE_TIMEOUT", 0.3),
                  mock.patch.object(worker.phonevoice, "voice_id", return_value=3)]:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        phonevoice.call_over()
        for path in SPOOL.glob("*.json"):
            path.unlink()

    def test_a_slow_phone_is_told_to_stop_before_the_laptop_says_it(self):
        clip, path = clip_file("First sentence here. Second sentence here.")
        sent = []
        stop = threading.Event()

        def server():  # the call server passing messages on, as its phone_voice() task does
            while not stop.is_set():
                sent.extend(phonevoice.outgoing())
                time.sleep(0.02)

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        worker.phone_play(FakeVoice(), clip, path, "")
        stop.set()
        thread.join(1)
        kinds = [next(iter(m)) for m in sent]
        self.assertEqual(kinds, ["say", "stop"])  # the say had gone out: the stop follows before the files go
        self.assertFalse((phonevoice.PHONE_SAY / f"{path.name}.json").exists())
        self.assertEqual(spool.Clip.load(path).sentences, ["First sentence here.", "Second sentence here."])
        self.assertFalse(phonevoice.phone_renders())  # the laptop speaks the rest of the call


class DroppedLine(unittest.TestCase):
    """A lift: the phone has the words and goes on speaking; the laptop waits for its report instead of replaying."""

    def setUp(self):
        phonevoice.offer({"tts": {"engine": "sherpa-kokoro", "model": phonevoice.VERSION}})
        self.calling, self.dropped = threading.Event(), threading.Event()
        self.calling.set()
        for p in [mock.patch.object(worker, "in_call", side_effect=lambda: self.calling.is_set()),
                  mock.patch.object(worker, "line_dropped",
                                    side_effect=lambda: {"until": time.time() + 30} if self.dropped.is_set() else None),
                  mock.patch.object(worker, "mic_busy", return_value=False),
                  mock.patch.object(worker.phonevoice, "voice_id", return_value=3)]:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        phonevoice.call_over()
        for path in SPOOL.glob("*.json"):
            path.unlink()

    def test_a_drop_then_the_phones_report_says_nothing_twice(self):
        clip, path = clip_file("First sentence here. Second sentence here. Third one too.")

        def lift(say):
            phonevoice.said({"said": {"id": say["id"], "piece": 0}})
            self.calling.clear(); self.dropped.set()  # the line drops; the phone speaks on, its acks lost
            time.sleep(0.4)
            self.dropped.clear(); self.calling.set()  # back: the phone reports how far it got
            phonevoice.said({"said": {"id": say["id"], "piece": len(say["pieces"]) - 1}})
            phonevoice.said({"said": {"id": say["id"], "done": True}})

        thread = phone(lift)
        worker.phone_play(FakeVoice(), clip, path, "")
        thread.join(3)
        self.assertFalse(path.exists())  # nothing replayed: the phone said it all
        self.assertFalse((phonevoice.PHONE_SAY / f"{path.name}.json").exists())

    def test_if_the_line_never_comes_back_the_rest_is_texted_from_the_last_confirmed_piece(self):
        clip, path = clip_file("First sentence here. Second sentence here. Third one too.")

        def lift(say):
            phonevoice.said({"said": {"id": say["id"], "piece": 1}})  # the second piece had started
            self.calling.clear(); self.dropped.set()
            time.sleep(0.3)
            self.dropped.clear()  # the window passed: the call is over

        thread = phone(lift)
        worker.phone_play(FakeVoice(), clip, path, "")
        thread.join(3)
        kept = spool.Clip.load(path)
        self.assertTrue(kept.for_call)  # texted, not said at the laptop
        self.assertEqual(kept.sentences, ["Second sentence here.", "Third one too."])


class Cap(unittest.TestCase):
    """A phone that started a clip but never says done (a resumed call that forgot it) doesn't hold talkback back."""

    def setUp(self):
        phonevoice.offer({"tts": {"engine": "sherpa-kokoro", "model": phonevoice.VERSION}})
        for p in [mock.patch.object(worker, "in_call", return_value=True),
                  mock.patch.object(worker, "mic_busy", return_value=False),
                  mock.patch.object(worker, "PHONE_SLACK", 0.2),
                  mock.patch.object(worker, "speech", return_value=0.05),
                  mock.patch.object(worker.phonevoice, "voice_id", return_value=3)]:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        phonevoice.call_over()
        for path in SPOOL.glob("*.json"):
            path.unlink()

    def test_no_report_after_the_last_ack_is_taken_as_said_in_time(self):
        clip, path = clip_file("First sentence here. Second sentence here.")
        thread = phone(lambda say: phonevoice.said({"said": {"id": say["id"], "piece": 0}}))  # then silence
        started = time.time()
        worker.phone_play(FakeVoice(), clip, path, "")
        thread.join(2)
        self.assertLess(time.time() - started, 3)  # 2 x 2 x 0.05 + 0.2 s, not the whole call
        self.assertFalse(path.exists())  # taken as said: not repeated


class DropWindowText(unittest.TestCase):
    def setUp(self):
        phonevoice.offer({"tts": {"engine": "sherpa-kokoro", "model": phonevoice.VERSION}})
        self.calling, self.dropped = threading.Event(), threading.Event()
        self.calling.set()
        for p in [mock.patch.object(worker, "in_call", side_effect=lambda: self.calling.is_set()),
                  mock.patch.object(worker, "line_dropped",
                                    side_effect=lambda: {"until": time.time() + 30} if self.dropped.is_set() else None),
                  mock.patch.object(worker, "mic_busy", return_value=False),
                  mock.patch.object(worker.phonevoice, "voice_id", return_value=3)]:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        phonevoice.call_over()
        for path in SPOOL.glob("*.json"):
            path.unlink()

    def test_the_text_starts_at_the_beginning_of_the_sentence_it_reached(self):
        long = ("The reviewer passed the round, so I committed it and pushed, then started the next piece of work "
                "while you were away from the laptop.")
        clip, path = clip_file("Okay. " + long)

        def lift(say):
            self.assertGreater(len(say["pieces"]), 2)  # the long sentence went in pieces
            last = len(say["pieces"]) - 1
            phonevoice.said({"said": {"id": say["id"], "piece": last}})  # the phone reached its last piece
            self.calling.clear(); self.dropped.set()
            time.sleep(0.3)
            self.dropped.clear()  # never back

        with mock.patch.object(worker.phonevoice, "PHONE_RTF", 5.0), \
                mock.patch.object(worker, "PHONE_MIN_CHARS", 0):  # slow and no minimum: the long sentence is cut
            thread = phone(lift)
            worker.phone_play(FakeVoice(), clip, path, "")
            thread.join(3)
        kept = spool.Clip.load(path)
        self.assertTrue(kept.for_call)
        self.assertEqual(kept.sentences, [long])  # the whole sentence, not its last piece
