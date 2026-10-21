"""Finviz per-ticker news parsing (quote page).

The /export/news endpoint ignores the ticker filter (it returns market-wide
news); the per-ticker feed lives on the quote page (quote.ashx?t=TICKER) with
finviz-hosted article links (/news/ID/slug). This pins the parser against the
captured fixture. No network.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from rotd.sources.finviz import _parse_quote_news

FIX = Path(__file__).resolve().parents[1] / "src" / "rotd" / "sources" / "fixtures"


def test_quote_news_parses_finviz_article_urls():
    html = (FIX / "finviz_quote_news.txt").read_text(encoding="utf-8")
    rows = _parse_quote_news(html)
    assert len(rows) == 2
    first = rows[0]
    assert first["Title"] == ("House of Doge Announces Board of Directors "
                              "Following Nasdaq Public Listing")
    # finviz-hosted link is made absolute against the elite host
    assert first["Url"] == ("https://elite.finviz.com/news/368464/"
                            "house-of-doge-announces-board-of-directors-"
                            "following-nasdaq-public-listing")
    assert first["Source"] == "GlobeNewswire"
    assert first["Date"] == "Jul-15-26 08:30AM"


def test_quote_news_keeps_external_urls():
    html = (FIX / "finviz_quote_news.txt").read_text(encoding="utf-8")
    rows = _parse_quote_news(html)
    second = rows[1]
    assert second["Url"].startswith("https://finance.yahoo.com/")
    assert second["Source"] == "Stocktwits"


def test_quote_news_empty_html_is_empty():
    assert _parse_quote_news("") == []
    assert _parse_quote_news("<html><body>no news</body></html>") == []


def test_quote_news_date_parses_to_age():
    """The quote-page date (Jul-15-26 08:30AM, ET) must yield a real age."""
    from datetime import datetime

    from rotd.runtime import _row_age_min
    from rotd.util import UTC

    now = datetime(2026, 7, 15, 13, 0, tzinfo=UTC)  # 09:00 ET
    assert _row_age_min("Jul-15-26 08:30AM", now) == 30.0
    # 09:30PM ET the previous day is 11.5h before 09:00 ET -> 690 min
    next_day = datetime(2026, 7, 16, 13, 0, tzinfo=UTC)  # 09:00 ET next day
    assert _row_age_min("Jul-15-26 09:30PM", next_day) == 690.0
    # ISO-8601 (SEC/export) still parses
    assert _row_age_min("2026-07-15T12:30:00+00:00", now) == 30.0
    assert _row_age_min("not a date", now) is None
    assert _row_age_min("", now) is None


# --- Finviz Elite same-day rows + catalyst pipeline ------------------------
#
# Fixes: "Today HH:MMAM" timestamps, the 3-minute re-poll of empty symbols,
# classification wiring into the SQL catalysts table and the 14h cause window.

class _FakeFinviz:
    """Finviz client stand-in: returns queued rows, counts news() calls."""

    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []
        self.news_calls = 0

    def news(self, ticker: str = "") -> list[dict[str, str]]:
        self.news_calls += 1
        return list(self.rows)

    def latest_filings(self, ticker: str) -> list[dict[str, str]]:
        return []


def _news_row(title: str = "EXMP receives FDA approval",
              date: str = "Today 08:00AM",
              url: str = "https://elite.finviz.com/news/42/exmp-fda") -> dict[str, str]:
    return {"Title": title, "Date": date, "Url": url, "Source": "PR",
            "Category": ""}


def test_today_timestamp_parses_to_age(now):
    """Finviz Elite same-day rows ("Today 08:00AM") must yield a real age.

    ``now`` is 2026-10-13 14:30 UTC = 10:30 ET (EDT), so a 08:00AM ET row is
    150 minutes old. Unparseable rows used to return None -> age_class OLD,
    which fed the fact bundle a stale cause and tanked the catalyst score.
    """
    from rotd.runtime import _row_age_min

    assert _row_age_min("Today 08:00AM", now) == 150.0
    # bare time is the same-day form without the "Today" prefix
    assert _row_age_min("08:00AM", now) == 150.0
    # a same-day time still in the future belongs to yesterday
    assert _row_age_min("Today 04:15PM", now) == 1095.0
    # AM/PM edges: 12:00AM = midnight (630m), 12:00PM = noon yesterday (1350m)
    assert _row_age_min("Today 12:00AM", now) == 630.0
    assert _row_age_min("Today 12:00PM", now) == 1350.0
    # ISO and the dated quote-page format are untouched
    assert _row_age_min("2026-10-13T12:30:00+00:00", now) == 120.0
    assert _row_age_min("not a date", now) is None


def test_cause_window_keeps_morning_press_fresh_at_midday(cfg):
    """A 08:00 ET press release read at 14:00 ET is 6h old: inside the 14h
    window (the old 4h window dropped it, leaving CATALYST with nothing)."""
    from rotd.runtime import Runtime
    from datetime import datetime
    from rotd.util import UTC

    now_mid = datetime(2026, 10, 13, 18, 0, tzinfo=UTC)  # 14:00 ET
    row = _news_row(date="Today 08:00AM")
    window_h = cfg.float("AI_CAUSE_WINDOW_H", 4)
    assert window_h == 14
    item = Runtime._news_item(row, now_mid, window_h)
    assert item is not None
    assert item["age_class"] == "FRESH"
    # and the legacy 4h window would have dropped the same row
    assert Runtime._news_item(row, now_mid, 4.0) is None


def test_fetch_catalyst_items_repolls_empty_after_3m(cfg, now):
    """An empty poll no longer locks the symbol out for the rest of the day."""
    from rotd.runtime import REFETCH_EMPTY_S, Runtime

    assert REFETCH_EMPTY_S == 180.0
    fv = _FakeFinviz()
    rt = Runtime(cfg, None, {"finviz": fv}, None)

    # First poll comes back empty (nothing published yet).
    rt._fetch_catalyst_items(["AHT"], now)
    assert fv.news_calls == 1
    assert rt._catalyst_items["AHT"] == []

    # Inside the 3-minute window the symbol is throttled, not fetched.
    for delta in (30, 179):
        rt._fetch_catalyst_items(["AHT"], now + timedelta(seconds=delta))
    assert fv.news_calls == 1

    # After the window a later (midday) press release is picked up.
    fv.rows = [_news_row(title="AHT prices registered direct offering",
                         url="https://elite.finviz.com/news/99/aht-offering")]
    rt._fetch_catalyst_items(["AHT"], now + timedelta(seconds=REFETCH_EMPTY_S))
    assert fv.news_calls == 2
    assert rt._catalyst_items["AHT"][0]["age_class"] == "FRESH"

    # Once items exist the symbol is done: no further polling.
    rt._fetch_catalyst_items(["AHT"], now + timedelta(hours=3))
    assert fv.news_calls == 2


def test_audit_candidates_classifies_headlines_into_store(cfg, tmp_path, now):
    """Headlines reach classify() so verdicts land in `catalysts` and feed
    the 0..100 Stage-1 CATALYST bridge (fixes 1 + 3 end to end)."""
    import json as _json

    import pytest

    from rotd.auditor import AuditorService
    from rotd.catalyst import CatalystService
    from rotd.features import compute_features
    from rotd.gates import run_gates
    from rotd.normalize import Canonical, Snapshot
    from rotd.runtime import Runtime
    from rotd.scoring import rank, score_candidate
    from rotd.store import Store

    day = now.date()
    rec = Canonical(symbol="EXMP", price=2.31, prev_close=1.90, open=2.00,
                    volume=3_000_000, volume_basis="SINGLE_VENUE", bid=2.30,
                    ask=2.32, spread_pct=0.9, day_high=2.40, day_low=1.98,
                    shares_float=4_000_000, short_float=22.0,
                    market_cap=9_240_000, company="Exemplar Inc",
                    data_grade="A", fetched_at=now)
    snap = Snapshot(trading_day=day, mode="OPEN", cycle_ts=now,
                    records={"EXMP": rec})
    fs = compute_features(rec, cfg, snap)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    rk = rank([sc], cfg)

    store = Store(tmp_path / "r.db")
    # Injected caller => configured() is True without a live endpoint; the
    # per-item model call is stubbed so the real classify() -> parse -> SQL
    # path runs offline.
    aud = AuditorService(cfg, caller=lambda system, user: "{}")
    svc = CatalystService(cfg, store, auditor=aud)
    svc._call_model = lambda headline, body: _json.dumps(
        {"verdict": "MAJOR", "confidence": 0.9, "reason": "definitive catalyst"})

    fv = _FakeFinviz()
    fv.rows = [_news_row()]
    rt = Runtime(cfg, store, {"finviz": fv}, None, catalyst=svc)

    out = rt._audit_candidates(day, "OPEN", now, rk, snap,
                               {s.symbol: s for s in rk.day_ranked},
                               {"EXMP": rep}, {"EXMP": fs}, async_mode=False)

    item_id = _news_row()["Url"]
    row = store.get_catalyst("EXMP", item_id)
    assert row is not None, "classification must be written to the catalysts table"
    assert row["verdict"] == "MAJOR"
    assert row["headline"] == _news_row()["Title"]
    assert out["audited"] == ["EXMP"]
    assert "EXMP" in out["verdicts"]
    # Stage-1 bridge: MAJOR @ 0.9 confidence -> 90 * (0.5 + 0.5*0.9)
    assert rt._catalyst_score("EXMP") == pytest.approx(85.5)
    store.close()


def test_audit_candidates_async_dispatches_classification(cfg, now):
    """async_mode must hand headlines to classify_async() with kind+headline."""
    from rotd.features import compute_features
    from rotd.gates import run_gates
    from rotd.normalize import Canonical, Snapshot
    from rotd.runtime import Runtime
    from rotd.scoring import rank, score_candidate

    calls: list[tuple] = []

    class _StubCatalyst:
        def configured(self) -> bool:
            return True

        def classify_async(self, sym, iid, kd, hl):
            calls.append(("classify_async", sym, iid, kd, hl))

        def classify(self, sym, iid, kd, hl):
            calls.append(("classify", sym, iid, kd, hl))

        def audit_report_async(self, inp):
            calls.append(("audit_async", inp.symbol))

        def audit_report(self, inp):
            calls.append(("audit", inp.symbol))

    day = now.date()
    rec = Canonical(symbol="EXMP", price=2.31, prev_close=1.90, open=2.00,
                    volume=3_000_000, volume_basis="SINGLE_VENUE", bid=2.30,
                    ask=2.32, spread_pct=0.9, day_high=2.40, day_low=1.98,
                    shares_float=4_000_000, short_float=22.0,
                    market_cap=9_240_000, company="Exemplar Inc",
                    data_grade="A", fetched_at=now)
    snap = Snapshot(trading_day=day, mode="OPEN", cycle_ts=now,
                    records={"EXMP": rec})
    fs = compute_features(rec, cfg, snap)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    rk = rank([sc], cfg)

    fv = _FakeFinviz()
    fv.rows = [_news_row()]
    rt = Runtime(cfg, None, {"finviz": fv}, None, catalyst=_StubCatalyst())

    out = rt._audit_candidates(day, "OPEN", now, rk, snap,
                               {s.symbol: s for s in rk.day_ranked},
                               {"EXMP": rep}, {"EXMP": fs}, async_mode=True)

    item_id = _news_row()["Url"]
    title = _news_row()["Title"]
    assert ("classify_async", "EXMP", item_id, "news", title) in calls, calls
    assert ("audit_async", "EXMP") in calls, calls
    # async fills verdicts on completion, never inline
    assert out["verdicts"] == {}
    assert out["audited"] == ["EXMP"]
