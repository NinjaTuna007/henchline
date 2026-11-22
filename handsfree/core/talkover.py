"""Talking over a clip: when the user's voice should stop it, and when it is only our own echo.

A voice over a clip has to add up to SUSTAIN seconds first; then Whisper
writes out what was heard, and if that is mostly the clip's own words, it was
our echo and the clip plays on. Loudness rules alone could not tell them apart:
the canceller sometimes lets our voice through for two or three seconds.

A voice that keeps coming back counts too: during double-talk the canceller
breaks the user's voice into scraps, so attempts that start again within
REJOIN of each other are one attempt, and each start after the first has its
words looked at early. Such thin evidence stops a clip only with words the clip
doesn't say (THIN_NEW_WORDS) and a voice nearly as loud as our playback in
the last PERSIST_SPAN (or, with no echo verdict, after many starts, two of
them that loud and recent); voice summed across scraps
never counts toward SUSTAIN's trust or UNSURE_LIMIT.

When the words leave it open (too few to tell, or a thin voice short of the
words it needs), a voice that sounds like the user's (voiceprint.py) stops the
clip; the clip's own words always keep it playing, whatever the voiceprint says.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

import numpy as np

from sensing import FRAME, RATE
from voiceprint import EC, Check, Voiceprint
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
# A person talking over a clip in double-talk: the canceller chopped a loud voice (mic -18 dB against -15 dB of
# playback) into scraps of 0.1-0.3 s, each attempt ended "then quiet", four in 8 s, and the clip played on to its
# end (2026-11-22 20:29). Scraps close together are one attempt, and their starts are evidence.
REJOIN = 4.0  # seconds after an attempt's last voice in which a new start on the same clip carries it on
PERSIST_SPAN = 8.0  # seconds in which starts are counted
PERSIST_LOOK = 2  # starts in PERSIST_SPAN after which the words are looked at, however little voice
PERSIST_STOP = 4  # starts in PERSIST_SPAN, PERSIST_LOUD of them loud, no echo verdict: stop with too few words too
PERSIST_LOUD = 2  # one loud echo burst among quiet scraps must not do it; the 20:29 user was loud twice in 3 s
# dB against the playback: our echo measured -33 median, -20 at worst healthy, -8 once wary. Only for PERSIST_STOP: as a
# reason to look early it stopped clips on 0.3 s of our own echo with the speakers at 100% ("train lift", replayed)
LOUD_MARGIN = -6.0
# Words the clip doesn't say before a thin voice (scraps, each under SUSTAIN) stops it: Whisper invents up to three
# from leaked echo ("Thanks for watching", "You're right", "Hands free"); the 20:29 user said more
THIN_NEW_WORDS = 4
STRICT_SUSTAIN = 1.0  # the laptop busy: its canceller leaks more, so more voice, and never length alone
STRICT_RECHECK = 1.0  # busy: more voice between looks (Whisper on the CPU takes ~2.5 s inside the frame loop)
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
    part: float = STEP  # of that, since it last started again: scraps far apart never add up to a sure voice
    next_check: float = 0.0  # seconds of voice before the next look at its words (after SUSTAIN)
    echoes: int = 0  # echo verdicts so far: after one, only the newest audio is looked at
    user_from: int = 0  # the frame the user's own words likely start at (after the last echo verdict)
    new_words_seen: int = 0  # looks in a row whose words included NEW_WORDS the clip doesn't say
    # when a full voice check (loud, voiced, Silero, near the playback's loudness) began a part of it
    starts: list[float] = field(default_factory=list)
    loud_at: list[float] = field(default_factory=list)  # full checks within LOUD_MARGIN of the playback: not echo
    looked: int = 0  # starts at the last look: a new one asks for another

    def speaking_for(self) -> float:
        return self.last - self.started

    def recent_starts(self, now: float) -> int:
        return sum(now - at <= PERSIST_SPAN for at in self.starts)

    def loud(self, now: float) -> int:
        """How often a voice was nearly as loud as our playback in the last PERSIST_SPAN: one loud scrap long ago
        says nothing about the scraps since."""
        return sum(now - at <= PERSIST_SPAN for at in self.loud_at)



class Verdict(str, Enum):
    WAITING = "waiting"  # not enough voice yet, or none at all
    ECHO = "echo"  # our own words: the clip plays on
    USER = "user"  # stop the clip: the user is talking


@dataclass
class TalkOver:
    """The one attempt at talking over a clip in progress, and when our voice last started."""

    quick_text: Callable[[np.ndarray], str]  # a fast transcript of a little audio
    over: Overlap | None = None
    parked: Overlap | None = None  # the last attempt, gone quiet: a voice within REJOIN carries it on
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
    strict: bool = False  # the laptop busy: more voice before a look, and the words always decide
    log_to: Path = HANDSFREE_LOG
    voiceprint: Voiceprint | None = None  # whose voice it is, when the words leave it open; None: words alone
    print_path: str = EC  # which of the user's voiceprints the mic is scored against (raw on a phone call)

    def track_onset(self, played_db: float | None) -> None:
        now = self.clock()
        if played_db is None or played_db < AUDIBLE_DB:
            self.silent_since = self.silent_since or now
        elif self.silent_since:
            if now - self.silent_since >= ONSET_QUIET:
                self.onset = now
            self.silent_since = 0.0

    def reset(self) -> None:
        self.over = self.parked = None

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
             clip: str = "", strong: bool = False, margin: float | None = None) -> Verdict:
        """One frame while a clip plays (or just after); voice: does it sound like someone talking; strong: the full
        voice check passed on this frame, with margin (mic minus playback, dB) when the playback level is known;
        clip_text: what we are saying, to tell our echo by; clip: which clip it is."""
        now = self.clock()
        if self.over is not None and clip and self.over.clip and clip != self.over.clip:
            log(self.log_to, f"talking over: {self.over.voiced:.1f}s of voice, then the next clip began: "
                               f"judged again from here (first: {self.over.first})")
            self.over = None  # the attempt had survived echo verdicts on the last clip: not judged against its words
        parked = self.parked
        if parked is not None and (now - parked.last > REJOIN or (clip and parked.clip and clip != parked.clip)):
            self.parked = parked = None
        loud = strong and margin is not None and margin >= LOUD_MARGIN
        if self.over is None:
            if not voice:
                return Verdict.WAITING
            if parked is None:
                self.over = Overlap(now, list(preroll), f"{how}, {now - self.onset:.1f}s after our voice started",
                                    clip_text, clip, last=now, starts=[now] if strong else [],
                                    loud_at=[now] if loud else [])
                return Verdict.WAITING
            # The same voice again after a scrap of quiet: one attempt, and a strong start is looked at now
            self.over, self.parked = parked, None
            parked.frames.extend(preroll)
            parked.last = now
            if loud:
                parked.loud_at.append(now)
            parked.voiced += STEP
            parked.part = STEP
            if not strong:
                return Verdict.WAITING
            parked.starts.append(now)
        else:
            self.over.frames.append(frame)
            if voice:
                self.over.last = now
                self.over.voiced += STEP
                self.over.part += STEP
                if loud:
                    self.over.loud_at.append(now)
        over = self.over
        if now - over.last > self.gap:
            log(self.log_to, f"talking over: {over.voiced:.1f}s of voice in {over.speaking_for():.1f}s"
                               f"{self.starts_said(over, now)}, then quiet: kept playing (first: {over.first})")
            self.over, self.parked = None, over
            return Verdict.WAITING
        if not self.due(over, now):
            return Verdict.WAITING
        over.looked = len(over.starts)
        text = self.heard_text(over)
        echo = self.echo_of_text(text, over.clip_text)
        why = ""
        fresh = new_words(text, over.clip_text) >= self.new_words_needed
        over.new_words_seen = over.new_words_seen + 1 if fresh else 0
        if echo and over.part >= NEW_WORDS_AFTER and over.new_words_seen >= 2:
            echo, why = False, ", words the clip doesn't say, twice"
        # Too few words to tell yet: listen a little longer. Past UNSURE_LIMIT of voice since it last started again,
        # or after PERSIST_STOP starts one of which was loud, a first look stops the clip anyway; an attempt that
        # already had an echo verdict never stops for too few words (that would be length alone), nor does one on a
        # busy laptop. Scraps far apart, joined, never add up to UNSURE_LIMIT
        loud = over.loud(now)
        persistent = over.recent_starts(now) >= PERSIST_STOP and loud >= PERSIST_LOUD
        thin = over.part < self.sustain  # looked at for starting again, not for its own voice
        if thin:
            fresh = new_words(text, over.clip_text) >= max(THIN_NEW_WORDS, self.new_words_needed)
        waiting = echo is None and (self.strict or over.echoes or not (over.part >= UNSURE_LIMIT or persistent))
        # Not our words isn't enough everywhere: a busy laptop wants words the clip doesn't say (Whisper writes
        # them from leaked echo, "Thanks for watching"), and a thin voice more of them and a recent loud one
        if echo is False and not why and (self.strict or thin) and not (fresh and (loud or not thin)):
            waiting = True
        # Never against the clip's own words, a busy laptop (its canceller leaks whole sentences, unmeasured) or an
        # attempt already judged our echo: there the audio holds our voice and the words alone decide
        if waiting and echo is not True and not self.strict and not over.echoes and self.voiceprint:
            check = self.voice_check(over, "words unsure")
            if check and check.user:
                waiting, why = False, f", too few words to tell but the user's voice ({check.said()})"
        if waiting:
            over.next_check = over.voiced + (STRICT_RECHECK if self.strict else RECHECK)
            return Verdict.WAITING
        if echo is None and over.part < UNSURE_LIMIT and not why:
            why = ", too few words to tell but a voice as loud as ours, again and again"
        log(self.log_to, f"talking over: {over.voiced:.1f}s of voice in {over.speaking_for():.1f}s"
                           f"{self.starts_said(over, now)}: "
                           + ("the clip's own words, our echo: kept playing" if echo else f"stopping the clip{why}")
                           + f" (first: {over.first}{', on ' + self.out if self.out else ''})")
        if echo:  # the attempt goes on: the user may still be talking under our echo
            if self.voiceprint:  # only logged, to tune by: what our echo scores (the clip plays on, so no delay)
                self.voice_check(over, "the clip's own words")
            over.echoes += 1
            over.next_check = over.voiced + (max(ECHO_RECHECK, STRICT_RECHECK) if self.strict else ECHO_RECHECK)
            over.user_from = max(0, len(over.frames) - round(LATEST * RATE / FRAME))
            return Verdict.ECHO
        self.over = None
        if over.echoes:  # the turn starts where the user's own words likely did, not with our echo before them
            over.frames = over.frames[over.user_from:]
        self.taken = over
        return Verdict.USER

    def due(self, over: Overlap, now: float) -> bool:
        """Time to look at the words: enough voice, added up; or a voice that started again within REJOIN, at each
        new start. Busy laptop: only enough voice."""
        if self.strict:
            return over.part >= max(STRICT_SUSTAIN, self.sustain) and over.voiced >= over.next_check
        if over.voiced >= max(self.sustain, over.next_check):
            return True
        return len(over.starts) != over.looked and over.recent_starts(now) >= PERSIST_LOOK

    @staticmethod
    def starts_said(over: Overlap, now: float) -> str:
        """For the log: how often the voice started again, and whether as loud as our playback."""
        n = over.recent_starts(now)
        return (f", {n} starts" if n > 1 else "") + (", one as loud as ours" if over.loud(now) else "")

    @staticmethod
    def looked_at(over: Overlap) -> np.ndarray:
        """The audio a look judges: all of it at first, only the newest LATEST seconds after an echo verdict."""
        return np.concatenate(over.frames[-round(LATEST * RATE / FRAME):] if over.echoes else over.frames)

    def heard_text(self, over: Overlap) -> str:
        """What was said over the clip."""
        if not over.clip_text:
            return ""
        return self.quick_text(self.looked_at(over))

    def voice_check(self, over: Overlap, why: str) -> Check | None:
        """The voiceprint of what the words were judged on, logged in numbers; None: no opinion (fail open)."""
        try:
            check = self.voiceprint.check(self.looked_at(over), self.print_path)
        except Exception:  # check() doesn't raise; a stand-in might
            return None
        if check:
            log(self.log_to, f"{check.said()}, talking over ({why}, {self.voiceprint.took * 1000:.0f} ms)")
        return check

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
