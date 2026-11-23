"""One turn: record the user until they stop, transcribe it, and act on it.

The listener decides when a turn starts (a reply window, talking over a clip,
the wake word, an earbud tap); this records until a pause, "over" or the
limit, and then routes the words: to an agent, to a switch, or nowhere.
"""

from __future__ import annotations

import re
import threading
import time
import wave
from enum import Enum
from typing import Callable

import numpy as np
from faster_whisper.vad import get_speech_timestamps

import routing
from routing import Heard
from sensing import PER_SECOND, RATE, SPEECH, level
from talkover import TalkOver, our_own_words, own_share, word_keys
from voicegate import START_DB
from voice.speech import nemotron, whisper
from voice.state import (BY_HANDSFREE, CALL_PUSH, HANDSFREE_LOG, HANDSFREE_WAV, Phase, in_call, log, ptt_busy,
                         release_phase, set_phase, set_target)

BUDS_LEAVING = 1.0  # seconds: buds that disconnect this soon before a tap turn starts, or during it, drop it
WAIT_FOR_WORDS = 5.0  # after the wake word, seconds to start talking before it gives up
CHIME_GRACE = 1.2  # the listening chime is loud in the mic; it must not count as them starting, then the pause close the turn
END_DB, END_FRAMES = 6, 50  # 1.5 s at most END_DB over the floor ends the turn
CALL_END_FRAMES = 30  # 0.9 s on a phone call: the user found 1.5 s slow there; a longer pause becomes a follow-on
# Nemotron's words so far end a sentence ("…tests.", not "…and uh"): the turn ends after this much quiet instead
# (the user's wish, 2026-11-17: answers sooner, now that the words are ready as they speak)
EARLY_END_FRAMES, CALL_EARLY_END_FRAMES = 27, 20  # 0.8 s; 0.6 s on a call
NO_STOP_FRAMES = 10  # its last full stop often doesn't come: then 0.3 s more, still short of the full pause
TRAILING = {"uh", "um", "er", "erm", "hmm", "and", "but", "or", "so", "because", "the", "a", "an", "to", "of",
            "with", "like", "if", "then", "that", "which", "is", "are", "my", "your", "for", "in", "on"}
BELOW_PEAK = 25  # dB under their loudest also counts as quiet, for a noisy room
ECHO_TURN_SECONDS = 3.0  # a turn over a clip shorter than this, of the clip's own words, is our echo
MOSTLY_OURS = 0.6  # or, longer, when at least this share of its words is the clip's
OVER_FRAMES = 20  # after 0.6 s of quiet, check whether the last word was "over"
# On a call the line can carry steady hiss (Henchline's clear sound has no call-mode noise suppression) that never
# gets quiet enough to end a turn: "Hearing you…" for 55 s (2026-11-15). There a second without speech (Silero),
# looked at every 0.3 s, ends the turn as well
VOICE_EVERY, VOICE_GONE = 10, 1.0
OVER_TAIL = 4 * PER_SECOND  # frames Whisper looks at for "over"
PTT_CHECK = 10  # frames between checks that push-to-talk has not taken the mic
MAX_SECONDS = 120  # the user talks in long stretches; 60 s cut them off
WAKE_PREFIX = re.compile(r"^\W*((hey|hi|okay|ok)\W+)?claude\W*", re.I)
OVER = re.compile(r"[\s,.!?]*\bover\b[\s.!?]*$", re.I)
TONE_NOTE = re.compile(r"\s*\[tone: [^\]]*\]$")


class Mode(str, Enum):
    """What the tray shows."""

    WAITING = "waiting"  # only the wake word gets in
    LISTENING = "listening"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"


def only_hint_words(text: str, hotwords: list[str]) -> bool:
    """Whisper given noise writes out the names it was told to expect ("Cursor, Codex, Kokoro",
    2026-11-15): a transcript of nothing but those is not the user talking."""
    words = re.findall(r"[a-z']+", text.lower())
    return bool(words) and set(words) <= set(re.findall(r"[a-z']+", " ".join(hotwords).lower()))


