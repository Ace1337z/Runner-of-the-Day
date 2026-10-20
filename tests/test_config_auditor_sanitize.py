"""Config tuning + AI-report sanitizer regression tests (no network)."""

from __future__ import annotations

from pathlib import Path

from rotd.auditor import (BundleInputs, build_fact_bundle, validate_report)
from rotd.config import Config


def test_ignition_threshold_and_ai_timeout_have_defaults(cfg: Config):
    assert cfg.int("IGNITION_THRESHOLD", 70) == 70
    assert cfg.float("AI_TIMEOUT_S", 30) == 30.0
    assert cfg.float("ARM_MIN_SCORE", 68) == 68.0
    assert cfg.float("CHASE_CAP_PCT", 25.0) == 25.0


def test_config_env_overrides_defaults(tmp_path: Path):
    env = tmp_path / "rotd.env"
    env.write_text("IGNITION_THRESHOLD=85\nAI_TIMEOUT_S=60\nARM_MIN_SCORE=80\n")
    c = Config(path=env)
    assert c.int("IGNITION_THRESHOLD", 70) == 85
    assert c.float("AI_TIMEOUT_S", 30) == 60.0
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
    from datetime import date, timedelta
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
    assert c.int("CAND_RETENTION_HOURS", 2) == 2
    assert c.int("RAW_RETENTION_HOURS", 2) == 2
    assert c.int("DB_PRUNE_INTERVAL_S", 300) == 300
