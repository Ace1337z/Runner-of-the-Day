# ROTD — VERIFIED

Phase 0 record of every fact the build prompt marks **VERIFY**, plus the extra
facts the ground rules require confirming before use. Status values:

- **VERIFIED** — confirmed by a local probe or the provider's documentation.
- **UNVERIFIED** — not yet confirmed; the safe default is stated and used.
- **BLOCKED** — cannot verify now (no credential / network); the safe default
  applies and the item is re-checked in Phase 1 with the real credential.

Probe date: **2026-02-15** (system clock reads 2026-10-13 UTC; see CLOCK-1).
Every probe below was run read-only; no existing system file was changed.

---

## A. Tooling and libraries

| # | Item | Status | Result / default | Evidence |
|---|---|---|---|---|
| LIB-1 | Python version available | VERIFIED | **3.12.10** | `python --version`. pyproject targets `>=3.10`. Run ROTD on 3.10+; develop/test on 3.12. |
| LIB-2 | `zoneinfo` available (timezone lib) | VERIFIED | Present; `America/New_York` and `Asia/Kuala_Lumpur` both resolve. | `zoneinfo` import + `datetime.now(ZoneInfo(...))` for both zones. |
| LIB-3 | Exchange-calendar library available | VERIFIED | **`exchange_calendars` 4.13.2** present and working (`XNYS`). `pandas_market_calendars` is also importable. | `xc.get_calendar('XNYS')`; holiday + half-day probes below. |
| LIB-4 | Chosen calendar library | DECIDED | **`exchange_calendars`** (default). Fall back to `pandas_market_calendars` only if a Phase-1 gap appears. | Same as LIB-3. |
| LIB-5 | `tzdata` available (Windows tz fallback) | VERIFIED | Present (penny already depends on it for Windows). | import check. |
| LIB-6 | `requests` available | VERIFIED | **2.34.2**. | import check. |
| LIB-7 | `httpx` available | VERIFIED | **Absent**. Use `requests` (already the house HTTP lib). | ImportError. |
| LIB-8 | ML libs for Stage 2 (`sklearn`) | UNVERIFIED | **Absent** (safe default). Stage 2 is not built until enough outcomes exist (§9.4). Add `scikit-learn` as an optional extra at Phase 2/`fit` time; do not import it anywhere else. | ImportError. |

## B. Exchange calendar behaviour (LIB-4 probes)

