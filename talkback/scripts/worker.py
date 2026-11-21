"""The one talkback worker: plays queued clips one after another, sentence by
sentence, through Kokoro on the CPU, and goes quiet the moment the mic is busy.

Whoever holds LOCK is the worker. It loads the model on the first clip and
exits when nothing is queued or held; text queued while it lets go starts a
new worker or is picked up here.
"""

from __future__ import annotations

import fcntl
import os
import queue
import re
import resource
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from voice import spool
from voice.agents import AGENT_VOICES, DEFAULT_VOICE
from voice.voices import for_agent as chosen_voice
from voice.state import (AWAY, BY_HANDSFREE, CALL_SILENT, KOKORO_MODELS, LOCK, PLAYING, TALKBACK_LOG, WORKER_PID, Playing,
                         Speaker, in_call, last_speaker, log, mic_busy, read_json, speaker_sink, target_session_since,
                         target_since)

THREADS = int(os.environ.get("TALKBACK_THREADS", "4"))  # CPU threads: 4 measured best (latency vs load)
RATE = 24000
PAUSE = 0.25  # seconds between sentences
JOIN = 0.05  # seconds between the first piece of a split sentence and its rest
# A clip is said in pieces, each made while the one before plays: a whole long sentence took 2.3-2.9 s to render
# under load, and a short first sentence ("Done.") then a long one left 2-3 s of silence while the long one was made
# (the user's catch, 2026-11-21). Pieces are cut only at punctuation (the user's call, 2026-11-22: a cut with none,
# "between my | first and second", sounded wrong), never inside a [tag], and only when a sentence said whole would
# not be ready in time; with no punctuation in reach the sentence is said whole and the small wait accepted
CUTS = ",;:—–"  # a word ending in one of these may end a piece
FIRST_MIN, FIRST_MAX = 3, 12  # the first sentence splits at punctuation ending one of these words, when long
SPLIT_FROM = 11  # first sentences shorter than this (in words) are said whole
PHRASE_MIN = 3  # no piece, and no rest, shorter than this (in words)
SPEECH_CPS = 17.5  # characters of text per second of Kokoro speech (measured 2026-11-22)
RENDER_RTF = 0.6  # render seconds per audio second the plan assumes (0.3 idle, 0.9 at load 10, measured)
RENDER_RTF_BUSY = 0.9  # ... when the laptop is busy: the same limit hands-free's load guard uses (0.55 x cores)
BUSY_PER_CORE = 0.55
LOOKAHEAD = 4  # pieces made ahead of the one playing
HOLD = 1.0  # at most this long, the first piece waits for a late second one: a pause before speaking, not in it
EDGE = 0.035  # seconds kept beyond the first and last sound where pieces of one sentence join
QUIET = 0.005  # below this share of the piece's peak (-46 dB) counts as quiet: soft s, f, th and breath stay
INTROS = 16  # rendered "<Agent> here, from <where>." kept, by text and voice
PIECE = 0.1  # seconds of audio written between checks of the mic
VOICES = {**AGENT_VOICES, BY_HANDSFREE: "bm_george"}  # system notices: the user's "Jarvis" voice, not an agent's
HANDSFREE = Path(__file__).resolve().parents[2] / "handsfree/handsfree.py"
SYSTEM = {BY_HANDSFREE}  # notices that neither introduce themselves nor take the user's replies
TAGS = re.compile(r"\[(?:laugh|chuckle|sigh|gasp|groan|cough|sniff|shush|clear throat|whispering)\]\s*")
PLAYER = ["pacat", "--playback", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=1",
          "--latency-msec=100", "--client-name=talkback", "--stream-name=talkback"]


def cuts(words: list[str], lo: int = PHRASE_MIN, hi: int | None = None) -> list[int]:
    """Where a piece may end: after a word ending in punctuation, outside a [tag], leaving PHRASE_MIN words on each
    side. Returned as word counts (cut after that many words), within lo..hi."""
    found, depth = [], 0
    hi = len(words) - PHRASE_MIN if hi is None else min(hi, len(words) - PHRASE_MIN)
    for i, word in enumerate(words):
        depth += word.count("[") - word.count("]")
        if lo <= i + 1 <= hi and depth <= 0 and word.rstrip("]")[-1:] in CUTS and not word.endswith("]"):
            found.append(i + 1)
    return found


