"""Acceptance invariants (build prompt §16) exercised end-to-end, offline.

These are the properties the system must always hold. Each test names the
invariant it pins. No test touches the network.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from rotd.auditor import (AuditorService, BundleInputs, build_fact_bundle,
                          mechanical_verdict, rules_only_report, validate_report)
from rotd.config import Config
from rotd.features import compute_features
from rotd.gates import run_gates
from rotd.normalize import (R_CONFLICT, R_STALE, BASIS_EXTENDED, BASIS_REGULAR,
                            Canonical, PricePoint, Snapshot, build_snapshot,
                            choose_basis, reconcile_symbol)
from rotd.scoring import rank, score_candidate
from rotd.state_machine import State, StateMachine
from rotd.util import UTC, stable_json


# --- helpers ---------------------------------------------------------------

def _runner(symbol="EXMP", price=2.31, prev=1.90, **kw) -> Canonical:
    now = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    base = dict(symbol=symbol, price=price, prev_close=prev, open=2.00,
                volume=3_000_000, volume_basis="SINGLE_VENUE", bid=price - 0.01,
                ask=price + 0.01, spread_pct=0.9, day_high=price * 1.04,
                day_low=1.98, shares_float=4_000_000, short_float=22.0,
                company="Exemplar Inc", data_grade="A", fetched_at=now)
    base.update(kw)
    return Canonical(**base)


def _snap(records, mode="OPEN") -> Snapshot:
    return Snapshot(trading_day=date(2026, 10, 13), mode=mode,
                    cycle_ts=datetime(2026, 10, 13, 14, 0, tzinfo=UTC),
                    records={r.symbol: r for r in records})


# The intra-cycle history a real tracker feeds in: a 15m dollar volume above the
# OPEN floor, a prior price for acceleration, and a VWAP below price.
_HISTORY = {"prev_price": 2.20, "vwap": 2.10, "dollar_volume_15m": 5_000_000.0}


def _gated(cfg: Config, rec: Canonical, mode="OPEN"):
    """Return (snapshot, rec, feature_set, gate_report, scored) for a candidate."""
    snap = _snap([rec], mode)
    fs = compute_features(rec, cfg, snap, catalyst_score=85.0, history=_HISTORY)
    rep = run_gates(rec, fs, cfg, mode, snap)
    sc = score_candidate(rec, fs, rep, cfg, mode)
    return snap, rec, fs, rep, sc


# --- AC1 / S5: determinism ------------------------------------------------

def test_ac1_identical_inputs_give_identical_decisions(cfg: Config):
    snap = _snap([_runner()])
    r1 = snap.records["EXMP"]
    fs = compute_features(r1, cfg, snap)
    rep1 = run_gates(r1, fs, cfg, "OPEN", snap)
    s1 = score_candidate(r1, fs, rep1, cfg, "OPEN")

    snap2 = _snap([_runner()])
    r2 = snap2.records["EXMP"]
    fs2 = compute_features(r2, cfg, snap2)
    rep2 = run_gates(r2, fs2, cfg, "OPEN", snap2)
    s2 = score_candidate(r2, fs2, rep2, cfg, "OPEN")

    assert snap.payload_hash == snap2.payload_hash
    assert s1.score == s2.score


# --- data reconciliation (DATA-01..12) ------------------------------------

def test_data_basis_by_mode():
    assert choose_basis("OPEN") == BASIS_REGULAR
    assert choose_basis("MIDDAY") == BASIS_REGULAR
    assert choose_basis("PRE") == BASIS_EXTENDED
    assert choose_basis("POST") == BASIS_EXTENDED


def test_data_conflict_and_stale_fail_closed(cfg: Config):
    now = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    ok = reconcile_symbol("T", [PricePoint("finviz", 2.00, now, scope="SINGLE_VENUE"),
                                PricePoint("twelvedata", 2.005, now)], cfg, "OPEN", now=now)
    assert ok.price is not None
    bad = reconcile_symbol("T", [PricePoint("finviz", 2.00, now, scope="SINGLE_VENUE"),
                                 PricePoint("twelvedata", 2.60, now)], cfg, "OPEN", now=now)
    assert R_CONFLICT in bad.reasons
    stale = reconcile_symbol("T", [PricePoint("finviz", 2.0, now - timedelta(seconds=9999),
                                              scope="SINGLE_VENUE")], cfg, "OPEN", now=now)
    assert stale.price is None and R_STALE in stale.reasons


def test_data_snapshot_grade_is_worst_record(cfg: Config):
    good = Canonical(symbol="A", price=1.0, data_grade="A", volume=1, volume_reason="OK")
    bad = Canonical(symbol="B", price=None, data_grade="F")
    snap = build_snapshot([good, bad], date(2026, 10, 13), "OPEN")
    assert snap.data_grade == "F"


# --- gates fail closed -----------------------------------------------------

def test_gates_fail_closed_without_spread(cfg: Config):
    now = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    rec = Canonical(symbol="X", price=2.0, prev_close=1.0, volume=1e6,
                    volume_basis="SINGLE_VENUE", data_grade="A", fetched_at=now)
    snap = _snap([rec])
    fs = compute_features(rec, cfg, snap)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    assert not rep.passed
    assert any(g.gate == "G-SPREAD" and not g.passed for g in rep.results)


def test_gates_reject_above_price_ceiling(cfg: Config):
    rec = _runner(price=11.0, prev=9.0)
    snap = _snap([rec])
    fs = compute_features(rec, cfg, snap)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    assert not rep.passed
    assert any(g.gate == "G-PRICE" and not g.passed for g in rep.results)


# --- scoring / ranking / ROTD ---------------------------------------------

def test_score_in_range_and_rotd_selection(cfg: Config):
    snap, rec, fs, rep, sc = _gated(cfg, _runner())
    assert 0.0 <= sc.score <= 100.0
    cfg._values["ROTD_MIN_SCORE"] = "10"   # force the ROTD path deterministically
    rk = rank([sc], cfg)
    assert rk.rotd is not None and rk.rotd.symbol == "EXMP"
    assert rk.board and rk.board[0].symbol == "EXMP"


def test_rotd_not_selected_below_min_score(cfg: Config):
    snap, rec, fs, rep, sc = _gated(cfg, _runner())
    cfg._values["ROTD_MIN_SCORE"] = "101"  # unreachable
    rk = rank([sc], cfg)
    assert rk.rotd is None and "BELOW_MIN" in rk.rotd_reason


# --- state machine + alert budget -----------------------------------------

def test_state_machine_ladder_and_budget(cfg: Config):
    cfg._values["QUALIFY_CONSEC"] = "2"
    cfg._values["ALERT_BUDGET_PER_DAY"] = "1"
    cfg._values["ARM_MIN_SCORE"] = "0"
    cfg._values["ARM_MAX_RANK"] = "5"
    cfg._values["ROTD_MIN_SCORE"] = "0"
    sm = StateMachine(cfg, date(2026, 10, 13))
    snap, rec, fs, rep, sc = _gated(cfg, _runner())
    assert rep.passed, rep.summary()
    rk = rank([sc], cfg)
    prices = {"EXMP": rec.price}
    t0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)

    # cycle 1: WATCH -> WATCH (pending consecutive)
    t1 = sm.step(rk, snap.records, "OPEN", now=t0, prices=prices)
    assert any(t.new_state == State.WATCH for t in t1)
    # cycle 2: the ladder WATCH -> QUALIFIED -> BOARD -> ARMED -> IGNITION
    spent = 0
    ladder = []
    for i in range(2, 8):
        ts = t0 + timedelta(seconds=30 * (i - 1))
        for t in sm.step(rk, snap.records, "OPEN", now=ts, prices=prices):
            ladder.append(t.new_state)
            if t.new_state == State.IGNITION and t.spend:
                spent += 1
    assert State.QUALIFIED in ladder and State.IGNITION in ladder, ladder
    assert spent == 1, f"budget 1 must be spent exactly once, got {spent}"


def test_transitions_carry_reason_codes(cfg: Config):
    sm = StateMachine(cfg, date(2026, 10, 13))
    snap, rec, fs, rep, sc = _gated(cfg, _runner())
    rk = rank([sc], cfg)
    ts = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    transitions = sm.step(rk, snap.records, "OPEN", now=ts, prices={"EXMP": rec.price})
    assert transitions
    for t in transitions:
        assert t.reason, "every transition must carry a reason code"


# --- auditor (§10 methodology_v3) -----------------------------------------

def test_auditor_bundle_and_rules_fallback(cfg: Config):
    inp = BundleInputs(symbol="EXMP", price=2.31, gate_passed=True, rules_verdict="ENTER")
    bundle = build_fact_bundle(inp, cfg)
    assert set(bundle) >= {"meta", "tape", "book", "fuel", "supply", "phase",
                           "gate", "rules", "levels", "news", "filings", "context"}
    # no model -> rules-only, never a model verdict
    rep = rules_only_report(bundle, cfg)
    assert rep.source == "rules"


def test_auditor_rejects_verdict_above_cap(cfg: Config):
    bundle = build_fact_bundle(BundleInputs(symbol="EXMP", gate_passed=True,
                                            rules_verdict="ENTER" ), cfg)
    out = {
        "schema": "decision_v3", "ticker": "EXMP", "verdict": "ENTER",
        "rating": "STRONG", "confidence_band": "HIGH", "confirmation_mode": "CONTINUATION",
        "cause": {"kind": "NEWS", "catalyst_type": "EARNINGS", "substance": "FILED",
                  "materiality": "YES", "promo_flag": False},
        "catalyst_quality": "REAL", "supply_state": "CLEAN",
        "rubric": {c: "PASS" for c in ("R1", "R2", "R3", "R4", "R5", "R6",
                                       "R7", "R8", "R9", "R10", "R11", "R12")},
        "caps_applied": ["NO_IGNITION"],   # ENTER is above a NO_IGNITION cap
        "strongest_risk": {"text": "x", "refs": []},
        "invalidation_beyond_rules": None, "contradictions": [],
        "levels": None, "reason": "x", "reason_refs": [], "unknowns": [],
    }
    rep, failures = validate_report(out, bundle, cfg)
    assert rep is None and failures


def test_auditor_mechanical_verdict_caps_enter(cfg: Config):
    clean = {c: "PASS" for c in ("R1", "R2", "R3", "R4", "R5", "R6",
                                 "R7", "R8", "R9", "R10", "R11", "R12")}
    mech, caps = mechanical_verdict(clean, True, "CLEAN", "NEWS")
    assert mech == "ENTER" and not caps
    # a failing distribution clause caps at ARMED
    dirty = dict(clean, R10="FAIL")
    mech2, caps2 = mechanical_verdict(dirty, True, "CLEAN", "NEWS")
    assert mech2 == "ARMED" and "NO_DISTRIBUTION" in caps2
    # active dilution is a hard REJECT
    mech3, caps3 = mechanical_verdict(clean, True, "ACTIVE_DILUTION", "NEWS")
    assert mech3 == "REJECT" and "ACTIVE_DILUTION" in caps3


def test_auditor_service_fails_closed_on_call_error(cfg: Config):
    def boom(_s, _u):
        raise RuntimeError("network down")

    svc = AuditorService(cfg, caller=boom)
    res = svc.audit(BundleInputs(symbol="EXMP", gate_passed=True, rules_verdict="ENTER"))
    assert res.report.source == "rules"


def test_auditor_service_never_exceeds_mechanical_verdict(cfg: Config):
    import json
    out = {
        "schema": "decision_v3", "ticker": "EXMP", "verdict": "ENTER",
        "rating": "STRONG", "confidence_band": "HIGH", "confirmation_mode": "CONTINUATION",
        "cause": {"kind": "NEWS", "catalyst_type": "EARNINGS", "substance": "FILED",
                  "materiality": "YES", "promo_flag": False},
        "catalyst_quality": "REAL", "supply_state": "CLEAN",
        "rubric": {c: "PASS" for c in ("R1", "R2", "R3", "R4", "R5", "R6",
                                       "R7", "R8", "R9", "R10", "R11", "R12")},
        "caps_applied": [], "strongest_risk": {"text": "x", "refs": []},
        "invalidation_beyond_rules": None, "contradictions": [], "levels": None,
        "reason": "x", "reason_refs": [], "unknowns": [],
    }
    # rules ceiling is ARMED, so an ENTER from the model must be clamped down
    svc = AuditorService(cfg, caller=lambda s, u: json.dumps(out))
    res = svc.audit(BundleInputs(symbol="EXMP", gate_passed=True, rules_verdict="ARMED"))
    assert res.report.verdict == "ARMED"
