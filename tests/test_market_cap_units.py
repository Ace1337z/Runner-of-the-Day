"""Market-cap unit normalisation at the ingestion boundary.

Finviz reports ``Market Cap`` in the column's own units. A suffixed header value
("895M") is scaled correctly by ``fnum``, but a bare ``895`` is the *same* $895M
with the unit dropped by the export. The universe ceiling and G-PRICE both
compare against ``MAX_MARKET_CAP`` in literal dollars, so a bare millions value
silently passed a $100M filter.

Observed in production (2026-10-27): XNDU alerted with
``G-PRICE detail "mcap 895 <= 100,000,000"`` on a ~$895M company. Against the
shipped universe fixture the defect admitted 2,313 of 2,333 names (99.1%),
i.e. the market-cap scope was effectively disabled.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from rotd.normalize import (MARKET_CAP_MILLIONS_CEILING, normalize_market_cap,
                            normalize_float_shares)
from rotd.sources.base import fnum

CAP = 100_000_000.0

FIXTURE = (Path(__file__).resolve().parents[1]
           / "src" / "rotd" / "sources" / "fixtures" / "finviz_universe.txt")


@pytest.mark.parametrize("raw,expected", [
    ("895", 895_000_000.0),        # the XNDU case: millions, no suffix
    ("895M", 895_000_000.0),       # the same value with a suffix
    ("896", 896_000_000.0),
    ("895000000", 895_000_000.0),  # already literal dollars: unchanged
    ("32.06", 32_060_000.0),
    ("48859.64", 48_859_640_000.0),
])
def test_market_cap_is_rescaled_from_millions(raw: str, expected: float):
    assert normalize_market_cap(fnum(raw)) == pytest.approx(expected)


def test_the_xndu_company_now_fails_the_scope_ceiling():
    """A ~$895M name must never pass a $100M ceiling again."""
    assert normalize_market_cap(fnum("895")) > CAP


def test_genuine_sub_cap_names_still_pass():
    for raw in ["45", "75", "99"]:
        assert normalize_market_cap(fnum(raw)) <= CAP


def test_none_and_unusable_values_stay_none():
    for raw in [None, "", "n/a", "-"]:
        assert normalize_market_cap(fnum(raw)) is None


def test_zero_and_negatives_are_left_for_the_caller():
    """Fail-closed checks live downstream; this must not invent a value."""
    assert normalize_market_cap(0) == 0
    assert normalize_market_cap(-5) == -5


def test_float_normalisation_is_unchanged():
    """The market-cap ceiling must not disturb the float contract."""
    assert normalize_float_shares(5.15) == 5_150_000
    assert normalize_float_shares(5_150_000) == 5_150_000


def test_ceiling_sits_far_below_any_plausible_cap_in_dollars():
    assert MARKET_CAP_MILLIONS_CEILING >= 1_000.0
    assert MARKET_CAP_MILLIONS_CEILING <= 1_000_000.0


def test_real_universe_fixture_scopes_to_micro_caps():
    """The fixture is real Finviz data and must now filter as designed."""
    rows = list(csv.DictReader(io.StringIO(FIXTURE.read_text())))
    assert len(rows) > 1000
    passed = [r for r in rows
              if normalize_market_cap(fnum(r.get("Market Cap"))) is not None
              and normalize_market_cap(fnum(r.get("Market Cap"))) <= CAP]
    ratio = len(passed) / len(rows)
    # Before the fix this was 99.1%: the ceiling was a no-op.
    assert ratio < 0.75, f"{ratio:.1%} of the universe passed a $100M ceiling"
    assert ratio > 0.10, f"{ratio:.1%} is implausibly tight; check the ceiling"


def test_fixture_contains_a_name_the_old_behaviour_admitted():
    """Guards the regression itself: XNDU-class values must be rejected."""
    rows = list(csv.DictReader(io.StringIO(FIXTURE.read_text())))
    caps = [normalize_market_cap(fnum(r.get("Market Cap"))) for r in rows]
    big = [c for c in caps if c is not None and c > CAP]
    assert big, "fixture should contain large caps"
    # The old behaviour compared the raw millions figure against a dollar
    # ceiling (e.g. 48859 <= 100,000,000), so every one of these was admitted.
    raw_small = [fnum(r.get("Market Cap")) for r in rows]
    assert all(v <= CAP for v in raw_small if v is not None), \
        "raw millions values should all sit under the old dollar ceiling"
