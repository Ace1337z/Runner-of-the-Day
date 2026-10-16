"""Regression tests for the 6 upgrades. No network.

Pins the new behaviors: short-term volume deltas, the rules-only catalyst score
fix, the /setbudget command, and the market-cap universe scope.
"""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta

import pytest

from rotd.catalyst import CatalystService
from rotd.config import Config
from rotd.features import compute_features
from rotd.normalize import Canonical, Snapshot
from rotd.runtime import Runtime
from rotd.scoring import score_candidate
from rotd.telegram import FakeTelegram
from rotd.util import UTC

DAY = date(2026, 10, 13)
T0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


def _rec(symbol="EXMP", price=2.31, volume=3_000_000, **kw) -> Canonical:
    base = dict(symbol=symbol, price=price, prev_close=1.90, open=2.00,
                volume=volume, volume_basis="SINGLE_VENUE", bid=price - 0.01,
                ask=price + 0.01, spread_pct=0.9, day_high=price * 1.04,
                day_low=1.98, shares_float=4_000_000, short_float=22.0,
                market_cap=9_240_000, company="Exemplar Inc", data_grade="A",
                fetched_at=T0)
    base.update(kw)
    return Canonical(**base)


def _snap(rec: Canonical, mode="OPEN") -> Snapshot:
    return Snapshot(trading_day=DAY, mode=mode, cycle_ts=T0, records={rec.symbol: rec})


# --- Upgrade 1: short-term volume deltas -----------------------------------

def test_delta_volume_returns_none_until_enough_history():
    dq = deque(maxlen=60)
    now = T0
    # only 1 minute of samples: no 3-minute delta yet
    dq.append((now - timedelta(seconds=60), 1_000_000))
    dq.append((now, 1_500_000))
    assert Runtime._delta_volume(dq, now, 180.0) is None
    # add a sample 4 minutes ago -> 3m delta is computable
    dq.appendleft((now - timedelta(seconds=240), 200_000))
    dq.append((now + timedelta(seconds=1), 1_600_000))
    assert Runtime._delta_volume(dq, now + timedelta(seconds=1), 180.0) == 1_400_000


def test_update_vol_history_writes_deltas_into_features(cfg: Config):
    rt = Runtime(cfg, None, {}, None)
    # first cycle: only one sample -> deltas MISSING
    rt._update_vol_history(_snap(_rec()), T0)
    fs = compute_features(_rec(), cfg, _snap(_rec()), history=rt._history.get("EXMP"))
    assert fs.value("vol_3m") is None and fs.value("dollar_volume_10m") is None
    # second cycle 12 minutes later with a higher cumulative volume
    rec2 = _rec(volume=4_000_000)
    snap2 = _snap(rec2)
    rt._update_vol_history(snap2, T0 + timedelta(seconds=720))
    fs2 = compute_features(rec2, cfg, snap2, history=rt._history.get("EXMP"))
    assert fs2.value("vol_3m") == 4_000_000 - 3_000_000
    assert fs2.value("dollar_volume_10m") == pytest.approx(1_000_000 * rec2.price)


# --- Bug Fix 2: rules-only catalyst score is None --------------------------

def test_report_score_none_for_rules_only(cfg: Config):
    svc = CatalystService(cfg)
    from rotd.auditor import AuditResult, BundleInputs, build_fact_bundle, rules_only_report
    inp = BundleInputs(symbol="EXMP")
    bundle = build_fact_bundle(inp, cfg)
    res = AuditResult(rules_only_report(bundle, cfg), bundle)
    svc._reports["EXMP"] = res
    assert svc.report_score("EXMP") is None
    assert svc.best_score("EXMP", []) is None


def test_runtime_catalyst_score_none_when_not_configured(cfg: Config):
    rt = Runtime(cfg, None, {}, None)
    assert rt._catalyst_score("EXMP") is None


# --- Upgrade 5: /setbudget -------------------------------------------------

def test_setbudget_persists_and_applies_live(tmp_path):
    path = tmp_path / "rotd.env"
    path.write_text("TELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    cfg = Config(path=path)
    tg = FakeTelegram(cfg)
    reply = tg.handle_command("/setbudget 7", "42", cfg)
    assert reply and "7" in reply
    assert cfg.int("ALERT_BUDGET_PER_DAY", 5) == 7
    assert "ALERT_BUDGET_PER_DAY=7" in path.read_text(encoding="utf-8")


def test_setbudget_rejects_out_of_range(tmp_path):
    path = tmp_path / "rotd.env"
    path.write_text("TELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    cfg = Config(path=path)
    tg = FakeTelegram(cfg)
    assert "must be" in tg.handle_command("/setbudget 99", "42", cfg)
    assert "invalid" in tg.handle_command("/setbudget abc", "42", cfg)
    assert cfg.int("ALERT_BUDGET_PER_DAY", 5) == 5  # unchanged


# --- Upgrade 6: market-cap scope -------------------------------------------

def test_gate_price_uses_market_cap_when_set(cfg: Config):
    from rotd.gates import gate_price
    ok = _rec(price=25.0, market_cap=80_000_000)
    assert gate_price(ok, cfg).passed
    big = _rec(price=2.0, market_cap=150_000_000)
    assert not gate_price(big, cfg).passed
    unknown = _rec(price=2.0, market_cap=None)
    assert not gate_price(unknown, cfg).passed


def test_universe_filter_uses_market_cap(cfg: Config):
    rt = Runtime(cfg, None, {}, None)
    rows = [{"symbol": "A", "price": 25.0, "market_cap": 80_000_000},
            {"symbol": "B", "price": 2.0, "market_cap": 150_000_000},
            {"symbol": "C", "price": 2.0, "market_cap": None}]
    # simulate the finviz client returning parsed rows
    class _FakeFinviz:
        def universe(self):
            return rows

        def parse_row(self, r):
            return r

    rt.clients["finviz"] = _FakeFinviz()
    got, _ = rt._fetch_universe("OPEN")
    assert [r["symbol"] for r in got] == ["A"]
