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

## §10 catalyst service = the methodology_v3 red-team auditor

`docs/methodology_v3.md` (also copied to `src/rotd/prompts/methodology_v3.md` as
the shipped contract) is a full **red-team auditor**, not a light classifier.
A4 was chosen: `src/rotd/auditor.py` builds one **FACT BUNDLE** per alert (§4
field guide), loads the exact system prompt between its `BEGIN/END SYSTEM
PROMPT` markers, and validates the returned `decision_v3` report with the A4
post-parse checks (reject, never repair). Code computes a **mechanical verdict**
(`mechanical_verdict`) and the model may never exceed it ("strongest
hardening"). The model only *classifies and audits* — it never ranks, scores or
times. The service runs off the decision path (`Runtime._audit_candidates`,
async in live mode) and falls back to the rules-only block on no-model, timeout,
bad response or a failed check. `CatalystService.audit_report` is the runtime
entry point; the 0..100 `CatalystService.report_score` is the only number the
Stage-1 scorer reads from it.

## Shadow mode and the 10-trading-day clock (§17 Phase 6)

No live push is allowed until ROTD has run in shadow for a full **10-trading-day**
window. The clock is **calendar-bound**: it counts NYSE sessions via
`exchange_calendars`, so weekends and holidays do not advance it. It is stored in
the `meta` table (`shadow_start`, `shadow_last_seen`, `shadow_cycles`,
`shadow_alerts`) and is therefore inspectable, restart-safe and testable without
the network. `src/rotd/shadow.py` owns the logic; the length is the named setting
`SHADOW_DAYS_REQUIRED` (unit: trading days, start: 10).

The gate is enforced in `rotd run`: while the clock is incomplete it refuses to
push (`SHADOW_INCOMPLETE`) and exits non-zero. `rotd shadow` always decides and
logs but pushes nothing. `rotd doctor` and `rotd status` show the clock.

**Clock started on 2026-10-13** (the A1 trading-day label in force when it was
started). Ten sessions from there ends **2026-10-26**. Until then the system is
shadow-only.

## Data-source reality (from VERIFIED.md §G)

- **Finviz** = primary (universe, movers, quote rows, news, filings, extended-hours price).
- **TwelveData** = regular-session 1-min bars + freshness quote.
- **Candle fallback**: TwelveData → Yahoo → Alpaca (Alpaca **BLOCKED**, key rejected).
- **Finnhub** = quote/news/**session** (`market-status`); no candles on this plan.
- **Alpha Vantage** = free `GLOBAL_QUOTE` only; no extended-hours intraday.
- **SEC** = filings; **FINRA** = short data; **Yahoo** = last-resort chart/regime.
