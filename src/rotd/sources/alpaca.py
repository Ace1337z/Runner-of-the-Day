"""Alpaca client — batched quote freshness and an optional candle fallback.

Alpaca's ``/v2/stocks/quotes/latest?symbols=...`` endpoint returns bid/ask for up
to 40 symbols in one round trip, which is how the runtime gets a real spread for
the tracked pool without a per-symbol request.

**Key status** (docs/VERIFIED.md G-9): the originally supplied key pair was
rejected with a clean ``401 unauthorized``. The adapter is built and fails soft —
``health()`` reports ``BLOCKED`` until a working key is configured, and the
runtime simply keeps the Finviz-only record. Do not depend on Alpaca.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from .base import BaseClient, SourceError, fnum
from ..util import UTC

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

    def bars(self, symbol: str, timeframe: str = "1Min", limit: int = 120) -> list[dict[str, Any]]:
        data = self._get(f"/v2/stocks/{symbol.upper()}/bars", timeframe=timeframe,
                         limit=limit)
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
            self.bars("AAPL", limit=1)
            return True, "ok"
        except AlpacaBlocked:
            return False, "BLOCKED: credentials rejected (regenerate key)"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
