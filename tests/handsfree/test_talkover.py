"""Talking over a clip: an echo verdict no longer throws the user's attempt away, the stricter mode doesn't grow
for ever, a turn over a clip is thrown away less readily, and the call's report waits for the clip's first audio."""

import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from tests import sandbox  # noqa: F401
import listener
import talkover
import turn
from talkover import TalkOver, Verdict
from voice import state

CLIP = "the weather today is sunny with a light breeze from the west and warm afternoons"
FRAME = np.zeros(480, dtype=np.int16)


class Fixture:
    """A TalkOver on a fake clock, with what Whisper "hears" scripted per look."""

    def __init__(self, heard: list[str]):
        self.now = [0.0]
        self.heard = list(heard)
        self.looks = 0
        self.over = TalkOver(self.text, clock=lambda: self.now[0], log_to=state.STATE / "test.log")

    def text(self, audio) -> str:
        self.looks += 1
        return self.heard.pop(0) if len(self.heard) > 1 else self.heard[0]  # the last one, over and over

    def voice_for(self, seconds: float) -> list[Verdict]:
        """Frames of voice, one every 30 ms, for that long; the verdicts that weren't WAITING."""
        verdicts = []
        for _ in range(round(seconds / 0.03)):
            self.now[0] += 0.03
            v = self.over.feed(FRAME, True, [], "a voice", CLIP, clip="clip-1")
            if v is not Verdict.WAITING:
                verdicts.append(v)
            if v is Verdict.USER:
                break
        return verdicts


class EchoKeepsTheAttempt(unittest.TestCase):
    def test_after_an_echo_the_attempt_goes_on_and_new_words_twice_win(self):
        # Each look: mostly the clip's words (our echo), but with the user's own words in it
        f = Fixture(["the weather today is sunny", "sunny with a light breeze stop wait",
                     "a light breeze from the west hold on"])
        verdicts = f.voice_for(3.0)
        self.assertEqual(verdicts[:2], [Verdict.ECHO, Verdict.ECHO])
        self.assertEqual(verdicts[-1], Verdict.USER)
        self.assertIsNone(f.over.over)
        self.assertIsNotNone(f.over.taken)

    def test_one_look_of_new_words_isnt_enough(self):
        f = Fixture(["the weather today is sunny", "sunny with a light breeze stop wait", "sunny with a light breeze"])
        verdicts = f.voice_for(4.0)
        self.assertNotIn(Verdict.USER, verdicts)

    def test_our_echo_alone_never_stops_the_clip_however_long(self):
        f = Fixture(["the weather today is sunny", "sunny with a light breeze from the west"])
        verdicts = f.voice_for(8.0)
        self.assertTrue(verdicts)
        self.assertTrue(all(v is Verdict.ECHO for v in verdicts))
        self.assertIsNotNone(f.over.over)  # still counting

    def test_after_an_echo_only_the_newest_audio_is_looked_at(self):
        seen = []
        f = Fixture([])
        f.over.quick_text = lambda audio: (seen.append(len(audio)), "the weather today is sunny")[1]
        f.voice_for(2.0)
        latest = round(talkover.LATEST * talkover.RATE / talkover.FRAME) * talkover.FRAME
        self.assertGreater(len(seen), 1)
        self.assertLessEqual(max(seen[1:]), latest)

    def test_the_turn_starts_after_our_echo(self):
        f = Fixture(["the weather today is sunny", "sunny with a light breeze stop wait",
                     "a light breeze from the west hold on"])
        f.voice_for(3.0)
        taken = f.over.taken
        self.assertIsNotNone(taken)
        self.assertLess(len(taken.frames), round(3.0 / 0.03))

    def test_too_few_words_after_an_echo_never_stops_it(self):
        # An echo verdict, then looks with a single word, past UNSURE_LIMIT: length alone must not stop the clip
        f = Fixture(["the weather today is sunny", "um"])
        verdicts = f.voice_for(2.4)
        self.assertEqual(verdicts, [Verdict.ECHO])
        self.assertGreater(f.over.over.voiced, talkover.UNSURE_LIMIT)

    def test_the_next_clip_ends_the_attempt(self):
        f = Fixture(["the weather today is sunny"])
        f.voice_for(1.0)
        self.assertIsNotNone(f.over.over)
        f.now[0] += 0.03
        f.over.feed(FRAME, False, [], "a voice", "another clip entirely", clip="clip-2")
        self.assertIsNone(f.over.over)

    def test_careful_needs_three_new_words(self):
        f = Fixture(["the weather today is sunny", "sunny with a light breeze stop wait",
                     "a light breeze from the west hold on"])
        f.over.new_words_needed = 3
        self.assertNotIn(Verdict.USER, f.voice_for(3.0))

    def test_a_user_with_new_words_at_first_still_wins_at_once(self):
        f = Fixture(["hang on stop talking please"])
        self.assertEqual(f.voice_for(1.0), [Verdict.USER])


