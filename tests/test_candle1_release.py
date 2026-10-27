"""Regression tests for the Candle-1 release:

  * float_shares millions-scaling (an unscaled ``8`` must mean 8M, not 8 shares)
  * the pre-ignition green-on-day guard
  * the pre-ignition 0.5% VWAP retest tolerance
  * the MIDDAY tape-velocity floor
  * the smart spread gate (unknown passes, measured-wide still fails)

No network: every fixture is built from deterministic in-memory objects.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rotd.config import Config
from rotd.features import compute_features
from rotd.gates import gate_spread, run_gates
from rotd.normalize import (Canonical, FLOAT_MILLIONS_CEILING,
                            normalize_float_shares, reconcile_symbol, PricePoint, Snapshot)
from rotd.notifier import render_ignition
from rotd.scoring import rank, score_candidate
from rotd.state_machine import (R_ARM_BELOW_VWAP, R_IGNITE_MIDDAY_TAPE,
                                R_IGNITE_RED_DAY, StateMachine, State)
from rotd.util import UTC

T0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


# --- 1. float unit normalization -----------------------------------------

def test_normalize_float_shares_scales_bare_millions():
    # The bug: 8 / 12 / 13 arrived unscaled and were read as literal shares.
    assert normalize_float_shares(8) == 8_000_000
    assert normalize_float_shares(12) == 12_000_000
    assert normalize_float_shares(13.0) == 13_000_000
    # 9,999 is still "millions" (just under the ceiling)
    assert normalize_float_shares(9_999) == 9_999 * 1_000_000


def test_normalize_float_shares_leaves_real_counts_alone():
    for v in (10_000, 250_000, 4_000_000, 250_000_000):
        assert normalize_float_shares(v) == v
    assert normalize_float_shares(None) is None
    assert normalize_float_shares(0) == 0
    assert normalize_float_shares(-5) == -5
    # the ceiling is the boundary, not an off-by-one
    assert normalize_float_shares(FLOAT_MILLIONS_CEILING) == FLOAT_MILLIONS_CEILING


def test_float_normalized_at_ingestion(tmp_path: Path):
    """A source passing float=8 must reconcile to 8,000,000 shares."""
    cfg = Config(path=tmp_path / "c.env")
    rec = reconcile_symbol(
        "ABX",
        [PricePoint(source="finviz", price=2.0, ts=T0, basis="REGULAR", scope="SINGLE_VENUE")],
        cfg, "OPEN", now=T0,
        float_sources=[("finviz", 8, T0)],
    )
    assert rec.shares_float == 8_000_000


def test_unscaled_float_no_longer_awards_free_fuel(tmp_path: Path):
    """8 shares used to max out FUEL. Normalized, 8M scores like 8M should."""
    cfg = Config(path=tmp_path / "c.env")

    def fuel_points(shares_float):
        rec = Canonical(symbol="X", price=2.0, prev_close=1.9, open=1.95,
                        volume=5e6, volume_basis="SINGLE_VENUE", bid=1.99,
                        ask=2.01, spread_pct=1.0, day_high=2.05, day_low=1.9,
                        shares_float=shares_float, short_float=20.0,
                        market_cap=16e6, data_grade="A", fetched_at=T0)
        snap = Snapshot(trading_day=T0.date(), mode="OPEN", cycle_ts=T0,
                        records={"X": rec})
        fs = compute_features(rec, cfg, snap)
        rep = run_gates(rec, fs, cfg, "OPEN", snap)
        return score_candidate(rec, fs, rep, cfg, "OPEN").components["FUEL"]

    bare = fuel_points(8)              # unscaled -> scaled to 8M by scoring
    real = fuel_points(8_000_000)      # already correct
    assert bare.raw == pytest.approx(real.raw)

    # The bug's signature: an unscaled value used to score as a TINY float and
    # therefore max out the low-float ramp. A 250M float must score far lower,
    # proving the ramp is actually reading a real share count.
    big = fuel_points(250_000_000)
    assert bare.raw > big.raw

    # And the ramp must be continuous across the scaling boundary: 9,999 means
    # 9.999B, so it cannot score better than a genuine 9.999B float.
    assert fuel_points(9_999) == pytest.approx(
        fuel_points(9_999 * 1_000_000))


def test_alert_float_line_is_human_readable(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    rec = Canonical(symbol="ABX", price=2.0, prev_close=1.9, open=1.95,
                    volume=5e6, volume_basis="SINGLE_VENUE", dollar_volume=10e6,
                    bid=1.99, ask=2.01, spread_pct=1.0, day_high=2.05,
                    day_low=1.9, shares_float=8, short_float=20.0,
                    market_cap=16e6, data_grade="A", fetched_at=T0)
    snap = Snapshot(trading_day=T0.date(), mode="OPEN", cycle_ts=T0, records={"ABX": rec})
    fs = compute_features(rec, cfg, snap)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    text = render_ignition("ABX", sc, rec, "1", "OPEN", [], now=T0)
    assert "Float 8.00M" in text
    assert "Float 8   " not in text


# --- 3. smart spread gate --------------------------------------------------

def _spread_rec(price=2.0, spread_pct=None):
    return Canonical(symbol="X", price=price, prev_close=1.9, volume=5e6,
                     volume_basis="SINGLE_VENUE", spread_pct=spread_pct,
                     data_grade="A", fetched_at=T0)


def test_spread_gate_unknown_passes_when_enabled(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    cfg._values["USE_SPREAD_GATE"] = "1"
    g = gate_spread(_spread_rec(spread_pct=None), cfg, "PRE")
    assert g.passed
    assert g.reason == "SPREAD_NOT_ENFORCED"


def test_spread_gate_still_rejects_measured_wide(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    cfg._values["USE_SPREAD_GATE"] = "1"
    g = gate_spread(_spread_rec(spread_pct=29.29), cfg, "MIDDAY")
    assert not g.passed
    assert g.reason == "SPREAD_WIDE"


def test_spread_gate_accepts_tight(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    cfg._values["USE_SPREAD_GATE"] = "1"
    g = gate_spread(_spread_rec(spread_pct=0.5), cfg, "OPEN")
    assert g.passed


def test_spread_gate_off_is_not_enforced(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")   # default: gate off
    g = gate_spread(_spread_rec(spread_pct=None), cfg, "OPEN")
    assert g.passed and g.reason == "SPREAD_NOT_ENFORCED"


# --- 2. pre-ignition guards ------------------------------------------------
#
# Harness copied from tests/test_state_machine_failsafe.py: the ladder is
# driven with the score gates relaxed and REG_CONFIRM_BARS high, so the symbol
# parks in ARMED and only the guard under test can decide ignition.

DAY = T0.date()
LIVE = {"RIME": {"vol_3m": 1_500_000.0, "dvol_10m": 20_000_000.0}}


def _rec(price: float, prev_close: float, symbol: str = "RIME") -> Canonical:
    return Canonical(symbol=symbol, price=price, prev_close=prev_close,
                     open=prev_close, volume=8_000_000,
                     volume_basis="SINGLE_VENUE", bid=price - 0.01,
                     ask=price + 0.01, spread_pct=0.9, day_high=price * 1.04,
                     day_low=prev_close, shares_float=5_000_000, short_float=22.0,
                     market_cap=10_000_000, company="Rime Inc", data_grade="A",
                     fetched_at=T0)


_HISTORY = {"prev_price": 2.00, "vwap": 1.95, "dollar_volume_15m": 5_000_000.0}


def _gate(cfg: Config, rec: Canonical, mode: str = "OPEN"):
    snap = Snapshot(trading_day=DAY, mode=mode, cycle_ts=T0,
                    records={rec.symbol: rec})
    fs = compute_features(rec, cfg, snap, catalyst_score=85.0, history=_HISTORY)
    rep = run_gates(rec, fs, cfg, mode, snap)
    sc = score_candidate(rec, fs, rep, cfg, mode)
    return snap, fs, rep, sc


def _park_armed(cfg: Config, rec: Canonical, mode: str = "OPEN",
                confirm_bars: int = 50):
    """Advance the ladder until ARMED, with ignition blocked; return the parts."""
    cfg._values["QUALIFY_CONSEC"] = "1"
    cfg._values["ARM_MIN_SCORE"] = "0"
    cfg._values["ARM_MAX_RANK"] = "5"
    cfg._values["IGNITION_THRESHOLD"] = "0"
    cfg._values[f"{mode}_ALLOW_IGNITION"] = "1"
    cfg._values["REG_CONFIRM_BARS"] = str(confirm_bars)
    cfg._values["EXT_CONFIRM_BARS"] = str(confirm_bars)
    sm = StateMachine(cfg, DAY)
    snap, fs, rep, sc = _gate(cfg, rec, mode)
    assert rep.passed, rep.summary()
    rk = rank([sc], cfg)
    prices = {rec.symbol: rec.price}
    for i in range(6):
        ts = T0 + timedelta(seconds=30 * i)
        sm.step(rk, snap.records, mode, now=ts, prices=prices,
                vwaps={rec.symbol: _HISTORY["vwap"]}, live=LIVE)
    assert sm.states[rec.symbol].state == State.ARMED, sm.states[rec.symbol].state
    return sm, rk, snap, prices


def _ignite_attempts(sm, rk, snap, prices, vwap, n=4, mode="OPEN"):
    """Set confirm bars to 1, then try to ignite; return the transitions."""
    sm.cfg._values["REG_CONFIRM_BARS"] = "1"
    sm.cfg._values["EXT_CONFIRM_BARS"] = "1"
    out = []
    for i in range(n):
        ts = T0 + timedelta(minutes=5 + i)
        out.extend(sm.step(rk, snap.records, mode, now=ts, prices=prices,
                           vwaps={"RIME": vwap}, live=LIVE))
    return out


def test_red_on_day_blocks_ignition(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    rec = _rec(price=1.95, prev_close=2.00)          # RED on the day
    sm, rk, snap, prices = _park_armed(cfg, rec)
    out = _ignite_attempts(sm, rk, snap, prices, vwap=1.80)
    assert sm.states["RIME"].state != State.IGNITION
    assert any(t.reason == R_IGNITE_RED_DAY for t in out), [t.reason for t in out]


def test_green_on_day_can_ignite(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    rec = _rec(price=2.10, prev_close=2.00)          # GREEN
    sm, rk, snap, prices = _park_armed(cfg, rec)
    out = _ignite_attempts(sm, rk, snap, prices, vwap=1.95)
    assert sm.states["RIME"].state == State.IGNITION, [t.reason for t in out]


def test_vwap_retest_tolerance_allows_half_percent(tmp_path: Path):
    """A wick 0.3% under VWAP is a retest of support, not a breakdown."""
    cfg = Config(path=tmp_path / "c.env")
    rec = _rec(price=2.10, prev_close=2.00)
    sm, rk, snap, prices = _park_armed(cfg, rec)
    vwap = 2.10
    out = _ignite_attempts(sm, rk, snap, prices, vwap=vwap)
    assert not any(t.reason == R_ARM_BELOW_VWAP for t in out), [t.reason for t in out]
    assert sm.states["RIME"].state == State.IGNITION


def test_below_vwap_tolerance_blocks(tmp_path: Path):
    """1.5% under VWAP is a genuine loss of the line."""
    cfg = Config(path=tmp_path / "c.env")
    rec = _rec(price=2.10, prev_close=2.00)
    sm, rk, snap, prices = _park_armed(cfg, rec)
    vwap = 2.14                                  # price 1.9% below VWAP
    out = _ignite_attempts(sm, rk, snap, prices, vwap=vwap)
    assert sm.states["RIME"].state != State.IGNITION
    assert any(t.reason == R_ARM_BELOW_VWAP for t in out), [t.reason for t in out]


# --- 4. MIDDAY tape floor --------------------------------------------------

def test_midday_tape_floor_setting_exists(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    assert cfg.float("MIDDAY_MIN_TAPE_RAW") == pytest.approx(0.72)


def test_midday_low_tape_is_refused(tmp_path: Path):
    """A weak-tape MIDDAY grind must not ignite even at a high total score."""
    cfg = Config(path=tmp_path / "c.env")
    rec = _rec(price=2.10, prev_close=2.00)
    sm, rk, snap, prices = _park_armed(cfg, rec, mode="MIDDAY")
    for sc in rk.day_ranked:
        tape = sc.components.get("TAPE")
        if tape is not None:
            tape.raw = 0.65                      # the reported stair-step case
    out = _ignite_attempts(sm, rk, snap, prices, vwap=1.95, mode="MIDDAY")
    assert sm.states["RIME"].state != State.IGNITION
    assert any(t.reason == R_IGNITE_MIDDAY_TAPE for t in out), [t.reason for t in out]


def test_midday_strong_tape_still_ignites(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    rec = _rec(price=2.10, prev_close=2.00)
    sm, rk, snap, prices = _park_armed(cfg, rec, mode="MIDDAY")
    for sc in rk.day_ranked:
        tape = sc.components.get("TAPE")
        if tape is not None:
            tape.raw = 0.90                      # breaking-news rocket
    out = _ignite_attempts(sm, rk, snap, prices, vwap=1.95, mode="MIDDAY")
    assert sm.states["RIME"].state == State.IGNITION, [t.reason for t in out]


def test_midday_floor_disabled_by_zero(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    cfg._values["MIDDAY_MIN_TAPE_RAW"] = "0"
    rec = _rec(price=2.10, prev_close=2.00)
    sm, rk, snap, prices = _park_armed(cfg, rec, mode="MIDDAY")
    for sc in rk.day_ranked:
        tape = sc.components.get("TAPE")
        if tape is not None:
            tape.raw = 0.10
    out = _ignite_attempts(sm, rk, snap, prices, vwap=1.95, mode="MIDDAY")
    assert not any(t.reason == R_IGNITE_MIDDAY_TAPE for t in out)


# --- score gates -----------------------------------------------------------

def test_production_score_gates_match(tmp_path: Path):
    """IGNITION_THRESHOLD must never fall below ARM_MIN_SCORE.

    IGNITION_THRESHOLD is the last gate before the alert, so a candidate must
    not qualify for ARMED at one score and then ignite on a weaker one — that is
    exactly the "armed on stale strength" failure the fail-closed ARMED logic
    exists to prevent.
    """
    cfg = Config(path=tmp_path / "c.env")
    arm = cfg.float("ARM_MIN_SCORE")
    ignite = cfg.float("IGNITION_THRESHOLD")
    assert arm == 70
    assert ignite == 70
    assert ignite >= arm, f"ignition {ignite} < arm {arm}"


def test_arming_then_firing_at_a_lower_score_is_blocked(tmp_path: Path):
    """Drive a name to ARMED, drop its score below ARM_MIN_SCORE, confirm no ignition.

    The candidate here scores ~53 with FUEL and CONTEXT de-weighted, so it can
    register as a candidate but cannot clear either production score gate.
    """
    cfg = Config(path=tmp_path / "c.env")
    # Read the production floors before _park_armed zeroes them to reach ARMED.
    floor = cfg.float("ARM_MIN_SCORE")
    ignite = cfg.float("IGNITION_THRESHOLD")
    cfg._values["W_FUEL"] = "0"        # shed ~30 raw points; the tape is intact
    cfg._values["W_CONTEXT"] = "0"
    rec = _rec(price=2.10, prev_close=2.00)
    sm, rk, snap, prices = _park_armed(cfg, rec)
    # Restore the real thresholds now that the name is parked ARMED: at this
    # score it must not ignite, and must not stay armed either.
    sm.cfg._values["IGNITION_THRESHOLD"] = str(ignite)
    sm.cfg._values["ARM_MIN_SCORE"] = str(floor)
    out = _ignite_attempts(sm, rk, snap, prices, vwap=1.95, n=2)
    assert sm.states["RIME"].state != State.IGNITION, [t.reason for t in out]
    assert any(t.reason == "ARM_STEP_DOWN_SCORE" for t in out), [t.reason for t in out]
