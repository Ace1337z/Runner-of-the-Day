# ROTD — Decisions (section 18) and build parameters

These are the user-selected defaults from the build prompt §18. ROTD proceeds
with them. They are **starting values**; each becomes a named setting with a
unit and a rationale in `src/rotd/config.py` (`SETTINGS` registry). None is a
magic number in code.

| # | Decision | Value | Note |
|---|---|---|---|
| 1 | Price floor | **none**; `MAX_PRICE = 10.00` only | Tradability is decided by `G-SPREAD`, `G-LIQ`, `G-TRADES`, `G-EXIT`, never by price. Metrics and the MISS REPORT break down by price band: `<0.20`, `0.20–0.50`, `0.50–1`, `1–5`, `5–10`. Prices render with up to **4** decimals. |
| 2 | Exchange scope | `INCLUDE_OTC = 0` | NASDAQ, NYSE, NYSE American only. |
| 3 | Alert budget | `ALERT_BUDGET_PER_DAY = 3` + 1 reserve | Reserve used only for a candidate at/above `EXCEPTIONAL_SCORE`. ABORT/DEGRADED/RECOVERED/BOARD never count. |
| 4 | Grading | runner = **+30% before −12%** | Runner list = `+100%` vs prior regular close; main list `dollar_vol ≥ $5,000,000`, second list `≥ $1,000,000` tagged `ILLIQUID`. |
| 5 | Display timezones | `America/New_York` + `Asia/Kuala_Lumpur` | `zoneinfo`; store UTC internally. Quiet hours: none by default, configurable per mode. |
| 6 | MISS REPORT gainers source | Finviz Elite daily export cross-checked against intraday **including extended hours** | Finviz is the only extended-hours source on these plans (see VERIFIED G-3/G-6/G-11). |
| 7 | MIDDAY pushes | allowed; `MIDDAY_MIN_SCORE = 85`, `MIDDAY_MIN_ROTATION = 2.0` | Stage-1 score is uncalibrated, so 85 has no probability meaning; tune from the MISS REPORT. |

## Non-negotiables carried into config

- Every threshold = named setting `(name, unit, default, rationale)`.
- LM never ranks, scores or times; it only classifies news/filings (async, cached).
- Fail closed: missing/stale/conflict lowers the grade and suppresses; never a pass.
- Determinism: same stored inputs → byte-identical decisions (AC1, S5).
- Log everything including rejects.

## Data-source reality (from VERIFIED.md §G)

- **Finviz** = primary (universe, movers, quote rows, news, filings, extended-hours price).
- **TwelveData** = regular-session 1-min bars + freshness quote.
- **Candle fallback**: TwelveData → Yahoo → Alpaca (Alpaca **BLOCKED**, key rejected).
- **Finnhub** = quote/news/**session** (`market-status`); no candles on this plan.
- **Alpha Vantage** = free `GLOBAL_QUOTE` only; no extended-hours intraday.
- **SEC** = filings; **FINRA** = short data; **Yahoo** = last-resort chart/regime.
