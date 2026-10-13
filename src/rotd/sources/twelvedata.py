"""TwelveData client — regular-session bars and a freshness quote.

Verified (docs/VERIFIED.md G-4/G-5/G-6): ``quote`` and regular-session
``time_series`` (1-min) work; ``prepost=true`` is **403** (Pro plan only), so
TwelveData is NOT an extended-hours source. Fields verified against
``fixtures/twelvedata_quote.txt`` and ``fixtures/twelvedata_time_series.txt``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .base import BaseClient, SourceError, fnum
from ..util import UTC, ET

log = logging.getLogger(__name__)

BASE = "https://api.twelvedata.com"


class TwelveDataClient(BaseClient):
    name = "twelvedata"

    def __init__(self, cfg, rate: Optional[tuple[int, float]] = (8, 60.0)):
        super().__init__(cfg, secrets=[cfg.raw("TWELVEDATA_KEY")], rate=rate)

    def _key(self) -> str:
        key = self.cfg.raw("TWELVEDATA_KEY")
        if not key:
            raise SourceError("twelvedata: TWELVEDATA_KEY is not set")
        return key

    def _get(self, path: str, **params: Any) -> Any:
        params["apikey"] = self._key()
        data = self.get_json(f"{BASE}{path}", params=params)
        if isinstance(data, dict) and data.get("status") == "error":
            raise SourceError(f"twelvedata: {data.get('message', 'error')}")
        return data

    # -- endpoints ----------------------------------------------------------
    def quote(self, symbol: str) -> dict[str, Any]:
        data = self._get("/quote", symbol=symbol.upper())
        if not isinstance(data, dict) or "close" not in data:
            raise SourceError(f"twelvedata: no quote for {symbol}")
        ts = data.get("timestamp")
        return {
            "symbol": (data.get("symbol") or symbol).upper(),
            "price": fnum(data.get("close")),
            "open": fnum(data.get("open")),
            "high": fnum(data.get("high")),
            "low": fnum(data.get("low")),
            "volume": fnum(data.get("volume")),
            "prev_close": fnum(data.get("previous_close")),
            "change": fnum(data.get("change")),
            "change_pct": fnum(data.get("percent_change")),
            "avg_volume": fnum(data.get("average_volume")),
            "is_market_open": bool(data.get("is_market_open")),
            "exchange": data.get("exchange"),
            "ts": datetime.fromtimestamp(int(ts), tz=UTC) if ts else None,
            "fetched_at": datetime.now(UTC),
        }

    def time_series(self, symbol: str, interval: str = "1min",
                    outputsize: int = 60) -> list[dict[str, Any]]:
        """Regular-session bars. Extended hours require the Pro plan (403)."""
        data = self._get("/time_series", symbol=symbol.upper(), interval=interval,
                         outputsize=outputsize, order="ASC")
        values = (data or {}).get("values") or []
        out = []
        for v in values:
            dt = self._parse_dt(v.get("datetime"))
            out.append({
                "ts": dt,
                "open": fnum(v.get("open")),
                "high": fnum(v.get("high")),
                "low": fnum(v.get("low")),
                "close": fnum(v.get("close")),
                "volume": fnum(v.get("volume")),
            })
        return [b for b in out if b["ts"] is not None]

    def time_series_extended(self, symbol: str, interval: str = "1min",
                             outputsize: int = 60) -> list[dict[str, Any]]:
        """Extended-hours bars. Expected to fail on the current plan (403)."""
        data = self._get("/time_series", symbol=symbol.upper(), interval=interval,
                         outputsize=outputsize, prepost="true", order="ASC")
        values = (data or {}).get("values") or []
        return [{"ts": self._parse_dt(v.get("datetime")), "close": fnum(v.get("close")),
                 "volume": fnum(v.get("volume"))} for v in values]

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=ET).astimezone(UTC)
            except ValueError:
                continue
        return None

    def health(self) -> tuple[bool, str]:
        try:
            self.quote("AAPL")
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
