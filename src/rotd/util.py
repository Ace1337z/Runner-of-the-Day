"""Time, calendar, math and logging helpers.

All times are stored UTC internally and rendered in America/New_York (ET) and
Asia/Kuala_Lumpur (KL). Timezone handling is delegated to ``zoneinfo`` — never
manual offset math — so US daylight-saving transitions are handled correctly and
the changing ET<->KL offset is derived, not assumed.

The exchange calendar comes from ``exchange_calendars`` (verified available,
see docs/VERIFIED.md LIB-3/LIB-4). This module is intentionally dependency-light
so it can be unit-tested without network or a live clock.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Iterable, Optional

try:  # pragma: no cover - exercised implicitly everywhere
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

# --- timezones -------------------------------------------------------------

UTC = timezone.utc
ET = ZoneInfo("America/New_York") if ZoneInfo else UTC
KL = ZoneInfo("Asia/Kuala_Lumpur") if ZoneInfo else UTC

# Seconds in a trading day for the mode windows (§3). Boundaries are ET wall
# clock and fixed by the design; only the *calendar source* was VERIFY'd.
MODE_WINDOWS: tuple[tuple[str, dtime, dtime], ...] = (
    ("POST", dtime(16, 0), dtime(20, 0)),
    ("OVERNIGHT", dtime(20, 0), dtime(4, 0)),   # wraps midnight
    ("PRE", dtime(4, 0), dtime(9, 30)),
    ("OPEN", dtime(9, 30), dtime(10, 30)),
    ("MIDDAY", dtime(10, 30), dtime(16, 0)),
)
ALL_MODES = ("CLOSED", "POST", "OVERNIGHT", "PRE", "OPEN", "MIDDAY")

# Cache the XNYS calendar; construction is not free.
_CAL_CACHE: dict[str, Any] = {}


def _calendar(name: str = "XNYS"):
    cal = _CAL_CACHE.get(name)
    if cal is None:
        import exchange_calendars as xc
        cal = xc.get_calendar(name)
        _CAL_CACHE[name] = cal
    return cal


# --- clock -----------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_et(dt: Optional[datetime] = None) -> datetime:
    return (dt or now_utc()).astimezone(ET)


def to_kl(dt: Optional[datetime] = None) -> datetime:
    return (dt or now_utc()).astimezone(KL)


def as_utc(dt: datetime) -> datetime:
    """Attach UTC to a naive datetime, or convert an aware one."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def now_et() -> datetime:
    return to_et(now_utc())


def epoch(seconds: float = 0.0) -> float:
    """Current unix time plus an offset (for tests and deadlines)."""
    return now_utc().timestamp() + seconds


