"""Alpha Vantage client — free ``GLOBAL_QUOTE`` only.

Verified (docs/VERIFIED.md G-10/G-11): ``GLOBAL_QUOTE`` works on the free key;
``TIME_SERIES_INTRADAY`` with ``extended_hours=true`` is a **premium** endpoint,
so Alpha Vantage is a last-resort regular-hours quote, never an extended-hours
source. Fields verified against ``fixtures/alpha_vantage_global_quote.txt``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .base import BaseClient, SourceError, fnum
from ..util import UTC

log = logging.getLogger(__name__)

BASE = "https://www.alphavantage.co/query"


class AlphaVantageClient(BaseClient):
    name = "alpha_vantage"

    def __init__(self, cfg, rate: Optional[tuple[int, float]] = (5, 60.0)):
        super().__init__(cfg, secrets=[cfg.raw("ALPHA_VANTAGE_KEY")], rate=rate)

    def _get(self, **params: Any) -> Any:
        key = self.cfg.raw("ALPHA_VANTAGE_KEY")
        if not key:
            raise SourceError("alpha_vantage: ALPHA_VANTAGE_KEY is not set")
        params["apikey"] = key
        data = self.get_json(BASE, params=params)
        if isinstance(data, dict):
            # A frequency/limit notice returns 200 with a message key.
            for k in ("Note", "Information", "Error Message"):
                if k in data:
                    raise SourceError(f"alpha_vantage: {str(data[k])[:120]}")
        return data

    def global_quote(self, symbol: str) -> dict[str, Any]:
        data = self._get(**{"function": "GLOBAL_QUOTE", "symbol": symbol.upper()})
        q = (data or {}).get("Global Quote") or {}
        if not q:
            raise SourceError(f"alpha_vantage: no quote for {symbol}")
        day = q.get("07. latest trading day")
        ts = None
        if day:
            try:
                ts = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                ts = None
        return {
            "symbol": (q.get("01. symbol") or symbol).upper(),
            "open": fnum(q.get("02. open")),
            "high": fnum(q.get("03. high")),
            "low": fnum(q.get("04. low")),
            "price": fnum(q.get("05. price")),
            "volume": fnum(q.get("06. volume")),
            "prev_close": fnum(q.get("08. previous close")),
            "change": fnum(q.get("09. change")),
            "change_pct": fnum((q.get("10. change percent") or "").rstrip("%")),
            "trading_day": ts,
            "fetched_at": datetime.now(UTC),
        }

    def health(self) -> tuple[bool, str]:
        try:
            self.global_quote("AAPL")
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
