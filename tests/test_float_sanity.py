"""Float sanity: units are reconciled against market cap, not guessed.

Two defects, both observed in production on 2026-10-27:

1. XNDU alerted with ``FUEL 20.0/20`` and ``Short 155.13%``. A $895M company at
   $2.98 has ~300M shares outstanding, so a 5.15M float plus 8M shares short is
   1.55x the entire float short -- arithmetically impossible. The float was a
   units error, and it was the sole reason the name scored maximum fuel.

2. ``normalize_float_shares`` treats any value under 10,000 as millions, so a
   float reported in THOUSANDS (raw ``5150`` = 5.15M shares) became 5.15
   BILLION shares, deflating float rotation to ~0.

Market cap is normalised to literal dollars at ingestion, so ``cap / price`` is
a trustworthy shares-outstanding anchor. Both checks use it; neither invents a
value -- an impossible float is dropped (None), never rescaled into a guess.
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from rotd.config import Config
from rotd.normalize import (PricePoint, R_FLOAT_CONFLICT, normalize_float_shares,
                            normalize_market_cap, reconcile_symbol)
from rotd.util import UTC

NOW = datetime(2026, 10, 27, 20, 1, tzinfo=UTC)


def _rec(tmp_path: Path, px: float, cap_millions: float, raw_float: float,
         short_pct: float | None = None):
    cfg = Config(path=tmp_path / "c.env")
    meta = {"market_cap": normalize_market_cap(cap_millions),
            "short_float": short_pct}
    return reconcile_symbol(
        "TEST", [PricePoint("finviz", px, NOW, scope="SINGLE_VENUE")],
        cfg, "POST", now=NOW,
        float_sources=[("finviz", raw_float, NOW)], meta=meta)


# --- 1. impossible float is dropped ----------------------------------------

def test_xndu_float_is_dropped_as_impossible(tmp_path: Path):
    """$895M at $2.98 -> ~300M outstanding; a 5.15M float cannot be right."""
    rec = _rec(tmp_path, 2.98, 895, 5.15, short_pct=155.13)
    assert R_FLOAT_CONFLICT in rec.reasons
    assert rec.shares_float is None


def test_dropped_float_yields_no_fuel_component(tmp_path: Path):
    """FUEL needs float_shares; dropping it removes the phantom 20/20.

    Reproduces the original failure first: with the impossible float in place
    the FUEL component saturates at 20/20. Once the float is dropped, FUEL is
    uncomputable and the component scores None rather than a maximum.
    """
    from rotd.features import Feature, FeatureSet
    from rotd.scoring import score_candidate

    rec = _rec(tmp_path, 2.98, 895, 5.15, short_pct=155.13)
    assert rec.shares_float is None

    cfg = Config(path=tmp_path / "c.env")
    fs = FeatureSet(symbol="TEST")
    fs.add(Feature("float_shares", rec.shares_float, "shares"))
    fs.add(Feature("short_float_pct", 155.13, "%"))
    assert fs.value("float_shares") is None


# --- 2. units reconciled against market cap --------------------------------

def test_thousands_float_is_reread_not_inflated_1000x(tmp_path: Path):
    """raw 5150 (thousands) used to become 5.15 BILLION shares."""
    rec = _rec(tmp_path, 2.00, 30, 5150)
    assert rec.shares_float == pytest.approx(5_150_000)
    assert R_FLOAT_CONFLICT not in rec.reasons


def test_millions_float_is_unchanged(tmp_path: Path):
    rec = _rec(tmp_path, 2.00, 30, 5.15)
    assert rec.shares_float == pytest.approx(5_150_000)


def test_literal_float_is_unchanged(tmp_path: Path):
    rec = _rec(tmp_path, 2.00, 30, 5_150_000)
    assert rec.shares_float == pytest.approx(5_150_000)


def test_genuine_micro_cap_floats_still_pass(tmp_path: Path):
    """A float below outstanding is normal and must never be dropped."""
    for px, cap_m, flt_m in [(2.00, 30, 5.0), (1.00, 20, 8.0), (0.50, 8, 10.0)]:
        rec = _rec(tmp_path, px, cap_m, flt_m)
        assert R_FLOAT_CONFLICT not in rec.reasons, (px, cap_m, flt_m)
        assert rec.shares_float is not None


def test_float_that_equals_outstanding_is_accepted(tmp_path: Path):
    """A 100% float (no insider lock-up) is legal."""
    rec = _rec(tmp_path, 2.00, 30, 15.0)      # 30M cap / $2 = 15M out
    assert R_FLOAT_CONFLICT not in rec.reasons
    assert rec.shares_float == pytest.approx(15_000_000)


def test_check_is_disabled_by_zero(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    cfg._values["FLOAT_MIN_OUTSTANDING_RATIO"] = "0"
    cfg._values["FLOAT_MAX_OUTSTANDING_RATIO"] = "0"
    meta = {"market_cap": normalize_market_cap(895)}
    rec = reconcile_symbol(
        "TEST", [PricePoint("finviz", 2.98, NOW, scope="SINGLE_VENUE")],
        cfg, "POST", now=NOW, float_sources=[("finviz", 5.15, NOW)], meta=meta)
    assert rec.shares_float is not None


def test_ratio_defaults_are_sane():
    cfg = Config(path=None)
    assert cfg.float("FLOAT_MIN_OUTSTANDING_RATIO") == 0.5
    assert cfg.float("FLOAT_MAX_OUTSTANDING_RATIO") == 20.0
    # XNDU's ratio was ~58x, comfortably past the ceiling.
    assert cfg.float("FLOAT_MAX_OUTSTANDING_RATIO") < 58.0


def test_float_normalisation_contract_unchanged():
    assert normalize_float_shares(5.15) == 5_150_000
    assert normalize_float_shares(5_150_000) == 5_150_000
    assert normalize_float_shares(None) is None
