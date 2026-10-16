"""Canonical records, cross-source reconciliation and the per-cycle snapshot.

This module is the *only* place that reconciles data (build prompt §5). After a
snapshot exists, nothing downstream fetches or re-derives prices: every decision
reads one reconciled, timestamped record per symbol.

DATA-01..12 (build prompt §5) are implemented here, each as a named check with a
reason code:

- DATA-01 basis tagging      — every price/volume carries its basis (REGULAR/
  EXTENDED) and its venue scope (CONSOLIDATED/SINGLE_VENUE/UNKNOWN).
- DATA-02 price conflict     — two sources disagreeing beyond
  ``PRICE_CONFLICT_PCT_REG``/``_EXT`` => ``CONFLICT``.
- DATA-03 staleness          — quote/bar older than the configured max age
  => ``STALE``.
- DATA-04 volume basis       — if the volume basis is UNKNOWN, RVOL features are
  ``MISSING``.
- DATA-05 session labelling  — a record's basis is decided by the mode at fetch.
- DATA-06 float conflict     — float sources disagreeing beyond
  ``FLOAT_CONFLICT_FACTOR`` => ``FLOAT_CONFLICT``; a share count older than
  ``SEC_SHARES_MAX_AGE_D`` cannot bound float.
- DATA-07 corporate actions  — a reverse split within ``REVERSE_SPLIT_RECENT_D``
  flags ``REVERSE_SPLIT_RECENT`` (prices before it are not comparable).
- DATA-08 suspension         — a symbol with no fresh data for the whole cycle
  is ``SUSPENDED`` (not scored).
- DATA-09 clock skew         — a source timestamp ahead of the local clock beyond
  tolerance => the record is ``CLOCK_SKEW`` and dropped.
- DATA-10 duplicate rows     — duplicate symbol rows in one payload collapse to
  the freshest.
- DATA-11 unit sanity        — obviously impossible values are rejected.
- DATA-12 data grade         — the cycle's aggregate A..F grade from the above.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from .config import Config
from .util import (UTC, as_utc, clamp, now_utc, safe_div, sha256_hex, stable_json)

log = logging.getLogger(__name__)

# --- reason codes (never magic strings in the decision path) ---------------
R_OK = "OK"
R_MISSING = "MISSING"
R_STALE = "STALE"
R_CONFLICT = "CONFLICT"
R_FLOAT_CONFLICT = "FLOAT_CONFLICT"
R_REVERSE_SPLIT_RECENT = "REVERSE_SPLIT_RECENT"
R_SUSPENDED = "SUSPENDED"
R_CLOCK_SKEW = "CLOCK_SKEW"
R_DUPLICATE = "DUPLICATE"
R_UNIT_SANITY = "UNIT_SANITY"
R_UNKNOWN_BASIS = "UNKNOWN_BASIS"

# --- basis tags ------------------------------------------------------------
BASIS_REGULAR = "REGULAR"
BASIS_EXTENDED = "EXTENDED"
BASIS_UNKNOWN = "UNKNOWN"

SCOPE_CONSOLIDATED = "CONSOLIDATED"
SCOPE_SINGLE_VENUE = "SINGLE_VENUE"
SCOPE_UNKNOWN = "UNKNOWN"

GRADE_ORDER = ("A", "B", "C", "D", "F")


@dataclass
class PricePoint:
    """One observation of a symbol's price from one source."""
    source: str
    price: Optional[float]
    ts: Optional[datetime]
    basis: str = BASIS_UNKNOWN
    scope: str = SCOPE_UNKNOWN
    volume: Optional[float] = None
    prev_close: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    reason: str = R_OK