def first_piece(sentence: str) -> tuple[str, str]:
    """A long first sentence in two at its first punctuation in reach: (head, rest); rest is "" when said whole."""
    words = sentence.split()
    if len(words) < SPLIT_FROM:
        return sentence, ""
    found = cuts(words, FIRST_MIN, FIRST_MAX)
    if not found:
        return sentence, ""
    return " ".join(words[:found[0]]), " ".join(words[found[0]:])


def speech(text: str) -> float:
    """Seconds of speech a piece of text makes, about."""
    return len(text) / SPEECH_CPS


class Plan:
    """The pieces of a clip, which go straight on the piece before (same sentence), and each piece's sentence."""

    def __init__(self, rtf: float = RENDER_RTF) -> None:
        self.rtf = rtf  # render seconds per audio second assumed
        self.pieces: list[str] = []
        self.joined: set[int] = set()
        self.sentence: list[int] = []  # each piece's sentence in the clip (-1: the intro)
        self.late: set[int] = set()  # pieces with no punctuation to cut at, expected after the audio ahead runs out
        self.made = 0.0  # when the pieces so far are made, from the start (seconds, estimated)
        self.ends = 0.0  # when the audio of the pieces so far has been said

    def add(self, text: str, sentence: int, joined: bool) -> None:
        if joined:
            self.joined.add(len(self.pieces))
        gap = 0.0 if not self.pieces else (JOIN if joined else PAUSE)
        self.made += self.rtf * speech(text)
        self.ends = max(self.ends + gap, self.made) + speech(text)
        self.pieces.append(text)
        self.sentence.append(sentence)

    def left(self, spoken: int, sentences: list[str]) -> list[str]:
        """What to say again after a cut during piece `spoken`: from the start of its sentence, as the clip's own
        sentences (pieces saved as sentences would get full pauses between them); a cut intro is not kept."""
        at = self.sentence[spoken] if spoken < len(self.pieces) else len(sentences)
        return sentences[max(at, 0):]

    def ready_in_time(self, text: str, joined: bool) -> bool:
        gap = JOIN if joined else PAUSE
        return not self.pieces or self.made + self.rtf * speech(text) <= self.ends + gap


def render_rtf() -> float:
    """The render speed to plan with: slower when the laptop is busy (1-minute load over 0.55 x cores)."""
    try:
        busy = os.getloadavg()[0] > BUSY_PER_CORE * (os.cpu_count() or 1)
    except OSError:
        busy = False
    return RENDER_RTF_BUSY if busy else RENDER_RTF


def pieces(sentences: list[str], intro: str = "", rtf: float | None = None) -> Plan:
    """The clip in the pieces it is made and said in.

    The first sentence splits at its first punctuation when long (speech starts sooner); a later sentence is said
    whole when it would be made before the audio queued ahead of it runs out, else cut at the punctuation that lets
    the longest piece still be ready in time (the earliest one if none does). Joined with spaces the pieces are the
    clip's words, unchanged: hands-free matches its echo on them."""
    plan = Plan(render_rtf() if rtf is None else rtf)
    if intro:
        plan.add(intro, -1, False)
    for n, sentence in enumerate(sentences):
        if n == 0 and not intro:
            head, rest = first_piece(sentence)
            plan.add(head, 0, False)
            words, first = rest.split(), False
        else:
            words, first = sentence.split(), True
        while words:
            text = " ".join(words)
            if plan.ready_in_time(text, not first):
                plan.add(text, n, not first)
                break
            found = cuts(words)
            if not found:  # no punctuation in reach: said whole, the wait accepted
                plan.late.add(len(plan.pieces))
                plan.add(text, n, not first)
                break
            fits = [c for c in found if plan.ready_in_time(" ".join(words[:c]), not first)]
            cut = fits[-1] if fits else found[0]
            plan.add(" ".join(words[:cut]), n, not first)
            words, first = words[cut:], False
    return plan


