"""Talking over a clip: when the user's voice should stop it, and when it is only our own echo.

A voice over a clip has to add up to SUSTAIN seconds first; then Whisper
writes out what was heard, and if that is mostly the clip's own words, it was
our echo and the clip plays on. Loudness rules alone could not tell them apart:
the canceller sometimes lets our voice through for two or three seconds.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

import numpy as np

from sensing import FRAME, RATE
from voice.state import BY_HANDSFREE, HANDSFREE_LOG, PLAYING as PLAYING_PATH, Playing, log

CHECK_EVERY = 3  # frames between voice checks (90 ms)
STEP = CHECK_EVERY * FRAME / RATE  # seconds of voice one positive check stands for
SUSTAIN = 0.6  # seconds of voice, added up, before the words are looked at
CALL_SUSTAIN = 0.4  # on a phone call, whose own echo cancelling keeps our voice out of its mic
GAP = 1.5  # seconds without a voice that end an attempt: the user spoke in 1.5 s phrases with pauses
UNSURE_LIMIT = 2.0  # seconds of voice after which the clip stops even if the words stayed too few
RECHECK = 0.3  # more seconds of voice before looking at the words again
# An echo verdict no longer ends the attempt (2026-11-21: with the canceller weak, the user's 10-25 s of talking was
# judged in fresh 0.6 s slices, each mostly our echo, and won only by luck). It goes on adding up, the newest audio is
# looked at again, and a voice that keeps going wins.
ECHO_RECHECK = 0.6  # more seconds of voice before looking again after an echo verdict
LATEST = 1.2  # seconds of the newest audio looked at after an echo verdict
# This much voice, and this many words that aren't the clip's, in two looks in a row: the user. Kept careful: a
# video's voices on the same speakers also say words the clip doesn't, and length alone never wins.
NEW_WORDS_AFTER, NEW_WORDS = 1.2, 2
CAREFUL_NEW_WORDS = 3  # on a call where our voice can reach the phone's mic (loudspeaker in clear sound, no canceller)
OWN_WORDS_MIN = 2  # words heard before telling our echo from the user
OWN_WORDS = 0.6  # share of what was heard that is the clip's own words, in order: our echo
OWN_RUN, OWN_SHARE = 3, 0.3  # or at least this many of the clip's words, in order, making up this share
ONSET_QUIET = 1.0  # seconds of silent playback before our voice counts as starting again (for the log)
AUDIBLE_DB = -45  # playback louder than this is our voice
NUMBERS = dict(enumerate("zero one two three four five six seven eight nine ten eleven twelve".split()))


def word_keys(text: str) -> list[str]:
    """Words as Whisper and talkback may spell them differently: "5" and "five", "recognize" and
    "recognised" (2026-11-15) match on their number word and first four letters."""
    words = re.findall(r"[a-z0-9']+", text.lower())
    words = [NUMBERS.get(int(w), w) if w.isdigit() else w for w in words]
    return [w[:4] for w in words]


def shared_words(heard_words: list[str], said_words: list[str]) -> int:
    """How many of the heard words are the said ones, in order (longest common subsequence)."""
    row = [0] * (len(said_words) + 1)
    for word in heard_words:
        prev = 0
        for j, other in enumerate(said_words, 1):
            prev, row[j] = row[j], prev + 1 if word == other else max(row[j], row[j - 1])
    return row[-1]


def own_share(heard: str, said: str) -> float:
    """The share of the heard words that are the clip's own, in order (0 with too few to tell)."""
    heard_words, said_words = word_keys(heard), word_keys(said)
    if len(heard_words) < OWN_WORDS_MIN or not said_words:
        return 0.0
    return shared_words(heard_words, said_words) / len(heard_words)


def new_words(heard: str, said: str) -> int:
    """Words heard that the clip never says: the user's, not our echo."""
    said_keys = set(word_keys(said))
    return sum(word not in said_keys for word in word_keys(heard))


def our_own_words(heard: str, said: str) -> bool:
    """Is what was heard over a clip mostly that clip's own words, in order? Then it was our echo.

    The user talking over us says something else. A single word is too little to tell.
    """
    heard_words, said_words = word_keys(heard), word_keys(said)
    if len(heard_words) < OWN_WORDS_MIN or not said_words:
        return False
    shared = shared_words(heard_words, said_words)
    # Whisper adds words of its own to echo ("That was a pretty good question, slowly, while nobody wa-":
    # all ours, 2026-11-15), so a run of our words counts even among others
    return shared / len(heard_words) >= OWN_WORDS or (shared >= OWN_RUN and shared / len(heard_words) >= OWN_SHARE)


@dataclass
class Overlap:
    """A voice over a clip, kept while it lasts, every frame since it began."""

    started: float
    frames: list[np.ndarray]
    first: str  # how it began, for the log
    clip_text: str  # what we were saying
    clip: str = ""  # which clip: another one starting ends the attempt (its words are another text)
    last: float = 0.0  # last time it still sounded like a voice
    voiced: float = STEP  # seconds that sounded like a voice, added up
    next_check: float = 0.0  # seconds of voice before the next look at its words (after SUSTAIN)
    echoes: int = 0  # echo verdicts so far: after one, only the newest audio is looked at
    user_from: int = 0  # the frame the user's own words likely start at (after the last echo verdict)
    new_words_seen: int = 0  # looks in a row whose words included NEW_WORDS the clip doesn't say

    def speaking_for(self) -> float:
        return self.last - self.started


class Verdict(str, Enum):
    WAITING = "waiting"  # not enough voice yet, or none at all
    ECHO = "echo"  # our own words: the clip plays on
    USER = "user"  # stop the clip: the user is talking


