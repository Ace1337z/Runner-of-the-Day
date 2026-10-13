"""The 10-trading-day shadow clock (build prompt §17, Phase 6).

ROTD must decide and log in shadow mode for a full run of trading days before a
single live push is allowed. The clock is *calendar-bound*: it counts actual
NYSE sessions (weekends and holidays do not advance it), so ten calendar days
are not ten trading days.

State lives in the ``meta`` table of the store, so it survives restarts and is
inspectable:

- ``shadow_start``     — the first trading day the clock ran (ISO date).
- ``shadow_last_seen`` — the most recent trading day observed while in shadow.
- ``shadow_cycles``    — how many cycles have been logged in shadow.
- ``shadow_alerts``    — how many alerts were computed (never pushed) in shadow.

Everything here is pure: it reads/writes ``meta`` and uses the exchange
calendar. No network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .config import Config
from .store import Store
from .util import is_trading_day, sessions_between

log = logging.getLogger(__name__)

K_START = "shadow_start"
K_LAST = "shadow_last_seen"
K_CYCLES = "shadow_cycles"
K_ALERTS = "shadow_alerts"

# Why a live push is or is not allowed.
R_CLOCK_UNSTARTED = "SHADOW_CLOCK_NOT_STARTED"
R_CLOCK_INCOMPLETE = "SHADOW_INCOMPLETE"
R_CLOCK_COMPLETE = "SHADOW_COMPLETE"
R_NOT_SHADOW = "NOT_IN_SHADOW"


@dataclass
class ShadowStatus:
    """A read-only view of the shadow clock."""

    enabled: bool
    started: Optional[date]
    last_seen: Optional[date]
    sessions_done: int
    sessions_required: int
    cycles: int
    alerts: int
    complete: bool
    live_allowed: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "started": self.started.isoformat() if self.started else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "sessions_done": self.sessions_done,
            "sessions_required": self.sessions_required,
            "days_remaining": max(0, self.sessions_required - self.sessions_done),
            "cycles": self.cycles,
            "alerts": self.alerts,
            "complete": self.complete,
            "live_allowed": self.live_allowed,
            "reason": self.reason,
        }

    def line(self) -> str:
        if not self.enabled:
            return "shadow: OFF (live pushes allowed)"
        if self.started is None:
            return (f"shadow: ON, clock not started "
                    f"({R_CLOCK_UNSTARTED})")
        return (f"shadow: ON, {self.sessions_done}/{self.sessions_required} "
                f"trading days ({self.cycles} cycles, {self.alerts} alerts) -> "
                f"{self.reason}")


def _as_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        log.warning("shadow: bad stored date %r", value)
        return None


def start(store: Store, day: date, *, force: bool = False) -> ShadowStatus:
    """Start the shadow clock on ``day`` (a trading day).

    Idempotent unless ``force``: an already-started clock is left alone so a
    restart mid-shadow does not reset progress.
    """
    if not is_trading_day(day):
        raise ValueError(f"{day} is not a trading day; the shadow clock only runs on sessions")
    if store.get_meta(K_START) and not force:
        return status(store, day)
    store.set_meta(K_START, day.isoformat())
    store.set_meta(K_LAST, day.isoformat())
    store.set_meta(K_CYCLES, "0")
    store.set_meta(K_ALERTS, "0")
    log.info("shadow clock started on %s", day)
    return status(store, day)


def note_cycle(store: Store, day: date, *, alerts: int = 0) -> None:
    """Record that a cycle ran on ``day`` and how many (unsent) alerts it produced."""
    if not store.get_meta(K_START):
        return
    if not is_trading_day(day):
        return
    prev = _as_date(store.get_meta(K_LAST))
    if prev is None or day >= prev:
        store.set_meta(K_LAST, day.isoformat())
    cycles = int(store.get_meta(K_CYCLES) or "0") + 1
    store.set_meta(K_CYCLES, str(cycles))
    if alerts:
        total = int(store.get_meta(K_ALERTS) or "0") + alerts
        store.set_meta(K_ALERTS, str(total))


def sessions_done(store: Store) -> int:
    """How many distinct trading sessions the clock has covered so far."""
    start_d = _as_date(store.get_meta(K_START))
    last_d = _as_date(store.get_meta(K_LAST))
    if start_d is None or last_d is None or last_d < start_d:
        return 0
    return len(sessions_between(start_d, last_d))


def status(store: Store, day: Optional[date] = None,
           cfg: Optional[Config] = None) -> ShadowStatus:
    """Read the clock and decide whether a live push is allowed.

    The clock is considered *enabled* when either the ``SHADOW`` setting is on or
    a clock has actually been started (its presence in ``meta`` is authoritative,
    so ``shadow-start`` alone is enough to arm the gate).
    """
    enabled_cfg = cfg.bool("SHADOW") if cfg is not None else True
    required = cfg.int("SHADOW_DAYS_REQUIRED", 10) if cfg is not None else 10
    started = _as_date(store.get_meta(K_START))
    last_seen = _as_date(store.get_meta(K_LAST))
    cycles = int(store.get_meta(K_CYCLES) or "0")
    alerts = int(store.get_meta(K_ALERTS) or "0")

    enabled = enabled_cfg or started is not None
    done = sessions_done(store)
    complete = done >= required

    if not enabled:
        return ShadowStatus(False, started, last_seen, done, required, cycles,
                            alerts, complete, True, R_NOT_SHADOW)
    if started is None:
        return ShadowStatus(True, None, last_seen, 0, required, cycles, alerts,
                            False, False, R_CLOCK_UNSTARTED)
    if complete:
        return ShadowStatus(True, started, last_seen, done, required, cycles,
                            alerts, True, True, R_CLOCK_COMPLETE)
    return ShadowStatus(True, started, last_seen, done, required, cycles, alerts,
                        False, False, R_CLOCK_INCOMPLETE)


def live_allowed(store: Store, cfg: Config) -> bool:
    """True only when not in shadow, or the shadow clock has completed."""
    return status(store, cfg=cfg).live_allowed