def trim(samples, lead: bool, tail: bool):
    """Kokoro's own quiet lead-in and tail cut (to EDGE) where pieces of one sentence join, so JOIN is the gap heard:
    about 40-80 ms in front and 60-210 ms behind each piece (measured 2026-11-22). Cut gently: a low threshold and
    EDGE kept beyond the sound, so a soft consonant before a comma or a breathy start isn't shaved off."""
    import numpy as np

    if not len(samples) or not (lead or tail):
        return samples
    level = np.abs(samples)
    loud = np.nonzero(level > max(float(level.max()) * QUIET, 1e-5))[0]
    if not len(loud):
        return samples
    keep = int(RATE * EDGE)
    start = max(0, loud[0] - keep) if lead else 0
    end = min(len(samples), loud[-1] + 1 + keep) if tail else len(samples)
    return samples[start:end]


def fast_cores(root: Path = Path("/sys/devices/system/cpu")) -> set[int] | None:
    """The fastest cores (within 10% of the top clock); None when all are alike or the clocks can't be read.

    ONNX waits for its slowest thread: a render with one thread on an efficiency core was 2.2x slower (2026-11-21)."""
    clocks = {}
    for cpu in root.glob("cpu[0-9]*"):
        try:
            clocks[int(cpu.name[3:])] = int((cpu / "cpufreq/cpuinfo_max_freq").read_text())
        except (OSError, ValueError):
            return None
    if not clocks:
        return None
    fast = {cpu for cpu, clock in clocks.items() if clock >= 0.9 * max(clocks.values())}
    return fast if len(fast) >= THREADS and len(fast) < len(clocks) else None