def from_epoch(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


# --- calendar --------------------------------------------------------------


def is_trading_day(d: Optional[date] = None) -> bool:
    """True when the NYSE is open on ``d`` (holidays and weekends excluded)."""
    d = d or now_et().date()
    return bool(_calendar().is_session(d.isoformat()))


def sessions_between(start: date, end: date) -> list[date]:
    cal = _calendar()
    sess = cal.sessions_in_range(start.isoformat(), end.isoformat())
    return [s.date() for s in sess]


def last_session(on_or_before: Optional[date] = None) -> date:
    """The most recent trading day on or before ``d`` (today if it is one)."""
    cal = _calendar()
    d = on_or_before or now_et().date()
    return cal.date_to_session(d.isoformat(), direction="previous").date()


def next_session(after: Optional[date] = None) -> date:
    cal = _calendar()
    d = after or now_et().date()
    return cal.date_to_session(d.isoformat(), direction="next").date()


def is_half_day(d: Optional[date] = None) -> bool:
    """True when the session is a half day (early close)."""
    d = d or now_et().date()
    cal = _calendar()
    if not cal.is_session(d.isoformat()):
        return False
    sess = cal.date_to_session(d.isoformat(), direction="next")
    close = cal.session_close(sess)
    # A regular close is 16:00 ET = 21:00/20:00 UTC; a half day closes at 13:00 ET.
    return close.astimezone(ET).time() < dtime(15, 30)


def session_open_utc(d: date) -> datetime:
    sess = _calendar().date_to_session(d.isoformat(), direction="next")
    return _calendar().session_open(sess).to_pydatetime().astimezone(UTC)


def session_close_utc(d: date) -> datetime:
    sess = _calendar().date_to_session(d.isoformat(), direction="next")
    return _calendar().session_close(sess).to_pydatetime().astimezone(UTC)


# --- mode engine -----------------------------------------------------------


def mode_for(dt: Optional[datetime] = None) -> str:
    """Which trading mode a moment belongs to.

    ``CLOSED`` on weekends/holidays. On a trading day the window is decided by
    the ET wall clock. OVERNIGHT wraps midnight and belongs to the *next*
    trading day's session per A1 (the day runs POST -> next MIDDAY).
    """
    dt = dt or now_utc()
    et = to_et(dt)
    d = et.date()
    if not is_trading_day(d):
        # A weekend/holiday night (Fri 20:00 -> Mon 04:00) is CLOSED.
        return "CLOSED"
    t = et.time()
    for name, start, end in MODE_WINDOWS:
        if start < end:
            if start <= t < end:
                return name
        else:  # wraps midnight
            if t >= start or t < end:
                return name
    return "CLOSED"


def trading_day_for(dt: Optional[datetime] = None) -> date:
    """The ROTD trading day a moment belongs to.

    A1: the day runs from the start of POST (16:00 ET) to the end of MIDDAY
    (16:00 ET) of the next session, so a post-market runner and its pre-market
    continuation are one name. Before 16:00 ET we are on *today's* day; at/after
    16:00 ET we are on the *next* session's day label.
    """
    dt = dt or now_utc()
    et = to_et(dt)
    d = et.date()
    if is_trading_day(d) and et.time() < dtime(16, 0):
        return d
    # Roll to the next session whose POST we are in (or about to be).
    return next_session(d)


# --- formatting ------------------------------------------------------------


def fmt_ts(dt: Optional[datetime] = None) -> str:
    """A log-friendly ``YYYY-MM-DD HH:MM:SS ET (UTC ...)`` string."""
    dt = dt or now_utc()
    return f"{to_et(dt):%Y-%m-%d %H:%M:%S} ET ({as_utc(dt):%H:%M:%S} UTC)"


def age_str(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    s = max(0.0, float(seconds))
    if s < 60:
        return f"{s:.0f}s ago"
    if s < 3600:
        return f"{s / 60:.0f}m ago"
    if s < 86400:
        return f"{s / 3600:.1f}h ago"
    return f"{s / 86400:.1f}d ago"


def price_str(x: Optional[float], digits: int = 4) -> str:
    """Render a price with as many decimals as the quote plausibly has.

    §18.1: show up to four decimals (sub-dollar names quote to $0.0001).
    A name >= $1 is shown to 2 decimals; below $1, to 4.
    """
    if x is None:
        return "n/a"
    v = float(x)
    places = 2 if abs(v) >= 1.0 else 4
    return f"{v:.{places}f}"[: 12]


def money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    av = abs(x)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if av >= div:
            return f"${x / div:.2f}{unit}"
    return f"${x:,.0f}"


def compact(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    av = abs(x)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if av >= div:
            return f"{x / div:.2f}{unit}"
    return f"{x:,.0f}"


def pct(x: Optional[float], digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:+.{digits}f}%"


def pct_abs(x: Optional[float], digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}%"


def num(x: Optional[float], digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


# --- math ------------------------------------------------------------------


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def safe_div(a: Optional[float], b: Optional[float], default: float = 0.0) -> float:
    if a is None or b in (None, 0):
        return default
    return a / b


def median(values: Iterable[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def mean(values: Iterable[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def percentile(values: Iterable[float], p: float) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = clamp(p, 0.0, 1.0) * (len(vals) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return vals[int(k)]
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def stdev(values: Iterable[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


# --- secrets / hashing -----------------------------------------------------

_SECRET_RE = re.compile(r"\b[A-Za-z0-9_\-]{20,}\b")


def scrub(text: Any, extra: Optional[Iterable[str]] = None) -> str:
    s = "" if text is None else str(text)
    for secret in list(extra or []):
        if secret:
            s = s.replace(secret, "***")
    # Also mask anything that looks like a long token.
    return _SECRET_RE.sub(lambda m: m.group(0)[:4] + "***", s)


def mask(value: Optional[str]) -> str:
    if not value:
        return "(unset)"
    v = str(value)
    if len(v) <= 6:
        return "***"
    return v[:4] + "***" + v[-2:]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json(obj: Any) -> str:
    """Deterministic JSON (sorted keys, no whitespace drift) for replay hashes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def short_hash(obj: Any) -> str:
    return sha256_hex(stable_json(obj).encode("utf-8"))[:16]
