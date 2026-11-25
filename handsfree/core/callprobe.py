"""Whether the phone is on a call (a phone call or an app's, or ringing), asked on every earbud tap.

Multipoint earbuds send the laptop a pause when the phone answers a call, and hands-free took it for a tap: it
recorded the private call and typed it to an agent (2026-11-25). The app is not kept running on the phone, so each
tap asks it through its push address, and it answers "on-call" or "free" on a one-time reply address, as a ring's
outcome comes back. No answer in time: the tap works as ever (a phone off the net must not break taps).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Callable

from voice.state import CALL_RING, CALL_RING_PROBES, HANDSFREE_LOG, log

ON_CALL, FREE = "on-call", "free"
ON_CALL_FOR = 180.0  # seconds after an "on-call" answer in which taps are ignored outright
ANSWER_FOR = 20.0  # seconds a probe waits for the phone's answer
POLL_EVERY = 1.0
SILENT_FOR = 600.0  # seconds an unanswered probe keeps later taps from waiting for an answer


class Probe:
    """One question to the phone: `answer` is ON_CALL, FREE, or "" (none yet, or none came); `done` once settled.
    `expected`: the phone answered the last probe, so an answer is worth waiting a moment for."""

    def __init__(self, expected: bool = True) -> None:
        self.answer = ""
        self.done = threading.Event()
        self.expected = expected


class PhoneCalls:
    def __init__(self, push: Callable[..., bool], replies: Callable[[str, int], set[str]], server: str) -> None:
        self.push, self.replies, self.server = push, replies, server
        self.until = 0.0  # taps are ignored before this (time.monotonic())
        self.silent_at = 0.0  # when the last probe got no answer (time.monotonic()); 0: it was answered

    @property
    def unanswered(self) -> bool:
        """The last probe, lately, got no answer (the phone off the net): taps don't wait for one. Only lately: a
        phone silent hours ago may be back and on a call now, and a turn not waited for can let a short one out."""
        return bool(self.silent_at) and time.monotonic() - self.silent_at < SILENT_FOR

    @staticmethod
    def understood() -> str:
        """The push address, if the app said it answers probes there; "" otherwise: an older build would ring the
        phone for one. An older build installed over this one, keeping its data and address, keeps the flag until
        it sends its address again (each time it opens, and on every call), which clears it."""
        try:
            address = CALL_RING.read_text().strip()
            return address if address and CALL_RING_PROBES.read_text().strip() == address else ""
        except OSError:
            return ""

    def on_call(self) -> bool:
        return time.monotonic() < self.until

    def ask(self) -> Probe | None:
        """Asks in the background (the turn starts recording at once); None when the app can't be asked."""
        address = self.understood()
        if not address:
            return None
        probe = Probe(expected=not self.unanswered)
        threading.Thread(target=self.wait_for, args=(probe, address), daemon=True).start()
        return probe

    def wait_for(self, probe: Probe, address: str) -> None:
        # Not a ring: no RANG, no ringer, no rate limit. The reply name is this probe's own, used once
        reply = f"henchline-{secrets.token_urlsafe(16)}"
        since = int(time.time()) - 1  # before the push: the answer may come before the push call returns
        try:
            # To the address checked above, never one read again later (it may have changed to an older app's)
            if self.push(json.dumps({"probe": "call", "reply": f"{self.server}/{reply}"}).encode(), to=address):
                deadline = time.monotonic() + ANSWER_FOR
                while time.monotonic() < deadline:
                    time.sleep(POLL_EVERY)
                    heard = self.replies(reply, since) & {ON_CALL, FREE}
                    if heard:
                        probe.answer = ON_CALL if ON_CALL in heard else FREE
                        # Each "on-call" keeps taps off a while longer; "free" lets them through again at once
                        self.until = time.monotonic() + ON_CALL_FOR if probe.answer == ON_CALL else 0.0
                        self.silent_at = 0.0
                        log(HANDSFREE_LOG, f"call probe: the phone is {probe.answer}")
                        return
            self.silent_at = time.monotonic()
            log(HANDSFREE_LOG, "call probe: no answer from the phone")
        finally:
            probe.done.set()