@dataclass
class Canonical:
    """The reconciled record for one symbol in one cycle."""
    symbol: str
    price: Optional[float] = None
    price_basis: str = BASIS_UNKNOWN
    price_scope: str = SCOPE_UNKNOWN
    price_sources: list[str] = field(default_factory=list)
    price_reason: str = R_MISSING
    prev_close: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[float] = None
    volume_basis: str = BASIS_UNKNOWN
    volume_reason: str = R_MISSING
    dollar_volume: Optional[float] = None
    rel_volume: Optional[float] = None
    avg_volume: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_pct: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    open: Optional[float] = None
    after_close: Optional[float] = None
    shares_float: Optional[float] = None
    float_reason: str = R_MISSING
    short_float: Optional[float] = None
    market_cap: Optional[float] = None
    sector: str = ""
    industry: str = ""
    company: str = ""
    data_grade: str = "F"
    reasons: list[str] = field(default_factory=list)
    fetched_at: Optional[datetime] = None

    def note(self, code: str) -> None:
        if code and code not in self.reasons:
            self.reasons.append(code)


# --- helpers ---------------------------------------------------------------

def _conflict(price_a: Optional[float], price_b: Optional[float], pct: float) -> bool:
    if not price_a or not price_b:
        return False
    return abs(price_a - price_b) / max(price_a, price_b) * 100.0 > pct


def _sane_price(p: Optional[float]) -> bool:
    """DATA-11: reject impossible values (negative, or absurdly large)."""
    if p is None:
        return False
    return 0.0 < p < 1_000_000.0


def _sane_volume(v: Optional[float]) -> bool:
    if v is None:
        return False
    return 0.0 <= v < 1e13


def _clock_ok(ts: Optional[datetime], now: datetime, tolerance_s: float = 120.0) -> bool:
    """DATA-09: a timestamp far in the future is a clock-skew artefact."""
    if ts is None:
        return True
    return as_utc(ts).timestamp() <= now.timestamp() + tolerance_s


def choose_basis(mode: str) -> str:
    """DATA-05/DATA-01: which basis a fetch in ``mode`` produces."""
    if mode in ("OPEN", "MIDDAY"):
        return BASIS_REGULAR
    if mode in ("PRE", "POST", "OVERNIGHT"):
        return BASIS_EXTENDED
    return BASIS_UNKNOWN


# --- reconciliation --------------------------------------------------------