| # | Item | Status | Result | Evidence |
|---|---|---|---|---|
| CAL-1 | Trading days exclude holidays, no hard-coded dates | VERIFIED | For 2026-01-01..01-10 sessions = 01-02, 01-05..01-09 (New Year's Day excluded). | probe. |
| CAL-2 | Half days detected | VERIFIED | 2026-11-27 (day after Thanksgiving) close = **18:00 UTC = 13:00 ET** — a half day. | `c.session_close(...)`. |
| CAL-3 | Full holidays excluded | VERIFIED | Thanksgiving 2026-11-26 absent from sessions (11-25, 11-27, 11-30 present). | probe. |
| CAL-4 | Mode windows (§3) map to ET 16:00–20:00 / 20:00–04:00 / 04:00–09:30 / 09:30–10:30 / 10:30–16:00 | NOT YET BUILT | Implemented in Phase 1 (`calendar_us.py`); the boundaries are fixed by the prompt, only the *calendar source* needed verifying. | plan. |

## C. Time and clock

| # | Item | Status | Result / default | Evidence |
|---|---|---|---|---|
| CLOCK-1 | External time source for the startup clock-skew check (§15) | VERIFIED (source reachable) | `https://api.github.com/meta` returns 200 and carries a `time` field; use it as the skew reference. `worldtimeapi.org` was **not** reachable from this host (DNS/network), so do **not** depend on it. Tradier `markets/clock` also returns 200 but needs a token for real use — not chosen. | `urllib.request` probes. |
| CLOCK-2 | DST handled by the timezone library | VERIFIED | `zoneinfo` gives NY −04:00 and KL +08:00 on the probe date; both zones track their own DST rules. No manual offset math. | probe. |
| CLOCK-3 | KL↔NY offset changes at US DST edges | VERIFIED | KL has no DST, so the offset to NY changes (12h/13h). Both zones via `zoneinfo`; ROTD stores UTC internally and renders in both. | probe; §18.5. |

## D. Data/business rules the prompt marks VERIFY

> **Live probe results are in section G below.** The D-series rows here record
> the *rule* and the safe default; section G records what the real APIs actually
> returned on the probe date. Where a probe contradicts the safe default, section
> G wins and is referenced here.

| # | Item | Status | Result / default to use | Evidence / note |
|---|---|---|---|---|
| D-1 | Finviz freezes `Price`/`Change` at the regular close after 16:00 and carries a separate After-Hours Close | VERIFIED (shape) | Confirmed: the universe export carries the separate **After-Hours Close/Change** columns (71/72) and `Prev Close` (81), which only exist because the plain price freezes. See **G-3**. | live probe `finviz_universe.txt`. |
| D-2 | Which volume basis each plan returns (IEX-only vs consolidated) | PARTIAL | Alpaca credentials were rejected (see **G-9**), so the Alpaca feed basis cannot be probed. **Safe default stands: treat any single-venue feed as `SINGLE_VENUE`; if basis `UNKNOWN`, RVOL features are `MISSING` (DATA-04).** TwelveData's `quote.volume` and Finnhub's quote volume are not basis-labelled — treat as `UNKNOWN` unless cross-checked. | probes; `alpaca_*` 401. |
| D-3 | Minimum price increments for stocks below/above $1.00 (sub-penny rule) | UNVERIFIED | **Safe default (re-confirm in Phase 1 from the rulebook):** ≥ $1.00 quotes in $0.01 increments; < $1.00 in $0.0001 increments. Used only to render decimals and store spread in ticks. Confirm against an exchange rulebook before relying on it for a gate. | to confirm from rulebook. |
| D-4 | EDGAR filing acceptance hours | UNVERIFIED | **Safe default:** EDGAR accepts filings roughly 06:00–22:00 ET on business days; expect overnight batches. Confirm from the SEC's published hours. Ingestion is robust to bursts either way. SEC endpoints are reachable (see **G-6**). | to confirm from SEC docs. |
| D-5 | Trade-halt feed availability (free/paid) | UNVERIFIED | **Safe default: no halt feed in Phase 1.** `G-HALT`/`HALTED_SUSPENDED` are implemented against what the primary feed exposes (a halt shows as stale/absent quotes). `halt_feed.py` returns `UNKNOWN` until a free or already-paid feed is confirmed. Ask the user before adding one. | no free feed confirmed. |
| D-6 | Volatility pauses in extended hours | UNVERIFIED | **Safe default: not modelled.** Treated as a normal quote gap; staleness rules (DATA-03) apply. | to probe. |
| D-7 | Finviz Elite provides the symbol grouping needed for sympathy (§6.5) | PARTIAL | The universe export carries **Sector** and **Industry** columns (see **G-3**), so a sector/industry grouping join is available. Sympathy stays **OFF** by default until a Phase-1 probe shows the join is reliable; the data to enable it exists. | live probe `finviz_universe.txt`. |
| D-8 | Source supports intraday data that includes extended hours (for the runner-list high, §12.3) | PARTIAL | **TwelveData prepost = 403** (Pro-plan only) and **Alpha Vantage intraday extended-hours = premium**. **Finviz carries the After-Hours Close/Change** and intraday performance columns, so the *primary* extended-hours builder is **Finviz**, not TwelveData/Alpha Vantage. Build the runner-list high from Finviz extended-hour fields. See **G-3, G-4, G-8**. | probes. |
| D-9 | Which credentials are actually available/paid | ANSWERED | Finviz Elite, TwelveData, Finnhub, Alpha Vantage, SEC, FINRA, Yahoo all authenticate and return data. **Alpaca credentials are rejected (401).** See **G-9**. | probes. |

## E. Storage/ops facts to MEASURE in Phase 1 (not assume)

| # | Item | Status | Plan |
|---|---|---|---|
| M-1 | Real growth rate of quotes/bars/snapshots | TO MEASURE (Phase 1) | Record a full trading day, report MB/day and rows/day in this file. Do not assume. (§13) |
| M-2 | Disk-warn threshold sanity | TO MEASURE (Phase 1) | Validate `DISK_WARN_PCT` against measured growth before trusting it. |

## F. Items confirmed by reading the existing system (not probes)

These are not prompt-VERIFY items but were confirmed during recon and inform the
build:

| # | Fact | Source |
|---|---|---|
| F-1 | Finviz token rejection pauses all Finviz steps until the token changes (no restart) | `sources/finviz.py` `FinvizTokenRejected`, `_token()`. Mirror this for ROTD's `source_health`. |
| F-2 | Alpaca default feed is `iex` (single venue) | `penny/config.py` `ALPACA_FEED=iex`. Feeds DATA-04. |
| F-3 | Penny's universe filter default is `sh_price_u10,ind_stocksonly` and the custom view is `152` | `penny/config.py`. ROTD's universe will be its own config but the same screen shape. |
| F-4 | Penny uses no overnight prints from the primary feed (04:00–20:00 ET only) | README. Matches the prompt's OVERNIGHT "no usable quotes" assumption (still VERIFY at D-5/D-6). |
| F-5 | `FakeTelegram` exists and records sends/edits in memory | `telegram.py`. Reused for ROTD offline tests. |
| F-6 | House style: no network inside unit tests; fixtures only | `tests/`. ROTD keeps it. |

---

## Summary of what still needs a live probe or a user answer

1. **D-9** — which credentials are paid/available (blocking Phase 1 recording).
2. **D-1, D-2, D-7, D-8** — live-feed shape probes (Finviz freeze, volume basis,
   sector grouping, extended-hours intraday) — Phase 1, need D-9 first.
3. **D-3, D-4, D-5, D-6** — market-rule facts; safe defaults stated, confirm in
   Phase 1 from the rulebook/provider docs.
4. **M-1, M-2** — measured in Phase 1, reported here.

---

## G. Live probe results (credentials supplied)

Probe date: **2026-10-13/14** (system clock 2026-10-13 ~22:00 UTC = 18:00 ET,
post-market). Raw responses saved under `src/rotd/sources/fixtures/`. Probe
driver: `probes/probe_sources.py`. Secrets live in a gitignored `config.env`
and are never logged or committed.

| # | Source | Probe | Status | Finding |
|---|---|---|---|---|
| G-1 | Finviz | `auth` on `/export/screener` + `/export/news` | 200 | Token **valid**. Universe CSV = 1.22 MB, full column set. News CSV works. |
| G-2 | Finviz | `/export/latest-filings` without a ticker | 400 | `"Unable to get CIK from ticker"` — the filings link needs a `t` param. Adapter must pass a ticker; do not call it bare. |
| G-3 | Finviz | Universe columns | 200 | Header includes `Sector`, `Industry`, and (per the column list) After-Hours Close/Change + Prev Close. **D-1, D-7 confirmed.** Extended-hours high builder = Finviz. |
| G-4 | TwelveData | `quote` AAPL | 200 | Works. Fields include `timestamp`, `last_quote_at`, `is_market_open`, `previous_close`, `percent_change`, `average_volume`. |
| G-5 | TwelveData | `time_series 1min` AAPL | 200 | Works; `meta.exchange_timezone = America/New_York`; last bar 15:59 ET. Regular-session 1-min bars. |
| G-6 | TwelveData | `time_series prepost=true` | **403** | `"Pre-market and post-market data are available on the Pro plan…"`. **TwelveData cannot supply extended-hours candles on this plan (D-8).** |
| G-7 | Finnhub | `quote`, `company-news`, `stock/market-status` | 200 | All work. `market-status` returns `{"session":"post-market","isOpen":false,"timezone":"America/New_York"}` — a usable **session detector**. |
| G-8 | Finnhub | `stock/candle` | **403** | Premium-only. Finnhub is **news/quote/session only**, not a candle fallback. |
| G-9 | Alpaca | `quotes/latest`, `bars`, `news`, `/v2/account` (paper + live) | **401** | Credentials **rejected** on every host: `{"message":"unauthorized."}`. Verified not a whitespace/header issue (key 26 chars, secret 44 chars, sent verbatim). **Alpaca is BLOCKED — key pair invalid/revoked/not activated. Non-blocking: Alpaca is only a candle fallback.** Re-probe when regenerated. |
| G-10 | Alpha Vantage | `GLOBAL_QUOTE` | 200 | Works (free tier). Fields: open/high/low/price/volume/previous close/latest trading day. |
| G-11 | Alpha Vantage | `TIME_SERIES_INTRADAY extended_hours=true` | premium | Premium endpoint message. Not usable on the free key. |
| G-12 | SEC EDGAR | Atom filings, `company_tickers.json`, daily index | 200 | All work with a `User-Agent` and `Accept-Encoding: identity`. Atom is ISO-8859-1 — the adapter must not assume UTF-8. |
| G-13 | Yahoo | `chart` | 200 | Works; `meta.hasPrePostMarketData: true`, `exchangeTimezoneName: America/New_York`. Usable as last-resort bars/regime. |
| G-14 | Yahoo | `v7/finance/quote` | **401** | Yahoo deprecated the v7 quote endpoint. Use `chart` only. |
| G-15 | FINRA | RegSHO daily short-volume txt | 200 | `Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market` — daily short-sale volume works. A browser-like `User-Agent` is required (default Python UA is 403). |
| G-16 | FINRA | `consolidatedShortInterest` API | 200 | **POST** a JSON body (`fields` + `compareFilters`) and the reply is **CSV** (not JSON; the earlier probe only proved reachability with `?limit=2`). Corrected details: (a) **exactly one** `symbolCode` EQUAL filter per request — multiple filters return **HTTP 204** with an empty body, so the adapter loops per symbol; (b) `sortFields` is **rejected** (HTTP 400 "Sorting is allowed only if all partitions keys are specified") and row order is **not stable** across limit/offset (newest-first at limit=1000, oldest-first at limit=150) → never trust order, keep the greatest `settlementDate`; (c) an `Accept: text/csv` header yields **HTTP 400**, leaving `Accept` at its JSON default works; (d) the API is **burst-sensitive** (rapid repeats return a transient 400); (e) ~211 rows per symbol, newest settlement observed **2026-09-30**. |
| G-17 | Finviz | screener `c=` column **id → header** map | 200 | The `c=` parameter is a comma list of Finviz's **own 0-based column indices**, and it **replaces** the header set — any id not listed is absent from the export (there is no implicit `Ticker`). Verified by requesting `c=0..99` in one call (order preserved) → the header table below; cross-checked singly (`c=1`→`Ticker`, `c=65`→`Price`, `c=68`→`Earnings Date`). **The first-cut `REQUIRED_COLUMNS` was misaligned by one row from id 68 onward (e.g. it read `Earnings Date` as After-Hours Close, `Price` as Short Ratio, `Volume` as Price), which silently zeroed every universe row (no `Ticker` was ever requested).** Ids are NOT the fixture header positions — they are Finviz's canonical indices, and the fixture was captured from a shipped named view, not `c=''`. Full table (`probes/probe_finviz_idmap.py`):

    `0 No. · 1 Ticker · 2 Company · 3 Sector · 4 Industry · 5 Country · 6 Market Cap · 7 P/E · 8 Fwd P/E · 9 PEG · 10 P/S · 11 P/B · 12 P/Cash · 13 P/FCF · 14 Div Yield · 15 Payout · 16 EPS(ttm) · 17–23 EPS/Sales growth · 24 Shares Out · 25 Shares Float · 26 Insider Own · 27 Insider Trans · 28 Inst Own · 29 Inst Trans · 30 Short Float · 31 Short Ratio · 32–41 profitability · 42 Perf(Week) · 43 (Month) · 44 (Quarter) · 45 (Half Yr) · 46 (Year) · 47 (YTD) · 48 Beta · 49 ATR · 50 Vol(Week) · 51 Vol(Month) · 52 20-DMA · 53 50-DMA · 54 200-DMA · 55 50-Day High · 56 50-Day Low · 57 52-Wk High · 58 52-Wk Low · 59 RSI(14) · 60 Chg from Open · 61 Gap · 62 Analyst Recom · 63 Average Volume · 64 Relative Volume · 65 Price · 66 Change · 67 Volume · 68 Earnings Date · 69 Target Price · 70 IPO Date · 71 After-Hours Close · 72 After-Hours Change · 73 Book/sh · 74 Cash/sh · 75 Dividend · 76 Employees · 77 EPS Next Q · 78 Income · 79 Index · 80 Optionable · 81 Prev Close · 82 Sales · 83 Shortable · 84 Short Interest · 85 Float % · 86 Open · 87 High · 88 Low · 89 Trades · 90 Perf(1m) · 91 (2m) · 92 (3m) · 93 (5m) · 94 (10m) · 95 (15m) · 96 (30m) · 97 (1h) · 98 (2h) · 99 (4h)` |

    **Fixed:** `REQUIRED_COLUMNS = (1,2,3,4, 6,24,25,30,31,48,49,59,61,63,64, 65,66,67,71,72,81,86,87,88, 90,91,92,93,94,95,97,99)`. After the fix the live universe returns **11,727 rows, all with a ticker**, **2,747 sub-$10 priced records** (snapshot grade B) vs. **0** before (grade F). Any future id edit MUST be re-probed and the header asserted (see the guarded self-check in `sources/finviz.py`). |

| G-18 | All sources | bid/ask book + 15-min print/volume | N/A | **No configured source provides a bid/ask book or a 15-minute trade-count feed.** Scanned every fixture + the Finviz full named view (`probe_bid_ask.py`): TwelveData `quote` has no `bid`/`ask` (fields: close/high/low/open/volume/average_volume/previous_close); Finnhub `quote` is `c/d/dp/h/l/o/pc/t` (no book); the Finviz export has no bid/ask/spread column; Yahoo `chart` and SEC have none either. **Consequence:** `G-SPREAD` (needs a spread), `G-EXIT` (needs slippage ≈ spread), and `G-LIQ` (needs 15m $ volume derived from a 1-min bar feed — TwelveData `prepost` is 403 here) cannot obtain their input from any live source. Per the user decision, these three gates are **OFF by default** (`USE_SPREAD_GATE`/`USE_LIQ_GATE`/`USE_EXIT_GATE = 0`) and **report `NOT_ENFORCED` (pass)** — mirroring the existing `G-TRADES` `TRADES_NOT_ENFORCED` pattern — so the system is not silently zeroed by data it structurally cannot obtain. Each gate re-arms the moment a feed supplies its input (e.g. Alpaca unblocked for the book). The remaining enforced gates (`G-DATA`, `G-PHASE`, `G-PRICE`, `G-HALT`, `G-TRADES`) still fail closed on genuinely missing data. With the gates fixed, a live POST cycle reconciles 2,747 sub-$10 names, 2,742 pass gates, 2,742 rank, and the board top is `AGMH/RKDA/MWG/TWG/TCRT` — ROTD stays `None` because the top score (55) is below `ROTD_MIN_SCORE` (70), which is the correct conservative outcome for a thin after-hours session. |### Consequences for the build

- **Primary feed:** Finviz (universe, movers, quote rows, news, filings, and the
  only extended-hours price source on these plans).
- **Freshness feed:** TwelveData (regular-session 1-min bars + quote).
- **Candle fallback chain:** TwelveData → Yahoo → (Alpaca when unblocked).
  Finnhub and Alpha Vantage do **not** provide candles on these plans.
- **Session detection:** Finnhub `market-status` (plus the local calendar).
- **Short/float fuel:** FINRA short-interest + RegSHO; float from Finviz.
- **Alpaca:** adapter built, fails soft, **BLOCKED** until the key is regenerated.
