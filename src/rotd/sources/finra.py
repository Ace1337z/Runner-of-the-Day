"""FINRA client — daily short-sale volume (RegSHO) and short interest.

Verified (docs/VERIFIED.md G-15/G-16): the RegSHO daily file is pipe-delimited
``Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market`` (see
``fixtures/finra_regsho.txt``); the short-interest API returns JSON with
``currentShortPositionQuantity`` / ``averageDailyVolumeQuantity`` /
``settlementDate`` (see ``fixtures/finra_short_interest.txt``).

Short data is a *fuel* signal only; it never gates a decision on its own.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from .base import BaseClient, SourceError, fnum, parse_csv

log = logging.getLogger(__name__)

REGSHO_BASE = "https://cdn.finra.org/equity/regsho/daily"
SHORT_INT_BASE = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"


class FinraClient(BaseClient):
    name = "finra"

    def __init__(self, cfg, rate: Optional[tuple[int, float]] = (10, 60.0)):
        super().__init__(cfg, rate=rate)

    # -- endpoints ----------------------------------------------------------
    def regsho_daily(self, d: date, market: str = "CNMS") -> dict[str, dict[str, float]]:
        """Short-sale volume for one day, keyed by symbol.

        ``market`` is CNMS (consolidated) / FNRA etc.; ``CNMSshvolYYYYMMDD.txt``
        was the verified file.
        """
        stem = {"CNMS": "CNMSshvol", "FNRA": "FNRAshvol"}.get(market, "CNMSshvol")
        url = f"{REGSHO_BASE}/{stem}{d:%Y%m%d}.txt"
        # FINRA's CDN rejects the default Python UA with 403; a browser-like UA
        # is required (verified live: the same URL 403s without one).
        text = self.get_text(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ROTD/0.1)"})
        # The file uses a single column whose name is the first data field, so
        # parse manually against a fixed schema rather than by header.
        out: dict[str, dict[str, float]] = {}
        for line in text.splitlines():
            parts = line.strip().split("|")
            if len(parts) < 6 or not parts[1].isalpha():
                continue
            sym = parts[1].upper()
            out[sym] = {
                "short_volume": fnum(parts[2]) or 0.0,
                "short_exempt": fnum(parts[3]) or 0.0,
                "total_volume": fnum(parts[4]) or 0.0,
                "market": parts[5],
            }
        return out

    def short_interest(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Latest reported short interest for the given symbols.

        Verified live (docs/VERIFIED.md §G):
        - The endpoint is a **POST with a JSON body** (``fields`` +
          ``compareFilters``) and returns **CSV**, not JSON.
        - Multiple ``symbolCode`` EQUAL filters are **not** OR-ed: a two-symbol
          filter returns HTTP 204 with an empty body. Exactly **one** symbol per
          request is therefore required (one call per symbol).
        - ``sortFields`` is rejected (HTTP 400: "Sorting is allowed only if all
          partitions keys are specified") and row order is *not* stable across
          limit/offset (observed newest-first at limit=1000 but oldest-first at
          limit=150), so we never trust order — we keep the row with the
          greatest ``settlementDate``.
        - An ``Accept: text/csv`` header makes the API reply 400 ("Bad
          Request"); the default JSON accept works even though the body is CSV.
        - The API is burst-sensitive: rapid repeats return a transient 400.
          The client's rate limiter spaces the per-symbol calls.
        """
        if not symbols:
            return {}
        import json as _json
        fields = ["symbolCode", "settlementDate", "currentShortPositionQuantity",
                  "averageDailyVolumeQuantity", "daysToCoverQuantity"]
        # The API caps a single symbol's history at ~211 rows; ask for more than
        # that so one page is enough.
        out: dict[str, dict[str, Any]] = {}
        newest: dict[str, str] = {}
        for raw in symbols:
            sym_req = raw.strip().upper()
            if not sym_req:
                continue
            body = {
                "limit": 500,
                "fields": fields,
                "compareFilters": [
                    {"fieldName": "symbolCode", "fieldValue": sym_req,
                     "compareType": "EQUAL"}
                ],
            }
            resp = self._request(
                "POST", SHORT_INT_BASE,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ROTD/0.1)"},
                data=_json.dumps(body))
            if resp.status_code == 204:   # symbol has no reported history
                continue
            if resp.status_code in (401, 403):
                raise SourceError(f"finra: rejected (HTTP {resp.status_code})")
            if resp.status_code >= 400:
                raise SourceError(f"finra: HTTP {resp.status_code}")
            for r in parse_csv(resp.content.decode("utf-8", errors="replace")):
                sym = (r.get("symbolCode") or "").upper()
                if not sym:
                    continue
                date = str(r.get("settlementDate") or "")
                if sym in out and date <= newest.get(sym, ""):
                    continue  # keep the newest settlement date per symbol
                newest[sym] = date
                out[sym] = {
                    "settlement_date": r.get("settlementDate"),
                    "short_position": fnum(r.get("currentShortPositionQuantity")),
                    "avg_daily_volume": fnum(r.get("averageDailyVolumeQuantity")),
                    "days_to_cover": fnum(r.get("daysToCoverQuantity")),
                }
        return out

    def health(self) -> tuple[bool, str]:
        # Today's file may not exist yet (it posts after the session); the most
        # recent full session is the meaningful probe.
        from datetime import timedelta
        from ..util import now_utc, to_et
        probe = to_et(now_utc()).date()
        for back in range(0, 6):
            try:
                rows = self.regsho_daily(probe - timedelta(days=back))
                if rows:
                    return True, f"ok ({len(rows)} symbols on {probe - timedelta(days=back)})"
            except Exception:  # noqa: BLE001 - try an earlier session
                continue
        return False, "no RegSHO file found in the last 6 days"
