"""TradingView scanner client — keyless batch freshness quotes (one POST per cycle).

Purpose: remove the TwelveData free-tier bottleneck. One scanner POST returns
price/change/volume for every tracked symbol in a single round trip, so the paced
one-quote-per-cycle TwelveData path is only a fallback.

**Column set is deliberately minimal** (``name, close, change, volume``). The
commonly circulated payload also asks for ``close|15``/``volume|15``/
``Value.Traded|15``/``premarket_*``/``float_shares_outstanding``. Those are
excluded on purpose:
  - the 15-minute aggregates are a 15-minute print feed, which the design forbids
    feeding into the decision path (VERIFIED G-18);
  - ``float_shares_outstanding`` is not a verified mapping here and Finviz already
    owns float (G-17). An unverified unit (raw shares vs millions) would silently
    distort the FUEL score.
  - an unknown column name can make the whole POST fail, taking the feed down.

Verification status: **PENDING live capture.** Only consume fields once
``probes/probe_sources.py tradingview`` has stored a raw fixture
(``sources/fixtures/tradingview_scan.txt``) and its shape is checked against this
adapter. Until then the runtime treats every scanner result as a guarded hint:
the price must stay within ``TRADINGVIEW_MAX_DEVIATION_PCT`` of the reconciled
Finviz price, and it is never used outside the regular session (extended-hours
truth stays with Finviz). Failures are ``SourceError`` — callers fall back to the
paced TwelveData path or keep the Finviz price.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import BaseClient, SourceError, fnum

log = logging.getLogger(__name__)

SCANNER_URL = "https://scanner.tradingview.com/america/scan"

# Minimal, long-standing scanner fields. Do not add columns without a captured
# fixture (see module docstring).
COLUMNS = ("name", "close", "change", "volume")


class TradingViewClient(BaseClient):
    """Keyless batch scanner. Inherits retry/429-cooldown/secret-scrubbing."""

    name = "tradingview"

    def __init__(self, cfg, rate: Optional[tuple[int, float]] = (60, 60.0)):
        super().__init__(cfg, rate=rate)

    def fetch_batch_metrics(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """One POST for all symbols; ``{SYMBOL: {close, change, volume, ...}}``.

        Raises ``SourceError`` on HTTP/network failure (callers fail soft). Empty
        input or no matching rows returns ``{}``.
        """
        tickers = sorted({s.upper() for s in symbols if s})
        if not tickers:
            return {}
        key = "tv:" + ",".join(tickers)
        ttl = float(self.cfg.float("TRADINGVIEW_CACHE_TTL_S", 10))
        cached = self._cache.get(key, ttl)
        if cached is not None:
            return cached
        payload = {
            "filter": [{"left": "name", "operation": "in_range", "right": tickers}],
            "columns": list(COLUMNS),
            "range": [0, len(tickers)],
        }
        timeout = float(self.cfg.float("TRADINGVIEW_TIMEOUT_S", 10))
        resp = self._request(
            "POST", SCANNER_URL, json=payload,
            headers={"Content-Type": "application/json"}, timeout=timeout,
        )
        if resp.status_code >= 400:
            raise SourceError(f"tradingview: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:  # noqa: BLE001
            raise SourceError("tradingview: invalid JSON") from exc
        out: dict[str, dict[str, Any]] = {}
        rows = (data or {}).get("data") or []
        for row in rows:
            sym = (row.get("s") or "").split(":")[-1].upper()
            vals = row.get("d") or []
            if not sym or len(vals) != len(COLUMNS):
                continue  # malformed row: drop it, never guess a mapping
            metrics = dict(zip(COLUMNS, vals))
            out[sym] = {
                "close": fnum(metrics.get("close")),
                "change_pct": fnum(metrics.get("change")),
                "volume": fnum(metrics.get("volume")),
            }
        self._cache.set(key, out)
        return out

    def health(self) -> tuple[bool, str]:
        try:
            res = self.fetch_batch_metrics(["AAPL"])
            if "AAPL" in res and res["AAPL"].get("close"):
                return True, "ok (keyless scanner)"
            return False, "no AAPL row returned (pending fixture verification?)"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
