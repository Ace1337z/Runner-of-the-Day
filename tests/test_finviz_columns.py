"""Finviz column-id regression tests (VERIFIED G-17).

The `c=` id set *replaces* the export header, so one wrong id silently drops a
column — the bug that once zeroed the entire universe (no `Ticker` was ever
requested). These tests pin the id -> header mapping against the recorded fact
table, and confirm the export guard fails loud on a misaligned set. No network:
the map is the verified constant, and the guard is exercised on synthetic text.
"""

from __future__ import annotations

import io

import pytest

from rotd.sources.base import SourceError
from rotd.sources.finviz import REQUIRED_COLUMNS, FinvizClient, merge_columns


# The verified table (VERIFIED.md G-17). Only the ids ROTD requests, plus the
# neighbours that the first cut confused them with.
VERIFIED_ID_NAME = {
    1: "Ticker", 2: "Company", 3: "Sector", 4: "Industry",
    6: "Market Cap", 24: "Shares Outstanding", 25: "Shares Float",
    30: "Short Float", 31: "Short Ratio", 48: "Beta",
    49: "Average True Range", 59: "Relative Strength Index (14)", 61: "Gap",
    63: "Average Volume", 64: "Relative Volume",
    65: "Price", 66: "Change", 67: "Volume",
    68: "Earnings Date", 69: "Target Price",
    71: "After-Hours Close", 72: "After-Hours Change", 81: "Prev Close",
    86: "Open", 87: "High", 88: "Low",
    90: "Performance (1 Minute)", 91: "Performance (2 Minutes)",
    92: "Performance (3 Minutes)", 93: "Performance (5 Minutes)",
    94: "Performance (10 Minutes)", 95: "Performance (15 Minutes)",
    97: "Performance (1 Hour)", 99: "Performance (4 Hours)",
}


def test_ticker_and_identity_columns_are_requested():
    # 1 = Ticker is non-negotiable: without it every row parses to no symbol.
    for cid in (1, 2, 3, 4):
        assert cid in REQUIRED_COLUMNS, f"id {cid} ({VERIFIED_ID_NAME[cid]}) missing"


def test_every_requested_id_has_a_verified_name():
    # no id may be requested that we have not mapped to a real Finviz column
    for cid in REQUIRED_COLUMNS:
        assert cid in VERIFIED_ID_NAME, f"id {cid} is not in the verified table"


def test_the_fields_parse_row_reads_are_all_requested():
    # the ids parse_row depends on, mapped from the verified table
    needed = {1, 2, 3, 4, 6, 24, 25, 30, 31, 48, 49, 59, 61, 63, 64,
              65, 66, 67, 71, 72, 81, 86, 87, 88, 90, 91, 92, 93, 94, 95, 97, 99}
    assert set(REQUIRED_COLUMNS) == needed


def test_merge_columns_appends_required_ids():
    merged = [int(x) for x in merge_columns("1,2").split(",")]
    assert merged[:2] == [1, 2]
    for cid in REQUIRED_COLUMNS:
        assert cid in merged
    # no duplicates
    assert len(merged) == len(set(merged))


def test_merge_columns_ignores_junk():
    merged = [int(x) for x in merge_columns("1, x, ,65").split(",")]
    assert 1 in merged and 65 in merged


def _csv(header: list[str]) -> str:
    return ",".join(f'"{h}"' for h in header) + "\n" + ",".join("0" for _ in header)


def test_guard_passes_on_a_complete_header():
    full = list(FinvizClient.REQUIRED_HEADERS) + ["Sector", "Volume"]
    # should not raise
    FinvizClient._assert_columns(_csv(full))


def test_guard_fails_loud_on_a_missing_column():
    missing = [h for h in FinvizClient.REQUIRED_HEADERS if h != "Ticker"]
    with pytest.raises(SourceError) as exc:
        FinvizClient._assert_columns(_csv(missing))
    assert "Ticker" in str(exc.value)
    assert "G-17" in str(exc.value)


def test_guard_is_silent_on_empty_text():
    # never crash on an empty body; the adapter handles that as no rows
    FinvizClient._assert_columns("")
    FinvizClient._assert_columns("\ufeff")


def test_guard_ignores_a_non_csv_body():
    # a token-rejection HTML body is checked earlier; the guard must not crash
    FinvizClient._assert_columns("<!doctype html><html></html>")
