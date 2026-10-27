"""POST -> PRE continuity across the 16:00 ET trading-day rollover.

No network. The trading-day label rolls at 16:00 ET (design A1), which sits in
the middle of the POST -> OVERNIGHT -> PRE arc of the *same* name. ``_ensure_day``
used to wipe the in-memory rolling history at that boundary, so pre-market began
blind: ``vol_3m`` needs 3 minutes of samples and ``dvol_10m`` needs 10. A name
that was running in POST and gapping at 4:00 AM is the exact case that stalled.

Three behaviours are pinned here:
1. the prior session's POST/OVERNIGHT tape is carried into the new day;
2. a cumulative counter that restarts at the boundary REBASES (no underflow to
   a fake 0, no bogus spike);
3. the carry-over fails soft — a missing store or empty history never blocks.
"""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from rotd.config import Config
from rotd.normalize import Canonical, Snapshot
from rotd.runtime import Runtime
from rotd.store import Store
from rotd.util import UTC

MON = date(2026, 10, 19)
TUE = date(2026, 10, 20)
# 04:05 ET == 08:05 UTC on the pre-market of the *next* trading day.
PRE_TS = datetime(2026, 10, 20, 8, 5, tzinfo=UTC)


def _rt(tmp_path: Path) -> Runtime:
    return Runtime(Config(path=tmp_path / "c.env"), Store(tmp_path / "t.db"), {}, None)


def _post(store: Store, hour: int, minute: int, vol: float, px: float,
          mode: str = "POST") -> None:
    store.save_snapshot(
        MON, mode, datetime(2026, 10, 19, hour, minute, tzinfo=UTC), "A",
        {"volumes": {"RIME": vol}, "prices": {"RIME": px}}, "h", 1)


def _pre_snap(vol: float, px: float, ts: datetime) -> Snapshot:
    rec = Canonical(symbol="RIME", price=px, volume=vol)
    return Snapshot(trading_day=TUE, mode="PRE", cycle_ts=ts,
                    records={"RIME": rec}, data_grade="A")


def test_post_tape_is_carried_into_the_premarket(tmp_path: Path):
    rt = _rt(tmp_path)
    # Mon POST runs 16:00-20:00 ET (20:00-00:00 UTC).
    _post(rt.store, 20, 1, 500_000, 2.00)
    _post(rt.store, 20, 4, 2_000_000, 2.15)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)

    dq = rt._vol_history.get("RIME")
    assert dq is not None and len(dq) == 2
    assert [s[1] for s in dq] == [500_000.0, 2_000_000.0]


def test_carry_is_anchored_to_the_end_of_post_not_to_now(tmp_path: Path):
    """A window measured back from 4:00 AM could never reach a 20:00 ET POST."""
    rt = _rt(tmp_path)
    _post(rt.store, 20, 30, 900_000, 2.10)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)
    assert rt._vol_history.get("RIME"), "POST tape 12h earlier must still carry"
    assert rt.cfg.int("PRIOR_SESSION_CARRY_MIN") >= 60


def test_regular_session_samples_are_not_carried(tmp_path: Path):
    """Only POST/OVERNIGHT is contiguous with pre-market."""
    rt = _rt(tmp_path)
    _post(rt.store, 14, 0, 5_000_000, 2.00, mode="OPEN")
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)
    assert not rt._vol_history.get("RIME")


def test_counter_reset_rebases_instead_of_underflowing(tmp_path: Path):
    """A PRE counter restarting below POST must not read as dead tape."""
    rt = _rt(tmp_path)
    _post(rt.store, 20, 3, 2_000_000, 2.15)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)
    assert [s[1] for s in rt._vol_history["RIME"]] == [2_000_000.0]

    # Pre-market counter restarts at 30,000 — far below the POST reading.
    for k in range(5):
        ts = PRE_TS + timedelta(minutes=k)
        snap = _pre_snap(30_000 + k * 200_000, 2.40 + k * 0.02, ts)
        rt._update_vol_history(snap, ts)

    dq = rt._vol_history["RIME"]
    assert [s[1] for s in dq] == [30_000, 230_000, 430_000, 630_000, 830_000]
    # The delta must be real pre-market tape, not 0 (underflow) or a spike.
    assert rt._delta_volume(dq, PRE_TS + timedelta(minutes=4), 180.0) == 600_000


def test_fewer_than_ten_premarket_minutes_yields_no_dvol(tmp_path: Path):
    """Honest absence feeds the warmup fallback; a fake 0 would block."""
    rt = _rt(tmp_path)
    _post(rt.store, 20, 3, 2_000_000, 2.15)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)
    for k in range(5):
        ts = PRE_TS + timedelta(minutes=k)
        rt._update_vol_history(_pre_snap(30_000 + k * 200_000, 2.4, ts), ts)
    dq = rt._vol_history["RIME"]
    assert rt._delta_volume(dq, PRE_TS + timedelta(minutes=4), 600.0) is None


def test_carry_is_disabled_by_zero(tmp_path: Path):
    rt = _rt(tmp_path)
    rt.cfg._values["PRIOR_SESSION_CARRY_MIN"] = "0"
    _post(rt.store, 20, 3, 2_000_000, 2.15)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)
    assert not rt._vol_history.get("RIME")


def test_carry_fails_soft_without_a_store(tmp_path: Path):
    """A cycle must never be blocked by a missing history."""
    rt = Runtime(Config(path=tmp_path / "c.env"), None, {}, None)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)      # must not raise
    assert not rt._vol_history


def test_first_day_has_nothing_to_carry(tmp_path: Path):
    rt = _rt(tmp_path)
    rt._ensure_day(TUE, PRE_TS)      # prev_day is None
    assert not rt._vol_history
