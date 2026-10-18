"""ARMED must fail closed: a demoted name cannot ignite on stale strength.

The ARMED state promises "this is a top name right now". While it waits for its
confirmation bars, rank and score are re-checked every cycle. If either slips
below the arm thresholds the name steps down (to BOARD, or QUALIFIED if it also
left the board) and its confirmation counter resets, so a name that loses its
standing can never be confirmed into an IGNITION it no longer deserves.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from rotd.config import Config
from rotd.features import compute_features
from rotd.gates import run_gates
from rotd.normalize import Canonical
from rotd.scoring import rank, score_candidate
from rotd.state_machine import (R_ARM_STEP_RANK, R_ARM_STEP_SCORE,
                                R_ARM_STEP_VOL, State, StateMachine)
from rotd.util import UTC

DAY = date(2026, 10, 13)
T0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
# Healthy live tape: the hard volume gate must not be what these tests exercise.
LIVE = {"EXMP": {"vol_3m": 1_000_000.0, "dvol_10m": 10_000_000.0}}


def _rec(symbol="EXMP", price=2.31) -> Canonical:
    return Canonical(symbol=symbol, price=price, prev_close=1.90, open=2.00,
                     volume=3_000_000, volume_basis="SINGLE_VENUE",
                     bid=price - 0.01, ask=price + 0.01, spread_pct=0.9,
                     day_high=price * 1.04, day_low=1.98, shares_float=4_000_000,
                     short_float=22.0, market_cap=9_240_000,
                     company="Exemplar Inc", data_grade="A",
                     fetched_at=T0)


_HISTORY = {"prev_price": 2.20, "vwap": 2.10, "dollar_volume_15m": 5_000_000.0}


def _snap(rec: Canonical):
    from rotd.normalize import Snapshot
    return Snapshot(trading_day=DAY, mode="OPEN", cycle_ts=T0, records={rec.symbol: rec})


def _gate(cfg: Config, rec: Canonical):
    snap = _snap(rec)
    fs = compute_features(rec, cfg, snap, catalyst_score=85.0, history=_HISTORY)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    return snap, fs, rep, sc


def _armed_machine(cfg: Config):
    """Drive one symbol up the ladder until it is ARMED, and return the machine.

    ``REG_CONFIRM_BARS`` is set high so the name parks in ARMED: this is the
    pending window we are testing, and it must not reach IGNITION during setup.
    """
    cfg._values["QUALIFY_CONSEC"] = "1"
    cfg._values["ARM_MIN_SCORE"] = "0"
    cfg._values["ARM_MAX_RANK"] = "5"
    cfg._values["REG_CONFIRM_BARS"] = "50"
    sm = StateMachine(cfg, DAY)
    rec = _rec()
    snap, fs, rep, sc = _gate(cfg, rec)
    assert rep.passed, rep.summary()
    rk = rank([sc], cfg)
    prices = {rec.symbol: rec.price}
    states = []
    for i in range(6):
        ts = T0 + timedelta(seconds=30 * i)
        for t in sm.step(rk, snap.records, "OPEN", now=ts, prices=prices, live=LIVE):
            states.append(t.new_state)
    assert State.ARMED in states, states
    assert sm.states[rec.symbol].state == State.ARMED
    return sm, rec, snap, rk, prices


def test_armed_steps_down_when_rank_slips(cfg: Config):
    sm, rec, snap, rk, prices = _armed_machine(cfg)
    # A competitor ranks above it: arm needs rank <= 2, so 3 is a demotion.
    cfg._values["ARM_MAX_RANK"] = "2"
    from rotd.scoring import Ranking, Scored
    sc_ours = next(s for s in rk.day_ranked if s.symbol == rec.symbol)
    higher = [Scored(symbol=s, score=99.0, rank=i + 1, components={})
              for i, s in enumerate(["AAA", "BBB"])]
    ours = Scored(symbol=rec.symbol, score=sc_ours.score, rank=3,
                  components=sc_ours.components)
    forced = Ranking(day_ranked=higher + [ours], board=higher + [ours])
    ts = T0 + timedelta(seconds=330)
    transitions = sm.step(forced, snap.records, "OPEN", now=ts, prices=prices, live=LIVE)
    demotions = [t for t in transitions
                 if t.symbol == rec.symbol and t.reason == R_ARM_STEP_RANK]
    assert demotions, [(t.symbol, t.reason, t.new_state) for t in transitions]
    assert sm.states[rec.symbol].state != State.ARMED
    assert sm.states[rec.symbol].confirm_bars == 0


def test_armed_steps_down_when_score_slips(cfg: Config):
    sm, rec, snap, rk, prices = _armed_machine(cfg)
    # Now demand a score the name cannot meet.
    cfg._values["ARM_MIN_SCORE"] = "1000"
    ts = T0 + timedelta(seconds=330)
    transitions = sm.step(rk, snap.records, "OPEN", now=ts, prices=prices, live=LIVE)
    demotions = [t for t in transitions
                 if t.symbol == rec.symbol and t.reason == R_ARM_STEP_SCORE]
    assert demotions, [(t.symbol, t.reason) for t in transitions]
    assert sm.states[rec.symbol].state != State.ARMED
    assert sm.states[rec.symbol].confirm_bars == 0


def test_demoted_name_does_not_accrue_confirmation(cfg: Config):
    """While demoted, the confirm counter stays 0, so no ignition can sneak in."""
    sm, rec, snap, rk, prices = _armed_machine(cfg)
    cfg._values["ARM_MIN_SCORE"] = "1000"
    for i in range(4):
        ts = T0 + timedelta(seconds=330 + 30 * i)
        transitions = sm.step(rk, snap.records, "OPEN", now=ts, prices=prices, live=LIVE)
        assert not any(t.symbol == rec.symbol and t.new_state == State.IGNITION
                       for t in transitions)
        assert sm.states[rec.symbol].confirm_bars == 0


def test_healthy_armed_name_still_ignites(cfg: Config):
    """The safeguard must not block a name that keeps its rank and score."""
    cfg._values["QUALIFY_CONSEC"] = "1"
    cfg._values["ARM_MIN_SCORE"] = "0"
    cfg._values["ARM_MAX_RANK"] = "5"
    cfg._values["REG_CONFIRM_BARS"] = "2"
    sm = StateMachine(cfg, DAY)
    rec = _rec()
    snap, fs, rep, sc = _gate(cfg, rec)
    assert rep.passed, rep.summary()
    rk = rank([sc], cfg)
    prices = {rec.symbol: rec.price}
    ignited = False
    for i in range(8):
        ts = T0 + timedelta(seconds=30 * i)
        for t in sm.step(rk, snap.records, "OPEN", now=ts, prices=prices, live=LIVE):
            if t.symbol == rec.symbol and t.new_state == State.IGNITION:
                ignited = True
    assert ignited, "a name holding its rank/score must still ignite"


def test_armed_steps_down_when_live_volume_dies(cfg: Config):
    """Tape that goes quiet mid-confirmation is a demotion, not a slow ignition."""
    sm, rec, snap, rk, prices = _armed_machine(cfg)
    dead = {"EXMP": {"vol_3m": 100.0, "dvol_10m": 1_000.0}}
    ts = T0 + timedelta(seconds=330)
    transitions = sm.step(rk, snap.records, "OPEN", now=ts, prices=prices, live=dead)
    demotions = [t for t in transitions
                 if t.symbol == rec.symbol and t.reason == R_ARM_STEP_VOL]
    assert demotions, [(t.symbol, t.reason) for t in transitions]
    assert sm.states[rec.symbol].state != State.ARMED
    assert sm.states[rec.symbol].confirm_bars == 0


def test_no_volume_history_never_arms(cfg: Config):
    """Fail closed: with no live-volume history a name can never reach ARMED."""
    cfg._values["QUALIFY_CONSEC"] = "1"
    cfg._values["ARM_MIN_SCORE"] = "0"
    cfg._values["ARM_MAX_RANK"] = "5"
    sm = StateMachine(cfg, DAY)
    rec = _rec()
    snap, fs, rep, sc = _gate(cfg, rec)
    rk = rank([sc], cfg)
    prices = {rec.symbol: rec.price}
    states = []
    for i in range(6):
        ts = T0 + timedelta(seconds=30 * i)
        for t in sm.step(rk, snap.records, "OPEN", now=ts, prices=prices):
            states.append((t.new_state, t.reason))
    assert not any(s == State.ARMED for s, _ in states), states
    assert any(r == "NO_VOL_HISTORY" for _, r in states), states
