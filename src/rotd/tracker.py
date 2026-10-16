"""Tracking, grading, the runner list and the MISS REPORT (build prompt §12).

After an alert, ROTD tracks the name at fixed checkpoints (``CHECKPOINTS_MIN``:
15m, 60m, eod, next_open, next_close) and records the path — the maximum gain and
the maximum drawdown — not just the endpoints.

Grading (§18.4): a runner is a name that reaches ``RUNNER_GAIN_PCT`` (+30%) before
it falls ``RUNNER_STOP_PCT`` (−12%); ``hit_gain_first`` is 1 for a true runner, 0
for a stop-first, NULL while pending.

The runner list (§12.3) is every sub-$10 US name that gained at least
``RUNNER_LIST_PCT`` (+100%) versus its prior regular close, with the extended-hours
high included. The main list needs ``RUNNER_LIST_MIN_DVOL`` ($5M) dollar volume;
below ``RUNNER_LIST_MIN_DVOL_LOW`` ($1M) a name is tagged ``ILLIQUID``. A name on
the list that ROTD did not alert becomes a MISS with a reason.

Lead time (§12.3): how long before a name crossed ``LEAD_MARK_PCT`` (+30%) ROTD
first saw it — the success measure S2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .config import Config
from .normalize import Canonical
from .store import Store
from .util import as_utc, now_utc, safe_div

log = logging.getLogger(__name__)

# Checkpoint labels (fixed vocabulary shared by the store and the report).
CP_15M = "15m"
CP_60M = "60m"
CP_EOD = "eod"
CP_NEXT_OPEN = "next_open"
CP_NEXT_CLOSE = "next_close"


@dataclass
class PathTracker:
    """Follows one alert's price path to grade it (§12.2)."""
    alert_id: int
    symbol: str
    trading_day: date
    ref_price: float
    ref_ts: datetime
    max_price: float = 0.0
    min_price: float = float("inf")
    hit_gain_at: Optional[datetime] = None
    hit_stop_at: Optional[datetime] = None
    checkpoints: dict[str, tuple[datetime, float]] = field(default_factory=dict)

    def observe(self, price: Optional[float], ts: Optional[datetime],
                cfg: Config) -> None:
        if price is None:
            return
        self.max_price = max(self.max_price, price)
        self.min_price = min(self.min_price, price)
        gain = cfg.float("RUNNER_GAIN_PCT", 30.0)
        stop = cfg.float("RUNNER_STOP_PCT", 12.0)
        if self.hit_gain_at is None and price >= self.ref_price * (1 + gain / 100.0):
            self.hit_gain_at = ts
        if self.hit_stop_at is None and price <= self.ref_price * (1 - stop / 100.0):
            self.hit_stop_at = ts

    def mark(self, checkpoint: str, price: Optional[float], ts: Optional[datetime]) -> None:
        if price is not None:
            self.checkpoints[checkpoint] = (as_utc(ts or now_utc()), price)

    @property
    def hit_gain_first(self) -> Optional[bool]:
        if self.hit_gain_at and self.hit_stop_at:
            return self.hit_gain_at <= self.hit_stop_at
        if self.hit_gain_at:
            return True
        if self.hit_stop_at:
            return False
        return None

    @property
    def max_gain_pct(self) -> Optional[float]:
        return safe_div(self.max_price - self.ref_price, self.ref_price) * 100.0

    @property
    def max_dd_pct(self) -> Optional[float]:
        if self.min_price == float("inf"):
            return None
        return safe_div(self.min_price - self.ref_price, self.ref_price) * 100.0

    def persist(self, store: Store) -> None:
        for cp, (ts, price) in self.checkpoints.items():
            ret = safe_div(price - self.ref_price, self.ref_price) * 100.0
            store.save_outcome(self.alert_id, self.symbol, self.trading_day, cp, ts,
                               price, ret, self.max_gain_pct, self.max_dd_pct,
                               self.hit_gain_first)


@dataclass
class Runner:
    symbol: str
    prior_close: float
    day_high: float
    gain_pct: float
    dollar_vol: Optional[float]
    illiquid: bool
    in_universe: bool
    first_seen: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "prior_close": self.prior_close,
                "day_high": self.day_high, "gain_pct": self.gain_pct,
                "dollar_vol": self.dollar_vol, "illiquid": self.illiquid,
                "in_universe": self.in_universe}


