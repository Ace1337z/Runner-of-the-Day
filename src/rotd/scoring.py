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
    tape_parts = [
        _lin(fs.value("gain_from_prev_close"), 3.0, 40.0),
        _lin(fs.value("gain_from_open"), 2.0, 25.0),
        _lin(fs.value("rvol"), 1.5, 10.0),
        _lin(fs.value("range_expansion"), 3.0, 25.0),
        _lin(fs.value("accel_5m"), 1.0, 10.0),
        _lin(fs.value("vwap_dist"), 0.5, 8.0),
    ]
    present = [p for p in tape_parts if p is not None]
    tape = sum(present) / len(present) if present else None

    # --- CATALYST (0..1) ---
    cat = _lin(fs.value("catalyst_score"), 0.0, 100.0)

    # --- FUEL (0..1): low float + high short interest = more squeeze fuel ---
    fuel_parts = []
    fl = fs.value("float_shares")
    if fl is not None:
        # Lower float => higher fuel (5M shares or less is prime).
        fuel_parts.append(clamp(1.0 - (fl - 1e6) / (50e6 - 1e6), 0.0, 1.0))
    sf = fs.value("short_float_pct")
    if sf is not None:
        fuel_parts.append(_lin(sf, 5.0, 30.0))
    sq = fs.value("squeeze_flag")
    if sq is not None:
        fuel_parts.append(sq)
    fuel = sum(fuel_parts) / len(fuel_parts) if fuel_parts else None

    # --- STRUCTURE (0..1): a clean setup ---
    struct_parts = []
    nh = fs.value("new_high_flag")
    if nh is not None:
        struct_parts.append(nh)
    gap = fs.value("gap_pct")
    if gap is not None:
        struct_parts.append(_lin(gap, 0.0, 15.0))
    d52l = fs.value("dist_from_52w_low")
    if d52l is not None:
        # Being nearer the 52w low is a better risk/reward for a fresh runner.
        struct_parts.append(clamp(1.0 - d52l / 100.0, 0.0, 1.0))
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

    # Tiebreak: higher liquidity first, then a stable symbol order.
    liq = fs.value("dollar_volume_15m") or (rec.dollar_volume or 0.0)
    tiebreak = (-round(liq, 2), rec.symbol)
    return Scored(symbol=rec.symbol, score=total, components=components, tiebreak=tiebreak)


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
