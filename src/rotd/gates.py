"""Hard gates (build prompt §7).

A gate is a boolean with a reason code. Gates run *before* scoring: a name that
fails any gate is not scored and cannot alert. Fail closed — if a gate's inputs
are missing, the gate **fails** with a ``*_UNKNOWN`` / ``*_MISSING`` reason rather
than passing.

Gates:

- ``G-PRICE``   — sub-$10 (``MAX_PRICE``); no price floor (§18.1).
- ``G-SPREAD``  — quoted spread within the price-tier limit
  (``SPREAD_TIERS``), loosened by ``EXT_SPREAD_MULT`` in POST/PRE. **OFF by
  default** (``USE_SPREAD_GATE=0``): VERIFIED G-18 shows no source carries a
  bid/ask book, so it reports ``SPREAD_NOT_ENFORCED`` instead of failing closed.
- ``G-LIQ``     — 15-minute dollar volume >= the mode floor (``LIQ_15M_*``).
  **OFF by default** (``USE_LIQ_GATE=0``): needs a 1-min bar feed this
  deployment lacks; reports ``LIQUIDITY_NOT_ENFORCED``.
- ``G-TRADES``  — enough prints at a sane average size; skipped when the feed
  cannot supply prints (``MIN_TRADES_15M=0`` means "not enforced", but the gate
  then reports ``TRADES_NOT_ENFORCED`` so the MISS REPORT can say so).
- ``G-EXIT``    — a test exit of ``EXIT_TEST_USD`` would fill within
  ``EXIT_MAX_SEC`` and ``EXIT_MAX_SLIP_PCT``. **OFF by default**
  (``USE_EXIT_GATE=0``): needs a spread/slippage estimate (a book) this
  deployment lacks; reports ``EXIT_NOT_ENFORCED``.
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
G_SPREAD_NOT_ENFORCED = "SPREAD_NOT_ENFORCED"
G_LIQ_NOT_ENFORCED = "LIQUIDITY_NOT_ENFORCED"
G_EXIT_NOT_ENFORCED = "EXIT_NOT_ENFORCED"
F_EXIT_FAIL = "EXIT_TEST_FAILED"
F_EXIT_UNKNOWN = "EXIT_UNKNOWN"
F_HALT = "HALTED_SUSPENDED"
G_HALT_UNKNOWN = "HALT_UNKNOWN"
F_PHASE = "PHASE_NOT_ALLOWED"
F_DATA = "DATA_QUALITY_LOW"
F_SUSPENDED = R_SUSPENDED
F_CONFLICT = R_CONFLICT

# Hard live-volume gate reasons (section 2). Fail closed: whole-day volume or a
# strong structure score can never substitute for tape trading RIGHT NOW.
R_NO_VOL_HISTORY = "NO_VOL_HISTORY"
R_RECENT_VOL_TOO_LOW = "RECENT_VOL_TOO_LOW"
R_DVOL_10M_TOO_LOW = "DVOL_10M_TOO_LOW"
R_BACKSIDE_OF_HOD = "BACKSIDE_OF_HOD"

# Warmup fallback: the 10-minute window is ~3.33x the 3-minute one, but the
# estimate uses 2.0 on purpose. A gapper must still out-trade a floor twice its
# own 3-minute dollars, so the fallback cannot carry a marginal name over the
# hurdle on a generous projection.
DVOL_10M_WARMUP_SCALE = 2.0


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
    max_mcap = cfg.float("MAX_MARKET_CAP", 0.0)
    if max_mcap > 0:
        # Market-cap scope (Upgrade 6): a $100M cap is the universe ceiling and
        # share price no longer restricts (high-priced micro-caps qualify). Fail
        # closed: an unknown market cap cannot pass G-PRICE.
        if rec.market_cap is None:
            return GateResult("G-PRICE", False, F_PRICE_UNKNOWN,
                              "no market cap; cannot confirm scope")
        if rec.market_cap > max_mcap:
            return GateResult("G-PRICE", False, F_PRICE_TOO_HIGH,
                              f"mcap {rec.market_cap:,.0f} > {max_mcap:,.0f}")
        return GateResult("G-PRICE", True, P_OK, f"mcap {rec.market_cap:,.0f} <= {max_mcap:,.0f}")
    if rec.price > cfg.float("MAX_PRICE", 10.0):
        return GateResult("G-PRICE", False, F_PRICE_TOO_HIGH,
                          f"{rec.price:.4f} > {cfg.float('MAX_PRICE', 10.0):.2f}")
    return GateResult("G-PRICE", True, P_OK)


def gate_spread(rec: Canonical, cfg: Config, mode: str) -> GateResult:
    # VERIFIED G-18: no configured source carries a bid/ask book, so the spread
    # input is structurally unavailable. When the gate is off it is NOT_ENFORCED
    # (passes, like G-TRADES) and the MISS REPORT lists it. Enable USE_SPREAD_GATE
    # the moment a book feed exists (e.g. Alpaca unblocked).
    if not cfg.bool("USE_SPREAD_GATE"):
        return GateResult("G-SPREAD", True, G_SPREAD_NOT_ENFORCED,
                          "no bid/ask feed; gate not enforced (VERIFIED G-18)")
    if rec.price is None:
        return GateResult("G-SPREAD", False, F_PRICE_UNKNOWN)
    # A missing book is NOT a wide spread. Extended/pre-market feeds routinely
    # carry no L1 quote (Finviz "Spread n/a"), and failing closed there blocked
    # valid pre-market runners while the gate was on. So: enforce a measured
    # spread strictly, and pass an UNKNOWN spread as NOT_ENFORCED (visible, not
    # silently accepted). A genuinely wide spread is still rejected below.
    if rec.spread_pct is None:
        return GateResult("G-SPREAD", True, G_SPREAD_NOT_ENFORCED,
                          "no bid/ask; passing unknown spread")
    limit = _spread_limit(rec.price, cfg, mode)
    if limit is None:
        return GateResult("G-SPREAD", False, F_SPREAD_UNKNOWN)
    if rec.spread_pct > limit:
        return GateResult("G-SPREAD", False, F_SPREAD_WIDE,
                          f"{rec.spread_pct:.2f}% > {limit:.2f}%")
    return GateResult("G-SPREAD", True, P_OK, f"{rec.spread_pct:.2f}% <= {limit:.2f}%")


def gate_liquidity(fs: FeatureSet, cfg: Config, mode: str) -> GateResult:
    # VERIFIED G-18: 15-min dollar volume needs a 1-min bar feed (Alpaca
    # prepost is 403 here), which this deployment does not have. When off the
    # gate is NOT_ENFORCED (passes) and the MISS REPORT lists it. Enable
    # USE_LIQ_GATE once a bar feed supplies dollar_volume_15m.
    if not cfg.bool("USE_LIQ_GATE"):
        return GateResult("G-LIQ", True, G_LIQ_NOT_ENFORCED,
                          "no 15m bar feed; gate not enforced (VERIFIED G-18)")
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

    VERIFIED G-18: the exit test needs a spread/slippage estimate, which needs a
    bid/ask book this deployment does not have. When off the gate is
    NOT_ENFORCED (passes) and the MISS REPORT lists it. Enable USE_EXIT_GATE
    once a book feed exists (e.g. Alpaca unblocked).
    """
    if not cfg.bool("USE_EXIT_GATE"):
        return GateResult("G-EXIT", True, G_EXIT_NOT_ENFORCED,
                          "no bid/ask feed; gate not enforced (VERIFIED G-18)")
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


