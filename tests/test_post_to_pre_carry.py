"""POST starts the new trading day, and its history carries through to MIDDAY.

No network. Design A1: the day runs from the start of POST (16:00 ET) THROUGH
the overnight and pre-market to the end of the next session's MIDDAY. POST
belongs to the *coming* session, so nothing inside
POST -> OVERNIGHT -> PRE -> OPEN -> MIDDAY rolls the day label, and the rolling
volume history is never wiped mid-arc.

The only wipe is at the POST boundary, where ``_carry_prior_session`` seeds the
deque from the session that just closed, so pre-market starts warm instead of
waiting 3-10 minutes for ``vol_3m``/``dvol_10m``.
"""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from rotd.config import Config
from rotd.normalize import Canonical, Snapshot
from rotd.runtime import Runtime
from rotd.store import Store
from rotd.util import UTC, following_session, mode_for, trading_day_for

MON = date(2026, 10, 19)
TUE = date(2026, 10, 20)
# Tue 04:05 ET == 08:05 UTC, pre-market of the day that started Mon 16:00 ET.
PRE_TS = datetime(2026, 10, 20, 8, 5, tzinfo=UTC)


def _rt(tmp_path: Path) -> Runtime:
    return Runtime(Config(path=tmp_path / "c.env"), Store(tmp_path / "t.db"), {}, None)


def _snap(store: Store, day: date, mode: str, ts: datetime, vol: float,
          px: float, sym: str = "RIME") -> None:
    store.save_snapshot(day, mode, ts, "A",
                        {"volumes": {sym: vol}, "prices": {sym: px}}, "h", 1)


def _pre_snap(vol: float, px: float, ts: datetime) -> Snapshot:
    rec = Canonical(symbol="RIME", price=px, volume=vol)
    return Snapshot(trading_day=TUE, mode="PRE", cycle_ts=ts,
                    records={"RIME": rec}, data_grade="A")


# --- the day boundary itself ------------------------------------------------

def test_post_starts_the_next_sessions_day():
    """Mon 16:01 ET POST belongs to Tuesday, not Monday."""
    assert trading_day_for(datetime(2026, 10, 19, 20, 1, tzinfo=UTC)) == TUE


def test_the_whole_arc_is_one_day():
    """POST -> OVERNIGHT -> PRE -> OPEN -> MIDDAY must never roll the label."""
    arc = [datetime(2026, 10, 19, 20, 1, tzinfo=UTC),    # Mon 16:01 POST
           datetime(2026, 10, 20, 0, 30, tzinfo=UTC),    # Mon 20:30 OVERNIGHT
           datetime(2026, 10, 20, 8, 5, tzinfo=UTC),     # Tue 04:05 PRE
           datetime(2026, 10, 20, 13, 45, tzinfo=UTC),   # Tue 09:45 OPEN
           datetime(2026, 10, 20, 19, 59, tzinfo=UTC)]   # Tue 15:59 MIDDAY
    assert {trading_day_for(t) for t in arc} == {TUE}
    assert [mode_for(t) for t in arc] == ["POST", "OVERNIGHT", "PRE", "OPEN", "MIDDAY"]


def test_midday_still_belongs_to_its_own_day():
    assert trading_day_for(datetime(2026, 10, 19, 19, 59, tzinfo=UTC)) == MON


def test_friday_post_starts_monday():
    """next_session/following_session must skip the weekend."""
    fri_post = datetime(2026, 10, 23, 20, 1, tzinfo=UTC)
    assert trading_day_for(fri_post) == date(2026, 10, 26)
    assert following_session(MON) == TUE


# --- carry-over at the POST boundary ----------------------------------------

def test_closing_session_tape_is_carried_into_the_new_day(tmp_path: Path):
    rt = _rt(tmp_path)
    # Monday's MIDDAY tape, ending at the 16:00 ET close.
    _snap(rt.store, MON, "MIDDAY", datetime(2026, 10, 19, 19, 58, tzinfo=UTC),
          2_000_000, 2.15)
    _snap(rt.store, MON, "MIDDAY", datetime(2026, 10, 19, 19, 50, tzinfo=UTC),
          500_000, 2.00)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)

    dq = rt._vol_history.get("RIME")
    assert dq is not None and len(dq) == 2
    assert [s[1] for s in dq] == [500_000.0, 2_000_000.0]


def test_carry_window_is_anchored_to_the_close_not_to_now(tmp_path: Path):
    """At 4:00 AM the gap back to the previous close is hours long."""
    rt = _rt(tmp_path)
    _snap(rt.store, MON, "MIDDAY", datetime(2026, 10, 19, 19, 58, tzinfo=UTC),
          900_000, 2.10)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)
    assert rt._vol_history.get("RIME"), "tape 12h earlier must still carry"
    assert rt.cfg.int("PRIOR_SESSION_CARRY_MIN") >= 60


def test_no_rollover_means_no_second_wipe(tmp_path: Path):
    """Once on TUE, later PRE/OPEN cycles must not re-run the carry or clear."""
    rt = _rt(tmp_path)
    _snap(rt.store, MON, "MIDDAY", datetime(2026, 10, 19, 19, 58, tzinfo=UTC),
          900_000, 2.10)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)
    seeded = list(rt._vol_history["RIME"])
    # Same day, later cycles (PRE -> OPEN) must not touch the history.
    rt._ensure_day(TUE, datetime(2026, 10, 20, 13, 45, tzinfo=UTC))
    assert list(rt._vol_history["RIME"]) == seeded


def test_counter_reset_rebases_instead_of_underflowing(tmp_path: Path):
    """A pre-market counter restarting below the carried value must not underflow."""
    rt = _rt(tmp_path)
    _snap(rt.store, MON, "MIDDAY", datetime(2026, 10, 19, 19, 58, tzinfo=UTC),
          2_000_000, 2.15)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)
    assert [s[1] for s in rt._vol_history["RIME"]] == [2_000_000.0]

    for k in range(5):
        ts = PRE_TS + timedelta(minutes=k)
        rt._update_vol_history(_pre_snap(30_000 + k * 200_000, 2.40, ts), ts)

    dq = rt._vol_history["RIME"]
    assert [s[1] for s in dq] == [30_000, 230_000, 430_000, 630_000, 830_000]
    assert rt._delta_volume(dq, PRE_TS + timedelta(minutes=4), 180.0) == 600_000


def test_fewer_than_ten_minutes_yields_no_dvol(tmp_path: Path):
    """Honest absence feeds the warmup fallback; a fake 0 would block."""
    rt = _rt(tmp_path)
    _snap(rt.store, MON, "MIDDAY", datetime(2026, 10, 19, 19, 58, tzinfo=UTC),
          2_000_000, 2.15)
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
    _snap(rt.store, MON, "MIDDAY", datetime(2026, 10, 19, 19, 58, tzinfo=UTC),
          2_000_000, 2.15)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)
    assert not rt._vol_history.get("RIME")


def test_carry_fails_soft_without_a_store(tmp_path: Path):
    rt = Runtime(Config(path=tmp_path / "c.env"), None, {}, None)
    rt._day = MON
    rt._ensure_day(TUE, PRE_TS)      # must not raise
    assert not rt._vol_history


def test_first_day_has_nothing_to_carry(tmp_path: Path):
    rt = _rt(tmp_path)
    rt._ensure_day(TUE, PRE_TS)      # prev_day is None
    assert not rt._vol_history
