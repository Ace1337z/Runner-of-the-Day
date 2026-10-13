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

| # | Item | Status | Result / default to use | Evidence / note |
|---|---|---|---|---|
| D-1 | Finviz freezes `Price`/`Change` at the regular close after 16:00 and carries a separate After-Hours Close | UNVERIFIED (needs `FINVIZ_TOKEN`) | **Safe default: encode it as the prompt states.** The penny system already relies on it: `FINVIZ_COLUMNS` includes 71/72 (After-Hours Close/Change) and 81 (Prev Close) precisely because the plain price freezes. Re-confirm with a live POST probe in Phase 1 and record the raw CSV as a fixture. | `penny/config.py` `FINVIZ_COLUMNS` comment; `sources/finviz.py`. |
| D-2 | VERIFY which volume basis each plan returns (IEX-only vs consolidated) | UNVERIFIED (needs keys) | **Safe default: treat single-venue feeds as `SINGLE_VENUE`.** Penny defaults `ALPACA_FEED=iex` — an IEX-only feed understates consolidated volume. Relative-volume features are computed only against same-basis history; if basis `UNKNOWN`, RVOL features are `MISSING` (DATA-04). | `penny/config.py` `ALPACA_FEED=iex`. |
| D-3 | Minimum price increments for stocks below/above $1.00 (sub-penny rule) | UNVERIFIED | **Safe default (US market rules, re-confirm in Phase 1):** ≥ $1.00 quotes in $0.01 increments; < $1.00 quotes in $0.0001 increments. Used only to store spread in ticks (G-SPREAD) and to render decimals (§18.1). Confirm against the exchange rulebook / a live quote probe before relying on it for a gate. | build-prompt rule; to probe. |
| D-4 | EDGAR filing acceptance hours | UNVERIFIED | **Safe default:** EDGAR accepts filings roughly 06:00–22:00 ET on business days (and the system should expect overnight batches). Confirm from the SEC's published hours in Phase 1 and record the source. Overnight ingestion must be robust to bursts either way. | to confirm from SEC docs. |
| D-5 | Trade-halt feed availability (free/paid) | UNVERIFIED | **Safe default: no halt feed in Phase 1.** `G-HALT` and `HALTED_SUSPENDED` are implemented against whatever the primary feed exposes (a halt shows as stale/absent quotes). The `halt_feed.py` adapter is a stub that returns `UNKNOWN` until a free or already-paid feed is confirmed. Ask the user before adding one. | to probe; no free feed confirmed. |
| D-6 | Volatility pauses in extended hours | UNVERIFIED | **Safe default: not modelled.** Treated as a normal quote gap; staleness rules (DATA-03) apply. Revisit if a feed exposes them. | to probe. |
| D-7 | Finviz Elite provides the symbol grouping needed for sympathy (§6.5) | UNVERIFIED | **Safe default: sympathy is OFF** unless the feed provides a usable sector/industry grouping. Penny's `finviz.groups(group="sector")` exists, so a sector-level grouping is likely available; confirm with a live probe and enable only if the join is reliable. | `sources/finviz.py` `groups()`. |
| D-8 | Source supports intraday data that includes extended hours (for the runner list high, §12.3) | UNVERIFIED | **Safe default: build the runner-list high from the extended-hours source (Finviz After-Hours Close/Change + intraday columns), not from daily bars.** Confirm in Phase 1 with a probe on a known pre-market spiker. | prompt §12.3; D-1. |
| D-9 | Which credentials are actually available/paid | BLOCKED (need user) | **Safe default: none assumed.** Wrappers fail soft; Phase 1 recording needs at least the primary feed (Finviz Elite) live. The user must supply `FINVIZ_TOKEN` (and optionally TwelveData/Alpaca/Finnhub) before Phase 1 recording can capture a real day. | no secrets in repo. |

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