def build_runner_list(records: list[Canonical], cfg: Config,
                      first_seen: Optional[dict[str, datetime]] = None,
                      in_universe: Optional[set[str]] = None) -> list[Runner]:
    """Every sub-$10 name that gained >= ``RUNNER_LIST_PCT`` vs prior close."""
    threshold = cfg.float("RUNNER_LIST_PCT", 100.0)
    main_floor = cfg.float("RUNNER_LIST_MIN_DVOL", 5_000_000)
    low_floor = cfg.float("RUNNER_LIST_MIN_DVOL_LOW", 1_000_000)
    seen = first_seen or {}
    universe = in_universe or set()
    out: list[Runner] = []
    for rec in records:
        if rec.price is None or not rec.prev_close or rec.prev_close <= 0:
            continue
        max_mcap = cfg.float("MAX_MARKET_CAP", 0.0)
        if max_mcap > 0:
            if rec.market_cap is None or rec.market_cap > max_mcap:
                continue
        elif rec.price > cfg.float("MAX_PRICE", 10.0):
            continue
        high = max(rec.day_high or rec.price, rec.after_close or rec.price, rec.price)
        gain = safe_div(high - rec.prev_close, rec.prev_close) * 100.0
        if gain is None or gain < threshold:
            continue
        dvol = rec.dollar_volume
        illiquid = (dvol is None) or (dvol < low_floor)
        # Main list requires the higher floor; illiquid names still appear tagged.
        if dvol is not None and dvol < low_floor:
            illiquid = True
        out.append(Runner(symbol=rec.symbol, prior_close=rec.prev_close, day_high=high,
                          gain_pct=gain, dollar_vol=dvol, illiquid=illiquid,
                          in_universe=rec.symbol in universe,
                          first_seen=seen.get(rec.symbol)))
    out.sort(key=lambda r: r.gain_pct, reverse=True)
    return out


def build_miss_report(store: Store, day: date, runners: list[Runner],
                      alerts: list[Any], data_grades: list[str],
                      in_universe: Optional[set[str]] = None,
                      cfg: Any = None) -> dict[str, Any]:
    """Assemble the daily MISS REPORT (§12.4). Always produced.

    For every runner the day produced it explains why ROTD did or did not flag it.
    Also surfaces any gates that are OFF by default (VERIFIED G-18) so the day's
    blind spots are called out explicitly rather than hidden.
    """
    try:
        from .gates import gates_not_enforced
        not_enforced = gates_not_enforced(cfg) if cfg is not None else []
    except Exception:  # noqa: BLE001
        not_enforced = []
    universe = in_universe or set()
    alerted = {a["symbol"] for a in alerts}
    hits: list[dict[str, Any]] = []
    false_positive: list[dict[str, Any]] = []

    for a in alerts:
        sym = a["symbol"]
        alert_id = a["id"]
        outcomes = store.query("SELECT * FROM outcomes WHERE alert_id=? ORDER BY ts",
                               (alert_id,))
        max_gain = None
        hit_gain = None
        for o in outcomes:
            if o["max_gain_pct"] is not None:
                max_gain = o["max_gain_pct"] if max_gain is None else max(max_gain, o["max_gain_pct"])
            if o["hit_gain_first"] is not None:
                hit_gain = bool(o["hit_gain_first"])
        if sym in {r.symbol for r in runners}:
            r = next(r for r in runners if r.symbol == sym)
            lead = None
            if r.first_seen and a["cycle_ts"]:
                try:
                    lead_delta = as_utc(datetime.fromisoformat(a["cycle_ts"])) - as_utc(r.first_seen)
                    lead = f"{lead_delta.total_seconds() / 60:.0f}m before +100%"
                except ValueError:
                    lead = None
            hits.append({"symbol": sym, "max_gain_pct": max_gain, "lead": lead})
        elif hit_gain is False or (max_gain is not None and max_gain < 5.0):
            false_positive.append({"symbol": sym, "why": "reversed after the alert"})

    missed: list[dict[str, Any]] = []
    for r in runners:
        if r.symbol in alerted:
            continue
        if not r.in_universe and universe and r.symbol not in universe:
            why = "outside the sub-$10 universe at the time"
        elif r.illiquid:
            why = "flagged ILLIQUID (dollar volume below the floor)"
        else:
            why = "in the universe but never reached the score/rank to alert"
        missed.append({"symbol": r.symbol, "gain_pct": r.gain_pct, "why": why})

    grade_counts: dict[str, int] = {}
    for g in data_grades:
        grade_counts[g] = grade_counts.get(g, 0) + 1
    mean_grade = (sum({"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}.get(g, 0)
                      for g in data_grades) / len(data_grades)) if data_grades else 0.0
    worst = max(data_grades, key=lambda g: "ABCDF".index(g)) if data_grades else "F"

    return {
        "trading_day": day.isoformat(),
        "n_alerts": len(alerts),
        "n_runners": len(runners),
        "n_caught": len(hits),
        "n_missed": len(missed),
        "data_grade": worst,
        "mean_grade": round(mean_grade, 2),
        "grade_counts": grade_counts,
        "hits": hits,
        "misses": missed,
        "false_positive": false_positive,
        "not_enforced_gates": not_enforced,
        "s1_quiet_when_quiet": len(alerts) == 0 and len(runners) == 0,
    }