class Words(unittest.TestCase):
    def test_new_words(self):
        self.assertEqual(talkover.new_words("the weather is sunny", CLIP), 0)
        self.assertEqual(talkover.new_words("stop the weather please", CLIP), 2)

    def test_own_share(self):
        self.assertEqual(talkover.own_share("the weather today", CLIP), 1.0)
        self.assertEqual(talkover.own_share("stop", CLIP), 0.0)  # too few to tell
        self.assertAlmostEqual(talkover.own_share("the weather stop now", CLIP), 0.5)


class Wary(unittest.TestCase):
    def fake(self, until: float = 0.0, calling: bool = False):
        return SimpleNamespace(restart_due=False, wary_until=until, calling=calling,
                               echo_watch=SimpleNamespace(may_restart=lambda: False))

    def test_never_on_a_call(self):
        me = self.fake(calling=True)
        listener.Listener.echo_slipped(me, "echo")
        self.assertEqual(me.wary_until, 0.0)

    def test_not_extended_while_already_stricter(self):
        me = self.fake()
        listener.Listener.echo_slipped(me, "echo")
        first = me.wary_until
        self.assertAlmostEqual(first - time.monotonic(), listener.WARY, delta=1)
        listener.Listener.echo_slipped(me, "echo again")
        self.assertEqual(me.wary_until, first)

    def test_set_again_once_over(self):
        me = self.fake(until=time.monotonic() - 1)
        listener.Listener.echo_slipped(me, "echo")
        self.assertGreater(me.wary_until, time.monotonic() + listener.WARY - 1)


class OnACall(unittest.TestCase):
    """The phone's mic: no laptop strictness, no loudness margin against the laptop's playback."""

    def gate_args(self, calling: bool) -> dict:
        seen = {}

        def talking(frame, db, floor, playing, played, settled, wary):
            seen.update(played=played, wary=wary)
            return True

        me = SimpleNamespace(calling=calling, wary=lambda: True, play_started=0.0,
                             playback=SimpleNamespace(loudest=lambda: -17.0),
                             gate=SimpleNamespace(talking=talking, margin=None, checked=0))
        listener.Listener.talking(me, FRAME, -30.0, -70.0, True)
        return seen

    def test_on_a_call_the_playback_level_and_strictness_dont_apply(self):
        self.assertEqual(self.gate_args(calling=True), {"played": None, "wary": False})

    def test_on_the_laptop_they_still_do(self):
        self.assertEqual(self.gate_args(calling=False), {"played": -17.0, "wary": True})

    def test_less_voice_before_the_words_are_looked_at_on_a_call(self):
        self.assertLess(talkover.CALL_SUSTAIN, talkover.SUSTAIN)


class CallSound(unittest.TestCase):
    def leaks(self, sound: dict) -> bool:
        state.write_json(state.CALL_SOUND, sound)
        me = SimpleNamespace(sound_read=0.0, sound_leaks=False, call_out="")
        return listener.Listener.call_sound_leaks(me)

    def test_loudspeaker_in_clear_sound_leaks(self):
        self.assertTrue(self.leaks({"sound": "clear", "out": "speaker", "aec": True}))

    def test_no_canceller_leaks(self):
        self.assertTrue(self.leaks({"sound": "clear", "out": "headset", "aec": False}))

    def test_headset_or_call_sound_doesnt(self):
        self.assertFalse(self.leaks({"sound": "clear", "out": "headset", "aec": True}))
        self.assertFalse(self.leaks({"sound": "call", "out": "speaker"}))
        self.assertFalse(self.leaks({}))


