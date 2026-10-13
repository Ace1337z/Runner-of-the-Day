"""Exchange calendar and the mode engine.

The authoritative session calendar comes from ``exchange_calendars`` (XNYS),
verified available (docs/VERIFIED.md LIB-3). The mode engine maps a moment to one
of POST / OVERNIGHT / PRE / OPEN / MIDDAY / CLOSED and to an ROTD *trading day*
whose boundary is 16:00 ET (design A1: the day runs from the start of POST to the
end of the next MIDDAY, so a post-market runner and its pre-market continuation
are one name).

``FinnhubClient.market_status`` is available as an independent cross-check
(VERIFIED G-7); a disagreement between the two lowers the data-quality grade
rather than being silently trusted (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Optional

from .util import (ET, MODE_WINDOWS, is_half_day, is_trading_day, mode_for,
                   next_session, now_utc, session_close_utc, session_open_utc,
                   to_et, trading_day_for)

# Re-export the primitives so callers have one import site.
__all__ = [
    "SessionInfo", "session_info", "mode_for", "trading_day_for",
    "is_trading_day", "is_half_day", "next_session", "session_open_utc",
    "session_close_utc", "phase_age_minutes", "MODE_ORDER",
]

MODE_ORDER = ("CLOSED", "POST", "OVERNIGHT", "PRE", "OPEN", "MIDDAY")


@dataclass(frozen=True)
class SessionInfo:
    """Everything a cycle needs to know about *when* it is running."""
    mode: str
    trading_day: date          # the ROTD day label (A1 boundary = 16:00 ET)
    session_date: date         # the calendar session the labels are anchored to
    et: datetime
    utc: datetime
    is_session: bool
    is_half: bool
    open_utc: Optional[datetime]
    close_utc: Optional[datetime]

    @property
    def is_regular(self) -> bool:
        return self.mode in ("OPEN", "MIDDAY")

    @property
    def is_extended(self) -> bool:
        return self.mode in ("POST", "PRE", "OVERNIGHT")

    @property
    def allows_ignition(self) -> bool:
        """OVERNIGHT never alerts on quotes (design A2)."""
        return self.mode != "OVERNIGHT" and self.mode != "CLOSED"


def session_info(dt: Optional[datetime] = None) -> SessionInfo:
    utc = dt or now_utc()
    et = to_et(utc)
    d = et.date()
    is_session = is_trading_day(d)
    half = is_half_day(d) if is_session else False
    o = c = None
    if is_session:
        try:
            o = session_open_utc(d)
            c = session_close_utc(d)
        except Exception:  # noqa: BLE001 - calendar gaps must not crash a cycle
            o = c = None
    return SessionInfo(
        mode=mode_for(utc),
        trading_day=trading_day_for(utc),
        session_date=d,
        et=et,
        utc=utc,
        is_session=is_session,
        is_half=half,
        open_utc=o,
        close_utc=c,
    )


def phase_age_minutes(dt: Optional[datetime] = None) -> Optional[float]:
    """Minutes since the current mode window opened (None if unknown/CLOSED)."""
    utc = dt or now_utc()
    et = to_et(utc)
    t = et.time()
    for name, start, end in MODE_WINDOWS:
        if start < end and start <= t < end:
            return (t.hour * 60 + t.minute) - (start.hour * 60 + start.minute)
        if start > end and (t >= start or t < end):
            base = start.hour * 60 + start.minute
            now_m = t.hour * 60 + t.minute
            return (now_m - base) % (24 * 60)
    return None