def echo_turn(text: str, clip_text: str, seconds: float) -> bool:
    """A turn that stopped a clip was only our echo: the clip's own words, and short or mostly them."""
    return our_own_words(text, clip_text) and (seconds < ECHO_TURN_SECONDS or own_share(text, clip_text) >= MOSTLY_OURS)


LEAD_MIN = 2  # leading words of the clip's own, at least, before they are taken off a turn over it


def without_clip_lead(text: str, clip_text: str) -> str:
    """A turn over a clip often starts with a few of the clip's words (our echo, before the user's): those go."""
    said = set(word_keys(clip_text))
    tokens = list(re.finditer(r"\S+", text))
    lead = 0
    for token in tokens:
        keys = word_keys(token.group())
        if not keys or not all(k in said for k in keys):
            break
        lead += 1
    if lead < LEAD_MIN or lead == len(tokens):
        return text
    return text[tokens[lead].start():]


class Turn:
    def __init__(self, cfg: whisper.Config, transcriber: whisper.Transcriber, talkover: TalkOver,
                 read: Callable[[], np.ndarray | None], show: Callable[[Mode], None],
                 echo_slipped: Callable[[str], None], stop: threading.Event) -> None:
        self.cfg, self.transcriber, self.talkover = cfg, transcriber, talkover
        self.read, self.show, self.echo_slipped, self.stop = read, show, echo_slipped, stop
        self.last_message: tuple[str, Heard] | None = None  # for "send that to …"
        self.live = None  # this turn, heard as it is recorded (Nemotron), when it is installed
        self.whistle = None  # set by the listener: quick checks (the wake word, "over") without Whisper
        self.tapped = None  # the earbud button, while a turn it started records
        self.buds_left = False
        self.streamer = None
        if cfg.streaming and nemotron.available():
            try:
                self.streamer = nemotron.Streamer(cfg.hotwords, HANDSFREE_LOG)
            except Exception as exc:  # Whisper as before
                log(HANDSFREE_LOG, f"nemotron didn't load ({exc}): Whisper transcribes turns")

    def take(self, frames: list[np.ndarray], floor: float, heard: Heard) -> float | None:
        """One turn, end to end; returns seconds to keep listening (0: the default), None if no words."""
        frames = self.record(frames, floor, heard)
        if frames is None:
            return None if self.buds_left else 0.0
        try:
            text, rich = self.transcribe(frames)
            tone = TONE_NOTE.search(rich)
            note = tone.group(0) if tone else ""
            rich = rich[: len(rich) - len(note)] if note else rich
            text, rich = WAKE_PREFIX.sub("", OVER.sub("", text)), WAKE_PREFIX.sub("", OVER.sub("", rich)) + note
            if only_hint_words(text, self.cfg.hotwords):
                log(HANDSFREE_LOG, "only Whisper's hint words: a noise, nothing sent")
                set_target(False)
                return None
            taken = self.talkover.taken
            # Thrown away as our echo only when short or mostly the clip's words: a 14 s turn reduced to two of
            # our words was thrown away whole (2026-11-21). A longer one goes out, said over the clip.
            if heard is Heard.OVER_YOU and taken and echo_turn(text, taken.clip_text, len(frames) / PER_SECOND):
                self.echo_slipped(f"what stopped the clip was its own words ({len(text.split())}): "
                                  "our echo; the clip plays on, nothing sent")
                set_target(False)
                return None
            if heard is Heard.OVER_YOU and taken:  # sent as said over the clip, without our echo at its start
                text, rich = without_clip_lead(text, taken.clip_text), without_clip_lead(rich, taken.clip_text)
            return self.act(text, rich, heard)
        finally:
            release_phase(BY_HANDSFREE)  # act() normally has already, before anything is sent

    def record(self, frames: list[np.ndarray], floor: float, heard: Heard) -> list[np.ndarray] | None:
        """Frames until a pause, "over", or the limit; None if push-to-talk took over."""
        set_phase(Phase.RECORDING, BY_HANDSFREE)  # talkback goes quiet and holds
        self.show(Mode.RECORDING)
        if self.live:  # a turn that failed before it was transcribed: its decoding stops
            self.live.cancel()
        self.live = self.streamer.begin() if self.streamer else None
        for frame in frames:  # what was heard before the turn began (the wake word, a word over a clip)
            if self.live:
                self.live.feed(frame)
        since, quiet, peak = time.monotonic(), 0, -120.0
        started = heard is not Heard.WOKE  # after "hey Claude" people pause for the chime
        calling = in_call()
        end = CALL_END_FRAMES if calling else END_FRAMES
        early = (CALL_EARLY_END_FRAMES if calling else EARLY_END_FRAMES) if self.cfg.early_end else 0
        finished = False
        voice_at, spoke = since, False  # when Silero last heard speech in the turn, and whether it ever did (calls)
        voiceless = False
        held = heard is Heard.PUSHED  # the button decides when it ends, not a pause
        self.buds_left = False
        released = False
        while (quiet < end or held) and time.monotonic() - since < MAX_SECONDS:
            if held and not CALL_PUSH.exists():
                released = True
                break
            samples = self.read()
            if samples is None:
                break
            if len(frames) % PTT_CHECK == 0 and ptt_busy():
                log(HANDSFREE_LOG, "push-to-talk took over; dropped the hands-free recording")
                if self.live:
                    self.live.cancel()
                    self.live = None
                return None  # it delivers these words; ours would arrive twice
            if self.tapped is not None and self.tapped.gone_at > since - BUDS_LEAVING:
                # Earbuds send a pause as they come off the ears or go into the case, and disconnect
                # seconds later (2026-11-23: 4-5 s). A tap whose buds then leave was not a tap.
                log(HANDSFREE_LOG, "earbuds disconnected right after the tap: dropped the recording")
                if self.live:
                    self.live.cancel()
                    self.live = None
                self.buds_left = True
                release_phase(BY_HANDSFREE)  # take() releases only a turn it transcribes; talkback must not hold on
                return None
            frame = samples.astype(np.float32) / 32768
            frames.append(frame)
            if self.live:
                self.live.feed(frame)
            db = level(frame)
            peak = max(peak, db)
            if not started:
                waited = time.monotonic() - since
                # The chime plays into the room as the turn opens. One loud moment of it used to start the
                # pause clock, and 1.5 s of quiet then closed the turn before they had said anything (Jarvis, 1.7 s).
                if heard is not Heard.WOKE or waited >= CHIME_GRACE:
                    started = db > floor + START_DB or waited > WAIT_FOR_WORDS
                continue
            quiet = quiet + 1 if db < max(floor + END_DB, peak - BELOW_PEAK) else 0
            if quiet == OVER_FRAMES and self.ends_with_over(frames):
                break
            # From the shorter pause on, looked at again now and then: the full stop can come a moment late
            if early and not held and quiet >= early and (quiet - early) % 5 == 0 and self.sentence_done(quiet - early):
                finished = True
                break
            if calling and not held and len(frames) % VOICE_EVERY == 0:
                recent = np.concatenate(frames[-int(VOICE_GONE * PER_SECOND):])
                if get_speech_timestamps(recent, SPEECH):
                    voice_at, spoke = time.monotonic(), True
                elif time.monotonic() - voice_at > (VOICE_GONE if spoke else WAIT_FOR_WORDS):
                    voiceless = True  # their words ended a second ago, or none came at all
                    break
        reason = ("the button" if released else "limit" if time.monotonic() - since >= MAX_SECONDS
                  else "no voice for a second (steady sound on the line)" if voiceless
                  else "a finished sentence" if finished else "over" if quiet == OVER_FRAMES else "pause")
        log(HANDSFREE_LOG, f"recorded {time.monotonic() - since:.1f}s, ended by {reason} (floor {floor:.0f} dB)")
        return frames

    def transcribe(self, frames: list[np.ndarray]) -> tuple[str, str]:
        """(plain text, text with tone marks); empty when there was no speech."""
        set_phase(Phase.TRANSCRIBING, BY_HANDSFREE)
        self.show(Mode.TRANSCRIBING)
        audio = (np.concatenate(frames) * 32767).astype("<i2") if frames else np.zeros(0, "<i2")
        # A cough or a bare "um" has no speech, and Whisper given only that tends
        # to write out its hint words ("Cursor, Cursor, Kokoro")
        live, self.live = self.live, None
        if not len(audio) or not get_speech_timestamps(audio.astype(np.float32) / 32768, SPEECH):
            log(HANDSFREE_LOG, "no speech in the recording")
            if live:
                live.cancel()
            return "", ""
        with wave.open(str(HANDSFREE_WAV), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(audio.tobytes())
        try:
            began = time.monotonic()
            heard = live.finish() if live else None
            if heard and heard[0]:
                text = nemotron.tidy(heard[0], self.cfg.hotwords)
                log(HANDSFREE_LOG, f"heard by nemotron {time.monotonic() - began:.2f}s after the turn ended")
                rich = text
                if self.cfg.tone:
                    try:
                        from voice.speech import tone

                        rich = tone.annotate(HANDSFREE_WAV, heard[1], text)
                    except Exception as exc:  # tone is extra; the words must still get through
                        log(HANDSFREE_LOG, f"tone failed: {exc}")
                return text, rich
            if live:
                log(HANDSFREE_LOG, "nemotron heard nothing: Whisper transcribes this turn")
            got = self.transcriber.file(HANDSFREE_WAV)
            log(HANDSFREE_LOG, f"heard by whisper {time.monotonic() - began:.2f}s after the turn ended")
            return got
        except Exception as exc:
            log(HANDSFREE_LOG, f"transcribe failed: {exc}")
            return "", ""
        finally:
            HANDSFREE_WAV.unlink(missing_ok=True)

    def act(self, text: str, rich: str, heard: Heard) -> float | None:
        def sent(message: tuple[str, Heard]) -> None:
            self.last_message = message

        return routing.act_on_turn(text, rich, heard, self.last_message, ding=lambda: whisper.ding(self.cfg),
                                   off=self.stop.set, echo=self.echo_slipped, sent=sent)

    def sentence_done(self, longer: int = 0) -> bool:
        """Nemotron's words so far end a sentence, not on a filler or a word that leads on: no need to wait out the
        whole pause. Its last full stop often doesn't come (the user's catch, 2026-11-17), so without one, once the
        quiet has gone `longer` than NO_STOP_FRAMES past the shorter pause. Without live words, never."""
        if not self.live:
            return False
        try:
            said = self.live.so_far().strip()
        except Exception:
            return False
        words = re.findall(r"[a-z']+", said.lower())
        if not words or words[-1] in TRAILING or OVER.search(said):
            return False
        return said[-1] in ".?!" or longer >= NO_STOP_FRAMES

    def ends_with_over(self, frames: list[np.ndarray]) -> bool:
        """Did they finish with "over"? Nemotron's words so far say it at once, the turn being heard as it is
        recorded (0.6 s of quiet has let its last word through); Whistle, else Whisper, looks at the tail."""
        if self.live:
            try:
                return bool(OVER.search(self.live.so_far().strip()))
            except Exception as exc:
                log(HANDSFREE_LOG, f"nemotron's words so far failed ({exc})")
        tail = np.concatenate(frames[-OVER_TAIL:])
        if self.whistle:
            try:
                return bool(OVER.search(self.whistle.hear(tail, None).strip()))
            except Exception as exc:
                log(HANDSFREE_LOG, f"whistle failed ({exc}): Whisper checks for over")
        return bool(OVER.search(self.transcriber.quick_text(tail).strip()))
