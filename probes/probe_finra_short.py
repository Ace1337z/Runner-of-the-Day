"""Probe the FINRA consolidated short-interest API shape (paging + max date).

Run:  python probes/probe_finra_short.py
Saves nothing; prints the observed behaviour so the adapter can be built to it
and docs/VERIFIED.md updated.
"""

from __future__ import annotations

import json
import time

import requests

URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
UA = {"User-Agent": "Mozilla/5.0 (compatible; ROTD/0.1)"}
FIELDS = ["symbolCode", "settlementDate", "currentShortPositionQuantity"]


def page(offset: int, limit: int, symbol: str = "AAPL"):
    body = {
        "limit": limit,
        "fields": FIELDS,
        "compareFilters": [
            {"fieldName": "symbolCode", "fieldValue": symbol, "compareType": "EQUAL"}
        ],
        "offset": offset,
    }
    time.sleep(3)
    r = requests.post(URL, data=json.dumps(body), headers=UA, timeout=30)
    rows = [ln for ln in r.text.splitlines() if ln.strip() and not ln.lower().startswith('"symbolcode"')]
    return r.status_code, len(rows), (rows[-1] if rows else ""), (rows[0] if rows else "")


def main() -> None:
    for off in (0, 210, 300, 400, 500, 600):
        st, n, last, first = page(off, 150)
        print(f"offset={off:4d} limit=150 -> {st} rows={n:3d} first={first[:60]} last={last[:60]}")


if __name__ == "__main__":
    main()
