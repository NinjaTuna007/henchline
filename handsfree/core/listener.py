"""The hands-free listener: when the user may talk, and when a turn starts.

It reads the echo-cancelled mic 30 ms at a time. Speech counts only while a
clip plays (talking over it, talkover.py) or while a window is open: after a
clip, after the user spoke, after "give me a minute", after a switch. The rest
of the time only the wake word or an earbud tap gets in. A turn itself, from
recording to acting on the words, is turn.py.
"""

from __future__ import annotations

import collections
import threading
import time
import traceback
from dataclasses import dataclass

import numpy as np

import routing
from routing import Heard
from call.host_config import own_name  # this computer's name, in words it says
from sensing import (FRAME, PER_SECOND, RATE, EarbudButton, EchoWatch, Load, Output, Playback, Wake, Xruns, level,
                     mic_stream, read_frame, start_canceller)
from talkover import CALL_SUSTAIN, CAREFUL_NEW_WORDS, NEW_WORDS, SUSTAIN, TalkOver, Verdict
from turn import Mode, Turn
import voiceprint
from voicegate import VoiceGate
from voice.speech import whisper, whistle
from voice.spool import queued
from voice.state import (AWAY, BY_HANDSFREE, CALL_BUTTON_ONLY, CALL_PUSH, CALL_SOUND, CLOSE, HANDSFREE_LOG,
                         HANDSFREE_MODE, LISTEN, PAUSE, PLAYING, WORKER_PID, in_call, log, mic_source, phone_listens,
                         ptt_busy, read_json, release_phase)

WINDOW = 8.0  # seconds to answer after a clip
FOLLOW = 6.0  # seconds to carry on after their own message (once, never chained)
BARGE_GRACE = 0.7  # seconds at the start of a clip before the canceller has caught up
WARMUP = 60.0  # seconds after the canceller starts in which talking over needs a louder voice
WARY = 60.0  # after our own echo slipped through, how long talking over stays stricter
FLOOR_SPAN = 10 * PER_SECOND  # frames of history a floor is taken from
FLOOR_MIN = 30  # frames of history before a floor is trusted; until then the frame itself is the floor
PREROLL = 15  # frames kept from before speech started
XRUN_REST = 30.0  # seconds a canceller is given before an xrun restarts it; doubles while xruns keep coming
XRUN_REST_MAX = 5 * 60.0


@dataclass
class Window:
    """When the user may talk without the wake word, and how that would be heard."""

    until: float = 0.0
    kind: Heard = Heard.REPLY

    def open(self, seconds: float, kind: Heard, never_shorter: bool = False) -> None:
        if not never_shorter or time.monotonic() + seconds > self.until:
            self.until, self.kind = time.monotonic() + seconds, kind

    def close(self) -> None:
        self.until = 0.0

    def is_open(self) -> bool:
        return time.monotonic() < self.until


def talkback_busy() -> bool:
    return PLAYING.exists() or WORKER_PID.exists() or bool(queued())


def floor_of(history: collections.deque, fallback: float) -> float:
    return float(np.percentile(history, 20)) if len(history) > FLOOR_MIN else fallback


