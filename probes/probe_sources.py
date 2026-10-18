"""Phase 0/1 live probes.

Reads secrets from a local env file (never committed) and runs one probe per
external source, saving the RAW response untouched into
src/rotd/sources/fixtures/. Nothing here is imported by the runtime; it exists
to confirm real API shapes before an adapter is built against them.

Usage:  python probes/probe_sources.py [name ...]
        python probes/probe_sources.py            # all probes
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "src" / "rotd" / "sources" / "fixtures"
CFG = ROOT / "config.env"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


ENV = load_env(CFG)
FIX.mkdir(parents=True, exist_ok=True)


def get(url: str, headers: dict[str, str] | None = None, timeout: float = 25.0):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "rotd-probe/0.1"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, dict(r.headers), body, time.time() - t0
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, dict(e.headers or {}), body, time.time() - t0
    except Exception as e:  # noqa: BLE001
        return -1, {}, f"ERROR {type(e).__name__}: {e}", time.time() - t0


SHORT_INT_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"


def _post_json(url: str, body: dict, headers: dict[str, str] | None = None,
               timeout: float = 25.0) -> tuple[int, str]:
    """POST a JSON body; return (status, text). FINRA rejects an explicit
    ``Accept: text/csv`` header with 400, so the caller controls headers."""
    data = json.dumps(body).encode("utf-8")
    hdrs = {"User-Agent": "rotd-probe/0.1", "Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"ERROR {type(e).__name__}: {e}"


def save(name: str, status: int, body: str, extra: dict | None = None) -> None:
    meta = {"probe": name, "status": status,
            "saved_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if extra:
        meta.update(extra)
    text = body if isinstance(body, str) else str(body)
    # Write bytes so arbitrary encodings (SEC serves latin-1/utf-8 mixes) never crash.
    (FIX / f"{name}.txt").write_bytes(text.encode("utf-8", "replace"))
    (FIX / f"{name}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    head = text[:400].replace("\n", " ")
    print(f"[{name}] status={status} bytes={len(text)} :: {head[:300]}")
    print(f"        -> saved {FIX / (name + '.txt')}")
    return None


# --------------------------------------------------------------------------
def probe_finviz() -> None:
    token = ENV.get("FINVIZ_TOKEN", "")
    base = "https://elite.finviz.com"
    # Universe export (the screener link from the penny adapter).
    params = {
        "v": "152",
        "f": "sh_price_u10,ind_stocksonly",
        "c": ("0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,"
              "29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,54,"
              "55,56,57,58,59,60,61,62,63,64,65,66,67,71,72,81,86,87,88,90,91,92,93,94,95,96,97,98,99"),
        "auth": token,
    }
    url = f"{base}/export/screener?" + urllib.parse.urlencode(params)
    st, hdrs, body, dt = get(url)
    save("finviz_universe", st, body, {"content_type": hdrs.get("Content-Type", ""), "elapsed_s": round(dt, 2)})
    # Latest filings link.
    url2 = f"{base}/export/latest-filings?" + urllib.parse.urlencode({"auth": token})
    st2, _, body2, _ = get(url2)
    save("finviz_latest_filings", st2, body2)
    # News link.
    url3 = f"{base}/export/news?" + urllib.parse.urlencode({"v": "1", "auth": token})
    st3, _, body3, _ = get(url3)
    save("finviz_news", st3, body3)


def probe_finnhub() -> None:
    key = ENV.get("FINNHUB_KEY", "")
    base = "https://finnhub.io/api/v1"
    url = f"{base}/quote?" + urllib.parse.urlencode({"symbol": "AAPL", "token": key})
    st, _, body, _ = get(url)
    save("finnhub_quote", st, body)
    url2 = f"{base}/company-news?" + urllib.parse.urlencode(
        {"symbol": "AAPL", "from": "2026-10-06", "to": "2026-10-13", "token": key})
    st2, _, body2, _ = get(url2)
    save("finnhub_company_news", st2, body2)
    url3 = f"{base}/stock/market-status?" + urllib.parse.urlencode({"exchange": "US", "token": key})
    st3, _, body3, _ = get(url3)
    save("finnhub_market_status", st3, body3)


def probe_alpaca() -> None:
    key = ENV.get("ALPACA_KEY", "")
    sec = ENV.get("ALPACA_SECRET", "")
    base = "https://data.alpaca.markets"
    hdrs = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec, "User-Agent": "rotd-probe/0.1"}
    # IEX and SIP feeds for the same symbol => confirms volume basis.
    for feed in ("iex", "sip"):
        url = f"{base}/v2/stocks/AAPL/quotes/latest?" + urllib.parse.urlencode({"feed": feed})
        st, _, body, _ = get(url, headers=hdrs)
        save(f"alpaca_quote_{feed}", st, body, {"feed": feed})
    # bars returns {"bars": null} unless an explicit start is sent.
    url2 = f"{base}/v2/stocks/AAPL/bars?" + urllib.parse.urlencode(
        {"timeframe": "1Min", "limit": "5", "feed": "iex", "adjustment": "raw",
         "start": (datetime.now(timezone.utc) - timedelta(days=5))
                  .strftime("%Y-%m-%dT%H:%M:%SZ")})
    st2, _, body2, _ = get(url2, headers=hdrs)
    save("alpaca_bars_iex", st2, body2)
    url3 = f"{base}/v1beta1/news?" + urllib.parse.urlencode({"symbols": "AAPL", "limit": "3"})
    st3, _, body3, _ = get(url3, headers=hdrs)
    save("alpaca_news", st3, body3)
    # Clock lives on the trading host, not data.alpaca.markets.
    url4 = "https://paper-api.alpaca.markets/v2/clock"
    st4, _, body4, _ = get(url4, headers=hdrs)
    save("alpaca_clock", st4, body4)


def probe_alpha_vantage() -> None:
    key = ENV.get("ALPHA_VANTAGE_KEY", "")
    base = "https://www.alphavantage.co/query"
    url = f"{base}?" + urllib.parse.urlencode(
        {"function": "GLOBAL_QUOTE", "symbol": "AAPL", "apikey": key})
    st, _, body, _ = get(url)
    save("alpha_vantage_global_quote", st, body)
    url2 = f"{base}?" + urllib.parse.urlencode(
        {"function": "TIME_SERIES_INTRADAY", "symbol": "AAPL", "interval": "1min",
         "extended_hours": "true", "apikey": key})
    st2, _, body2, _ = get(url2)
    save("alpha_vantage_intraday_ext", st2, body2)


def probe_sec() -> None:
    ua = ENV.get("SEC_USER_AGENT", "ROTD research contact@example.com")
    hdrs = {"User-Agent": ua, "Accept-Encoding": "identity", "Host": "www.sec.gov"}
    # Full-text/latest filings Atom feed + company tickers JSON.
    st, _, body, _ = get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=8-K&dateb=&owner=include&count=5&output=atom", headers=hdrs)
    save("sec_edgar_atom", st, body)
    st2, _, body2, _ = get("https://www.sec.gov/files/company_tickers.json", headers=hdrs)
    save("sec_company_tickers", st2, body2[:200000])
    # Daily index: acceptance-hour behaviour source.
    st3, _, body3, _ = get("https://www.sec.gov/Archives/edgar/daily-index/", headers=hdrs)
    save("sec_daily_index", st3, body3)


def probe_yahoo() -> None:
    st, _, body, _ = get("https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1m")
    save("yahoo_chart", st, body)
    st2, _, body2, _ = get("https://query1.finance.yahoo.com/v7/finance/quote?symbols=SPY,QQQ,IWM&fields=symbol,regularMarketPrice,regularMarketChangePercent")
    save("yahoo_quote", st2, body2)


def probe_finra() -> None:
    # FINRA daily short-sale volume file list (used by penny's finra adapter).
    # A browser-like User-Agent is required; the default Python UA is 403.
    st, _, body, _ = get("https://cdn.finra.org/equity/regsho/daily/CNMSshvol20261013.txt",
                         headers={"User-Agent": "Mozilla/5.0 (compatible; ROTD/0.1)"})
    save("finra_regsho", st, body[:100000])
    # Consolidated short interest: POST a JSON body, receive CSV. One symbol per
    # request (multiple EQUAL filters -> HTTP 204). See probe_finra_short.py for
    # the paging / ordering probe and fixtures/finra_short_interest_post.meta.json.
    si_body = {
        "limit": 500,
        "fields": ["symbolCode", "settlementDate", "currentShortPositionQuantity",
                   "averageDailyVolumeQuantity", "daysToCoverQuantity"],
        "compareFilters": [{"fieldName": "symbolCode", "fieldValue": "AAPL",
                            "compareType": "EQUAL"}],
    }
    resp = _post_json(SHORT_INT_URL, si_body,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; ROTD/0.1)"})
    save("finra_short_interest_post", resp[0], resp[1][:100000])


PROBES = {
    "finviz": probe_finviz,
    "finnhub": probe_finnhub,
    "alpaca": probe_alpaca,
    "alpha_vantage": probe_alpha_vantage,
    "sec": probe_sec,
    "yahoo": probe_yahoo,
    "finra": probe_finra,
}


def probe_alpaca_auth_diag() -> None:
    """Diagnose the Alpaca 401 across both the data and trading hosts."""
    key = ENV.get("ALPACA_KEY", "")
    sec = ENV.get("ALPACA_SECRET", "")
    hdrs = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}
    targets = {
        "alpaca_diag_data_account": "https://data.alpaca.markets/v2/account",
        "alpaca_diag_paper_account": "https://paper-api.alpaca.markets/v2/account",
        "alpaca_diag_live_account": "https://api.alpaca.markets/v2/account",
        "alpaca_diag_data_clock": "https://data.alpaca.markets/v2/clock",
    }
    for name, url in targets.items():
        st, _, body, _ = get(url, headers=hdrs)
        save(name, st, body[:2000])


PROBES["alpaca_diag"] = probe_alpaca_auth_diag


def probe_tradingview() -> None:
    """Keyless capture of the scanner POST (resolves the PENDING verification).

    Saves the raw JSON so the column mapping in sources/tradingview.py can be
    checked against reality before those fields are trusted. No token needed.
    """
    body = {
        "filter": [{"left": "name", "operation": "in_range", "right": ["AAPL", "ZVZZT"]}],
        "columns": ["name", "close", "change", "volume"],
        "range": [0, 2],
    }
    st, text = _post_json("https://scanner.tradingview.com/america/scan", body)
    save("tradingview_scan", st, text, extra={"columns": body["columns"]})


PROBES["tradingview"] = probe_tradingview


def main(argv: list[str]) -> int:
    names = argv[1:] or list(PROBES)
    for n in names:
        fn = PROBES.get(n)
        if fn is None:
            print(f"unknown probe: {n} (have: {', '.join(PROBES)})")
            continue
        print(f"\n===== probe {n} =====")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  probe {n} crashed: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
