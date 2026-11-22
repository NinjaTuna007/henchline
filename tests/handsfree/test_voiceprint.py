"""The voiceprint as a soft signal: synthetic voiceprints only (no recordings), failing open everywhere, and never
stopping a clip whose words were its own."""

import json
import os
import unittest
from types import SimpleNamespace

import numpy as np

from tests import sandbox  # noqa: F401
import listener
import voiceprint
from talkover import TalkOver, Verdict
from voice import state
from voiceprint import EC, RAW, THRESHOLDS, WAKE_LOW, Check, Model, Voiceprint

CLIP = "the weather today is sunny with a light breeze from the west and warm afternoons"
DIM = 192
rng = np.random.default_rng(7)
USER = voiceprint.unit(rng.standard_normal(DIM))


def at_score(score: float) -> np.ndarray:
    """A unit voiceprint whose cosine with USER is exactly `score`."""
    other = rng.standard_normal(DIM)
    other -= other.dot(USER) * USER
    return score * USER + np.sqrt(1 - score ** 2) * voiceprint.unit(other)


def speech(seconds: float, level: float = 0.1) -> np.ndarray:
    return (level * np.sin(np.arange(round(seconds * 16000)) * 0.05)).astype(np.float32)


def scoring(score: float | None) -> Voiceprint:
    return Voiceprint({EC: USER, RAW: USER}, lambda audio: None if score is None else at_score(score))


class Scoring(unittest.TestCase):
    def test_score_is_the_cosine_with_the_paths_centroid(self):
        self.assertAlmostEqual(scoring(0.42).score(speech(2), EC), 0.42, places=5)

    def test_no_opinion_without_enough_speech_or_a_centroid(self):
        vp = scoring(0.9)
        self.assertIsNone(vp.check(speech(0.3), EC))
        self.assertIsNone(vp.check(np.zeros(32000, dtype=np.float32), EC))
        self.assertIsNone(Voiceprint({RAW: USER}, lambda a: at_score(0.9)).check(speech(2), EC))

    def test_gaps_between_scraps_are_left_out(self):
        took = []
        vp = Voiceprint({EC: USER}, lambda a: took.append(len(a)) or at_score(0.5))
        scraps = np.concatenate([speech(0.3), np.zeros(16000, np.float32), speech(0.4), np.zeros(8000, np.float32)])
        self.assertIsNotNone(vp.check(scraps, EC))
        self.assertLessEqual(took[0], round(0.75 * 16000))

    def test_failures_never_raise(self):
        def broken(audio):
            raise RuntimeError("onnx")
        self.assertIsNone(Voiceprint({EC: USER}, broken).check(speech(2), EC))
        self.assertIsNone(Voiceprint({EC: USER}, lambda a: np.full(DIM, np.nan)).check(speech(2), EC))
        self.assertIsNone(Model(state.SHARE / "no-such-model.onnx").embed(speech(2)))

    def test_missing_or_broken_enrolment_means_none(self):
        path = state.SHARE / "vp-test.json"
        self.assertIsNone(Voiceprint.load(path))
        path.write_text("{not json")
        self.assertIsNone(Voiceprint.load(path))
        path.write_text(json.dumps({"centroids": {"ec": USER.tolist()}}))
        self.assertIsNotNone(Voiceprint.load(path, Model(state.SHARE / "no-such-model.onnx")))

    def test_thresholds(self):
        self.assertTrue(Check(THRESHOLDS[EC], EC).user)
        self.assertFalse(Check(THRESHOLDS[EC] - 0.01, EC).user)
        self.assertTrue(Check(WAKE_LOW[RAW] - 0.01, RAW).unlike)
        self.assertEqual(Check(0.4237, RAW).said(), "voiceprint: 0.42 (raw)")


class Enrolment(unittest.TestCase):
    def test_identical_twin_takes_are_left_out_and_the_file_is_private(self):
        folder = state.SHARE / "voice-id-test"
        folder.mkdir(parents=True, exist_ok=True)
        import wave
        def write(name, audio):
            with wave.open(str(folder / name), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
                w.writeframes((audio * 32767).astype("<i2").tobytes())
        write("laptop-1.wav", speech(4))
        write("room-raw-1.wav", speech(4, 0.2))
        write("room-ec-1.wav", speech(4, 0.2))  # the canceller wasn't running: same as raw
        write("room2-raw-2.wav", speech(4, 0.3))
        write("room2-ec-2.wav", speech(4, 0.05))
        found = voiceprint.recordings(folder)
        self.assertEqual([f.name for f in found[RAW]], ["laptop-1.wav", "room2-raw-2.wav"])
        self.assertEqual([f.name for f in found[EC]], ["room2-ec-2.wav"])

        model = SimpleNamespace(warm=lambda: True, embed=lambda a: at_score(0.6), path=state.SHARE / "m.onnx")
        voiceprint.speech_only, real = (lambda a: a), voiceprint.speech_only
        try:
            out = folder / "voiceprint.json"
            self.assertEqual(voiceprint.enrol(folder, out, model, say=lambda s: None), 0)
        finally:
            voiceprint.speech_only = real
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads(out.read_text())["centroids"]), {RAW, EC})


