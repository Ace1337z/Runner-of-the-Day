"""Config tuning + AI-report sanitizer + structure-gate regression tests.

No network. Pins the settings the live pre-market tuning depends on and the
front-side structure protection that keeps dying double-tops from alerting.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from rotd.auditor import BundleInputs, build_fact_bundle, validate_report
from rotd.config import Config
from rotd.util import UTC

DAY_S = date(2026, 10, 21)
T0_S = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)


def test_ignition_threshold_and_ai_timeout_have_defaults(cfg: Config):
    assert cfg.int("IGNITION_THRESHOLD", 70) == 70
    assert cfg.float("AI_TIMEOUT_S", 30) == 12.0
    assert cfg.float("ARM_MIN_SCORE", 68) == 70.0
    assert cfg.float("CHASE_CAP_PCT", 25.0) == 18.0


def test_structure_gate_defaults(cfg: Config):
    # Front-side protection defaults: the 2.0/15 structure floor (lowered from
    # 4.0 so beaten-down penny setups with strong tape can still arm) and a 15%
    # HOD drawdown threshold (the sub-score is zeroed at/over it).
    assert cfg.float("MIN_STRUCTURE_SCORE", 4.0) == 2.0
    assert cfg.float("MAX_DRAWDOWN_FROM_HOD_PCT", 15.0) == 15.0


def test_config_env_overrides_defaults(tmp_path: Path):
    env = tmp_path / "rotd.env"
    env.write_text("IGNITION_THRESHOLD=85\nAI_TIMEOUT_S=60\nARM_MIN_SCORE=80\n")
    c = Config(path=env)
    assert c.int("IGNITION_THRESHOLD", 70) == 85
    assert c.float("AI_TIMEOUT_S", 30) == 60.0  # file value wins
    assert c.float("ARM_MIN_SCORE", 68) == 80.0


def test_sanitize_fixes_ticker_and_invented_subpath(tmp_path: Path):
    cfg = Config(path=tmp_path / "e.env")
    inp = BundleInputs(
        symbol="EXMP", company="Exemplar Inc", gate_passed=True,
        rules_verdict="ENTER", price=2.31, time_et="14:00", session="OPEN")
    bundle = build_fact_bundle(inp, cfg)
    assert bundle["meta"]["ticker"] == "EXMP"

    # A well-formed decision_v3 report, except:
    #  - ticker says the company name (a SPAC variant) instead of EXMP
    #  - identity.symbol says the same wrong value
    #  - strongest_risk refs an invented sub-path under context.market
    report = {
        "schema": "decision_v3",
        "ticker": "Southern Cross Acquisition II Corp",
        "identity": {"symbol": "Southern Cross Acquisition II Corp"},
        "verdict": "NO_TRADE", "rating": "STRONG", "confidence_band": "HIGH",
        "confirmation_mode": "CONTINUATION",
        "cause": {"kind": "NEWS", "catalyst_type": "FDA_REGULATORY",
                  "substance": "FILED", "materiality": "YES", "promo_flag": False},
        "catalyst_quality": "REAL", "supply_state": "CLEAN",
        "caps_applied": [],
        "rubric": {c: "PASS" for c in (
            "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11", "R12")},
        "strongest_risk": {"text": "x", "refs": ["context.market.regime.nope"]},
        "invalidation_beyond_rules": {"text": "y", "refs": ["tape.vwap_state"]},
        "contradictions": [],
        "reason_refs": ["tape.volx_15m"],
        "levels": None,
    }
    rep, failures = validate_report(dict(report), bundle, cfg)
    assert failures == [], failures
    assert rep is not None
    assert rep.ticker == "EXMP"
    # The invented sub-path was shortened to its valid parent.
    assert report["strongest_risk"]["refs"][0] == "context.market.regime" or \
        report["strongest_risk"]["refs"][0] == "context.market"


def test_candidates_overwrite_per_symbol_not_per_tick(tmp_path: Path):
    from rotd.store import Store
    from rotd.util import now_utc

    store = Store(tmp_path / "r.db")
    day = date(2026, 10, 13)
    t0 = now_utc()
    # Two ticks for the same symbol/mode/day: only the latest row survives.
    store.save_candidate(day, "OPEN", t0, "EXMP", 80.0, 1, True, {"g": {}}, {"f": {}})
    store.save_candidate(day, "OPEN", t0 + timedelta(seconds=5),
                         "EXMP", 82.0, 1, True, {"g": {}}, {"f": {}})
    rows = store.query(
        "SELECT symbol, score FROM candidates WHERE trading_day=? AND mode=? AND symbol=?",
        (day.isoformat(), "OPEN", "EXMP"))
    assert len(rows) == 1, "per-symbol overwrite must leave exactly one row per tick"
    assert rows[0]["score"] == 82.0
    store.close()


def test_retention_defaults_lowered(tmp_path: Path):
    c = Config(path=tmp_path / "e.env")
    assert c.int("CAND_RETENTION_HOURS", 2) == 1
    assert c.int("RAW_RETENTION_HOURS", 2) == 1
    assert c.int("DB_PRUNE_INTERVAL_S", 300) == 300


# --- structure gate: dying double-tops must never alert ---------------------

def _prso_like(price: float, day_high: float, day_low: float):
    """A heavy-tape low-float runner; price vs day_high sets how extended it is."""
    from rotd.normalize import Canonical
    return Canonical(symbol="PRSO", price=price, prev_close=0.975, open=1.10,
                     volume=40_000_000, volume_basis="SINGLE_VENUE",
                     bid=price - 0.01, ask=price + 0.01, spread_pct=0.9,
                     day_high=day_high, day_low=day_low, shares_float=3_000_000,
                     short_float=8.0, market_cap=25_000_000, company="PRSO Inc",
                     data_grade="A", fetched_at=T0_S)


def _scored(tmp_path: Path, rec):
    """Gate + score one record; returns (cfg, snapshot, scored)."""
    from rotd.features import compute_features
    from rotd.gates import run_gates
    from rotd.normalize import Snapshot
    from rotd.scoring import score_candidate

    cfg = Config(path=tmp_path / "e.env")
    snap = Snapshot(trading_day=DAY_S, mode="OPEN", cycle_ts=T0_S,
                    records={rec.symbol: rec})
    fs = compute_features(rec, cfg, snap)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    return cfg, snap, score_candidate(rec, fs, rep, cfg, "OPEN")


def test_structure_zero_when_drawdown_reaches_threshold(tmp_path: Path):
    # 20% off HOD (threshold 15%): the structure sub-score must be a hard zero so
    # heavy tape/fuel cannot carry a dying double-top over the ignition gate.
    _, _, sc = _scored(tmp_path, _prso_like(1.28, 1.60, 1.05))
    st = sc.components["STRUCTURE"]
    assert st.raw == 0.0 and st.points == 0.0


def test_frontside_name_clears_structure_gate(tmp_path: Path):
    # Price at/near HOD: no fade, so the structure sub-score stays comfortably
    # above the MIN_STRUCTURE_SCORE floor and the name remains armable.
    cfg, _, sc = _scored(tmp_path, _prso_like(1.58, 1.60, 1.05))
    st = sc.components["STRUCTURE"]
    floor = cfg.float("MIN_STRUCTURE_SCORE", 4.0)
    assert st.raw is not None and st.raw > 0
    assert st.points > floor


def test_min_structure_gate_blocks_armed(tmp_path: Path):
    from rotd.features import compute_features
    from rotd.gates import run_gates
    from rotd.normalize import Snapshot
    from rotd.scoring import rank, score_candidate
    from rotd.state_machine import R_ARM_STRUCT, State, StateMachine

    cfg = Config(path=tmp_path / "e.env")
    cfg._values["QUALIFY_CONSEC"] = "1"
    cfg._values["ARM_MIN_SCORE"] = "0"
    cfg._values["ARM_MAX_RANK"] = "5"
    cfg._values["IGNITION_THRESHOLD"] = "0"
    rec = _prso_like(1.28, 1.60, 1.05)  # 20% off HOD, heavy tape, tiny float
    snap = Snapshot(trading_day=DAY_S, mode="OPEN", cycle_ts=T0_S,
                    records={"PRSO": rec})
    fs = compute_features(rec, cfg, snap, catalyst_score=85.0,
                          history={"prev_price": 1.0, "vwap": 1.05,
                                   "dollar_volume_15m": 8_000_000.0})
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    assert sc.components["STRUCTURE"].points < 4.0
    sm = StateMachine(cfg, DAY_S)
    live = {"PRSO": {"vol_3m": 900_000.0, "dvol_10m": 900_000.0}}
    out = sm.step(rank([sc], cfg), snap.records, "OPEN", now=T0_S,
                  prices={"PRSO": 1.28}, live=live)
    reasons = [(t.symbol, t.reason) for t in out]
    assert not any(t.new_state == State.ARMED for t in out), reasons
    # The ladder stops specifically on the structure floor (not silently).
    assert ("PRSO", R_ARM_STRUCT) in reasons, reasons


def test_structure_rollover_steps_down_mid_confirmation(tmp_path: Path):
    """A name parked in ARMED whose chart rolls over mid-confirmation steps back."""
    from rotd.features import compute_features
    from rotd.gates import run_gates
    from rotd.normalize import Snapshot
    from rotd.scoring import rank, score_candidate
    from rotd.state_machine import R_ARM_STEP_STRUCT, State, StateMachine

    cfg = Config(path=tmp_path / "e.env")
    cfg._values["QUALIFY_CONSEC"] = "1"
    cfg._values["ARM_MIN_SCORE"] = "0"
    cfg._values["ARM_MAX_RANK"] = "5"
    cfg._values["IGNITION_THRESHOLD"] = "0"
    cfg._values["REG_CONFIRM_BARS"] = "50"  # park in ARMED while confirming
    sm = StateMachine(cfg, DAY_S)
    live = {"PRSO": {"vol_3m": 900_000.0, "dvol_10m": 900_000.0}}

    # Cycle until armed while the chart is still front-side (at HOD).
    rec = _prso_like(1.58, 1.60, 1.05)
    snap = Snapshot(trading_day=DAY_S, mode="OPEN", cycle_ts=T0_S,
                    records={"PRSO": rec})
    fs = compute_features(rec, cfg, snap, catalyst_score=85.0,
                          history={"prev_price": 1.0, "vwap": 1.55,
                                   "dollar_volume_15m": 8_000_000.0})
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    rk = rank([sc], cfg)
    for i in range(4):
        sm.step(rk, snap.records, "OPEN", now=T0_S + timedelta(seconds=30 * i),
                prices={"PRSO": 1.58}, live=live)
    assert sm.states["PRSO"].state == State.ARMED, sm.states["PRSO"].state

    # Now the name rolls over: 20% off HOD zeroes STRUCTURE, so the confirmation
    # window is revoked rather than allowed to drift into a stale ignition.
    rec.price = 1.60 * (1 - 0.20)
    fs = compute_features(rec, cfg, snap, catalyst_score=85.0,
                          history={"prev_price": 1.0, "vwap": 1.55,
                                   "dollar_volume_15m": 8_000_000.0})
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    assert sc.components["STRUCTURE"].points == 0.0
    out = sm.step(rank([sc], cfg), snap.records, "OPEN",
                  now=T0_S + timedelta(seconds=300),
                  prices={"PRSO": rec.price}, live=live)
    reasons = [(t.symbol, t.reason) for t in out]
    assert ("PRSO", R_ARM_STEP_STRUCT) in reasons, reasons
    assert sm.states["PRSO"].state in (State.BOARD, State.QUALIFIED), reasons