class Voice:
    """Kokoro, loaded on first use; SIGUSR1 skips the clip being played."""

    def __init__(self) -> None:
        self._kokoro = None
        self.skip = False
        self.intros: dict[tuple[str, str], object] = {}  # an agent's intro, said again and again: rendered once
        self.intros_lock = threading.Lock()  # two clips' render threads can overlap
        signal.signal(signal.SIGUSR1, self.on_skip)

    def on_skip(self, *_) -> None:
        self.skip = True

    @property
    def kokoro(self):
        if self._kokoro is None:
            import onnxruntime as rt
            from kokoro_onnx import Kokoro

            opts = rt.SessionOptions()
            opts.intra_op_num_threads = THREADS
            opts.inter_op_num_threads = 1
            # Threads sleep between runs instead of spinning: about 20% less CPU, first audio no slower (measured
            # 2026-11-21, 4 threads, on the fast cores and off them)
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            session = rt.InferenceSession(str(KOKORO_MODELS / "kokoro-v1.0.onnx"), opts,
                                          providers=["CPUExecutionProvider"])
            self._kokoro = Kokoro.from_session(session, str(KOKORO_MODELS / "voices-v1.0.bin"))
        return self._kokoro

    def render(self, sentence: str, voice: str, keep: bool = False):
        """Audio for one sentence; empty for a bare tag Kokoro cannot say. keep: an intro, rendered once."""
        import numpy as np

        plain = TAGS.sub("", sentence).strip()  # Kokoro would read [laugh] aloud
        if not plain:
            return np.zeros(0, dtype="<f4")
        if keep:
            with self.intros_lock:
                kept = self.intros.get((plain, voice))
            if kept is not None:
                return kept
        samples = self.kokoro.create(plain, voice=voice, lang="en-us")[0]
        if keep:
            with self.intros_lock:
                if len(self.intros) >= INTROS and (plain, voice) not in self.intros:
                    self.intros.pop(next(iter(self.intros)))
                self.intros[(plain, voice)] = samples
        return samples

    def play(self, path: Path) -> None:
        """Speak one clip; put back what is left if the user cuts in."""
        import numpy as np

        clip = spool.Clip.load(path)
        if clip is None:
            path.unlink(missing_ok=True)
            return
        calling = in_call()
        if calling and CALL_SILENT.exists():  # the line dropped: kept until the phone is back, or the call ends
            time.sleep(0.5)
            return
        if AWAY.exists() and not calling:  # queued before Away began: not said into an empty room
            path.unlink(missing_ok=True)
            log(TALKBACK_LOG, f"{clip.agent} clip dropped: the user is away (agents are told to text)")
            return
        if clip.for_call and not calling:  # the call is over: not said aloud at the laptop, sent to the phone
            if clip.agent in SYSTEM:
                path.unlink(missing_ok=True)
                log(TALKBACK_LOG, f"{clip.agent} notice for the call dropped: the call ended")
                return
            try:  # in the background, by its own path (a worker started by a service has no ~/.local/bin)
                subprocess.Popen([sys.executable, str(HANDSFREE), "text", "--as", clip.session, " ".join(clip.sentences)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, env={**os.environ, "TALKBACK_AGENT": clip.agent})
            except OSError as exc:  # kept in the spool: said or texted on a later try, never lost
                log(TALKBACK_LOG, f"{clip.agent} clip for the ended call couldn't be texted ({exc}): kept")
                time.sleep(2)
                return
            path.unlink(missing_ok=True)
            log(TALKBACK_LOG, f"{clip.agent} clip for the call, which ended: texted to the phone instead")
            return
        began = time.time()
        intro = ""
        if clip.agent not in SYSTEM:
            last = last_speaker()
            if not last or last.session != clip.session:
                intro = f"{clip.agent.capitalize()} here, from {clip.where}."
            Speaker(clip.session, clip.agent, clip.where, clip.tab).remember()
        plan = pieces(list(clip.sentences), intro)
        sentences, joined = plan.pieces, plan.joined  # joined: said straight after the piece before, one sentence
        # Once, with the words, before the clip file goes: no moment when neither says a clip is under way.
        # Hands-free checks what it hears over a clip against the words (its own echo)
        marker = Playing(path.name, clip.session, clip.agent, " ".join(sentences))
        marker.save()
        path.unlink(missing_ok=True)
        voice = clip.voice or (VOICES[clip.agent] if clip.agent in SYSTEM else chosen_voice(clip.agent))
        ready: queue.Queue = queue.Queue(maxsize=LOOKAHEAD)
        done = threading.Event()

        def make() -> None:
            try:
                for n, sentence in enumerate(sentences):
                    samples = trim(self.render(sentence, voice, keep=plan.sentence[n] < 0),
                                   lead=n in joined, tail=n + 1 in joined)
                    while not done.is_set():
                        try:
                            ready.put(samples, timeout=0.2)
                            break
                        except queue.Full:
                            continue
            finally:
                ready.put(None)

        threading.Thread(target=make, daemon=True).start()
        # On a phone call, into the phone's sink
        player = subprocess.Popen([*PLAYER, f"--device={speaker_sink()}"], stdin=subprocess.PIPE)
        piece = int(RATE * PIECE)
        spoken, cut, self.skip = 0, False, False
        try:
            while (samples := ready.get()) is not None:
                if spoken == 0 and 1 in plan.late:
                    # A short first piece and a second that can't be cut: wait for it a little before speaking
                    # rather than stop in the middle of the reply
                    until = time.time() + HOLD
                    while ready.empty() and time.time() < until and not (mic_busy() or self.skip):
                        time.sleep(0.02)
                if spoken == 0:
                    # Waiting (behind another clip, or put back after a cut) apart from making the first piece:
                    # from the queue alone, a clip behind another looked like a 20 s render (2026-11-21)
                    now = time.time()
                    log(TALKBACK_LOG, f"{clip.agent} first_audio={now - began:.2f}s "
                                      f"(waited {max(0.0, began - clip.queued):.2f}s before)")
                    # The clip is audible from now: until then the call's report of silence on the phone was the
                    # first sentence still being made, not sound lost (2026-11-21, 4-20 s first audio under load)
                    marker.sounding = time.time()
                    marker.save()
                else:
                    gap = JOIN if spoken in joined else PAUSE
                    samples = np.concatenate([np.zeros(int(RATE * gap), "<f4"), samples])
                data = samples.astype("<f4")
                for n, start in enumerate(range(0, len(data), piece)):
                    # The call started or ended mid-clip: the rest is said where the user is now (or texted), not
                    # carried on at the laptop after a hang-up (the user's catch, 2026-11-17)
                    moved = n % 5 == 0 and (in_call() != calling or calling and CALL_SILENT.exists())
                    # Away switched on mid-sentence (the user's catch, 2026-11-17: a reply went on aloud): stopped
                    gone_away = n % 5 == 0 and not calling and AWAY.exists()
                    if gone_away:
                        cut, self.skip = True, True  # dropped, not kept: the agent is told to text from now on
                        log(TALKBACK_LOG, f"{clip.agent} cut: the user went away")
                        break
                    if mic_busy() or self.skip or moved:
                        cut = True
                        if moved:
                            clip.for_call = True  # resumed on the phone if a call began; texted if it ended
                            log(TALKBACK_LOG, f"{clip.agent} cut: the call {'began' if not calling else 'ended or dropped'}")
                        break
                    player.stdin.write(data[start:start + piece].tobytes())
                    player.stdin.flush()
                if cut:
                    break
                spoken += 1
        finally:
            done.set()
            if cut:
                player.terminate()
            else:
                player.stdin.close()
            player.wait()
            PLAYING.unlink(missing_ok=True)
        if cut and clip.agent in SYSTEM:
            log(TALKBACK_LOG, f"{clip.agent} notice cut and dropped: said later it would be out of place")
        elif cut and not self.skip:
            # The intro, once heard, is not said again; resume from the start of the cut sentence, as sentences
            # (pieces saved as sentences would be said with full pauses between them)
            clip.sentences = plan.left(spoken, list(clip.sentences))
            clip.save(path)
            log(TALKBACK_LOG, f"{clip.agent} cut by the mic, {len(clip.sentences)} sentences kept")


def after_input(since: float) -> None:
    """The mic just went idle: hold waiting clips if the words went to an agent."""
    waiting = spool.queued_sessions(skip_agents=SYSTEM)  # notices are never held: they play now or not at all
    if not waiting:
        return
    deadline = time.time() + 2
    while time.time() < deadline:
        session = target_session_since(since)
        if session:
            # Hands-free knows which agent the words went to: only its clips wait for its answer; the others play
            # on (Cursor's reply was held for a minute and a half while the user talked to Claude, 2026-11-15)
            spool.hold(waiting & {session})
            return
        to_agent = target_since(since)
        if to_agent is not None:
            if to_agent:
                spool.hold(waiting)  # push-to-talk: the window in front, whichever agent's; all wait
            return
        time.sleep(0.1)
    # Nothing was pasted (too short, empty): nobody got new input, play on


def lock() -> int | None:
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def work() -> int:
    fd = lock()
    if fd is None:
        return 0  # another worker holds the lock and will see our text
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    cores = fast_cores()
    if cores:  # before Kokoro loads: its threads are made on the cores allowed now
        try:
            os.sched_setaffinity(0, cores)
        except OSError:
            pass
    WORKER_PID.write_text(str(os.getpid()))
    t0, voice, since = time.monotonic(), None, None
    while fd is not None:
        if mic_busy():
            since = since or time.time()
            time.sleep(0.1)
            continue
        if since is not None:
            after_input(since - 1)
            since = None
        spool.expire()
        ready = [path for path in spool.queued() if not spool.is_held(read_json(path).get("session", ""))]
        if ready:
            voice = voice or Voice()
            voice.play(ready[0])
            continue
        if spool.queued() or spool.any_held():
            time.sleep(0.2)
            continue
        # Let go, then look once more: text queued while we were letting go
        # either sees the lock free and starts its own worker, or is seen here
        WORKER_PID.unlink(missing_ok=True)
        os.close(fd)
        fd = lock() if spool.queued() else None
        if fd is not None:
            WORKER_PID.write_text(str(os.getpid()))
    if voice is not None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        log(TALKBACK_LOG, f"worker done wall={time.monotonic() - t0:.1f}s "
                          f"cpu={usage.ru_utime + usage.ru_stime:.1f}s threads={THREADS}")
    return 0
