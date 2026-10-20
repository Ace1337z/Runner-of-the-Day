"""Scoring, ranking and supply (build prompt §9).

Stage-1 score (§9.1): a weighted sum of five component scores, each 0..1 scaled
by its weight (``W_TAPE`` 30, ``W_CATALYST`` 25, ``W_FUEL`` 20, ``W_STRUCTURE``
15, ``W_CONTEXT`` 10), for a 0..100 total. Only gated names are scored.

The score is **uncalibrated** in Stage 1 — 85 has no probability meaning; it is a
starting threshold to be tuned from the MISS REPORT.

Ranking (§9.2): candidates compete by score, ties broken by liquidity then by
recency of the igniting move, all deterministically (AC1/S5). The top
``BOARD_SIZE`` form the BOARD. Rank 1 is ROTD only if its score is at least
``ROTD_MIN_SCORE`` (otherwise the day correctly has "no qualified runner").

Supply (§9.3): the alert budget is a *supply* constraint. Ranking picks the best
names; the budget spends at most ``ALERT_BUDGET_PER_DAY`` ignitions
(``+RESERVE_SLOTS`` for a name at/above ``EXCEPTIONAL_SCORE``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config
from .features import FeatureSet
from .gates import GateReport
from .normalize import Canonical
from .util import clamp, stable_json

log = logging.getLogger(__name__)


@dataclass
class ComponentScore:
    name: str
    raw: Optional[float]        # 0..1, or None when uncomputable
    weight: float
    points: float               # raw * weight (0 when raw is None)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"raw": self.raw, "weight": self.weight, "points": round(self.points, 2),
                "detail": self.detail}


@dataclass
class Scored:
    symbol: str
    score: float
    components: dict[str, ComponentScore] = field(default_factory=dict)
    rank: Optional[int] = None
    tiebreak: tuple = ()

    def as_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 2),
                "components": {k: v.as_dict() for k, v in self.components.items()}}


def _lin(value: Optional[float], lo: float, hi: float) -> Optional[float]:
    """Linear map value in [lo, hi] -> [0, 1], clamped; None stays None."""
    if value is None:
        return None
    if hi == lo:
        return 1.0 if value >= hi else 0.0
    return clamp((value - lo) / (hi - lo), 0.0, 1.0)


def score_candidate(rec: Canonical, fs: FeatureSet, gates: GateReport, cfg: Config,
                    mode: str) -> Scored:
    """Compute the Stage-1 score for one gated candidate."""
    W = {k: cfg.float(f"W_{k}", 0.0) for k in
         ("TAPE", "CATALYST", "FUEL", "STRUCTURE", "CONTEXT")}

    # --- TAPE (0..1): how hard the tape is igniting right now ---
    # POST: use the true after-hours move vs today's 16:00 ET regular close when
    # the feed supplies it, so a 10:00 AM regular-session spike cannot carry an
    # after-hours alert (section 3).
    gain = fs.value("gain_from_prev_close")
    if mode == "POST" and fs.value("ah_change_pct") is not None:
        gain = fs.value("ah_change_pct")
    tape_parts = [
        _lin(gain, 2.0, 20.0),
        _lin(fs.value("gain_from_open"), 1.5, 15.0),
        _lin(fs.value("rvol"), 1.2, 6.0),
        _lin(fs.value("range_expansion"), 2.0, 15.0),
        _lin(fs.value("accel_5m"), 0.5, 6.0),
        _lin(fs.value("vwap_dist"), 0.2, 5.0),
    ]
    # Live volume velocity: reward a stock that is trading right now, punishes
    # a dead-afternoon name that spiked at the open. Take the max of dollar
    # volume and share volume so sub-$1 names with high share volume are not
    # penalized by their low share price.
    dvol10 = fs.value("dollar_volume_10m")
    vol3 = fs.value("vol_3m")
    vel = None
    if dvol10 is not None:
        vel = _lin(dvol10, 40_000.0, 1_000_000.0)
    if vol3 is not None:
        vel_v = _lin(vol3, 50_000.0, 800_000.0)
        vel = vel_v if vel is None else max(vel, vel_v)
    if vel is not None:
        tape_parts.append(vel)
    present = [p for p in tape_parts if p is not None]
    tape = sum(present) / len(present) if present else None

    # --- CATALYST (0..1) ---
    cat = _lin(fs.value("catalyst_score"), 0.0, 100.0)

    # --- FUEL (0..1): low float + high short interest = more squeeze fuel ---
    fl = fs.value("float_shares")
    fuel = None
    if fl is not None:
        # Feature adapters normally normalize float to shares; Finviz-origin values
        # may be in millions. Treat plausible small magnitudes as millions, then
        # score relative to the methodology's 1M..35M range.
        fl_m = fl if fl < 100_000 else fl / 1e6
        base_fuel = clamp(1.0 - (fl_m - 1.0) / (35.0 - 1.0), 0.0, 1.0)

        sf_bonus = 0.0
        sf = fs.value("short_float_pct")
        if sf is not None:
            sf_bonus = _lin(sf, 5.0, 30.0) * 0.20

        rot_bonus = 0.0
        rot = fs.value("float_rotation")
        if rot is not None:
            rot_bonus = _lin(rot, 0.15, 1.5) * 0.25

        fuel = clamp(base_fuel + sf_bonus + rot_bonus, 0.0, 1.0)

    # --- STRUCTURE (0..1): a clean setup ---
    struct_parts = []
    nh = fs.value("new_high_flag")
    if nh is not None:
        struct_parts.append(nh)
    gap = fs.value("gap_pct")
    if mode == "POST" and fs.value("ah_change_pct") is not None:
        gap = fs.value("ah_change_pct")
    if gap is not None:
        struct_parts.append(_lin(gap, 0.0, 15.0))
    d52l = fs.value("dist_from_52w_low")
    if d52l is not None:
        # Being nearer the 52w low is a better risk/reward for a fresh runner.
        struct_parts.append(clamp(1.0 - d52l / 100.0, 0.0, 1.0))
    # Front-side only: fading off HOD is penalized in proportion to the drawdown
    # (section 8), so a +80%-then-35%-off-high chart cannot rank on old strength.
    dd = fs.value("drawdown_from_hod_pct")
    if dd is not None:
        max_dd = cfg.float("MAX_DRAWDOWN_FROM_HOD_PCT", 12.0)
        struct_parts.append(clamp(1.0 - dd / max_dd, 0.0, 1.0) if max_dd > 0 else 0.0)
    structure = sum(struct_parts) / len(struct_parts) if struct_parts else None

    # --- CONTEXT (0..1): session and data quality ---
    ctx_parts = []
    ctx_parts.append({"PRE": 0.7, "OPEN": 1.0, "MIDDAY": 0.8, "POST": 0.6}.get(mode, 0.3))
    grade_map = {"A": 1.0, "B": 0.85, "C": 0.6, "D": 0.3, "F": 0.0}
    ctx_parts.append(grade_map.get(rec.data_grade, 0.0))
    context = sum(ctx_parts) / len(ctx_parts)

    raw_by = {"TAPE": tape, "CATALYST": cat, "FUEL": fuel,
              "STRUCTURE": structure, "CONTEXT": context}
    components: dict[str, ComponentScore] = {}
    total = 0.0
    for key in ("TAPE", "CATALYST", "FUEL", "STRUCTURE", "CONTEXT"):
        raw = raw_by[key]
        weight = W[key]
        points = (raw or 0.0) * weight
        components[key] = ComponentScore(key, raw, weight, points,
                                         "" if raw is not None else "MISSING")
        total += points

    # Missing optional components (especially catalyst when running rules-only)
    # must not cap an otherwise complete score. Normalize against only active
    # component weights so the result remains on a 0..100 scale. The breakdown
    # preserves raw weighted points for auditability; Scored.score is normalized.
    active_weight = sum(W[k] for k in raw_by if raw_by[k] is not None and W[k] > 0)
    score = (clamp(total * (100.0 / active_weight), 0.0, 100.0)
             if active_weight > 0 else 0.0)

    # Tiebreak: higher liquidity first, then a stable symbol order.
    liq = fs.value("dollar_volume_15m") or (rec.dollar_volume or 0.0)
    tiebreak = (-round(liq, 2), rec.symbol)
    return Scored(symbol=rec.symbol, score=score, components=components, tiebreak=tiebreak)


@dataclass
class Ranking:
    day_ranked: list[Scored] = field(default_factory=list)
    board: list[Scored] = field(default_factory=list)
    rotd: Optional[Scored] = None
    rotd_reason: str = ""
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_ranked": len(self.day_ranked),
            "board": [s.symbol for s in self.board],
            "rotd": self.rotd.symbol if self.rotd else None,
            "rotd_score": round(self.rotd.score, 2) if self.rotd else None,
            "rotd_reason": self.rotd_reason,
        }


def rank(day: list[Scored], cfg: Config) -> Ranking:
    """Rank the gated candidates and select BOARD + ROTD (§9.2)."""
    ordered = sorted(day, key=lambda s: (-s.score, s.tiebreak))
    for i, s in enumerate(ordered, start=1):
        s.rank = i
    board_size = cfg.int("BOARD_SIZE", 5)
    board = ordered[:board_size]
    r = Ranking(day_ranked=ordered, board=board)

    if not ordered:
        r.rotd_reason = "NO_CANDIDATES"
        return r
    top = ordered[0]
    min_score = cfg.float("ROTD_MIN_SCORE", 70.0)
    if top.score < min_score:
        r.rotd_reason = f"TOP_SCORE_BELOW_MIN ({top.score:.1f} < {min_score:.0f})"
        return r
    r.rotd = top
    r.rotd_reason = "TOP_CANDIDATE_QUALIFIED"
    return r
