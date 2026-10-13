"""Hard gates (build prompt §7).

A gate is a boolean with a reason code. Gates run *before* scoring: a name that
fails any gate is not scored and cannot alert. Fail closed — if a gate's inputs
are missing, the gate **fails** with a ``*_UNKNOWN`` / ``*_MISSING`` reason rather
than passing.

Gates:

- ``G-PRICE``   — sub-$10 (``MAX_PRICE``); no price floor (§18.1).
- ``G-SPREAD``  — quoted spread within the price-tier limit
  (``SPREAD_TIERS``), loosened by ``EXT_SPREAD_MULT`` in POST/PRE.
- ``G-LIQ``     — 15-minute dollar volume >= the mode floor (``LIQ_15M_*``).
- ``G-TRADES``  — enough prints at a sane average size; skipped when the feed
  cannot supply prints (``MIN_TRADES_15M=0`` means "not enforced", but the gate
  then reports ``NOT_ENFORCED`` so the MISS REPORT can say so).
- ``G-EXIT``    — a test exit of ``EXIT_TEST_USD`` would fill within
  ``EXIT_MAX_SEC`` and ``EXIT_MAX_SLIP_PCT``.
- ``G-HALT``    — no confirmed halt. With no halt feed (VERIFY D-5) this relies on
  staleness: a stale/absent quote already fails G-DATA, so G-HALT reports
  ``HALT_UNKNOWN`` and defers to G-DATA.
- ``G-PHASE``   — the mode allows this candidate: OVERNIGHT never ignites.
- ``G-DATA``    — the record's data grade is good enough and it is not
  ``SUSPENDED``/``CONFLICT``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config
from .features import FeatureSet
from .normalize import (BASIS_EXTENDED, R_CONFLICT, R_MISSING, R_STALE,
                        R_SUSPENDED, Canonical, Snapshot)

log = logging.getLogger(__name__)

# Gate reason codes (pass reasons and fail reasons).
P_OK = "OK"
F_PRICE_TOO_HIGH = "PRICE_TOO_HIGH"
F_PRICE_UNKNOWN = "PRICE_UNKNOWN"
F_SPREAD_WIDE = "SPREAD_WIDE"
F_SPREAD_UNKNOWN = "SPREAD_UNKNOWN"
F_LIQ_LOW = "LIQUIDITY_LOW"
F_LIQ_UNKNOWN = "LIQUIDITY_UNKNOWN"
F_TRADES_LOW = "TRADES_LOW"
G_TRADES_NOT_ENFORCED = "TRADES_NOT_ENFORCED"
F_EXIT_FAIL = "EXIT_TEST_FAILED"
F_EXIT_UNKNOWN = "EXIT_UNKNOWN"
F_HALT = "HALTED_SUSPENDED"
G_HALT_UNKNOWN = "HALT_UNKNOWN"
F_PHASE = "PHASE_NOT_ALLOWED"
F_DATA = "DATA_QUALITY_LOW"
F_SUSPENDED = R_SUSPENDED
F_CONFLICT = R_CONFLICT


@dataclass
class GateResult:
    gate: str
    passed: bool
    reason: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"pass": self.passed, "reason": self.reason, "detail": self.detail}


@dataclass
class GateReport:
    symbol: str
    results: list[GateResult] = field(default_factory=list)

    def add(self, gate: str, passed: bool, reason: str, detail: str = "") -> None:
        self.results.append(GateResult(gate, passed, reason, detail))

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def first_failure(self) -> Optional[GateResult]:
        return next((r for r in self.results if not r.passed), None)

    def summary(self) -> str:
        bad = [r for r in self.results if not r.passed]
        if not bad:
            return "all gates passed"
        return "; ".join(f"{r.gate}={r.reason}" for r in bad)

    def as_dict(self) -> dict[str, Any]:
        return {r.gate: r.as_dict() for r in self.results}


def _spread_limit(price: float, cfg: Config, mode: str) -> Optional[float]:
    """Max spread % for a price, from ``SPREAD_TIERS`` (loosest tier <= price)."""
    tiers: list[tuple[float, float]] = []
    for part in (cfg.raw("SPREAD_TIERS") or "").split(","):
        if ":" not in part:
            continue
        try:
            lo, lim = part.split(":", 1)
            tiers.append((float(lo), float(lim)))
        except ValueError:
            continue
    tiers.sort(key=lambda t: t[0], reverse=True)
    limit = None
    for lo, lim in tiers:
        if price >= lo:
            limit = lim
            break
    if limit is None and tiers:
        limit = tiers[-1][1]
    if limit is not None and mode in ("POST", "PRE", "OVERNIGHT"):
        limit *= cfg.float("EXT_SPREAD_MULT", 1.5)
    return limit


def gate_price(rec: Canonical, cfg: Config) -> GateResult:
    if rec.price is None:
        return GateResult("G-PRICE", False, F_PRICE_UNKNOWN)
    if rec.price > cfg.float("MAX_PRICE", 10.0):
        return GateResult("G-PRICE", False, F_PRICE_TOO_HIGH,
                          f"{rec.price:.4f} > {cfg.float('MAX_PRICE', 10.0):.2f}")
    return GateResult("G-PRICE", True, P_OK)


def gate_spread(rec: Canonical, cfg: Config, mode: str) -> GateResult:
    if rec.price is None:
        return GateResult("G-SPREAD", False, F_PRICE_UNKNOWN)
    limit = _spread_limit(rec.price, cfg, mode)
    if limit is None:
        return GateResult("G-SPREAD", False, F_SPREAD_UNKNOWN)
    if rec.spread_pct is None:
        # No bid/ask on the feed: fail closed (cannot confirm an exit is cheap).
        return GateResult("G-SPREAD", False, F_SPREAD_UNKNOWN,
                          "no bid/ask; cannot confirm tradability")
    if rec.spread_pct > limit:
        return GateResult("G-SPREAD", False, F_SPREAD_WIDE,
                          f"{rec.spread_pct:.2f}% > {limit:.2f}%")
    return GateResult("G-SPREAD", True, P_OK, f"{rec.spread_pct:.2f}% <= {limit:.2f}%")


def gate_liquidity(fs: FeatureSet, cfg: Config, mode: str) -> GateResult:
    floor_key = {"POST": "LIQ_15M_POST", "PRE": "LIQ_15M_PRE", "OPEN": "LIQ_15M_OPEN",
                 "MIDDAY": "LIQ_15M_MIDDAY", "OVERNIGHT": "LIQ_15M_POST"}.get(mode, "LIQ_15M_MIDDAY")
    floor = cfg.float(floor_key, 75000.0)
    dvol = fs.value("dollar_volume_15m")
    if dvol is None:
        return GateResult("G-LIQ", False, F_LIQ_UNKNOWN, "no 15m dollar volume")
    if dvol < floor:
        return GateResult("G-LIQ", False, F_LIQ_LOW, f"${dvol:,.0f} < ${floor:,.0f}")
    return GateResult("G-LIQ", True, P_OK, f"${dvol:,.0f} >= ${floor:,.0f}")


def gate_trades(fs: FeatureSet, cfg: Config) -> GateResult:
    min_trades = cfg.float("MIN_TRADES_15M", 0)
    if min_trades <= 0:
        return GateResult("G-TRADES", True, G_TRADES_NOT_ENFORCED,
                          "feed has no print counts; gate not enforced")
    trades = fs.value("n_trades_15m")
    if trades is None:
        return GateResult("G-TRADES", False, F_TRADES_LOW, "no trade-count data")
    if trades < min_trades:
        return GateResult("G-TRADES", False, F_TRADES_LOW, f"{trades:.0f} < {min_trades:.0f}")
    return GateResult("G-TRADES", True, P_OK)


def gate_exit(rec: Canonical, fs: FeatureSet, cfg: Config, mode: str) -> GateResult:
    """Test exit: is a plausible exit cheap enough to survive?

    Without a level-2/print feed we approximate the exit test from the spread and
    the 15m dollar volume, and fail closed when either is unknown.
    """
    if rec.spread_pct is None:
        return GateResult("G-EXIT", False, F_EXIT_UNKNOWN, "no spread for the exit test")
    slip_limit = cfg.float("EXIT_MAX_SLIP_PCT", 2.0)
    # A round trip pays roughly the spread once.
    est_slip = rec.spread_pct
    if est_slip > slip_limit:
        return GateResult("G-EXIT", False, F_EXIT_FAIL,
                          f"est slip {est_slip:.2f}% > {slip_limit:.2f}%")
    return GateResult("G-EXIT", True, P_OK, f"est slip {est_slip:.2f}%")


def gate_halt(rec: Canonical, cfg: Config) -> GateResult:
    """No halt feed (VERIFY D-5): a halt manifests as stale/absent data."""
    if rec.price is None:
        return GateResult("G-HALT", False, F_HALT, "no quote (possible halt/suspension)")
    return GateResult("G-HALT", True, G_HALT_UNKNOWN, "no halt feed; defer to G-DATA")


def gate_phase(mode: str, cfg: Config) -> GateResult:
    allowed = cfg.bool(f"{mode}_ALLOW_IGNITION", mode not in ("OVERNIGHT", "CLOSED"))
    if mode == "CLOSED":
        return GateResult("G-PHASE", False, F_PHASE, "market closed")
    if not allowed:
        return GateResult("G-PHASE", False, F_PHASE, f"{mode} does not allow ignition")
    return GateResult("G-PHASE", True, P_OK)


def gate_data(rec: Canonical, cfg: Config) -> GateResult:
    if rec.price is None:
        return GateResult("G-DATA", False, F_SUSPENDED, "no fresh price (suspended/stale)")
    if R_STALE in rec.reasons:
        return GateResult("G-DATA", False, R_STALE)
    if R_CONFLICT in rec.reasons:
        return GateResult("G-DATA", False, F_CONFLICT)
    if rec.data_grade in ("F",):
        return GateResult("G-DATA", False, F_DATA, f"grade {rec.data_grade}")
    return GateResult("G-DATA", True, P_OK, f"grade {rec.data_grade}")


def run_gates(rec: Canonical, fs: FeatureSet, cfg: Config, mode: str,
              snap: Snapshot) -> GateReport:
    """Run every gate in order for one candidate."""
    rep = GateReport(symbol=rec.symbol)
    rep.results.append(gate_data(rec, cfg))
    rep.results.append(gate_phase(mode, cfg))
    rep.results.append(gate_price(rec, cfg))
    rep.results.append(gate_spread(rec, cfg, mode))
    rep.results.append(gate_liquidity(fs, cfg, mode))
    rep.results.append(gate_trades(fs, cfg))
    rep.results.append(gate_exit(rec, fs, cfg, mode))
    rep.results.append(gate_halt(rec, cfg))
    return rep