class Fixture:
    """A TalkOver on a fake clock, with what Whisper "hears" scripted, and a voiceprint scoring `score`."""

    def __init__(self, heard: list[str], score: float | None, vp: bool = True):
        self.now = [0.0]
        self.heard = list(heard)
        self.over = TalkOver(lambda audio: self.heard.pop(0) if len(self.heard) > 1 else self.heard[0],
                             clock=lambda: self.now[0], log_to=state.STATE / "test.log")
        self.checks = 0
        if vp:
            def embed(audio):
                self.checks += 1
                return None if score is None else at_score(score)
            self.over.voiceprint = Voiceprint({EC: USER}, embed)

    def voice_for(self, seconds: float) -> list[Verdict]:
        verdicts = []
        frame = (0.1 * np.sin(np.arange(480) * 0.05)).astype(np.float32)
        for _ in range(round(seconds / 0.03)):
            self.now[0] += 0.03
            v = self.over.feed(frame, True, [frame] * 15, "a voice", CLIP, clip="clip-1")  # the listener's preroll
            if v is not Verdict.WAITING:
                verdicts.append(v)
            if v is Verdict.USER:
                break
        return verdicts


class TalkingOver(unittest.TestCase):
    def test_the_users_voice_overrules_too_few_words(self):
        f = Fixture(["um"], score=THRESHOLDS[EC] + 0.05)
        verdicts = f.voice_for(0.4)  # each frame stands for a 90 ms voice check: 1.2 s of voice, short of UNSURE_LIMIT
        self.assertEqual(verdicts, [Verdict.USER])
        self.assertIn("voiceprint: ", (state.STATE / "test.log").read_text())

    def test_without_the_voiceprint_too_few_words_keep_waiting(self):
        for f in (Fixture(["um"], score=None), Fixture(["um"], score=0.9, vp=False),
                  Fixture(["um"], score=THRESHOLDS[EC] - 0.05)):
            self.assertEqual(f.voice_for(0.4), [])

    def test_the_clips_own_words_never_stop_it_whatever_the_voiceprint(self):
        f = Fixture(["the weather today is sunny with a light breeze"], score=0.99)
        verdicts = f.voice_for(4.0)
        self.assertTrue(verdicts)
        self.assertNotIn(Verdict.USER, verdicts)
        self.assertTrue(all(v is Verdict.ECHO for v in verdicts))

    def test_after_an_echo_the_voiceprint_doesnt_stop_it(self):
        # That attempt holds our voice: the words alone decide, as without a voiceprint
        f = Fixture(["the weather today is sunny", "um"], score=0.99)
        verdicts = f.voice_for(3.0)
        self.assertNotIn(Verdict.USER, verdicts)

    def test_a_busy_laptop_lets_the_words_decide_alone(self):
        f = Fixture(["um"], score=0.99)
        f.over.strict = True
        self.assertEqual(f.voice_for(0.4), [])

    def test_a_clear_stop_doesnt_wait_for_a_voiceprint(self):
        f = Fixture(["stop that please wait a moment"], score=0.9)
        self.assertEqual(f.voice_for(1.0), [Verdict.USER])
        self.assertEqual(f.checks, 0)


class WakeWord(unittest.TestCase):
    def listener(self, score: float | None, again: bool | Exception, hinted: bool = True):
        lst = listener.Listener.__new__(listener.Listener)
        lst.calling = False
        lst.voiceprint = scoring(score) if score is not False else None
        lst.wake = SimpleNamespace(audio=speech(1.2), hinted=hinted, recheck=None)
        self.rechecks = 0

        def run(use):
            self.rechecks += 1
            if isinstance(again, Exception):
                raise again
            return again
        lst.transcriber = SimpleNamespace(run=run)
        return lst

    def test_the_users_voice_needs_nothing_more(self):
        self.assertTrue(self.listener(0.5, False).sounds_like_user("whistle"))
        self.assertEqual(self.rechecks, 0)

    def test_an_unlike_voice_needs_whisper_without_a_hint(self):
        low = WAKE_LOW[EC] - 0.05
        self.assertFalse(self.listener(low, False).sounds_like_user("whistle"))
        self.assertTrue(self.listener(low, True).sounds_like_user("whistle"))
        self.assertEqual(self.rechecks, 1)

    def test_never_a_lockout(self):
        low = WAKE_LOW[EC] - 0.05
        self.assertTrue(self.listener(low, RuntimeError("cuda")).sounds_like_user("whistle"))  # Whisper failing
        self.assertTrue(self.listener(None, False).sounds_like_user("whistle"))  # no score
        self.assertTrue(self.listener(False, False).sounds_like_user("whistle"))  # not enrolled / off
        self.assertTrue(self.listener(low, False, hinted=False).sounds_like_user("whisper"))  # already unhinted


class Setting(unittest.TestCase):
    def test_off_in_config_or_not_enrolled_means_no_voiceprint(self):
        self.assertIsNone(listener.Listener.load_voiceprint(SimpleNamespace(voiceprint=False)))
        self.assertIsNone(listener.Listener.load_voiceprint(SimpleNamespace(voiceprint=True)))  # nothing enrolled


if __name__ == "__main__":
    unittest.main()