@dataclass
class TalkOver:
    """The one attempt at talking over a clip in progress, and when our voice last started."""

    quick_text: Callable[[np.ndarray], str]  # a fast transcript of a little audio
    over: Overlap | None = None
    playing: Playing | None = None  # the clip now playing, read once per clip
    taken: Overlap | None = None  # the last attempt that stopped a clip: its frames and the clip's words
    onset: float = 0.0  # when our voice last became audible after a silence
    silent_since: float = 0.0
    read_at: float = 0.0  # when the playing marker was last read
    clock: Callable[[], float] = time.monotonic  # replaced by the recording's own time in echo_replay.py
    sustain: float = SUSTAIN
    new_words_needed: int = NEW_WORDS
    out: str = ""  # where our voice plays (laptop speakers, headphones, the phone's loudspeaker…): for the log
    gap: float = GAP
    log_to: Path = HANDSFREE_LOG

    def track_onset(self, played_db: float | None) -> None:
        now = self.clock()
        if played_db is None or played_db < AUDIBLE_DB:
            self.silent_since = self.silent_since or now
        elif self.silent_since:
            if now - self.silent_since >= ONSET_QUIET:
                self.onset = now
            self.silent_since = 0.0

    def reset(self) -> None:
        self.over = None

    def clip(self) -> Playing | None:
        """The clip playing now; the marker is read again only when talkback starts another."""
        try:
            changed = PLAYING_PATH.stat().st_mtime
        except OSError:  # no clip, or talkback removed the marker just now
            self.playing = None
            return None
        if self.playing is None or changed > self.read_at:
            self.playing, self.read_at = Playing.now(), time.time()
        return self.playing

    def notice_playing(self) -> bool:
        """Hands-free's own short notices: their echo was taken for the user ("Now talking to Claude in
        peace", 2026-11-15), and nobody needs to talk over two seconds of "Now talking to …"."""
        playing = self.clip()
        return bool(playing) and playing.agent == BY_HANDSFREE

    def feed(self, frame: np.ndarray, voice: bool, preroll: list[np.ndarray], how: str, clip_text: str,
             clip: str = "") -> Verdict:
        """One frame while a clip plays (or just after); voice: does it sound like someone talking;
        clip_text: what we are saying, to tell our echo by; clip: which clip it is."""
        now = self.clock()
        if self.over is not None and clip and self.over.clip and clip != self.over.clip:
            log(self.log_to, f"talking over: {self.over.voiced:.1f}s of voice, then the next clip began: "
                               f"judged again from here (first: {self.over.first})")
            self.over = None  # the attempt had survived echo verdicts on the last clip: not judged against its words
        if self.over is None:
            if voice:
                self.over = Overlap(now, list(preroll), f"{how}, {now - self.onset:.1f}s after our voice started",
                                    clip_text, clip, last=now)
            return Verdict.WAITING
        over = self.over
        over.frames.append(frame)
        if voice:
            over.last = now
            over.voiced += STEP
        if now - over.last > self.gap:
            log(self.log_to, f"talking over: {over.voiced:.1f}s of voice in {over.speaking_for():.1f}s, "
                               f"then quiet: kept playing (first: {over.first})")
            self.over = None
            return Verdict.WAITING
        if over.voiced < max(self.sustain, over.next_check):
            return Verdict.WAITING
        text = self.heard_text(over)
        echo = self.echo_of_text(text, over.clip_text)
        why = ""
        fresh = new_words(text, over.clip_text) >= self.new_words_needed
        over.new_words_seen = over.new_words_seen + 1 if fresh else 0
        if echo and over.voiced >= NEW_WORDS_AFTER and over.new_words_seen >= 2:
            echo, why = False, ", words the clip doesn't say, twice"
        # Too few words to tell yet: listen a little longer. Past UNSURE_LIMIT a first look stops the clip anyway; an
        # attempt that already had an echo verdict never stops for too few words (that would be length alone)
        if echo is None and (over.voiced < UNSURE_LIMIT or over.echoes):
            over.next_check = over.voiced + RECHECK
            return Verdict.WAITING
        log(self.log_to, f"talking over: {over.voiced:.1f}s of voice in {over.speaking_for():.1f}s: "
                           + ("the clip's own words, our echo: kept playing" if echo else f"stopping the clip{why}")
                           + f" (first: {over.first}{', on ' + self.out if self.out else ''})")
        if echo:  # the attempt goes on: the user may still be talking under our echo
            over.echoes += 1
            over.next_check = over.voiced + ECHO_RECHECK
            over.user_from = max(0, len(over.frames) - round(LATEST * RATE / FRAME))
            return Verdict.ECHO
        self.over = None
        if over.echoes:  # the turn starts where the user's own words likely did, not with our echo before them
            over.frames = over.frames[over.user_from:]
        self.taken = over
        return Verdict.USER

    def heard_text(self, over: Overlap) -> str:
        """What was said over the clip: all of it at first, only the newest LATEST seconds after an echo verdict."""
        if not over.clip_text:
            return ""
        frames = over.frames[-round(LATEST * RATE / FRAME):] if over.echoes else over.frames
        return self.quick_text(np.concatenate(frames))

    @staticmethod
    def echo_of_text(text: str, clip_text: str) -> bool | None:
        """Are the words heard over the clip its own? None when there are too few words to tell.

        Checking only after stopping let the resumed clip's echo cut it again,
        over and over; deciding on one word did the same (2026-11-15).
        """
        if not clip_text:
            return False
        if len(word_keys(text)) < OWN_WORDS_MIN:
            return None
        return our_own_words(text, clip_text)

    def echo_of_clip(self, over: Overlap) -> bool | None:
        return self.echo_of_text(self.heard_text(over), over.clip_text)