def live_volume_reason(vol_3m: Optional[float], dvol_10m: Optional[float],
                       cfg: Config, mode: str,
                       price: Optional[float] = None) -> Optional[str]:
    """The hard live-volume reason code, or None when the tape is alive.

    Fail closed on warmup (``vol_3m is None``) and on dead tape. One helper is
    used by both the BOARD->ARMED and ARMED->IGNITION checks so the two can never
    disagree about what "alive" means.

    ``price`` is only needed for the 10-minute warmup fallback. ``dvol_10m`` is
    a rolling 10-minute delta, so on the first candles after the 4:00 AM open it
    is ``None`` (deque too shallow) or ``0.0`` (no earlier sample to subtract).
    Failing closed on that history gap blocked valid pre-market runners for the
    full 10 minutes, so when the delta is unusable the dollar volume is
    estimated from the already-vetted 3-minute tape instead. The session floor
    still applies — this changes how the input is measured, never whether a
    floor is enforced.
    """
    if vol_3m is None:
        return R_NO_VOL_HISTORY
    if vol_3m <= 0 or vol_3m < cfg.float("MIN_VOL_3M_SHARES", 15000):
        return R_RECENT_VOL_TOO_LOW
    key = "MIN_DVOL_10M_EXT" if mode in ("PRE", "POST", "OVERNIGHT") else "MIN_DVOL_10M_REG"
    floor = cfg.float(key, 15000 if key.endswith("EXT") else 40000)
    # Warmup fallback for a 4:00 AM gapper (Candle-1). ``vol_3m`` is already
    # vetted above, so an unusable 10m delta is estimated from the live
    # 3-minute tape instead of failing closed on the history gap.
    effective_dvol = dvol_10m
    if (effective_dvol is None or effective_dvol <= 0) and price is not None:
        if price > 0:
            effective_dvol = vol_3m * price * DVOL_10M_WARMUP_SCALE
    if effective_dvol is not None and effective_dvol < floor:
        return R_DVOL_10M_TOO_LOW
    return None


def backside_of_hod(price: Optional[float], day_high: Optional[float],
                    cfg: Config) -> bool:
    """True when price has faded more than ``MAX_DRAWDOWN_FROM_HOD_PCT`` off HOD."""
    if not price or not day_high:
        return False
    max_dd = cfg.float("MAX_DRAWDOWN_FROM_HOD_PCT", 15.0)
    return max_dd > 0 and price < day_high * (1.0 - max_dd / 100.0)


def gates_not_enforced(cfg: Config) -> list[str]:
    """Gate names currently OFF by default (no feed supplies their input).

    Surfaced in the MISS REPORT so the daily post-mortem honestly lists the
    system's blind spots (VERIFIED G-18). When a book/15m feed is added, set
    the matching ``USE_*_GATE`` flag and the gate stops appearing here.
    """
    out = []
    if not cfg.bool("USE_SPREAD_GATE"):
        out.append("G-SPREAD (needs a bid/ask book)")
    if not cfg.bool("USE_LIQ_GATE"):
        out.append("G-LIQ (needs a 15-min bar feed)")
    if not cfg.bool("USE_EXIT_GATE"):
        out.append("G-EXIT (needs a bid/ask book)")
    return out


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
