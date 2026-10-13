"""Halt feed — a stub that answers UNKNOWN.

VERIFY D-5: no free or already-paid trade-halt feed was confirmed
(docs/VERIFIED.md). Per the fail-closed rule the stub returns ``UNKNOWN``, and
``G-HALT`` / ``HALTED_SUSPENDED`` are implemented against what the primary feed
exposes — a halt shows up as a stale or absent quote, which the staleness rules
(DATA-03) already suppress.

Do not invent a halt feed. Ask the user before adding one.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class HaltState(str, Enum):
    UNKNOWN = "UNKNOWN"
    OPEN = "OPEN"
    HALTED = "HALTED"
    PAUSED = "PAUSED"


class HaltFeed:
    """Always ``UNKNOWN`` until a feed is confirmed."""

    name = "halt_feed"
    available = False

    def state(self, symbol: str) -> HaltState:  # noqa: ARG002
        return HaltState.UNKNOWN

    def health(self) -> tuple[bool, str]:
        return False, "no halt feed confirmed (VERIFY D-5); state = UNKNOWN"
