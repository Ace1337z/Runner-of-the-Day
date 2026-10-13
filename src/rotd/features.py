"""Features (build prompt §8).

Every feature is computed from the reconciled snapshot only — no module fetches
its own data in the decision path. A feature that cannot be computed is
``MISSING`` with a reason code; it is never 0 and never silently dropped.

Features carry the tags that make them explainable:

- the value (or ``None`` when missing),
- a ``missing_reason`` when ``None``,
- the ``basis`` the input used (REGULAR/EXTENDED) and the venue ``scope``,
- for relative measures, the window.

Named features (grouped by the score they feed, §9.1):

Tape / ignition  : ``gain_from_open``, ``gain_from_prev_close``, ``rvol``,
                   ``range_expansion``, ``accel_5m``, ``vwap_dist``,
                   ``dollar_volume_15m``, ``spread_pct``, ``new_high_flag``.
Catalyst         : ``catalyst_score`` (from the catalyst service), ``news_count``.
Fuel             : ``float_shares``, ``float_dollars``, ``short_float_pct``,
                   ``days_to_cover``, ``squeeze_flag``.
Structure        : ``gap_pct``, ``dist_from_52w_high``, ``dist_from_52w_low``,
                   ``price_band``.
Context          : ``mode``, ``phase_age_min``, ``data_grade``, ``is_half_day``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .config import Config
from .normalize import Canonical, Snapshot, R_OK, BASIS_REGULAR
from .util import clamp, safe_div

log = logging.getLogger(__name__)

# Feature missing reason codes.
M_NO_PRICE = "NO_PRICE"
M_NO_VOLUME = "NO_VOLUME"
M_BASIS_UNKNOWN = "BASIS_UNKNOWN"
M_NO_PREV = "NO_PREV_CLOSE"
M_NO_OPEN = "NO_OPEN"
M_NO_HISTORY = "NO_HISTORY"
M_NO_FLOAT = "NO_FLOAT"
M_LOW_GRADE = "LOW_DATA_GRADE"


@dataclass
class Feature:
    """One explainable feature value."""
    name: str
    value: Optional[float]
    unit: str = ""
    missing_reason: str = ""
    basis: str = ""
    scope: str = ""
    window: str = ""
    label: str = ""

    @property
    def present(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict[str, Any]:
        return {"v": self.value, "u": self.unit, "missing": self.missing_reason,
                "basis": self.basis, "scope": self.scope, "window": self.window}


@dataclass
class FeatureSet:
    """All features for one symbol in one cycle."""
    symbol: str
    features: dict[str, Feature] = field(default_factory=dict)

    def add(self, f: Feature) -> None:
        self.features[f.name] = f

    def __getitem__(self, name: str) -> Feature:
        return self.features.get(name, Feature(name, None, missing_reason=M_NO_PRICE))

    def value(self, name: str) -> Optional[float]:
        f = self.features.get(name)
        return f.value if f else None

    def as_dict(self) -> dict[str, Any]:
        return {k: v.as_dict() for k, v in self.features.items()}

    def missing(self) -> list[str]:
        return [k for k, v in self.features.items() if not v.present]


def price_band(price: Optional[float], cfg: Config) -> str:
    """Which price band a price falls in (§18.1 breakdown bands)."""
    if price is None:
        return "unknown"
    bounds = cfg.float_list("PRICE_BANDS") or [0.20, 0.50, 1.0, 5.0, 10.0]
    labels = ["<0.20", "0.20-0.50", "0.50-1", "1-5", "5-10"]
    for i, b in enumerate(bounds):
        if price < b:
            return labels[i] if i < len(labels) else f"<{b}"
    return f">={bounds[-1]}"


def compute_features(rec: Canonical, cfg: Config, snap: Snapshot,
                     catalyst_score: Optional[float] = None,
                     news_count: Optional[int] = None,
                     history: Optional[dict[str, Any]] = None,
                     mode_age_min: Optional[float] = None) -> FeatureSet:
    """Compute every feature for one reconciled record.

    ``history`` optionally carries prior-cycle observations for this symbol
    (``prev_price``, ``vol_15m``, ``avg_vol_15m``) collected by the tracker; when
    absent the related features are MISSING, not zero.
    """
    fs = FeatureSet(symbol=rec.symbol)
    basis = rec.price_basis
    scope = rec.price_scope
    history = history or {}

    # --- tape / ignition ---
    if rec.price is None:
        for n in ("gain_from_open", "gain_from_prev_close", "rvol", "range_expansion",
                  "accel_5m", "vwap_dist", "dollar_volume_15m", "spread_pct"):
            fs.add(Feature(n, None, missing_reason=M_NO_PRICE, basis=basis))
    else:
        go = safe_div(rec.price - rec.open, rec.open) * 100.0 if rec.open else None
        fs.add(Feature("gain_from_open", go, "%", "" if go is not None else M_NO_OPEN,
                       basis=basis, scope=scope))
        gc = safe_div(rec.price - rec.prev_close, rec.prev_close) * 100.0 if rec.prev_close else None
        fs.add(Feature("gain_from_prev_close", gc, "%",
                       "" if gc is not None else M_NO_PREV, basis=basis, scope=scope))

        # RVOL — needs a volume with a known basis and comparable history (DATA-04).
        if rec.volume is None:
            fs.add(Feature("rvol", None, "x", missing_reason=M_NO_VOLUME, basis=basis))
        elif rec.volume_basis == "UNKNOWN":
            fs.add(Feature("rvol", None, "x", missing_reason=M_BASIS_UNKNOWN, basis=basis))
        else:
            avg = rec.avg_volume or history.get("avg_volume")
            rvol = safe_div(rec.volume, avg) if avg else None
            fs.add(Feature("rvol", rvol, "x", "" if rvol is not None else M_NO_HISTORY,
                           basis=basis, scope=rec.volume_basis))

        if rec.day_high and rec.day_low and rec.prev_close:
            rng = safe_div(rec.day_high - rec.day_low, rec.prev_close) * 100.0
            fs.add(Feature("range_expansion", rng, "%", basis=basis, scope=scope))
        else:
            fs.add(Feature("range_expansion", None, "%", missing_reason=M_NO_HISTORY, basis=basis))

        prev_price = history.get("prev_price")
        accel = safe_div(rec.price - prev_price, prev_price) * 100.0 if prev_price else None
        fs.add(Feature("accel_5m", accel, "%", "" if accel is not None else M_NO_HISTORY,
                       basis=basis, scope=scope, window="5m"))

        vwap = history.get("vwap")
        vd = safe_div(rec.price - vwap, vwap) * 100.0 if vwap else None
        fs.add(Feature("vwap_dist", vd, "%", "" if vd is not None else M_NO_HISTORY,
                       basis=basis, scope=scope))

        dvol = history.get("dollar_volume_15m")
        fs.add(Feature("dollar_volume_15m", dvol, "USD",
                       "" if dvol is not None else M_NO_HISTORY, basis=basis,
                       scope=rec.volume_basis, window="15m"))
        fs.add(Feature("spread_pct", rec.spread_pct, "%",
                       "" if rec.spread_pct is not None else M_NO_HISTORY, basis=basis))

    # new-high flag: price at/near the day high (a fresh 15m high is a breakout).
    if rec.price and rec.day_high:
        fs.add(Feature("new_high_flag", 1.0 if rec.price >= rec.day_high * 0.999 else 0.0,
                       "bool", basis=basis))

    # --- catalyst ---
    fs.add(Feature("catalyst_score", catalyst_score, "0-100",
                   "" if catalyst_score is not None else "NO_CATALYST_VERDICT"))
    fs.add(Feature("news_count", float(news_count) if news_count is not None else None,
                   "items", "" if news_count is not None else "NO_NEWS_FETCH"))

    # --- fuel ---
    if rec.shares_float is not None:
        fs.add(Feature("float_shares", rec.shares_float, "shares", basis=basis))
        if rec.price:
            fs.add(Feature("float_dollars", rec.shares_float * rec.price, "USD", basis=basis))
        else:
            fs.add(Feature("float_dollars", None, "USD", missing_reason=M_NO_PRICE))
    else:
        fs.add(Feature("float_shares", None, "shares", missing_reason=M_NO_FLOAT))
        fs.add(Feature("float_dollars", None, "USD", missing_reason=M_NO_FLOAT))
    fs.add(Feature("short_float_pct", rec.short_float, "%",
                   "" if rec.short_float is not None else "NO_SHORT_DATA"))
    dtc = history.get("days_to_cover")
    fs.add(Feature("days_to_cover", dtc, "days", "" if dtc is not None else "NO_SHORT_DATA"))
    squeeze = None
    if rec.short_float is not None and (dtc or 0) >= 3:
        squeeze = 1.0
    elif rec.short_float is not None:
        squeeze = 0.0
    fs.add(Feature("squeeze_flag", squeeze, "bool",
                   "" if squeeze is not None else "NO_SHORT_DATA"))

    # --- structure ---
    fs.add(Feature("gap_pct", history.get("gap_pct"), "%",
                   "" if history.get("gap_pct") is not None else M_NO_HISTORY))
    fs.add(Feature("dist_from_52w_high", history.get("dist_52w_high"), "%",
                   "" if history.get("dist_52w_high") is not None else M_NO_HISTORY))
    fs.add(Feature("dist_from_52w_low", history.get("dist_52w_low"), "%",
                   "" if history.get("dist_52w_low") is not None else M_NO_HISTORY))
    fs.add(Feature("price_band", None, "", label=price_band(rec.price, cfg), basis=basis))

    # --- context ---
    fs.add(Feature("phase_age_min", mode_age_min, "min",
                   "" if mode_age_min is not None else "NO_PHASE"))
    fs.add(Feature("is_half_day", 1.0 if snap.quality.get("half_day") else 0.0, "bool"))

    return fs


def data_quality_ok(rec: Canonical, cfg: Config) -> tuple[bool, str]:
    """Whether a record is fit to be scored at all (fail closed)."""
    if rec.price is None:
        return False, M_NO_PRICE
    if rec.data_grade == "F":
        return False, M_LOW_GRADE
    return True, R_OK