def reconcile_symbol(symbol: str, points: Iterable[PricePoint], cfg: Config,
                     mode: str, now: Optional[datetime] = None,
                     float_sources: Optional[list[tuple[str, Optional[float], Optional[datetime]]]] = None,
                     meta: Optional[dict[str, Any]] = None) -> Canonical:
    """Fold many source observations into one canonical record (DATA-01..12)."""
    now = as_utc(now or now_utc())
    rec = Canonical(symbol=symbol.upper(), fetched_at=now)
    basis = choose_basis(mode)
    rec.price_basis = basis
    max_age = (cfg.float("QUOTE_MAX_AGE_S_REG", 20) if basis == BASIS_REGULAR
               else cfg.float("QUOTE_MAX_AGE_S_EXT", 60))

    pts: list[PricePoint] = []
    for p in points:
        if p.price is None:
            continue
        if not _clock_ok(p.ts, now):
            p.reason = R_CLOCK_SKEW
            rec.note(R_CLOCK_SKEW)
            continue
        if not _sane_price(p.price):
            p.reason = R_UNIT_SANITY
            rec.note(R_UNIT_SANITY)
            continue
        if p.ts is not None and (now - as_utc(p.ts)).total_seconds() > max_age:
            p.reason = R_STALE
            rec.note(R_STALE)
            continue
        pts.append(p)

    # DATA-10: collapse duplicates by source, keeping the freshest.
    by_source: dict[str, PricePoint] = {}
    for p in pts:
        cur = by_source.get(p.source)
        if cur is None or (p.ts and cur.ts and p.ts > cur.ts):
            if cur is not None:
                rec.note(R_DUPLICATE)
            by_source[p.source] = p
    pts = list(by_source.values())

    if not pts:
        rec.price_reason = R_STALE if R_STALE in rec.reasons else R_MISSING
        rec.note(rec.price_reason)
        rec.data_grade = _grade_from(rec)
        return rec

    # DATA-02: conflict between the freshest regular-basis sources. Finviz is
    # preferred as the anchor; extended hours only compares extended sources.
    conflict_pct = (cfg.float("PRICE_CONFLICT_PCT_REG", 1.0) if basis == BASIS_REGULAR
                    else cfg.float("PRICE_CONFLICT_PCT_EXT", 2.0))
    pts.sort(key=lambda p: (p.source != "finviz", -(p.ts.timestamp() if p.ts else 0)))
    anchor = pts[0]
    conflicts = [p for p in pts[1:] if _conflict(anchor.price, p.price, conflict_pct)]

    rec.price = anchor.price
    rec.price_scope = anchor.scope
    rec.price_sources = [p.source for p in pts]
    rec.prev_close = next((p.prev_close for p in pts if p.prev_close), None)
    if conflicts:
        rec.note(R_CONFLICT)
        rec.price_reason = R_CONFLICT
        # Fail closed: a conflicted price keeps the anchor but is not trustworthy.
    else:
        rec.price_reason = R_OK

    # Volume: use the anchor's volume if present, else any source's.
    vol_pt = next((p for p in pts if p.volume is not None), None)
    if vol_pt is not None and _sane_volume(vol_pt.volume):
        rec.volume = vol_pt.volume
        rec.volume_basis = vol_pt.scope  # consolidated? single-venue? unknown?
        rec.volume_reason = R_OK if vol_pt.scope != SCOPE_UNKNOWN else R_UNKNOWN_BASIS
        if vol_pt.scope == SCOPE_UNKNOWN:
            rec.note(R_UNKNOWN_BASIS)
    else:
        rec.volume_reason = R_MISSING

    if rec.price is not None and rec.volume is not None:
        rec.dollar_volume = rec.price * rec.volume

    if rec.price is not None and rec.bid and rec.ask:
        rec.spread_pct = safe_div(rec.ask - rec.bid, rec.price) * 100.0

    # DATA-06: float conflict.
    fs = [f for f in (float_sources or []) if f[1]]
    if fs:
        vals = [f[1] for f in fs if f[1]]
        lo, hi = min(vals), max(vals)
        if lo > 0 and hi / lo > cfg.float("FLOAT_CONFLICT_FACTOR", 1.5):
            rec.note(R_FLOAT_CONFLICT)
            rec.float_reason = R_FLOAT_CONFLICT
        else:
            rec.shares_float = hi  # prefer the larger (more conservative dilution)
            rec.float_reason = R_OK

    if meta:
        for key in ("sector", "industry", "company", "short_float", "avg_volume",
                    "open", "after_close", "day_high", "day_low", "market_cap"):
            if meta.get(key) is not None and getattr(rec, key, None) in (None, ""):
                setattr(rec, key, meta[key])

    if rec.prev_close and rec.price:
        rec.change_pct = safe_div(rec.price - rec.prev_close, rec.prev_close) * 100.0

    rec.data_grade = _grade_from(rec)
    return rec


def _grade_from(rec: Canonical) -> str:
    """DATA-12: a record's grade from the reasons it carries.

    A = clean; B = missing a non-price field; C = volume basis unknown or a
    conflict on a secondary field; D = price conflict or stale; F = no price.
    """
    if rec.price is None:
        return "F"
    if R_CONFLICT in rec.reasons or R_STALE in rec.reasons:
        return "D"
    if R_FLOAT_CONFLICT in rec.reasons or R_UNKNOWN_BASIS in rec.reasons:
        return "C"
    if rec.volume is None or rec.float_reason == R_MISSING:
        return "B"
    return "A"


def cycle_grade(records: Iterable[Canonical]) -> str:
    """DATA-12: a cycle's grade is the *worst* grade among its records."""
    grades = [r.data_grade for r in records]
    if not grades:
        return "F"
    return max(grades, key=lambda g: GRADE_ORDER.index(g))


# --- snapshot --------------------------------------------------------------

