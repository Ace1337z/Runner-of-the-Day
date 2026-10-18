"""Alpaca client — batched quote freshness and an optional candle fallback.

Alpaca's ``/v2/stocks/quotes/latest?symbols=...`` endpoint returns bid/ask for up
to 40 symbols in one round trip, which is how the runtime gets a real spread for
the tracked pool without a per-symbol request.

**Key status** (docs/VERIFIED.md G-9): a paper-account key pair works against
``data.alpaca.markets`` on the free ``iex`` feed. SIP is 403 on that plan, so
``ALPACA_FEED=iex`` stays the default. The adapter still fails soft: a rejected
key reports ``BLOCKED`` and the runtime keeps the Finviz-only record.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from .base import BaseClient, SourceError, fnum
from ..util import UTC, now_utc

log = logging.getLogger(__name__)

DATA_BASE = "https://data.alpaca.markets"
MAX_QUOTE_SYMBOLS = 40


class AlpacaBlocked(SourceError):
    """The Alpaca credentials were rejected by the provider."""


class AlpacaClient(BaseClient):
    name = "alpaca"

    def __init__(self, cfg, rate: Optional[tuple[int, float]] = (180, 60.0)):
        super().__init__(cfg, secrets=[cfg.raw("ALPACA_KEY"), cfg.raw("ALPACA_SECRET")],
                         rate=rate)

    def _headers(self) -> dict[str, str]:
        key = self.cfg.raw("ALPACA_KEY")
        secret = self.cfg.raw("ALPACA_SECRET")
        if not key or not secret:
            raise AlpacaBlocked("alpaca: ALPACA_KEY/ALPACA_SECRET are not set")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _get(self, path: str, **params: Any) -> Any:
        feed = self.cfg.str("ALPACA_FEED", "iex")
        params.setdefault("feed", feed)
        resp = self._request("GET", f"{DATA_BASE}{path}", headers=self._headers(),
                             params=params, accept="application/json")
        if resp.status_code in (401, 403):
            raise AlpacaBlocked("alpaca: credentials rejected (HTTP 401/403) — "
                                "regenerate the key (VERIFIED G-9)")
        if resp.status_code >= 400:
            raise SourceError(f"alpaca: HTTP {resp.status_code}")
        return resp.json()

    def bars(self, symbol: str, timeframe: str = "1Min", limit: int = 120,
             start: Optional[datetime] = None) -> list[dict[str, Any]]:
        # Alpaca returns ``bars: null`` when neither start nor end is given, so
        # default to a window that covers the last ``limit`` bars. The window is
        # at least 5 days wide so a weekend/holiday gap still finds a session.
        unit = 86400 if timeframe.endswith("Day") else 60
        params: dict[str, Any] = {"timeframe": timeframe, "limit": limit}
        if start is None:
            span = max(unit * max(limit, 1), 5 * 86400)
            start = now_utc() - timedelta(seconds=span)
            params["sort"] = "desc"  # newest first, then re-ordered below
        params["start"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._get(f"/v2/stocks/{symbol.upper()}/bars", **params)
        out = []
        for b in (data or {}).get("bars") or []:
            ts = b.get("t")
            out.append({
                "ts": datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)
                      if ts else None,
                "open": fnum(b.get("o")), "high": fnum(b.get("h")),
                "low": fnum(b.get("l")), "close": fnum(b.get("c")),
                "volume": fnum(b.get("v")),
            })
        out.sort(key=lambda b: b["ts"] or datetime.min.replace(tzinfo=UTC))
        return out

    def latest_quote(self, symbol: str) -> dict[str, Any]:
        data = self._get(f"/v2/stocks/{symbol.upper()}/quotes/latest")
        q = (data or {}).get("quote") or {}
        return {"bid": fnum(q.get("bp")), "ask": fnum(q.get("ap")),
                "bid_size": fnum(q.get("bs")), "ask_size": fnum(q.get("as"))}

    def latest_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Batched quotes for up to ``MAX_QUOTE_SYMBOLS`` symbols in one request.

        Returns ``{SYMBOL: {bid, ask, ts}}``; symbols with no usable quote are
        omitted. Raises ``AlpacaBlocked``/``SourceError`` (callers fail soft).
        """
        syms = [s.upper() for s in dict.fromkeys(symbols) if s][:MAX_QUOTE_SYMBOLS]
        if not syms:
            return {}
        data = self._get("/v2/stocks/quotes/latest", symbols=",".join(syms))
        out: dict[str, dict[str, Any]] = {}
        for sym, q in ((data or {}).get("quotes") or {}).items():
            bid, ask = fnum((q or {}).get("bp")), fnum((q or {}).get("ap"))
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                continue
            ts = (q or {}).get("t")
            out[sym.upper()] = {
                "bid": bid, "ask": ask,
                "ts": datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)
                      if ts else None,
            }
        return out

    def health(self) -> tuple[bool, str]:
        if not self.cfg.raw("ALPACA_KEY"):
            return False, "BLOCKED: no key configured"
        try:
            # The quotes endpoint is the one the runtime actually uses; it
            # validates auth and the feed entitlement in one call.
            self.latest_quotes(["AAPL"])
            return True, "ok"
        except AlpacaBlocked:
            return False, "BLOCKED: credentials rejected (regenerate key)"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
