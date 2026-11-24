"""call/server.py: the phone's own ears on a call (its speech-to-text over a bad line): which ears hear, its turns
acting as the laptop's do, late turns as words only, pings, the clip playing, the takeover gap and the long hold."""

import asyncio
import importlib.util
import json
import time
import unittest
from unittest import mock

from tests import sandbox  # noqa: F401  (first: a throwaway HOME)

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:  # the call's own packages are in the project's venv: run the suite with its python
    raise unittest.SkipTest("aiohttp missing: run with ~/.local/share/henchline/venv/bin/python3")

from voice import state
from voice.delivery import Delivery, Refusal
from voice.state import Ears, Phase

spec = importlib.util.spec_from_file_location("call_server", sandbox.REPO / "call/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

WORDS = "please check the parser logs"  # must never reach the log


def logged() -> str:
    return state.HANDSFREE_LOG.read_text() if state.HANDSFREE_LOG.exists() else ""


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state.STATE.mkdir(parents=True, exist_ok=True)
        for path in (state.CALL_EAR, state.CALL_EAR_SINCE, state.CALL_GAP, state.CALL_BUTTON_ONLY,
                     state.CALL_LAPTOP_HEARD, state.HANDSFREE_LOG):
            path.unlink(missing_ok=True)
        server.HEARD.clear()
        self.sent: list[str] = []

        async def send(text):
            self.sent.append(text)

        self.listening = server.PhoneListening(send)
        self.ear = server.Ear()

    async def page(self, message):
        return await server.from_page(message, self.ear, None, self.listening)

    def heard(self, tid="t1", text=WORDS, **more):
        return {"heard": {"id": tid, "text": text, "pushed": False, "said_at": time.time() * 1000 - 1500,
                          "late": False, **more}}


class Pings(Base):
    async def test_a_ping_is_answered_with_its_number(self):
        self.assertEqual(json.loads(await self.page({"ping": 7, "t": 123})), {"pong": 7})

    async def test_answered_without_the_phones_ears_too(self):
        self.assertEqual(json.loads(await server.from_page({"ping": 1}, self.ear)), {"pong": 1})


class WhichEars(Base):
    async def test_the_phone_takes_the_ears_and_gives_them_back(self):
        before = time.monotonic()
        ms = time.time() * 1000
        await self.page({"ear": "phone", "from_ms": ms - 8000, "t": ms})  # it has the last 8 s
        self.assertEqual(state.CALL_EAR.read_text(), Ears.PHONE.value)
        took, words = state.ears_taken()
        self.assertGreaterEqual(took, before)
        self.assertAlmostEqual(words, took - 8 + server.EAR_FROM_SLACK, delta=0.05)
        self.assertIs(self.listening.ear, Ears.PHONE)
        with mock.patch.object(state, "in_call", return_value=True):
            self.assertTrue(state.phone_listens())
        await self.page({"ear": "laptop"})
        self.assertEqual(state.CALL_EAR.read_text(), Ears.LAPTOP.value)
        with mock.patch.object(state, "in_call", return_value=True):
            self.assertFalse(state.phone_listens())
            self.assertTrue(state.phone_took_ears_since(before - 5))  # a turn begun 5 s before: it has its words
            self.assertFalse(state.phone_took_ears_since(before - 9))  # 9 s before: not from its start
            self.assertFalse(state.phone_took_ears_since(time.monotonic() + 1))  # one begun after it handed back

    async def test_a_takeover_stamp_from_past_now_is_ignored(self):
        """A stamp left from before a reboot (the monotonic clock started again) is not a takeover of this turn."""
        state.CALL_EAR_SINCE.write_text(f"{time.monotonic() + 3600:.3f} {time.monotonic() - 60:.3f}")
        with mock.patch.object(state, "in_call", return_value=True):
            self.assertFalse(state.phone_took_ears_since(time.monotonic() - 5))
            state.CALL_EAR_SINCE.write_text(f"{time.monotonic():.3f} {time.monotonic() - 60:.3f}")
            self.assertTrue(state.phone_took_ears_since(time.monotonic() - 5))


class WordsFrom(Base):
    """A takeover never makes the laptop drop a turn the phone doesn't have from its start."""

    async def test_without_a_start_a_turn_already_begun_stays_the_laptops(self):
        begun = time.monotonic() - 2
        await self.page({"ear": "phone"})  # an older phone: it doesn't say from when it has the words
        took, words = state.ears_taken()
        self.assertEqual(took, words)
        with mock.patch.object(state, "in_call", return_value=True):
            self.assertTrue(state.phone_listens())
            self.assertFalse(state.phone_took_ears_since(begun))
            self.assertTrue(state.phone_took_ears_since(took))

    async def test_its_words_starting_after_the_turn_leave_the_turn_here(self):
        begun = time.monotonic() - 3
        ms = time.time() * 1000
        await self.page({"ear": "phone", "from_ms": ms - 1000, "t": ms})  # 1 s back: after the turn began
        with mock.patch.object(state, "in_call", return_value=True):
            self.assertFalse(state.phone_took_ears_since(begun))
            self.assertTrue(state.phone_took_ears_since(time.monotonic() - 0.5))

    async def test_its_clock_need_not_agree_with_ours(self):
        """The phone's clock an hour off: "from_ms" and "t" are both its own, so only their difference counts."""
        begun = time.monotonic() - 3
        ms = time.time() * 1000 - 3_600_000
        await self.page({"ear": "phone", "from_ms": ms - 5000, "t": ms})
        with mock.patch.object(state, "in_call", return_value=True):
            self.assertTrue(state.phone_took_ears_since(begun))
            self.assertFalse(state.phone_took_ears_since(begun - 3))

    async def test_its_trip_here_only_makes_the_start_later(self):
        now = time.monotonic()
        self.assertGreaterEqual(server.words_from(10_000, 12_000, now), now - 2)
        self.assertEqual(server.words_from(10_000, 12_000, now), now - 2 + server.EAR_FROM_SLACK)

    async def test_nonsense_starts(self):
        now = time.monotonic()
        for bad in (None, "soon", True, float("nan"), -5, 0):
            self.assertEqual(server.words_from(bad, 12_000, now), now)
        self.assertEqual(server.words_from(20_000, 12_000, now), now + server.EAR_FROM_SLACK)  # "from" ahead of "t"
        self.assertEqual(server.words_from(1, 10_000_000, now), now - server.EAR_FROM_MOST + server.EAR_FROM_SLACK)
        for no_t in (None, "soon", float("nan")):  # no "t": the phone's clock isn't ours, so its words start now
            self.assertEqual(server.words_from(96_000, no_t, now), now)
        with mock.patch.object(server.time, "time", return_value=100.0):  # our wall clock means nothing to it
            self.assertEqual(server.words_from(96_000, None, now), now)

    async def test_a_garbled_stamp_covers_nothing(self):
        for text in ("", "soon", "1 2 3"):
            state.CALL_EAR_SINCE.write_text(text)
            self.assertIsNone(state.ears_taken())
            self.assertFalse(state.phone_took_ears_since(time.monotonic()))

    async def test_ears_there_are_none_of_are_ignored(self):
        await self.page({"ear": "elbow"})
        self.assertFalse(state.CALL_EAR.exists())
        self.assertIs(self.listening.ear, Ears.LAPTOP)

    async def test_not_without_a_call(self):
        await self.page({"ear": "phone"})
        with mock.patch.object(state, "in_call", return_value=False):
            self.assertFalse(state.phone_listens())

    async def test_ready_to_take_turns_over(self):
        await self.page({"ear_ready": True})
        self.assertTrue(self.listening.ready)
        await self.page({"ear_ready": False})
        self.assertFalse(self.listening.ready)


class Hearing(Base):
    async def test_its_turn_holds_talkback_as_hands_free_does(self):
        await self.page({"ear": "phone"})
        await self.page({"hearing": True})
        self.assertEqual(state.phase_now(), (Phase.RECORDING, state.BY_PHONE_EAR))
        self.assertFalse(state.ptt_busy())  # not push-to-talk: the laptop's ears aren't told to stand aside for it
        state.release_phase(state.BY_HANDSFREE)  # a laptop turn ending doesn't put the phone's turn back
        self.assertEqual(state.phase_now()[0], Phase.RECORDING)
        await self.page({"hearing": False})
        self.assertEqual(state.phase_now()[0], Phase.IDLE)

    async def test_a_turn_open_too_long_is_no_longer_held_for(self):
        await self.page({"ear": "phone"})
        await self.page({"hearing": True})
        self.listening.expire()
        self.assertEqual(state.phase_now()[0], Phase.RECORDING)
        self.listening.hearing -= server.MOST_HEARING + 1
        self.listening.expire()
        self.assertEqual(state.phase_now()[0], Phase.IDLE)
        self.assertFalse(self.listening.hearing)

    async def test_ignored_while_the_laptop_listens(self):
        state.set_phase(Phase.IDLE, state.BY_HANDSFREE)
        await self.page({"hearing": True})
        self.assertEqual(state.phase_now()[0], Phase.IDLE)

    async def test_giving_the_ears_back_ends_its_turn(self):
        await self.page({"ear": "phone"})
        await self.page({"hearing": True})
        await self.page({"ear": "laptop"})
        self.assertEqual(state.phase_now()[0], Phase.IDLE)

    async def test_a_drop_ends_its_turn_but_keeps_its_ears(self):
        await self.page({"ear": "phone"})
        await self.page({"hearing": True})
        self.listening.over(line_lost=True)
        self.assertEqual(state.phase_now()[0], Phase.IDLE)
        self.assertTrue(state.CALL_EAR.exists())
        self.listening.over(line_lost=False)
        self.assertFalse(state.CALL_EAR.exists())


class HeardTurns(Base):
    async def test_a_turn_acts_as_the_laptops_with_phone_ears(self):
        def act(text, rich, heard, last, **kw):
            kw["sent"]((rich, heard))
            return 0.0

        with mock.patch.object(server.routing, "act_on_turn", side_effect=act) as acting:
            answer = json.loads(await self.page(self.heard()))
        self.assertEqual(answer, {"delivered": "t1"})
        args, kw = acting.call_args
        self.assertEqual(args[:4], (WORDS, WORDS, server.Heard.CALL, None))
        self.assertIs(kw["ear"], Ears.PHONE)
        self.assertEqual(self.listening.last, (WORDS, server.Heard.CALL))
        self.assertNotIn(WORDS, logged())
        self.assertIn("call: the phone heard a turn (5 words", logged())

    async def test_the_talk_button(self):
        with mock.patch.object(server.routing, "act_on_turn", return_value=0.0) as acting:
            await self.page(self.heard(pushed=True))
        self.assertIs(acting.call_args.args[2], server.Heard.PUSHED)

    async def test_the_same_turn_again_is_answered_not_acted_twice(self):
        with mock.patch.object(server.routing, "act_on_turn", return_value=None) as acting:
            first = await self.page(self.heard(text="never mind"))
            again = await self.page(self.heard(text="never mind"))
        self.assertEqual(json.loads(first), {"taken": "t1"})
        self.assertEqual(first, again)
        acting.assert_called_once()

    async def test_one_not_delivered_says_why_and_may_be_sent_again(self):
        def act(text, rich, heard, last, **kw):
            kw["unsent"](Delivery.refused(Refusal.NOBODY, "No agent is listening."))
            return 0.0

        with mock.patch.object(server.routing, "act_on_turn", side_effect=act) as acting:
            answer = json.loads(await self.page(self.heard()))
            await self.page(self.heard())
        self.assertEqual(answer, {"note": "Not sent: No agent is listening.", "unsent": "t1"})
        self.assertEqual(acting.call_count, 2)

    async def test_hands_free_off_turns_the_open_line_off(self):
        def act(text, rich, heard, last, **kw):
            kw["off"]()
            self.assertEqual(kw["off_words"], server.OPEN_LINE_OFF)
            return 0.0

        with mock.patch.object(server.routing, "act_on_turn", side_effect=act):
            await self.page(self.heard(text="hands-free off"))
        self.assertEqual(server.OPEN_LINE_OFF, "Open line off: hold the button to talk.")
        await asyncio.sleep(0)  # the note to the phone goes from the loop
        self.assertTrue(state.CALL_BUTTON_ONLY.exists())
        self.assertIn(json.dumps({"button_only": True}), self.sent)

    async def test_a_turn_that_isnt_one(self):
        with mock.patch.object(server.routing, "act_on_turn") as acting:
            answer = json.loads(await self.page({"heard": {"id": "t9", "text": 5}}))
            self.assertEqual(await self.page({"heard": "words"}), "")  # not even a turn: as any unknown message
        self.assertEqual(answer["unsent"], "t9")
        acting.assert_not_called()

    async def test_through_routing_for_real(self):
        """No mock of act_on_turn: a plain turn reaches deliver with the phone's ears, a filler goes nowhere."""
        with mock.patch.object(server.routing, "deliver", return_value=Delivery.done()) as deliver, \
                mock.patch.object(server.routing, "last_speaker", return_value=None), \
                mock.patch.object(server.routing, "in_call", return_value=True), \
                mock.patch.object(server.routing, "interrupt_waiting", return_value=False), \
                mock.patch.object(server.routing, "cuts_in", return_value=False), \
                mock.patch.object(server.routing, "set_target"):
            self.assertEqual(json.loads(await self.page(self.heard())), {"delivered": "t1"})
            self.assertEqual(json.loads(await self.page(self.heard(tid="t2", text="um"))), {"taken": "t2"})
        deliver.assert_called_once_with(WORDS, server.Heard.CALL, to=None, urgent=False, ear=Ears.PHONE)


class Cut(Base):
    """A turn the laptop delivered before the phone took the ears isn't heard again from the phone's kept audio."""

    PER_S = round(server.MIC_BYTES_PER_MS * 1000)  # bytes of the phone's mic a second

    def mic(self, now: float, seconds: int, backlog: int = 0) -> server.MicStream:
        """A connection whose audio arrived one second's worth a second, up to `now`, all played at once."""
        mic = server.MicStream()
        for ago in range(seconds, 0, -1):
            mic.arrived(self.PER_S)
            mic.handed(now - ago + 1, backlog)
        return mic

    async def test_the_phone_is_told_where_the_laptops_last_turn_ended_on_its_own_clock(self):
        now = time.monotonic()
        self.listening.mic = self.mic(now, 10)
        # played 2.5 s ago: surely as far as handed over 3 s ago (counted 3 s of audio before the phone's "t")
        state.note_laptop_heard(now - 2.5)
        answer = json.loads(await self.page({"ear": "phone", "from_ms": 40_000, "t": 50_000}))
        path = server.LAPTOP_AUDIO_PATH * 1000
        self.assertAlmostEqual(answer["ear_cut"], 50_000 - 3000 - path - server.CUT_SLACK * 1000, delta=1)

    async def test_a_slow_line_cannot_make_the_cut_late(self):
        """The line stalled 3 s right as the laptop's turn ended, then caught up in a burst, and the phone took the
        ears 5 s (its clock) after the turn's last word: the message came fast, the turn's audio slow. The old cut
        (the message's trip taken as the audio's) put the phone's start 2.4 s after that word, and the words said
        meanwhile, still in the pipe here, were heard by nobody. Counted in the phone's own audio, it is before it."""
        now, t_ms = time.monotonic(), 50_000
        mic = server.MicStream()
        for second in range(5):  # phone seconds t-10 … t-6 arrived 0.1 s after being read
            mic.arrived(self.PER_S)
            mic.handed(now - (10 - second - 1) - 0.1, 0)
        mic.arrived(self.PER_S)  # t-6 … t-5, the turn's last second: 3 s late
        heard = now - 5 - 0.1 + 3  # when the laptop heard the turn's last word (t-5 on the phone's clock)
        mic.handed(heard, 0)
        mic.arrived(5 * self.PER_S)  # the burst, t-5 … t: mostly still in the pipe as the {"ear"} comes
        mic.handed(now - 0.05, 3 * self.PER_S)
        cut = server.ear_cut(t_ms, now, heard, mic)
        self.assertLessEqual(cut, t_ms - 5000)  # never after what the laptop heard: no word skipped
        self.assertGreaterEqual(cut, t_ms - 5000 - 1000)  # nor needlessly early
        old = t_ms + (heard - now) * 1000 - 500  # the old cut, for the record
        self.assertGreater(old, t_ms - 5000 + 2000)

    async def test_audio_still_in_the_pipe_is_not_played_yet(self):
        now = time.monotonic()
        quick, slow = self.mic(now, 10), self.mic(now, 10, backlog=self.PER_S)
        self.assertEqual(server.ear_cut(50_000, now, now - 2.5, quick) - server.ear_cut(50_000, now, now - 2.5, slow),
                         1000)

    async def test_no_cut_from_audio_this_connection_never_had(self):
        now = time.monotonic()
        self.assertIsNone(server.ear_cut(50_000, now, now - 20, self.mic(now, 10)))  # before its audio began
        self.assertIsNone(server.ear_cut(50_000, now, now - 2, server.MicStream()))  # no audio at all
        self.assertIsNone(server.ear_cut(50_000, now, now - 2, None))  # nothing counted
        self.assertIsNone(server.ear_cut(None, now, now - 2, self.mic(now, 10)))  # no clock of the phone's

    async def test_a_mic_restart_forgets_the_count(self):
        """The phone's mic started again mid-connection (a sound or route switch sends {"sound"} first): the time it
        was off isn't in the bytes, so a cut counted across it would be late by that much. No cut instead."""
        now = time.monotonic()
        self.listening.mic = self.mic(now, 10)
        state.note_laptop_heard(now - 2.5)
        with mock.patch.object(server, "write_json"):
            await self.page({"sound": "call", "out": "earpiece", "aec": True})
        answer = json.loads(await self.page({"ear": "phone", "from_ms": 40_000, "t": 50_000}))
        self.assertEqual(answer, {"ear_cut": None})
        self.listening.mic.arrived(self.PER_S)  # audio after the restart counts again
        self.listening.mic.handed(time.monotonic(), 0)
        self.assertIsNotNone(self.listening.mic.played_by(time.monotonic()))

    async def test_the_count_keeps_only_what_the_phone_keeps(self):
        mic = server.MicStream()
        for at in range(100):
            mic.arrived(self.PER_S)
            mic.handed(float(at), 0)
        self.assertLessEqual(len(mic.history), server.MIC_KEPT + 1)
        self.assertIsNone(mic.played_by(50.0))

    async def test_the_backlog_of_a_real_pipe(self):
        import os
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.write(write, b"x" * 1000)
        transport = mock.Mock(get_write_buffer_size=mock.Mock(return_value=24),
                              get_extra_info=mock.Mock(return_value=open(write, "wb", closefd=False)))
        self.assertEqual(server.pipe_backlog(mock.Mock(transport=transport)), 1024)
        transport.get_extra_info.return_value = None  # unknown: counted as a full pipe, so the cut stays early
        self.assertEqual(server.pipe_backlog(mock.Mock(transport=transport)), 24 + (1 << 16))

    async def test_no_cut_without_a_laptop_turn_or_its_clock(self):
        self.assertEqual(json.loads(await self.page({"ear": "phone", "from_ms": 40_000, "t": 50_000})), {"ear_cut": None})
        state.note_laptop_heard(time.monotonic() - 2)
        self.assertEqual(json.loads(await self.page({"ear": "phone"})), {"ear_cut": None})
        self.assertEqual(await self.page({"ear": "laptop"}), "")

    async def test_a_turn_older_than_what_the_phone_keeps_or_from_before_a_reboot(self):
        now = time.monotonic()
        self.assertIsNone(server.ear_cut(50_000, now, now - server.EAR_FROM_MOST - 1))
        state.CALL_LAPTOP_HEARD.write_text(f"{now + 3600:.3f}")
        self.assertIsNone(state.laptop_heard_until())

    async def test_always_a_little_early(self):
        now = time.monotonic()
        self.assertEqual(server.ear_cut(50_000, now, now, self.mic(now, 10)),
                         50_000 - (server.LAPTOP_AUDIO_PATH + server.CUT_SLACK) * 1000)

    async def test_forgotten_when_the_call_ends(self):
        state.note_laptop_heard(time.monotonic())
        self.listening.over(line_lost=True)
        self.assertTrue(state.CALL_LAPTOP_HEARD.exists())
        self.listening.over(line_lost=False)
        self.assertFalse(state.CALL_LAPTOP_HEARD.exists())


class SentAgain(Base):
    async def test_a_turn_sent_again_leaves_the_turn_heard_now_held(self):
        """After a reconnect the phone sends its unanswered turns again: a cached answer acts on nothing and must
        not end the hold of the turn it is hearing now."""
        await self.page({"ear": "phone"})
        await self.page({"hearing": True})
        with mock.patch.object(server.routing, "act_on_turn", return_value=0.0) as acting:
            await self.page(self.heard(tid="a"))
            await self.page({"hearing": True})  # the next turn opens
            await self.page(self.heard(tid="a"))  # and the first comes again
        acting.assert_called_once()
        self.assertTrue(self.listening.hearing)
        self.assertEqual(state.phase_now(), (Phase.RECORDING, state.BY_PHONE_EAR))


class NewerTurnKeepsItsHold(Base):
    """A heard turn that waited behind a slow message must not end the turn the phone opened after it."""

    async def test_a_queued_turn_leaves_a_newer_turn_held(self):
        gate = asyncio.Event()

        async def handle(message):
            if "chat" in message:
                await gate.wait()  # a typed line still being typed
                return ""
            return await self.page(message)

        inbox = server.Inbox(handle, mock.AsyncMock(), self.listening.arrived)
        await inbox.take({"ear": "phone"})
        await inbox.take({"hearing": True})  # turn A
        await inbox.take({"chat": "hi", "session": "claude-1"})
        await inbox.take({"hearing": False})
        await inbox.take(self.heard(tid="a"))  # A's words, behind the typed line
        await inbox.take({"hearing": True})  # turn B opens before A is acted on
        with mock.patch.object(server.routing, "deliver", return_value=Delivery.done()) as deliver, \
                mock.patch.object(server.routing, "last_speaker", return_value=None), \
                mock.patch.object(server.routing, "in_call", return_value=True), \
                mock.patch.object(server.routing, "interrupt_waiting", return_value=False), \
                mock.patch.object(server.routing, "cuts_in", return_value=False), \
                mock.patch.object(server.routing, "set_target"):
            gate.set()
            await inbox.close(2.0)
        deliver.assert_called_once()  # A's words went
        self.assertEqual(state.phase_now(), (Phase.RECORDING, state.BY_PHONE_EAR))  # B still held
        self.assertTrue(self.listening.hearing)
        self.assertEqual(self.listening.arrivals, {})

    async def test_with_no_newer_turn_the_phase_is_put_back(self):
        await self.page({"ear": "phone"})
        await self.page({"hearing": True})
        self.listening.arrived(self.heard(tid="a"))
        with mock.patch.object(server.routing, "act_on_turn", return_value=0.0) as acting:
            await self.page(self.heard(tid="a"))
        self.assertTrue(acting.call_args.kwargs["phase"])
        self.assertFalse(self.listening.hearing)


class LateTurns(Base):
    async def test_late_words_go_as_words_tagged_with_when_they_were_said(self):
        said = time.time() - 120
        with mock.patch.object(server.routing, "act_on_turn") as acting, \
                mock.patch.object(server, "to_agent", return_value=("Claude", "")) as to_agent:
            answer = json.loads(await self.page(self.heard(text="hang up", late=True, said_at=said * 1000)))
        acting.assert_not_called()  # a "hang up" said with the line down is moot now: words only
        self.assertEqual(answer, {"delivered": "t1", "to": "Claude"})
        args, kw = to_agent.call_args
        self.assertEqual(args, ("hang up", "", server.Heard.CALL))
        self.assertEqual(kw["ear"], Ears.PHONE)
        self.assertFalse(kw["switch"])
        self.assertAlmostEqual(kw["said_at"], said, places=2)

    async def test_to_the_agent_it_was_meant_for(self):
        with mock.patch.object(server, "to_agent", return_value=("Codex", "")) as to_agent:
            await self.page(self.heard(late=True, session="codex-1"))
        self.assertEqual(to_agent.call_args.args[1], "codex-1")

    async def test_a_late_turn_needs_its_time(self):
        with mock.patch.object(server, "to_agent") as to_agent:
            answer = json.loads(await self.page(self.heard(late=True, said_at=None)))
        self.assertIn("unsent", answer)
        to_agent.assert_not_called()

    async def test_never_typed_as_a_slash_command(self):
        with mock.patch.object(server, "to_agent", return_value=("Claude", "")) as to_agent:
            await self.page(self.heard(text="/clear everything", late=True))
            answer = json.loads(await self.page(self.heard(tid="t2", text=" / ", late=True)))
        self.assertEqual(to_agent.call_args_list[0].args[0], "clear everything")
        self.assertEqual(answer, {"taken": "t2"})
        to_agent.assert_called_once()

    async def test_the_turn_of_now_is_left_alone(self):
        await self.page({"ear": "phone"})
        await self.page({"hearing": True})
        with mock.patch.object(server, "to_agent", return_value=("Claude", "")):
            await self.page(self.heard(late=True))
        self.assertEqual(state.phase_now(), (Phase.RECORDING, state.BY_PHONE_EAR))
        self.assertTrue(self.listening.hearing)

    async def test_a_late_filler_is_taken_not_sent(self):
        with mock.patch.object(server, "to_agent") as to_agent:
            answer = json.loads(await self.page(self.heard(text="uh", late=True)))
        self.assertEqual(answer, {"taken": "t1"})
        to_agent.assert_not_called()


class Playing(Base):
    async def test_the_clip_is_told_only_while_the_phone_listens_and_when_it_changes(self):
        clip = state.Playing("c1", "claude-1", "claude", "Here is the plan.")
        with mock.patch.object(server.Playing, "now", return_value=clip):
            self.assertIsNone(self.listening.playing())
            await self.page({"ear": "phone"})
            self.assertEqual(self.listening.playing(), {"playing": {"clip": "c1", "text": "Here is the plan."}})
            self.assertIsNone(self.listening.playing())
        with mock.patch.object(server.Playing, "now", return_value=None):
            self.assertEqual(self.listening.playing(), {"playing": None})
            self.assertIsNone(self.listening.playing())


class TalkButton(Base):
    async def test_the_button_while_the_phone_listens_opens_nothing_here(self):
        """The app sends {"push"} to its own ears and here: the flag is kept (should the phone hand the ears back
        mid-press, the laptop hears the rest of it), and nothing else happens: no phase, no turn of the laptop's
        (the listener stands aside while the phone listens: test_phone_takeover.ListenerStandsAside)."""
        await self.page({"ear": "phone"})
        with mock.patch.object(server, "set_phase") as phase:
            self.assertEqual(await self.page({"push": True}), "")
            self.assertTrue(state.CALL_PUSH.exists())
            await self.page({"push": False})
            self.assertFalse(state.CALL_PUSH.exists())
        phase.assert_not_called()


class Gap(Base):
    async def test_a_pause_is_a_takeover_only_once_the_phone_can_take_over(self):
        self.listening.gap(0.8, 1000.0)
        self.assertFalse(state.CALL_GAP.exists())
        await self.page({"ear_ready": True})
        self.listening.gap(0.3, 1000.0)
        self.assertFalse(state.CALL_GAP.exists())
        self.listening.gap(0.6, 1000.0)
        self.assertEqual(float(state.CALL_GAP.read_text()), 1000.0)
        self.listening.gap(0.9, 1000.0)  # the same pause: written once
        self.listening.gap(0.1, 1002.0)
        self.listening.gap(0.6, 1002.0)
        self.assertEqual(float(state.CALL_GAP.read_text()), 1002.0)
        self.assertTrue(state.gap_since(1001.0))
        self.assertFalse(state.gap_since(1003.0))

    async def test_no_gap_while_the_phone_listens(self):
        await self.page({"ear_ready": True})
        await self.page({"ear": "phone"})
        self.listening.gap(2.0, 1000.0)
        self.assertFalse(state.CALL_GAP.exists())


class Hold(Base):
    async def test_a_phone_that_listens_itself_is_held_for_the_line_down_hold(self):
        self.assertEqual(self.listening.hold(), state.REJOIN)
        await self.page({"ear": "laptop"})  # it could listen, but the laptop did: it reconnects as ever
        self.assertEqual(self.listening.hold(), state.REJOIN)
        await self.page({"ear": "phone"})
        self.assertEqual(self.listening.hold(), state.LINE_DOWN_HOLD + server.LINE_HOLD_MARGIN)
        self.assertEqual(state.LINE_DOWN_HOLD, 180)

    async def test_a_resumed_call_remembers_the_phones_ears(self):
        state.CALL_EAR.write_text("phone")
        self.assertIs(server.PhoneListening(self.listening.send, resumed=True).ear, Ears.PHONE)
        self.assertIs(server.PhoneListening(self.listening.send).ear, Ears.LAPTOP)


class TodaysApp(Base):
    """Every message today's app sends on a call (CallService.kt, PhoneVoice.kt, MainActivity.kt, RingService.kt) is
    handled as before with the phone's ears in place: none is taken for a turn, an ear or a ping."""

    MESSAGES = [
        {"tts": "fallback", "reason": "fell behind", "id": "clip-1", "heard": 2},  # PhoneVoice.fallback
        {"tts": "fallback", "reason": "not speaking on the phone", "id": "clip-2"},
        {"tts": "laptop"},
        {"tts": {"engine": "kokoro", "model": "m", "rtf": 0.4}, "chosen": True},
        {"said": {"id": "clip-1", "piece": 0, "done": True, "rtf": 0.3}},
        {"push": True}, {"push": False}, {"button_only": True}, {"button_only": False}, {"stay_on": True},
        {"cut_in": True}, {"cut_in": False}, {"cut_in": "auto"}, {"interrupt": True},
        {"note": "mic silent", "restarted": False}, {"note": "dropped: timeout"},
        {"sound": "call", "out": "earpiece", "aec": True},
        {"stats": {"got": 100, "dropped": 0, "loud_ms": 1500, "audio": "running", "hidden": False}},
        {"answering": "ring-1"}, {"ring_endpoint": "https://ntfy.sh/up-x"},
        {"switch": "claude-1"}, {"chat": "hello", "session": "claude-1", "id": "m1", "urgent": False},
    ]

    async def test_none_is_a_turn(self):
        handled = []

        async def turn(data):
            handled.append(data)
            return {}

        with mock.patch.object(self.listening, "heard", side_effect=turn), \
                mock.patch.object(self.listening, "switch_ears") as ears, \
                mock.patch.object(self.listening, "hears") as hears, \
                mock.patch.object(server.phonevoice, "offer") as offer, \
                mock.patch.object(server.phonevoice, "said"), \
                mock.patch.object(server.phonevoice, "chosen", return_value=None), \
                mock.patch.object(server, "confirm_switch"), mock.patch.object(server, "route_to_ringer"), \
                mock.patch.object(server, "keep_ring_endpoint"), mock.patch.object(server, "switch"), \
                mock.patch.object(server, "tell_mic_silent"), \
                mock.patch.object(server.routing, "arm_interrupt"), \
                mock.patch.object(server, "typed", return_value=(False, "")), \
                mock.patch.object(server, "write_json"), mock.patch.object(server, "flag"), \
                mock.patch.object(server, "CALL_CUT_IN"):
            for message in self.MESSAGES:
                answer = await self.page(dict(message))
                self.assertNotIn("pong", answer, message)
        self.assertEqual(handled, [])
        ears.assert_not_called()
        hears.assert_not_called()
        self.assertEqual(offer.call_count, 4)  # each tts message reached the phone voice, the fallback with "heard" too

    def test_only_the_slow_ones_queue(self):
        slow = [m for m in self.MESSAGES if server.queued(m)]
        self.assertEqual([next(iter(m)) for m in slow], ["switch", "chat"])
        self.assertTrue(server.queued(self.heard()))
        for fast in ({"ping": 1}, {"ear": "phone"}, {"ear_ready": True}, {"hearing": True}):
            self.assertFalse(server.queued(fast), fast)


class InboxOrder(unittest.IsolatedAsyncioTestCase):
    """The receive loop's inbox: slow messages in order, one at a time, behind each other; the rest at once."""

    async def asyncSetUp(self):
        self.sent, self.handled = [], []
        self.gate = asyncio.Event()

        async def handle(message):
            if "chat" in message:
                await self.gate.wait()  # a typed line still being typed
            self.handled.append(message)
            return json.dumps({"done": next(iter(message))}) if "ping" not in message else json.dumps({"pong": 1})

        async def send(text):
            self.sent.append(json.loads(text))

        self.inbox = server.Inbox(handle, send)

    async def test_a_ping_and_a_flag_pass_a_slow_line(self):
        await self.inbox.take({"chat": "hi"})
        await self.inbox.take({"switch": "codex-1"})
        await self.inbox.take({"ping": 1})
        await self.inbox.take({"push": True})
        await asyncio.sleep(0.01)
        self.assertEqual(self.sent, [{"pong": 1}, {"done": "push"}])
        self.gate.set()
        await self.inbox.close(1.0)
        self.assertEqual(self.sent[2:], [{"done": "chat"}, {"done": "switch"}])  # in the order sent

    async def test_what_was_sent_is_handled_after_the_line_ends(self):
        await self.inbox.take({"chat": "hi"})
        asyncio.get_running_loop().call_later(0.05, self.gate.set)
        await self.inbox.close(1.0)
        self.assertEqual(self.handled, [{"chat": "hi"}])

    async def test_given_up_on_after_the_wait(self):
        await self.inbox.take({"chat": "hi"})
        await self.inbox.close(0.05)
        self.assertEqual(self.handled, [])
        self.assertIn("1 messages from the phone left unhandled", logged())

    async def test_one_failing_does_not_stop_the_rest(self):
        async def handle(message):
            if message.get("chat") == "bad":
                raise RuntimeError("words")
            self.handled.append(message)
            return ""

        inbox = server.Inbox(handle, mock.AsyncMock())
        await inbox.take({"chat": "bad"})
        await inbox.take({"chat": "good"})
        await inbox.close(1.0)
        self.assertEqual(self.handled, [{"chat": "good"}])
        self.assertIn("a message from the phone failed (RuntimeError)", logged())
        self.assertNotIn("words", logged().split("failed")[-1])


class PhoneTurnParse(unittest.TestCase):
    def test_fields(self):
        turn = server.PhoneTurn.parse({"id": "a", "text": " two\nwords ", "pushed": True, "said_at": 1_700_000_000_000})
        self.assertEqual((turn.text, turn.heard, turn.said_at, turn.late), ("two words", server.Heard.PUSHED,
                                                                            1_700_000_000.0, False))

    def test_refused(self):
        for bad in ({}, {"id": ""}, {"id": "x" * 129}, {"id": "a", "text": None}, {"id": "a", "said_at": "now"},
                    {"id": "a", "said_at": True}, {"id": "a", "late": True}, []):
            with self.assertRaises(ValueError, msg=bad):
                server.PhoneTurn.parse(bad)

    def test_logged_without_words(self):
        line = server.PhoneTurn.parse({"id": "a", "text": WORDS, "said_at": time.time() * 1000}).logged()
        self.assertNotIn("parser", line)
        self.assertTrue(line.startswith("5 words, said 0."))


class LateRoute(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_post("/call/heard", server.heard_late)
        return app

    def origin(self):
        return {"Origin": f"https://{self.server.host}:{self.server.port}"}

    def turn(self, **more):
        return {"id": "late-1", "text": WORDS, "pushed": False, "said_at": time.time() * 1000 - 90_000,
                "session": "claude-1", **more}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        server.HEARD.clear()
        state.HANDSFREE_LOG.unlink(missing_ok=True)

    async def test_another_sites_page_is_refused(self):
        answer = await self.client.post("/call/heard", json=self.turn(), headers={"Origin": "https://evil.example"})
        self.assertEqual(answer.status, 403)

    async def test_delivered_once_as_late_words(self):
        with mock.patch.object(server, "to_agent", return_value=("Claude", "")) as to_agent, \
                mock.patch.object(server.routing, "act_on_turn") as acting:
            first = await self.client.post("/call/heard", json=self.turn(), headers=self.origin())
            again = await self.client.post("/call/heard", json=self.turn(), headers=self.origin())
        self.assertEqual(await first.json(), {"delivered": "late-1", "to": "Claude"})
        self.assertEqual(await again.json(), {"delivered": "late-1", "to": "Claude"})
        to_agent.assert_called_once()
        acting.assert_not_called()
        self.assertEqual(to_agent.call_args.args[1], "claude-1")
        self.assertIsNotNone(to_agent.call_args.kwargs["said_at"])
        self.assertNotIn(WORDS, logged())

    async def test_late_even_unflagged_and_needs_its_time(self):
        answer = await self.client.post("/call/heard", json=self.turn(said_at=None), headers=self.origin())
        self.assertEqual(answer.status, 400)

    async def test_not_delivered(self):
        with mock.patch.object(server, "to_agent", return_value=("", "That agent isn't reachable.")):
            answer = await self.client.post("/call/heard", json=self.turn(), headers=self.origin())
        self.assertEqual(await answer.json(), {"note": "That agent isn't reachable.", "unsent": "late-1"})
        self.assertNotIn("late-1", server.HEARD)


if __name__ == "__main__":
    unittest.main()
