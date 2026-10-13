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
        text = self.get_text(url)
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
        """Latest reported short interest for the given symbols."""
        if not symbols:
            return {}
        body = {
            "limit": len(symbols) * 4,
            "compareFilters": [
                {"fieldName": "symbolCode", "fieldValue": s.upper(),
                 "compareType": "EQUAL"} for s in symbols
            ],
        }
        url = f"{SHORT_INT_BASE}?limit={body['limit']}"
        data = self.get_json(url, params={"fields": "symbolCode,settlementDate,"
                                                    "currentShortPositionQuantity,"
                                                    "averageDailyVolumeQuantity,"
                                                    "daysToCoverQuantity",
                                          "limit": body["limit"]})
        out: dict[str, dict[str, Any]] = {}
        for r in (data or {}).get("data", data if isinstance(data, list) else []):
            sym = (r.get("symbolCode") or "").upper()
            if sym:
                out[sym] = {
                    "settlement_date": r.get("settlementDate"),
                    "short_position": fnum(r.get("currentShortPositionQuantity")),
                    "avg_daily_volume": fnum(r.get("averageDailyVolumeQuantity")),
                    "days_to_cover": fnum(r.get("daysToCoverQuantity")),
                }
        return out

    def health(self) -> tuple[bool, str]:
        try:
            self.regsho_daily(date.today())
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
