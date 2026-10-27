"""Pre-market latency fixes: the 10m warmup fallback and the conflict tolerance.

No network. These guard the two ways the pre-market path used to stall or go
blind at 4:00 AM:

1. ``live_volume_reason`` failed closed on an empty 10-minute roll, so a Candle-1
   gapper was blocked for the full warmup even with a hot 3-minute tape. It now
   estimates dollar volume from the 3-minute tape (conservatively, x2) while
   still enforcing the session floor.
2. ``_conflict`` measures the gap RELATIVE to the higher price, so the tolerance
   is bounded below 100%. A value >= 100 silently disables the gate; the shipped
   default must stay under it.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from rotd.config import Config
from rotd.gates import DVOL_10M_WARMUP_SCALE, live_volume_reason
from rotd.normalize import PricePoint, R_CONFLICT, reconcile_symbol
from rotd.util import UTC

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
PRE = "PRE"
OPEN = "OPEN"


def _cfg() -> Config:
    # An empty path: no on-disk config, so the registry defaults apply.
    return Config(path=None)


# --- 1. 10-minute warmup fallback -------------------------------------------

def test_warmup_fallback_uses_3m_tape_in_premarket():
    """An empty 10m roll must not read as dead tape when the 3m tape is alive."""
    cfg = _cfg()
    assert cfg.float("MIN_DVOL_10M_EXT") == 75000.0
    # 1,000,000 shares * $0.50 * 2.0 = $1,000,000, comfortably over the floor.
    assert live_volume_reason(1_000_000, None, cfg, PRE, 0.50) is None


def test_warmup_fallback_treats_zero_delta_as_warmup():
    """dvol_10m == 0.0 is "no earlier sample", not "no volume spent"."""
    cfg = _cfg()
    assert live_volume_reason(1_000_000, 0.0, cfg, PRE, 0.50) is None
    assert DVOL_10M_WARMUP_SCALE == 2.0


def test_warmup_fallback_still_enforces_the_session_floor():
    """The fallback changes the measurement, never whether a floor applies."""
    cfg = _cfg()
    # 60,000 shares clears MIN_VOL_3M_SHARES, but * $0.50 * 2.0 = $60,000 is
    # still under the $75,000 extended floor.
    assert live_volume_reason(60_000, None, cfg, PRE, 0.50) == "DVOL_10M_TOO_LOW"
    # The same tape in regular hours faces the higher $150,000 floor:
    # 100,000 * $0.50 * 2.0 = $100,000 clears the extended floor but not this one.
    assert live_volume_reason(100_000, None, cfg, PRE, 0.50) is None
    assert live_volume_reason(100_000, None, cfg, OPEN, 0.50) == "DVOL_10M_TOO_LOW"


def test_warmup_fallback_requires_a_price():
    """Without a price there is nothing to convert shares into dollars."""
    cfg = _cfg()
    assert live_volume_reason(1_000_000, 0.0, cfg, PRE) == "DVOL_10M_TOO_LOW"


def test_real_dvol_is_never_overridden_by_the_fallback():
    """A genuine 10m number always wins, even a small one."""
    cfg = _cfg()
    assert live_volume_reason(1_000_000, 1_000.0, cfg, PRE, 5.00) == "DVOL_10M_TOO_LOW"
    assert live_volume_reason(1_000_000, 1e6, cfg, PRE, 5.00) is None


def test_dead_tape_still_fails_closed():
    """The pre-existing fail-closed guards are untouched."""
    cfg = _cfg()
    assert live_volume_reason(None, None, cfg, OPEN) == "NO_VOL_HISTORY"
    assert live_volume_reason(100.0, 1e6, cfg, OPEN) == "RECENT_VOL_TOO_LOW"


# --- 2. Price-conflict tolerance -------------------------------------------

def _conflicts(cfg: Config, a: float, b: float) -> bool:
    rec = reconcile_symbol(
        "T",
        [PricePoint("finviz", a, NOW, scope="SINGLE_VENUE"),
         PricePoint("finnhub", b, NOW)],
        cfg, PRE, now=NOW)
    return R_CONFLICT in rec.reasons


def test_extended_conflict_tolerance_stays_below_its_ceiling():
    """The gap is relative to the max price, so >= 100% disables the gate."""
    cfg = _cfg()
    assert cfg.float("PRICE_CONFLICT_PCT_EXT") < 100.0


def test_extended_conflict_still_catches_an_order_of_magnitude():
    """A stale quote may be wide, but a 40x disagreement is never acceptable."""
    cfg = _cfg()
    assert _conflicts(cfg, 2.00, 0.05) is True


def test_extended_conflict_tolerates_a_premarket_rereating():
    """A name that has doubled off the anchor is a gapper, not a data conflict."""
    cfg = _cfg()
    assert _conflicts(cfg, 2.00, 4.20) is False


def test_extended_conflict_is_wider_than_regular_hours():
    cfg = _cfg()
    assert cfg.float("PRICE_CONFLICT_PCT_EXT") > cfg.float("PRICE_CONFLICT_PCT_REG")
