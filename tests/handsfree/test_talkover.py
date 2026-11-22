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


class Scraps:
    """A clip with a voice in scraps, as the canceller leaves double-talk: (start, seconds of voice, margin in dB)
    per scrap, one positive voice check every 90 ms, the first of each scrap a full check (strong) at that margin."""

    def __init__(self, heard: list[str], scraps: list[tuple[float, float, float | None]], strict: bool = False):
        self.f = Fixture(heard)
        self.f.over.strict = strict
        if strict:
            self.f.over.new_words_needed = talkover.CAREFUL_NEW_WORDS
        self.scraps = scraps

    def play(self, until: float) -> tuple[Verdict | None, float, list[Verdict]]:
        """(the stopping verdict or None, when, every verdict that wasn't WAITING)."""
        checks = {}
        for start, seconds, margin in self.scraps:
            for k in range(max(1, round(seconds / talkover.STEP))):
                checks[round((start + k * talkover.STEP) / 0.03)] = (k == 0, margin)
        verdicts = []
        for i in range(round(until / 0.03)):
            self.f.now[0] = i * 0.03
            strong, margin = checks.get(i, (False, None))
            v = self.f.over.feed(FRAME, i in checks, [FRAME], "a voice", CLIP, clip="clip-1", strong=strong,
                                 margin=margin if strong else None)
            if v is not Verdict.WAITING:
                verdicts.append(v)
            if v is Verdict.USER:
                return v, self.f.now[0], verdicts
        return None, self.f.now[0], verdicts


# 2026-11-22 20:29, a 25 s clip on the laptop speakers: four starts, 13.0-20.6 s in, each "then quiet"
BULLDOZED = [(13.0, 0.1, -14.0), (14.6, 0.2, -8.0), (18.0, 0.3, -3.0), (20.6, 0.1, 7.0)]


class TalkingOverInScraps(unittest.TestCase):
    def test_the_2029_talk_over_stops_at_its_first_loud_start(self):
        # Looked at from the second start; stopped at the third, 3 dB under our playback, saying words we don't
        verdict, at, _ = Scraps(["hey stop please listen to me"], BULLDOZED).play(25.0)
        self.assertIs(verdict, Verdict.USER)
        self.assertAlmostEqual(at, 18.0, delta=0.1)  # was: never (the clip played to its end)

    def test_the_2029_talk_over_stops_even_when_the_words_stay_too_few(self):
        # Whisper makes nothing of the chopped voice: four starts in 8 s, one as loud as our playback, stop anyway
        verdict, at, _ = Scraps(["uh"], BULLDOZED).play(25.0)
        self.assertIs(verdict, Verdict.USER)
        self.assertAlmostEqual(at, 20.6, delta=0.1)

    def test_one_loud_start_alone_still_waits_for_enough_voice(self):
        # At 100% volume our echo came within 6 dB too: loudness alone never asks Whisper about 0.3 s of audio
        f = Scraps(["train lift"], [(5.0, 0.3, -3.0)])
        verdict, _, _ = f.play(10.0)
        self.assertIsNone(verdict)
        self.assertEqual(f.f.looks, 0)

    def test_the_turn_keeps_every_scrap(self):
        f = Scraps(["hey stop please listen to me"], BULLDOZED)
        f.play(25.0)
        self.assertGreater(len(f.f.over.taken.frames), round(1.5 / 0.03))  # the first scrap and the quiet after


