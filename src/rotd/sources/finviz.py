"""Finviz Elite client — ROTD's primary feed.

Finviz is the only source on these plans that carries extended-hours prices
(``After-Hours Close`` / ``After-Hours Change``), so it is the primary feed for
the universe, mover rows, quote rows, news, filings and the extended-hours high
(docs/VERIFIED.md G-3/G-6/G-8). The token is read from configuration on every
request, never cached in code, never logged.

Column ids verified against the live export
(``fixtures/finviz_universe.txt``): Price, Change, Volume, After-Hours Close,
After-Hours Change, Prev Close, Open, High, Low, Shares Float, Short Float,
Average Volume, Relative Volume, and the intraday Performance columns.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any, Optional
from urllib.parse import urlencode

from .base import (BaseClient, SourceError, fnum, finviz_avg_volume_shares,
                   looks_like_html, parse_csv)
from ..util import ET, now_utc

log = logging.getLogger(__name__)

FINVIZ_LINKS: dict[str, str] = {
    "screener": "/export/screener",
    "stock": "/export/stock",
    "groups": "/export/groups",
    "latest-filings": "/export/latest-filings",
    "news": "/export/news",
    "insiders": "/export/insiders",
    "calendar/economic": "/export/calendar/economic",
}

# Column ids (Finviz "custom" view). Kept here so a link change needs no new code.
COL: dict[str, int] = {
    "no": 0, "ticker": 1, "company": 2, "sector": 3, "industry": 4, "country": 5,
    "market_cap": 6, "shares_out": 25, "shares_float": 26, "short_float": 31,
    "short_ratio": 32, "avg_volume": 65, "rel_volume": 66,
    "price": 67, "change": 68, "volume": 69,
    "after_close": 71, "after_change": 72, "prev_close": 81,
    "open": 86, "high": 87, "low": 88,
    "perf_1m": 89, "perf_2m": 77, "perf_3m": 78,
    "perf_5m": 90, "perf_10m": 79, "perf_15m": 91, "perf_30m": 80,
    "perf_1h": 92, "perf_2h": 81, "perf_4h": 94,
    "perf_week": 71, "perf_month": 43, "perf_quarter": 44, "perf_year": 46,
    "atr": 49, "rsi": 60, "gap": 62, "beta": 48, "change_from_open": 61,
}

# The screener "signal" -> Finviz code. Only these bases honour a timeframe suffix.
SIGNALS: dict[str, str] = {
    "top_gainers": "ta_topgainers",
    "top_losers": "ta_toplosers",
    "new_high": "ta_newhigh",
    "most_active": "ta_mostactive",
    "most_volatile": "ta_mostvolatile",
    "unusual_volume": "ta_unusualvolume",
    "overbought": "ta_overbought",
    "oversold": "ta_oversold",
}
INTRADAY_BASES = ("ta_topgainers", "ta_toplosers")
INTRADAY_TIMEFRAMES = ("1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h")

# Overview/Valuation/... views ignore `c=`; always request the custom view.
FIXED_VIEWS = {"111", "121", "131", "141", "161", "171"}
DEFAULT_VIEW = "152"

# Columns the ROTD quote shape needs, merged into every screener request so a
# stale column list can never silently zero a field.
#
# EVERY id below is verified against the live export — see VERIFIED.md G-17 for
# the full id -> header table (probe: probes/probe_finviz_idmap.py). Do not hand-
# edit an id without re-probing: id 1 is Ticker, and a wrong id silently returns
# the wrong column (that is how the first cut zeroed the universe).
#
#  1 Ticker          2 Company         3 Sector        4 Industry
#  6 Market Cap     24 Shares Out     25 Shares Float 30 Short Float
# 31 Short Ratio    48 Beta           49 Average True Range
# 59 RSI(14)        61 Gap            63 Average Volume  64 Relative Volume
# 65 Price          66 Change         67 Volume          71 After-Hours Close
# 72 After-Hours Change  81 Prev Close 86 Open           87 High
# 88 Low            90 Perf(1m)       91 Perf(2m)       92 Perf(3m)
# 94 Perf(10m)
REQUIRED_COLUMNS: tuple[int, ...] = (1, 2, 3, 4,
                                     6, 24, 25, 30, 31, 48, 49, 59, 61, 63, 64,
                                     65, 66, 67, 71, 72, 81, 86, 87, 88,
                                     90, 91, 92, 93, 94, 95, 97, 99)


class FinvizTokenRejected(SourceError):
    """The token is invalid or expired."""


def merge_columns(columns: str) -> str:
    seen: list[int] = []
    for part in str(columns or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            cid = int(part)
        except ValueError:
            continue
        if cid not in seen:
            seen.append(cid)
    for cid in REQUIRED_COLUMNS:
        if cid not in seen:
            seen.append(cid)
    return ",".join(str(c) for c in seen)


def _http_status_ok(status: int) -> bool:
    return 200 <= status < 300


class FinvizClient(BaseClient):
    name = "finviz"

    def __init__(self, cfg, rate: Optional[tuple[int, float]] = (5, 60.0)):
        super().__init__(cfg, secrets=[cfg.raw("FINVIZ_TOKEN")], rate=rate)
        self.base = cfg.str("FINVIZ_BASE", "https://elite.finviz.com").rstrip("/")

    def _url(self, link: str, **params: Any) -> str:
        token = self.cfg.raw("FINVIZ_TOKEN")
        if not token:
            raise FinvizTokenRejected("finviz: FINVIZ_TOKEN is not set")
        query = {k: v for k, v in params.items() if v not in (None, "")}
        query["auth"] = token
        path = FINVIZ_LINKS.get(link, link)
        return f"{self.base}{path}?{urlencode(query)}"

    def _export(self, link: str, **params: Any) -> str:
        url = self._url(link, **params)
        resp = self._request("GET", url, accept="text/csv")
        if resp.status_code in (401, 403):
            raise FinvizTokenRejected(f"finviz: token rejected (HTTP {resp.status_code})")
        if not _http_status_ok(resp.status_code):
            raise SourceError(f"finviz: HTTP {resp.status_code} for {link}")
        text = resp.content.decode("utf-8", errors="replace")
        if looks_like_html(text):
            raise FinvizTokenRejected("finviz: got HTML (token rejected or logged out)")
        return text

    # -- endpoints ----------------------------------------------------------
    # Headers `parse_row` reads; a screener response missing any of these means
    # the requested `c=` id set is misaligned and every row would parse garbage
    # (VERIFIED G-17). Assert, never silently accept.
    REQUIRED_HEADERS: tuple[str, ...] = (
        "Ticker", "Price", "Change", "Volume", "After-Hours Close",
        "After-Hours Change", "Prev Close", "Open", "High", "Low",
    )

    def screener(self, signal: str = "", columns: str = "", tickers: str = "",
                 timeframe: str = "") -> list[dict[str, str]]:
        """Screener export. ``signal`` is a friendly name or a raw Finviz code."""
        code = SIGNALS.get(signal, signal)
        if code and timeframe:
            if code not in INTRADAY_BASES:
                raise SourceError(f"finviz: {code} does not support a timeframe")
            if timeframe not in INTRADAY_TIMEFRAMES:
                raise SourceError(f"finviz: unknown timeframe {timeframe!r}")
            code = f"{code}_{timeframe}"
        view = "152"
        text = self._export("screener", s=code, c=merge_columns(columns),
                            v=view, t=tickers)
        self._assert_columns(text)
        return parse_csv(text)

    @classmethod
    def _assert_columns(cls, text: str) -> None:
        """Fail loud if the export is missing a required column (VERIFIED G-17).

        The ``c=`` id set *replaces* the header, so a single wrong id drops a
        column silently. An empty universe once hid here; a header check turns
        that into an immediate, named error.
        """
        head = (text or "").lstrip("\ufeff").splitlines()[:1]
        if not head:
            return
        try:
            cols = next(csv.reader(io.StringIO(head[0])))
        except Exception:  # noqa: BLE001
            return
        # Only audit a genuine multi-column CSV header. A single field means this
        # is not a screener export (e.g. an error body); the caller's earlier
        # HTML/status checks own that case.
        if len(cols) < 2:
            return
        have = {c.strip() for c in cols}
        missing = [h for h in cls.REQUIRED_HEADERS if h not in have]
        if missing:
            raise SourceError(
                f"finviz: export missing required columns {missing} — the "
                f"REQUIRED_COLUMNS id set is misaligned (see VERIFIED G-17)")

    def universe(self) -> list[dict[str, str]]:
        """Whole sub-$10 universe (no ``s=`` filter; price band applied downstream)."""
        return self.screener(columns="")

    def stock(self, ticker: str) -> list[dict[str, str]]:
        return parse_csv(self._export("stock", t=ticker.upper()))

    def news(self, ticker: str = "") -> list[dict[str, str]]:
        return parse_csv(self._export("news", t=ticker.upper()))

    def latest_filings(self, ticker: str) -> list[dict[str, str]]:
        """Filings for one ticker. Bare calls 400 ('Unable to get CIK'); pass a ticker."""
        if not ticker:
            raise SourceError("finviz: latest-filings requires a ticker (VERIFIED G-2)")
        return parse_csv(self._export("latest-filings", t=ticker.upper()))

    def groups(self, group: str = "sector") -> list[dict[str, str]]:
        return parse_csv(self._export("groups", g=group))

    # -- row shape ----------------------------------------------------------
    @staticmethod
    def parse_row(row: dict[str, str]) -> dict[str, Any]:
        """Normalise one Finviz row into ROTD fields.

        Plain ``Price`` freezes at the regular close after 16:00, so the
        after-hours pair is carried separately and the caller (reconcile) chooses
        which basis to use.
        """
        price = fnum(row.get("Price"))
        after = fnum(row.get("After-Hours Close"))
        return {
            "symbol": (row.get("Ticker") or "").upper().strip(),
            "company": row.get("Company") or "",
            "sector": row.get("Sector") or "",
            "industry": row.get("Industry") or "",
            "market_cap": fnum(row.get("Market Cap")),
            "shares_out": fnum(row.get("Shares Outstanding")),
            "shares_float": fnum(row.get("Shares Float")),
            "short_float": fnum(row.get("Short Float")),
            "short_ratio": fnum(row.get("Short Ratio")),
            "avg_volume": finviz_avg_volume_shares(row),
            "rel_volume": fnum(row.get("Relative Volume")),
            "price": price,
            "change_pct": fnum(row.get("Change")),
            "volume": fnum(row.get("Volume")),
            "after_close": after,
            "after_change_pct": fnum(row.get("After-Hours Change")),
            "prev_close": fnum(row.get("Prev Close")),
            "open": fnum(row.get("Open")),
            "high": fnum(row.get("High")),
            "low": fnum(row.get("Low")),
            "atr": fnum(row.get("Average True Range")),
            "gap_pct": fnum(row.get("Gap")),
            "rsi": fnum(row.get("Relative Strength Index (14)")),
            "beta": fnum(row.get("Beta")),
            "perf": {
                "1m": fnum(row.get("Performance (1 Minute)")),
                "5m": fnum(row.get("Performance (5 Minutes)")),
                "15m": fnum(row.get("Performance (15 Minutes)")),
                "1h": fnum(row.get("Performance (1 Hour)")),
                "4h": fnum(row.get("Performance (4 Hours)")),
            },
            "fetched_at": now_utc(),
        }

    def health(self) -> tuple[bool, str]:
        try:
            self._export("news")
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