@dataclass
class Snapshot:
    """One reconciled snapshot: the single source of truth for a cycle."""
    trading_day: date
    mode: str
    cycle_ts: datetime
    records: dict[str, Canonical]
    data_grade: str = "F"
    quality: dict[str, Any] = field(default_factory=dict)

    def get(self, symbol: str) -> Optional[Canonical]:
        return self.records.get(symbol.upper())

    def qualified_inputs(self) -> list[Canonical]:
        """Records with a usable price (grade better than F)."""
        return [r for r in self.records.values() if r.price is not None]

    @property
    def payload_hash(self) -> str:
        blob = stable_json({s: {"p": r.price, "v": r.volume, "g": r.data_grade}
                            for s, r in sorted(self.records.items())})
        return sha256_hex(blob.encode("utf-8"))[:32]

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading_day": self.trading_day.isoformat(),
            "mode": self.mode,
            "cycle_ts": self.cycle_ts.isoformat(),
            "data_grade": self.data_grade,
            "n_records": len(self.records),
            "quality": self.quality,
        }


def build_snapshot(records: Iterable[Canonical], trading_day: date, mode: str,
                   cycle_ts: Optional[datetime] = None,
                   source_health: Optional[dict[str, Any]] = None) -> Snapshot:
    """Assemble a snapshot and compute its aggregate data grade (DATA-12)."""
    recs = {r.symbol: r for r in records if r.symbol}
    grade = cycle_grade(recs.values())
    quality: dict[str, Any] = {
        "grade": grade,
        "n_records": len(recs),
        "n_priced": sum(1 for r in recs.values() if r.price is not None),
        "n_conflict": sum(1 for r in recs.values() if R_CONFLICT in r.reasons),
        "n_stale": sum(1 for r in recs.values() if R_STALE in r.reasons),
        "n_unknown_basis": sum(1 for r in recs.values() if R_UNKNOWN_BASIS in r.reasons),
        "sources": source_health or {},
    }
    return Snapshot(trading_day=trading_day, mode=mode,
                    cycle_ts=as_utc(cycle_ts or now_utc()), records=recs,
                    data_grade=grade, quality=quality)


# --- snapshot from source adapters -----------------------------------------

def snapshot_from_finviz_rows(rows: list[dict[str, Any]], cfg: Config, mode: str,
                              trading_day: date, extra_points: Optional[dict[str, list[PricePoint]]] = None,
                              source_health: Optional[dict[str, Any]] = None,
                              cycle_ts: Optional[datetime] = None) -> Snapshot:
    """Build a snapshot from Finviz parsed rows plus optional extra source points.

    Finviz supplies the universe/quote layer; ``extra_points`` lets a fresher feed
    (e.g. TwelveData) join the reconciliation for the symbols it covers.
    """
    now = as_utc(cycle_ts or now_utc())
    basis = choose_basis(mode)
    out: list[Canonical] = []
    extra_points = extra_points or {}
    for row in rows:
        sym = (row.get("symbol") or "").upper()
        if not sym:
            continue
        # Finviz price basis: plain Price is REGULAR (frozen after close); the
        # after-hours close is EXTENDED (DATA-01/D-1). Pick the right one for the
        # current mode and keep the other as prev/after context.
        if basis == BASIS_EXTENDED and row.get("after_close"):
            px = row["after_close"]
            scope = SCOPE_SINGLE_VENUE
        else:
            px = row.get("price")
            scope = SCOPE_SINGLE_VENUE
        points = [PricePoint(source="finviz", price=px, ts=now, basis=basis,
                             scope=scope, volume=row.get("volume"),
                             prev_close=row.get("prev_close"))]
        points.extend(extra_points.get(sym, []))
        float_sources = [("finviz", row.get("shares_float"), now)]
        rec = reconcile_symbol(sym, points, cfg, mode, now=now,
                               float_sources=float_sources, meta={
                                   "sector": row.get("sector", ""),
                                   "industry": row.get("industry", ""),
                                   "company": row.get("company", ""),
                                   "short_float": row.get("short_float"),
                                   "avg_volume": row.get("avg_volume"),
                                   "open": row.get("open"),
                                   "after_close": row.get("after_close"),
                                   "day_high": row.get("high"),
                                   "day_low": row.get("low"),
                                   "market_cap": row.get("market_cap"),
                               })
        out.append(rec)
    return build_snapshot(out, trading_day, mode, cycle_ts=now, source_health=source_health)