class ClipLead(unittest.TestCase):
    def test_our_words_at_the_start_go(self):
        self.assertEqual(turn.without_clip_lead("sunny with a light breeze stop talking please", CLIP),
                         "stop talking please")

    def test_one_word_or_all_of_it_stays(self):
        self.assertEqual(turn.without_clip_lead("the mic is broken", CLIP), "the mic is broken")
        self.assertEqual(turn.without_clip_lead("sunny with a light breeze", CLIP), "sunny with a light breeze")


class EchoTurn(unittest.TestCase):
    def test_short_and_ours_is_echo(self):
        self.assertTrue(turn.echo_turn("the weather today is sunny", CLIP, 2.0))

    def test_long_and_only_partly_ours_goes_out(self):
        # A 14 s turn of the user's own words with a run of ours inside: not thrown away whole
        said = "today is sunny with a light breeze but listen I need you to stop and check the mic now please"
        self.assertTrue(turn.our_own_words(said, CLIP))
        self.assertFalse(turn.echo_turn(said, CLIP, 14.0))

    def test_long_and_mostly_ours_is_still_echo(self):
        self.assertTrue(turn.echo_turn("the weather today is sunny with a light breeze", CLIP, 6.0))


class Sounding(unittest.TestCase):
    def test_playing_keeps_when_it_became_audible(self):
        state.Playing("c1", "s", "claude", "words").save()
        self.assertEqual(state.Playing.now().sounding, 0.0)
        state.Playing("c1", "s", "claude", "words", sounding=12.5).save()
        self.assertEqual(state.Playing.now().sounding, 12.5)

    def test_playing_on_disk_keeps_its_four_old_keys(self):
        state.Playing("c3", "s", "claude", "words", sounding=7.0).save()
        self.assertEqual(set(state.read_json(state.PLAYING)), {"clip", "session", "agent", "text"})
        self.assertEqual(state.read_json(state.SOUNDING), {"clip": "c3", "at": 7.0})
        self.assertEqual(state.Playing.now().sounding, 7.0)

    def test_another_clips_sounding_isnt_taken(self):
        state.write_json(state.SOUNDING, {"clip": "old", "at": 3.0})
        state.Playing("c4", "s", "claude", "words").save()
        self.assertEqual(state.Playing.now().sounding, 0.0)

    def test_an_unknown_key_doesnt_break_it(self):
        state.write_json(state.PLAYING, {"clip": "c", "session": "s", "agent": "a", "text": "t", "later": 1})
        self.assertEqual(state.Playing.now().clip, "c")

    def test_the_call_report_waits_for_first_audio(self):
        import importlib
        server = importlib.import_module("call.server")
        ear = server.Ear()
        silent = {"loud_ms": 0, "got": 100, "audio": "running"}
        rendering = state.Playing("c2", "s", "claude", "words")
        with mock.patch.object(server.Playing, "now", return_value=rendering):
            ear.report(silent)
            ear.report(silent)
        self.assertEqual(ear.silent, 0)
        audible = state.Playing("c2", "s", "claude", "words", sounding=time.time() - 5)
        with mock.patch.object(server.Playing, "now", return_value=audible):
            ear.report(silent)
        self.assertEqual(ear.silent, 1)

    def test_the_call_report_after_a_flush_isnt_lost_sound(self):
        import importlib
        server = importlib.import_module("call.server")
        ear = server.Ear()
        silent = {"loud_ms": 0, "got": 100, "audio": "running"}
        audible = state.Playing("c5", "s", "claude", "words", sounding=time.time() - 5)
        ear.flushed()
        with mock.patch.object(server.Playing, "now", return_value=audible):
            ear.report(silent)
            ear.report(silent)
        self.assertEqual(ear.silent, 0)


if __name__ == "__main__":
    unittest.main()
