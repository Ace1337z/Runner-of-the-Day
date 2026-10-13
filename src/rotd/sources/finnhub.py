"""Finnhub client — quote, news and the session detector.

Verified (docs/VERIFIED.md G-7/G-8): ``/quote``, ``/company-news`` and
``/stock/market-status`` work; ``/stock/candle`` is **403** (premium), so Finnhub
is quote/news/**session** only, not a candle fallback. ``market-status`` returns
``{"session":"post-market","isOpen":false,"timezone":"America/New_York"}`` and is
used as a cross-check on the local exchange calendar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .base import BaseClient, SourceError, fnum
from ..util import UTC

log = logging.getLogger(__name__)

BASE = "https://finnhub.io/api/v1"

# Finnhub session labels -> ROTD modes.
SESSION_MAP = {
    "pre-market": "PRE",
    "regular": "OPEN",       # narrows to OPEN vs MIDDAY by the local clock
    "post-market": "POST",
    "closed": "CLOSED",
}


class FinnhubClient(BaseClient):
    name = "finnhub"

    def __init__(self, cfg, rate: Optional[tuple[int, float]] = (55, 60.0)):
        super().__init__(cfg, secrets=[cfg.raw("FINNHUB_KEY")], rate=rate)

    def _get(self, path: str, **params: Any) -> Any:
        key = self.cfg.raw("FINNHUB_KEY")
        if not key:
            raise SourceError("finnhub: FINNHUB_KEY is not set")
        params["token"] = key
        return self.get_json(f"{BASE}{path}", params=params)

    # -- endpoints ----------------------------------------------------------
    def quote(self, symbol: str) -> dict[str, Any]:
        """Quote. ``c``=current, ``pc``=prev close, ``t``=unix ts (see fixture)."""
        d = self._get("/quote", symbol=symbol.upper())
        if not isinstance(d, dict) or not d.get("c"):
            raise SourceError(f"finnhub: no quote for {symbol}")
        ts = d.get("t")
        return {
            "symbol": symbol.upper(),
            "price": fnum(d.get("c")),
            "change": fnum(d.get("d")),
            "change_pct": fnum(d.get("dp")),
            "high": fnum(d.get("h")),
            "low": fnum(d.get("l")),
            "open": fnum(d.get("o")),
            "prev_close": fnum(d.get("pc")),
            "ts": datetime.fromtimestamp(int(ts), tz=UTC) if ts else None,
        }

    def market_status(self) -> dict[str, Any]:
        d = self._get("/stock/market-status", exchange="US")
        if not isinstance(d, dict):
            raise SourceError("finnhub: bad market-status")
        session = (d.get("session") or "").lower()
        return {
            "session": session,
            "mode": SESSION_MAP.get(session, "UNKNOWN"),
            "is_open": bool(d.get("isOpen")),
            "holiday": d.get("holiday"),
            "timezone": d.get("timezone"),
            "ts": datetime.fromtimestamp(int(d["t"]), tz=UTC) if d.get("t") else None,
        }

    def company_news(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        d = self._get("/company-news", symbol=symbol.upper(), **{"from": start, "to": end})
        if not isinstance(d, list):
            raise SourceError("finnhub: bad company-news")
        return [{"id": str(item.get("id", "")), "headline": item.get("headline", ""),
                 "summary": item.get("summary", ""), "source": item.get("source", ""),
                 "url": item.get("url", ""),
                 "ts": datetime.fromtimestamp(int(item["datetime"]), tz=UTC)
                       if item.get("datetime") else None}
                for item in d]

    def health(self) -> tuple[bool, str]:
        try:
            self.market_status()
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