class EchoInScrapsKeepsPlaying(unittest.TestCase):
    """Our own echo in scraps (the memory notes: -33 dB median, -20 at worst healthy, -8 and -10 once wary)."""

    def test_echo_scraps_saying_the_clips_words(self):
        scraps = [(3.0, 0.2, -14.0), (4.6, 0.3, -10.0), (6.5, 0.2, -8.0), (8.0, 0.3, -12.0), (9.9, 0.2, -10.0)]
        verdict, _, verdicts = Scraps(["the weather today is sunny"], scraps).play(15.0)
        self.assertIsNone(verdict)
        self.assertIn(Verdict.ECHO, verdicts)

    def test_echo_scraps_too_mangled_for_words_never_stop_it(self):
        # Many starts but none as loud as our voice: too few words is never enough
        scraps = [(3.0 + 1.5 * k, 0.2, -12.0 - k % 3) for k in range(8)]
        verdict, _, verdicts = Scraps(["uh"], scraps).play(16.0)
        self.assertIsNone(verdict)
        self.assertEqual(verdicts, [])

    def test_scattered_scraps_far_apart_are_never_looked_at(self):
        scraps = [(3.0, 0.1, -15.0), (9.0, 0.1, -14.0), (15.0, 0.1, -15.0)]
        f = Scraps(["hey stop please"], scraps)
        verdict, _, _ = f.play(20.0)
        self.assertIsNone(verdict)
        self.assertEqual(f.f.looks, 0)

    def test_a_loud_echo_burst_saying_our_words(self):
        verdict, _, verdicts = Scraps(["sunny with a light breeze"], [(4.0, 1.0, -8.0), (5.5, 0.5, -6.0)]).play(9.0)
        self.assertIsNone(verdict)
        self.assertIn(Verdict.ECHO, verdicts)

    # The reviewer's repro: wary echo below LOUD_MARGIN that passes the full gate, 0.2 s every 3 s, joined by REJOIN
    ECHO_EVERY_3S = [(3.0 * k, 0.2, -12.0) for k in range(1, 15)]

    def test_far_apart_echo_scraps_too_mangled_for_words_never_add_up_to_a_stop(self):
        verdict, _, _ = Scraps(["uh"], self.ECHO_EVERY_3S).play(46.0)
        self.assertIsNone(verdict)

    def test_far_apart_echo_scraps_whisper_writes_words_for_never_stop_it(self):
        verdict, _, _ = Scraps(["thanks for watching"], self.ECHO_EVERY_3S).play(46.0)
        self.assertIsNone(verdict)

    def test_one_loud_scrap_long_ago_doesnt_make_later_quiet_ones_persistent(self):
        scraps = [(3.0, 0.2, -3.0)] + [(3.0 + 2.5 * k, 0.2, -13.0) for k in range(1, 9)]
        for heard in (["uh"], ["you"]):
            verdict, _, _ = Scraps(heard, scraps).play(28.0)
            self.assertIsNone(verdict, heard)

    def test_loud_echo_bursts_whisper_invents_words_for_never_stop_it(self):
        # Speakers at 100%: our echo within 6 dB of the playback, in scraps; Whisper's inventions are short
        scraps = [(4.0, 0.2, -4.0), (5.8, 0.2, -3.0), (7.5, 0.2, -5.0)]
        for heard in (["train lift"], ["thanks for watching"]):
            verdict, _, _ = Scraps(heard, scraps).play(10.0)
            self.assertIsNone(verdict, heard)

    def test_a_scrap_on_the_next_clip_starts_afresh(self):
        f = Scraps(["hey stop please"], [(3.0, 0.1, -14.0)])
        f.play(3.5)
        f.f.now[0] = 3.6
        f.f.over.feed(FRAME, False, [], "a voice", "another clip entirely", clip="clip-2")
        self.assertIsNone(f.f.over.over)
        self.assertIsNone(f.f.over.parked)


class BusyLaptop(unittest.TestCase):
    """Load over the limit: talking over is still judged, strictly, instead of not at all."""

    def test_a_voice_that_keeps_talking_with_its_own_words_still_stops_it(self):
        f = Fixture(["hang on stop talking please"])
        f.over.strict, f.over.new_words_needed = True, talkover.CAREFUL_NEW_WORDS
        verdicts = f.voice_for(talkover.STRICT_SUSTAIN + 0.3)
        self.assertEqual(verdicts, [Verdict.USER])
        self.assertGreaterEqual(f.over.taken.voiced, talkover.STRICT_SUSTAIN)

    def test_scraps_and_length_alone_never_stop_it(self):
        verdict, _, _ = Scraps(["uh"], BULLDOZED, strict=True).play(25.0)
        self.assertIsNone(verdict)
        f = Fixture(["um"])
        f.over.strict = True
        self.assertNotIn(Verdict.USER, f.voice_for(4.0))

    def test_two_words_the_clip_doesnt_say_are_not_enough(self):
        f = Fixture(["thanks watching"])
        f.over.strict, f.over.new_words_needed = True, talkover.CAREFUL_NEW_WORDS
        self.assertNotIn(Verdict.USER, f.voice_for(4.0))

    def test_looks_are_further_apart(self):
        looks = {}
        for strict in (False, True):
            f = Fixture(["the weather today is sunny"])
            f.over.strict = strict
            f.voice_for(4.0)
            looks[strict] = f.looks
        self.assertLessEqual(looks[True] * 1.5, looks[False])

    def test_our_echo_still_keeps_playing(self):
        f = Fixture(["the weather today is sunny", "sunny with a light breeze from the west"])
        f.over.strict = True
        self.assertNotIn(Verdict.USER, f.voice_for(6.0))

    def test_the_log_says_it_is_judged_strictly(self):
        import sensing
        loads = [20.0]
        load = sensing.Load(cores=22, loadavg=lambda: (loads[0], 0, 0))
        with mock.patch.object(sensing, "log") as said:
            self.assertTrue(load.high())
        self.assertIn("judged strictly", said.call_args[0][1])


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
        return SimpleNamespace(restart_due=False, wary_until=until, calling=calling, load=SimpleNamespace(busy=False),
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
