"""Yahoo Finance chart client — last-resort bars and regime.

Verified (docs/VERIFIED.md G-13/G-14): ``v8/finance/chart`` works and carries
``hasPrePostMarketData: true`` and ``exchangeTimezoneName``; ``v7/finance/quote``
is **401** (deprecated). Used only as a last-resort fallback in the candle chain
(TwelveData -> Yahoo -> Alpaca).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .base import BaseClient, SourceError, fnum
from ..util import UTC

log = logging.getLogger(__name__)

BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


class YahooClient(BaseClient):
    name = "yahoo"

    def __init__(self, cfg, rate: Optional[tuple[int, float]] = (30, 60.0)):
        super().__init__(cfg, rate=rate)

    def chart(self, symbol: str, interval: str = "1m", range_: str = "1d",
              include_prepost: bool = True) -> dict[str, Any]:
        url = f"{BASE}/{symbol.upper()}"
        params = {"interval": interval, "range": range_,
                  "includePrePost": "true" if include_prepost else "false"}
        data = self.get_json(url, params=params,
                             headers={"User-Agent": "Mozilla/5.0"})
        result = ((data or {}).get("chart") or {}).get("result") or []
        if not result:
            err = ((data or {}).get("chart") or {}).get("error")
            raise SourceError(f"yahoo: no chart for {symbol} ({err})")
        meta = result[0].get("meta") or {}
        return {
            "symbol": (meta.get("symbol") or symbol).upper(),
            "price": fnum(meta.get("regularMarketPrice")),
            "prev_close": fnum(meta.get("chartPreviousClose")),
            "day_high": fnum(meta.get("regularMarketDayHigh")),
            "day_low": fnum(meta.get("regularMarketDayLow")),
            "volume": fnum(meta.get("regularMarketVolume")),
            "has_prepost": bool(meta.get("hasPrePostMarketData")),
            "tz": meta.get("exchangeTimezoneName"),
            "ts": datetime.fromtimestamp(int(meta["regularMarketTime"]), tz=UTC)
                  if meta.get("regularMarketTime") else None,
        }

    def bars(self, symbol: str, interval: str = "1m", range_: str = "1d") -> list[dict[str, Any]]:
        url = f"{BASE}/{symbol.upper()}"
        data = self.get_json(url, params={"interval": interval, "range": range_,
                                          "includePrePost": "true"},
                             headers={"User-Agent": "Mozilla/5.0"})
        result = ((data or {}).get("chart") or {}).get("result") or []
        if not result:
            raise SourceError(f"yahoo: no bars for {symbol}")
        r = result[0]
        stamps = r.get("timestamp") or []
        quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        out = []
        for i, ts in enumerate(stamps):
            def g(key):
                arr = quote.get(key) or []
                return fnum(arr[i]) if i < len(arr) else None
            out.append({"ts": datetime.fromtimestamp(int(ts), tz=UTC),
                        "open": g("open"), "high": g("high"), "low": g("low"),
                        "close": g("close"), "volume": g("volume")})
        return out

    def health(self) -> tuple[bool, str]:
        try:
            self.chart("AAPL")
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
