"""Time, exchange-calendar and formatting tests (no network).

The calendar tests pin the properties the decision path relies on: the US
session opens at the right UTC instant across DST, weekends/holidays are not
trading days, and the mode engine returns the expected session label.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from rotd.util import (ET, KL, UTC, as_utc, clamp, fmt_ts, is_trading_day,
                       mask, mode_for, now_utc, pct, safe_div, scrub,
                       session_close_utc, session_open_utc, short_hash,
                       stable_json, to_et, to_kl)


def test_zone_conversions_are_consistent():
    dt = datetime(2026, 10, 13, 14, 30, tzinfo=UTC)   # 10:30 ET, 22:30 KL
    assert to_et(dt).hour == 10 and to_et(dt).minute == 30
    assert to_kl(dt).hour == 22 and to_kl(dt).minute == 30
    assert as_utc(to_et(dt)) == dt


def test_trading_day_excludes_weekend_and_holiday():
    assert is_trading_day(date(2026, 10, 13))          # Tuesday
    assert not is_trading_day(date(2026, 10, 10))      # Saturday
    assert not is_trading_day(date(2026, 10, 11))      # Sunday
    assert not is_trading_day(date(2026, 12, 25))      # Christmas


def test_session_open_close_utc_edt():
    # 2026-10-13 is in US daylight time: 09:30 ET = 13:30 UTC, 16:00 ET = 20:00 UTC.
    d = date(2026, 10, 13)
    assert session_open_utc(d) == datetime(2026, 10, 13, 13, 30, tzinfo=UTC)
    assert session_close_utc(d) == datetime(2026, 10, 13, 20, 0, tzinfo=UTC)


def test_session_open_close_utc_est():
    # 2026-01-06 is in US standard time: 09:30 ET = 14:30 UTC, 16:00 ET = 21:00 UTC.
    d = date(2026, 1, 6)
    assert session_open_utc(d) == datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
    assert session_close_utc(d) == datetime(2026, 1, 6, 21, 0, tzinfo=UTC)


def test_mode_engine_labels():
    # mid-session (10:00 ET = 14:00 UTC, inside the OPEN window)
    assert mode_for(datetime(2026, 10, 13, 14, 0, tzinfo=UTC)) == "OPEN"
    # MIDDAY (11:00 ET = 15:00 UTC)
    assert mode_for(datetime(2026, 10, 13, 15, 0, tzinfo=UTC)) == "MIDDAY"
    # pre-market (08:00 ET = 12:00 UTC)
    assert mode_for(datetime(2026, 10, 13, 12, 0, tzinfo=UTC)) == "PRE"
    # after the close (18:00 ET = 22:00 UTC)
    assert mode_for(datetime(2026, 10, 13, 22, 0, tzinfo=UTC)) == "POST"
    # weekend -> CLOSED
    assert mode_for(datetime(2026, 10, 10, 15, 0, tzinfo=UTC)) == "CLOSED"


def test_fmt_ts_shows_et_now():
    s = fmt_ts(now_utc())
    assert "ET" in s and "UTC" in s


def test_math_helpers():
    assert clamp(5, 0, 3) == 3 and clamp(-1, 0, 3) == 0
    assert safe_div(10, 0, default=-1) == -1
    assert safe_div(6, 3) == 2.0
    assert pct(12.34) == "+12.3%"          # the formatter carries an explicit sign
    assert pct(-3.0) == "-3.0%"


def test_determinism_helpers():
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert stable_json(a) == stable_json(b)
    assert short_hash(a) == short_hash(b)


def test_secret_scrubbing_and_masking():
    text = "key is sk-supersecret123 in the log"
    out = scrub(text, extra=["sk-supersecret123"])
    assert "sk-supersecret123" not in out
    assert "abcdefgh" not in mask("abcdefgh")   # only the tail survives
    assert mask("") == "(unset)"
    assert mask(None) == "(unset)"