class Listener:
    def __init__(self, cfg: whisper.Config, transcriber: whisper.Transcriber, stop, canceller_since: float) -> None:
        self.cfg, self.stop = cfg, stop
        self.canceller_since = canceller_since
        own_name()  # its first lookup (tailscale, up to 2 s) now, not later inside the per-frame step
        self.transcriber = transcriber
        self.stream, self.source, self.opened, self.frames_read = None, "", 0.0, 0
        self.talkover = TalkOver(self.transcriber.quick_text)
        self.voiceprint = self.load_voiceprint(cfg)
        self.talkover.voiceprint = self.voiceprint
        self.turns = Turn(cfg, self.transcriber, self.talkover, self.read, self.show, self.echo_slipped, stop)
        self.whistle = None  # confirms wake words while Whisper is on the CPU (battery)
        if whistle.available():
            try:
                self.whistle = whistle.Checker(HANDSFREE_LOG)
                self.turns.whistle = self.whistle
            except Exception as exc:
                log(HANDSFREE_LOG, f"whistle didn't load ({exc}): Whisper confirms wake words")
        self.last_check = ""
        self.mode = Mode.WAITING
        HANDSFREE_MODE.write_text(self.mode.value)
        self.window = Window()
        self.wake = Wake.available()
        self.button, self.playback, self.output = EarbudButton(stop), Playback(stop), Output()
        self.echo_watch, self.restart_due = EchoWatch(restarted=canceller_since), False
        self.xruns, self.load = Xruns(stop), Load()
        self.xrun_due, self.xrun_rest, self.last_xrun = False, XRUN_REST, 0.0
        self.busy_told = False  # this busy spell's spoken notice, said once
        self.wary_until, self.play_started = 0.0, 0.0
        self.calling = False  # on a phone call (set each frame)
        self.sound_read, self.sound_leaks, self.call_out = 0.0, False, ""  # the phone's sound, from CALL_SOUND
        self.crashed = False
        # Floors kept apart: while a clip plays the cancelled mic sits far below the room
        self.room, self.echo = collections.deque(maxlen=FLOOR_SPAN), collections.deque(maxlen=FLOOR_SPAN)
        self.preroll = collections.deque(maxlen=PREROLL)
        self.gate = VoiceGate()

    @staticmethod
    def load_voiceprint(cfg: whisper.Config) -> voiceprint.Voiceprint | None:
        """The user's voiceprint, a soft signal (voiceprint.py): on when enrolled, unless config.json says
        "voiceprint": false. The model loads in the background, so the first check doesn't wait for it."""
        if not getattr(cfg, "voiceprint", True):
            log(HANDSFREE_LOG, "voiceprint: off in config.json")
            return None
        model = voiceprint.Model()
        found = voiceprint.Voiceprint.load(model=model)
        if found is None:
            log(HANDSFREE_LOG, "voiceprint: not enrolled (handsfree voiceprint enrol): words alone")
            return None
        threading.Thread(target=model.warm, daemon=True).start()
        log(HANDSFREE_LOG, f"voiceprint: on ({', '.join(found.centroids)})")
        return found

    def print_path(self) -> str:
        """Which voiceprint the mic is scored against: the phone's mic on a call is a plain mic, as the laptop's raw."""
        return voiceprint.RAW if self.calling else voiceprint.EC

    # --- the loop -----------------------------------------------------------

    def run(self) -> None:
        self.button.start()
        self.playback.start()
        self.xruns.start()
        try:
            self.listen()
        except Exception:
            log(HANDSFREE_LOG, "listener crashed, restarting: " + traceback.format_exc().strip().splitlines()[-1])
            self.crashed = True
            release_phase(BY_HANDSFREE)
            self.stop.set()

    def listen(self) -> None:
        self.open_stream()
        was_busy = False
        try:
            while not self.stop.is_set() and (samples := self.read()) is not None:
                frame = samples.astype(np.float32) / 32768
                busy = talkback_busy()
                if not busy and was_busy:  # a clip just ended: the user may answer
                    self.window.open(WINDOW, Heard.REPLY, never_shorter=True)
                was_busy = busy
                self.follow_requests()
                self.follow_source()
                if AWAY.exists() and not in_call():  # away: nothing heard, no reply window, the stream kept drained
                    self.window.close()
                    self.button.tap.clear()
                    continue
                if self.button.tap.is_set():
                    self.button.tap.clear()
                    if not ptt_busy():
                        log(HANDSFREE_LOG, "earbud tap")
                        self.turns.tapped = self.button
                        try:
                            self.turn([], Heard.WOKE, floor_of(self.room, level(frame)))
                        finally:
                            self.turns.tapped = None
                        was_busy = talkback_busy()
                        continue
                self.watch_xruns()
                if self.step(samples, frame):
                    was_busy = talkback_busy()
                elif (self.restart_due or self.xrun_ready()) and not busy and not ptt_busy():
                    self.restart_canceller()  # between clips; a reply window may be open
                elif not busy and not self.window.is_open():
                    self.transcriber.settle()  # back to the GPU once a training run lets go
        finally:
            self.stream.terminate()

    def show(self, mode: Mode) -> None:
        """The tray's colour, and the call page's talk button."""
        if mode is not self.mode:
            self.mode = mode
            HANDSFREE_MODE.write_text(mode.value)

    def open_stream(self) -> None:
        self.source = mic_source()  # the phone's mic during a call
        self.stream = mic_stream(self.source)
        self.opened, self.frames_read = time.monotonic(), 0
        log(HANDSFREE_LOG, f"listening to {self.source}")

    def follow_source(self) -> None:
        """A call started or ended: listen to the phone, or to the laptop mic again."""
        if self.frames_read % PER_SECOND == 0 and mic_source() != self.source:
            self.stream.terminate()
            self.open_stream()

    def read(self) -> np.ndarray | None:
        self.frames_read += 1
        return read_frame(self.stream)

    def behind(self) -> float:
        """Seconds the frame being handled lags real time (the stream's start latency included)."""
        return time.monotonic() - self.opened - self.frames_read * FRAME / RATE

    def echo_slipped(self, why: str) -> None:
        """Our own voice got past the canceller: a fresh one when it may, and stricter for a while.

        Not longer while already stricter: each echo verdict pushed it 60 s further, and with the canceller weak
        it stayed strict for 13 minutes and the user couldn't get a word in (2026-11-21)."""
        if self.calling:  # the phone's mic: says nothing about the laptop's canceller
            log(HANDSFREE_LOG, f"{why} (on a call: not stricter)")
            return
        self.restart_due |= self.echo_watch.may_restart() and not self.load.busy  # busy: a fresh one breaks too
        now = time.monotonic()
        if now < self.wary_until:
            log(HANDSFREE_LOG, f"{why} (already stricter, {self.wary_until - now:.0f}s left)")
            return
        self.wary_until = now + WARY
        log(HANDSFREE_LOG, f"{why}: stricter about talking over for {WARY:.0f}s")

    def watch_xruns(self) -> None:
        """New xruns on the canceller's path ask for a fresh canceller at the next quiet moment (not on a call: the
        phone's mic doesn't go through it)."""
        grew = self.xruns.take()
        if not grew or self.calling:
            return
        now = time.monotonic()
        if now - self.last_xrun > XRUN_REST_MAX:  # the first xrun after a quiet spell: the short rest again
            self.xrun_rest = XRUN_REST
        self.last_xrun, self.xrun_due = now, True
        log(HANDSFREE_LOG, "canceller xrun: " + ", ".join(f"{node} +{n}" for node, n in grew.items())
            + ", restart when quiet")

    def xrun_ready(self) -> bool:
        """An xrun asked for a restart and may have it now: the canceller had XRUN_REST since its last start (not the
        drift watch's five minutes: an xrun broke a canceller for good in one step), and the laptop isn't so busy that
        a fresh one would break again at once."""
        return (self.xrun_due and time.monotonic() - self.echo_watch.restarted >= self.xrun_rest
                and not self.load.high())

    def restart_canceller(self) -> None:
        """A fresh canceller, between clips (about a second; a reply starting then loses its first words)."""
        if self.xrun_due:  # each xrun restart makes the next wait longer, up to five minutes, until xruns stop
            self.xrun_rest = min(XRUN_REST_MAX, self.xrun_rest * 2)
        log(HANDSFREE_LOG, "restarting the echo canceller " + ("after an xrun" if self.xrun_due
                                                                else "before it drifts further"))
        self.restart_due = self.xrun_due = False
        self.xruns.settle()
        self.stream.terminate()
        self.canceller_since = start_canceller(restart=True)
        self.echo_watch.restarted = time.monotonic()
        self.echo.clear()
        self.open_stream()

    def follow_requests(self) -> None:
        """`handsfree close` and `handsfree listen N` / a switch, through their files."""
        if CLOSE.exists():  # only the wake word opens it again
            CLOSE.unlink(missing_ok=True)
            LISTEN.unlink(missing_ok=True)
            self.window.close()
        try:
            self.window.open(float(LISTEN.read_text()) - time.time(), Heard.THINK, never_shorter=True)
        except (FileNotFoundError, ValueError):
            pass

    def step(self, samples: np.ndarray, frame: np.ndarray) -> bool:
        """One 30 ms frame; True when it led to a turn."""
        db = level(frame)
        playing = PLAYING.exists()
        self.talkover.track_onset(self.playback.loudest())
        # On a phone call the line is always open, as on any call: the phone's own echo cancelling keeps
        # our voice out, and only "hey Claude" getting in made calls deaf between clips (2026-11-15).
        # The laptop's canceller is not in that path, so the phone's mic says nothing about it: no echo
        # watching (a loud street restarted it mid-call, 2026-11-15)
        calling = self.calling = in_call()
        self.talkover.print_path = self.print_path()
        # On a call the phone's own canceller keeps our voice out of its mic: less voice is needed before the words are
        # looked at, so the user cuts in sooner (2026-11-21: "you need to stop sooner"). Not on its loudspeaker in clear
        # sound, or without its canceller: our voice can reach its mic there, and it is judged with care
        careful = not calling or self.call_sound_leaks()
        self.talkover.sustain = SUSTAIN if careful else CALL_SUSTAIN
        # Laptop busy: its canceller leaks more and a restart can't help while it lasts, so talking over is judged
        # strictly (more voice, three words the clip doesn't say, never length alone) and the wake word also stops a
        # clip. Not judged at all, the user couldn't stop a clip by talking (2026-11-22)
        busy_laptop = not calling and self.load.high()
        self.talkover.strict = busy_laptop
        self.talkover.new_words_needed = CAREFUL_NEW_WORDS if (calling and careful) or busy_laptop else NEW_WORDS
        self.tell_busy(playing, busy_laptop, calling)
        if self.play_started and not playing and not calling:
            if busy_laptop:
                self.echo_watch.discard()
            else:
                self.restart_due |= self.echo_watch.clip_over()
        self.play_started = (self.play_started or time.monotonic()) if playing else 0.0
        button_only = calling and CALL_BUTTON_ONLY.exists()  # a noisy place: noise must not talk over us
        # The phone hears the user itself (a bad line): it sends the words, so nothing here opens a turn, the talk
        # button's and the wake word's included; what arrives on the line meanwhile is not the user's turn
        phone_ears = calling and phone_listens(calling)
        open_ = ((playing or calling or self.window.is_open()) and not button_only and not phone_ears
                 and not ptt_busy() and not PAUSE.exists())
        self.show(Mode.LISTENING if open_ else Mode.WAITING)
        if phone_ears:
            self.gate.clear()
            self.preroll.clear()
            self.talkover.reset()
            return False
        if calling and CALL_PUSH.exists() and not ptt_busy():  # the call page's talk button: a turn until let go
            self.turn(list(self.preroll), Heard.PUSHED, floor_of(self.room, db))
            return True
        if button_only:
            self.preroll.append(frame)  # the button's turn starts with what came just before it
            return False
        if not open_:
            self.gate.clear()
            self.preroll.clear()
            self.talkover.reset()
            (self.echo if playing else self.room).append(db)
            return not playing and self.woke(samples, floor_of(self.room, db))
        history = self.echo if playing else self.room
        floor = floor_of(history, db)
        turn_floor = max(floor, floor_of(self.room, floor))
        self.preroll.append(frame)
        self.gate.add(frame, db)
        if playing and not calling and not busy_laptop and time.monotonic() - self.play_started > BARGE_GRACE:
            self.echo_watch.add(max(self.gate.levels), self.playback.loudest())
        unknown = self.output.on_speakers() and self.playback.loudest() is None
        # Our own notice, or playback level unknown: only the wake word
        if playing and (self.talkover.notice_playing() or unknown):
            history.append(db)
            return self.woke(samples, turn_floor)
        if playing and busy_laptop and self.woke(samples, turn_floor):  # busy: the wake word as well as talking over
            return True
        speech = self.talking(frame, db, floor, playing)
        if playing or self.talkover.over:
            return self.talking_over(frame, speech, turn_floor, history, db)
        if not speech:
            history.append(db)
            return False
        self.turn(list(self.preroll), self.window.kind if self.window.is_open() or not calling else Heard.CALL,
                  turn_floor)
        return True

    def tell_busy(self, playing: bool, busy_laptop: bool, calling: bool) -> None:
        """Once a busy spell, over a clip and never on a call: why talking over it is slower now (said after the
        clip it began in)."""
        if playing and busy_laptop and not self.busy_told:
            self.busy_told = True
            routing.say(f"{own_name()} is busy, so talking over me takes a little longer. The wake word works at once.")
        elif not busy_laptop and not calling:
            self.busy_told = False

    def call_sound_leaks(self) -> bool:
        """On a call: can our voice reach the phone's mic? Its loudspeaker in clear sound, or no canceller on its mic.
        Read at most once a second."""
        now = time.monotonic()
        if now - self.sound_read >= 1:
            self.sound_read, sound = now, read_json(CALL_SOUND)
            self.sound_leaks = (sound.get("aec") is False
                                or sound.get("sound") == "clear" and sound.get("out") == "speaker")
            self.call_out = f"the phone's {sound.get('out', 'speaker')}, {sound.get('sound', '?')} sound" if sound else ""
        return self.sound_leaks

    def output_now(self) -> str:
        """Where our voice is playing, for the log of every talk-over decision."""
        if self.calling:
            return self.call_out or "the phone"
        return "the laptop's speakers" if self.output.on_speakers() else "headphones"

    def wary(self) -> bool:
        """A young canceller, or one that just let our echo through: it slips more."""
        return time.monotonic() - self.canceller_since < WARMUP or time.monotonic() < self.wary_until

    def talking_over(self, frame: np.ndarray, speech: bool, floor: float, history: collections.deque,
                     db: float) -> bool:
        """A voice over a clip (talkover.py decides); True when it stopped the clip and a turn ran."""
        if not self.talkover.over:
            history.append(db)
        how = self.last_check if speech else "a voice, quieter"
        playing = self.talkover.clip()
        self.talkover.out = self.output_now()
        verdict = self.talkover.feed(frame, speech or self.gate.still_speaking(), list(self.preroll), how,
                                     playing.text if playing else "", playing.clip if playing else "",
                                     strong=speech, margin=self.gate.margin if speech else None)
        if verdict is Verdict.ECHO:
            self.echo_slipped("talking over was the clip's own words")
            return False
        if verdict is Verdict.WAITING:
            return False
        self.turn(self.talkover.taken.frames, Heard.OVER_YOU, floor)
        return True

    def talking(self, frame: np.ndarray, db: float, floor: float, playing: bool) -> bool:
        """Is the user speaking now (and, over a clip, not just our own echo)? voicegate.py decides.

        On a call neither the laptop canceller's strictness nor the loudness check against our playback applies:
        the phone's mic level and the clip's level on the laptop are unrelated (a quiet phone mic, -30 dB against
        -17 dB of playback, failed the 15 dB margin, and the user couldn't cut in on calls, 2026-11-21); the
        phone's own echo cancelling keeps our voice out, and the word check still catches echo that gets in."""
        calling = self.calling
        wary = playing and not calling and self.wary()
        played = self.playback.loudest() if playing and not calling else None
        settled = not playing or time.monotonic() - self.play_started > BARGE_GRACE
        speech = self.gate.talking(frame, db, floor, playing, played, settled, wary)
        margin = self.gate.margin
        if not playing or calling or margin is None or self.gate.checked:  # no check over a clip ran on this frame
            return speech
        near_miss = not speech and not in_call() and not self.load.busy and self.echo_watch.near_miss(margin)
        self.restart_due |= near_miss
        # In numbers (never audio), so a talk-over, or a false one, can be explained
        self.last_check = (f"mic {margin + played:.0f} dB, playing {played:.0f} dB, floor {floor:.0f} dB, "
                           f"{time.monotonic() - self.play_started:.1f}s in, {self.behind() * 1000:.0f} ms behind"
                           f"{', wary' if wary else ''}{', whisper on cpu' if self.transcriber.on_cpu else ''}"
                           f"{', near miss' if near_miss else ''}")
        return speech

    def woke(self, samples: np.ndarray, floor: float) -> bool:
        if not self.wake:
            return False
        if PAUSE.exists() or ptt_busy():
            self.wake.forget()  # a nomination from before must not be checked against audio from after
            return False
        if not self.wake.feed(samples):
            return False
        began = time.monotonic()
        # Whistle first, always (~0.1 s, the GPU left asleep; the user's choice, 2026-11-17): Whisper on the CPU took
        # ~2.5 s; Whisper is the fallback
        by = "whistle" if self.whistle else "whisper"
        try:
            woke = (self.wake.confirm_by(self.whistle.hear) if by == "whistle"
                    else self.transcriber.run(self.wake.confirm))
        except Exception as exc:  # Whistle failing: Whisper as before
            log(HANDSFREE_LOG, f"whistle failed ({exc}): Whisper confirms")
            by, woke = "whisper", self.transcriber.run(self.wake.confirm)
        if woke:
            log(HANDSFREE_LOG, f"wake word confirmed by {by} in {time.monotonic() - began:.2f}s")
            woke = self.sounds_like_user(by)
        if not woke:
            near = f", nearest {self.wake.near:.2f}" if self.wake.near is not None else ""
            log(HANDSFREE_LOG, f"wake candidate rejected ({self.wake.word}{near})")
            return False
        log(HANDSFREE_LOG, f"woke ({self.wake.word})")
        self.turn([], Heard.WOKE, floor)
        return True

    def sounds_like_user(self, by: str) -> bool:
        """After a wake word was confirmed: a voice unlike the user's must also pass Whisper with no hint of the word.
        Never a lockout: the user said it clearly enough, it passes; no score, or Whisper failing, passes too."""
        if not self.voiceprint:
            return True
        check = self.voiceprint.check(self.wake.audio, self.print_path(), min_seconds=voiceprint.WAKE_MIN_SECONDS)
        if check is None:
            return True
        log(HANDSFREE_LOG, f"{check.said()}, wake word ({self.voiceprint.took * 1000:.0f} ms)")
        if not check.unlike or (by == "whisper" and not self.wake.hinted):  # Whisper unhinted already: as strict
            return True
        began = time.monotonic()
        try:
            again = self.transcriber.run(self.wake.recheck)
        except Exception as exc:
            log(HANDSFREE_LOG, f"stricter wake check failed ({exc}): let through")
            return True
        log(HANDSFREE_LOG, f"voice unlike the user's: Whisper with no hint {'heard' if again else 'did not hear'} "
                           f"the word ({time.monotonic() - began:.2f}s)")
        return bool(again)

    # --- one turn -----------------------------------------------------------

    def turn(self, frames: list, heard: Heard, floor: float) -> None:
        """Record, transcribe, act; then decide how long to keep listening."""
        if heard is Heard.WOKE:
            whisper.ding(self.cfg)  # "I'm listening"
        if heard is Heard.OVER_YOU:
            self.echo_watch.discard()
        kept = self.window.until, self.window.kind
        LISTEN.unlink(missing_ok=True)  # they spoke: an earlier long window is used up (a switch writes a new one)
        extra = self.turns.take(frames, floor, heard)
        self.gate.clear()
        self.preroll.clear()
        if extra is None:  # no words in it (a noise): the window they had stays theirs
            self.window.until, self.window.kind = kept
            if kept[0] > time.monotonic() and not LISTEN.exists():
                LISTEN.write_text(str(time.time() + kept[0] - time.monotonic()))
            return
        if extra:
            self.window.open(extra, Heard.THINK)
            LISTEN.write_text(str(time.time() + extra))
        elif heard is Heard.FOLLOW_ON:
            self.window.close()  # a follow-on never opens another, so chatter cannot chain
        else:
            self.window.open(FOLLOW, Heard.FOLLOW_ON)
