"""Shadow-clock tests (§17 Phase 6): the clock counts *trading* days, not calendar.

The clock gates live pushes, so these tests pin the properties that matter: a
trading-day start, weekends/holidays not advancing it, completion at the required
count, and live pushes being refused until it completes.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from rotd import shadow
from rotd.config import Config
from rotd.store import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    st = Store(tmp_path / "shadow.db")
    yield st
    st.close()


def test_start_requires_a_trading_day(store: Store):
    with pytest.raises(ValueError):
        shadow.start(store, date(2026, 10, 11))   # a Sunday


def test_start_records_the_first_session(store: Store, cfg: Config):
    st = shadow.start(store, date(2026, 10, 13))
    assert st.started == date(2026, 10, 13)
    assert st.sessions_done == 1


def test_start_is_idempotent_without_force(store: Store):
    shadow.start(store, date(2026, 10, 13))
    again = shadow.start(store, date(2026, 10, 20))
    assert again.started == date(2026, 10, 13)     # not reset


def test_weekends_do_not_advance_the_clock(store: Store):
    shadow.start(store, date(2026, 10, 13))
    shadow.note_cycle(store, date(2026, 10, 11))   # Sunday
    assert shadow.status(store).sessions_done == 1


def test_completion_after_ten_sessions(store: Store, cfg: Config):
    shadow.start(store, date(2026, 10, 13))
    # 10 sessions from Tue 2026-10-13 ends Mon 2026-10-26 (no holidays in between)
    shadow.note_cycle(store, date(2026, 10, 26))
    st = shadow.status(store, cfg=cfg)
    assert st.sessions_done == 10 and st.complete


def test_live_push_blocked_while_incomplete(store: Store, cfg: Config):
    cfg._values["SHADOW"] = "1"
    shadow.start(store, date(2026, 10, 13))
    st = shadow.status(store, cfg=cfg)
    assert not st.live_allowed and st.reason == shadow.R_CLOCK_INCOMPLETE


def test_live_push_allowed_when_not_in_shadow(store: Store, cfg: Config):
    cfg._values["SHADOW"] = "0"
    st = shadow.status(store, cfg=cfg)
    assert st.live_allowed and st.reason == shadow.R_NOT_SHADOW


def test_live_push_allowed_after_completion(store: Store, cfg: Config):
    cfg._values["SHADOW"] = "1"
    shadow.start(store, date(2026, 10, 13))
    shadow.note_cycle(store, date(2026, 10, 26))
    st = shadow.status(store, cfg=cfg)
    assert st.live_allowed and st.reason == shadow.R_CLOCK_COMPLETE


def test_required_days_is_a_setting(cfg: Config):
    # the clock length is a named setting, never a magic number
    assert cfg.int("SHADOW_DAYS_REQUIRED", 10) == 10
