"""Finviz per-ticker news parsing (quote page).

The /export/news endpoint ignores the ticker filter (it returns market-wide
news); the per-ticker feed lives on the quote page (quote.ashx?t=TICKER) with
finviz-hosted article links (/news/ID/slug). This pins the parser against the
captured fixture. No network.
"""
from __future__ import annotations

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
