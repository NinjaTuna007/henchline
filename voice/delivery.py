"""Whether words reached an agent, and if not, why: what deliver() and the Cursor bridge answer.

Returned by the call itself, never left in a module or thread global: the call server delivers on several threads at
once. Truthy exactly when delivered, so ``if not deliver(...)`` reads as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Refusal(str, Enum):
    """Why words weren't delivered. The bridge's own answers keep the words its window sends ("not working")."""

    # The Cursor bridge in a window, or finding that window
    NOT_WORKING = "not working"  # a stop asked of a chat that had just finished
    NOT_A_CHAT_ID = "not a chat id"  # an older bridge, before a reload
    OTHER_CHAT_SELECTED = "another chat is selected"
    NO_WINDOW = "no Cursor window has that project"
    TWO_WINDOWS = "more than one Cursor window has that project"
    UNKNOWN_CHAT = "no Cursor project holds that chat"
    NO_ANSWER = "no answer"  # its socket couldn't be reached or answered nothing readable
    BRIDGE_SAID = "the bridge said no"  # another answer from the bridge: its words are in Delivery.why
    # Typing to an agent (routing.deliver)
    NOBODY = "nobody to send to"
    NOT_PLACED = "not placed"  # the chat couldn't be found running or started
    CURSOR_REFUSED = "Cursor didn't take it"
    CURSOR_CHAT_NOT_OPENED = "the Cursor chat couldn't be opened"
    NO_CURSOR_WINDOW = "no Cursor window"
    SCREEN_LOCKED = "the screen is locked"
    NOT_RAISED = "Cursor's window couldn't be raised"
    NO_TAB = "no terminal tab"
    AGENT_GONE = "the agent isn't running"
    TERMINAL_GONE = "the terminal is gone"

    @classmethod
    def from_bridge(cls, said: str) -> Refusal:
        """The bridge's own answer as a reason: one it names, else BRIDGE_SAID (or NO_ANSWER when it said nothing)."""
        if not said:
            return cls.NO_ANSWER
        try:
            return cls(said)
        except ValueError:
            return cls.BRIDGE_SAID


@dataclass(frozen=True)
class Delivery:
    """ok, or why not: ``why`` is said to the user (out loud, or on the phone), ``refusal`` is for code to branch on."""

    ok: bool
    why: str = ""
    refusal: Refusal | None = None

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def done(cls) -> Delivery:
        return cls(True)

    @classmethod
    def refused(cls, refusal: Refusal, why: str = "") -> Delivery:
        return cls(False, why or refusal.value, refusal)
