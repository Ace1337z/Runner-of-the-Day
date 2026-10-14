# ROTD — Runner of the Day

**A separate, stricter system that surfaces the few sub-$10 US stocks most likely
to be that day's explosive runner (+100% or more), as early as the evidence
allows — and stays silent the rest of the time.**

ROTD is decision support only. It places no orders, never shorts, and never
promises a move. It exists because the earlier penny early-warning system
(`penny-early-warning`, read-only reference in this repo) alerts too often and
its data is inconsistent. ROTD fixes that structurally:

- **Rank, then budget** — candidates compete; an alert is a scarce resource spent
  on the day's best name, not a reaction to a per-stock threshold.
- **One snapshot of truth** — every decision reads one reconciled, timestamped
  snapshot per cycle. No module fetches its own data in the decision path.
- **Fail closed** — missing, stale or conflicting data lowers the data-quality
  grade and suppresses alerts. It never becomes a pass.
- **Code counts, model reads** — all numbers, scores and ranks come from code.
  The language model only classifies news and filings, asynchronously, and can
  never raise a score above the rules-only verdict.
- **Daily self-measurement** — a MISS REPORT explains, for every sub-$10 runner
  the day produced, exactly why ROTD did or did not flag it, and how early.

## Install

```bash
pip install -e .[dev] && rotd doctor
```

(`python -m rotd.cli` works too if you don't install.) Copy `config.env` and fill
in the source tokens (Finviz Elite token is the only one required to run; see
`docs/VERIFIED.md`). Secrets stay in `config.env` (mode 600, gitignored) — they
are never read from the command line and are redacted from logs.

## Status

Built and running in shadow. At most `ALERT_BUDGET_PER_DAY` (3, +1 reserve)
ignition alerts per trading day; zero alerts on a quiet day is a correct outcome.

**Shadow mode is the default gate.** ROTD must run for a full **10-trading-day
shadow clock** before a single live push is allowed. The clock counts actual NYSE
sessions (weekends and holidays do not advance it), is stored in the database, and
survives restarts. A live `rotd run` refuses to push until the clock completes.
See `docs/DECISIONS.md` and the `shadow_clock` row in `rotd doctor`.

## Quickstart

```bash
rotd doctor          # check config, sources, calendar, shadow clock
rotd selftest        # offline self-test (no network)
rotd shadow-start    # start the 10-trading-day shadow clock
rotd shadow          # run every decision, push nothing
rotd shadow-status   # how far the clock has come
rotd finalize        # end of day: runner list + MISS REPORT
rotd miss            # print the MISS REPORT for a day
rotd status          # current day, state machine, budget used
rotd replay DATE     # re-run a stored day through the same code
```

## How it works

ROTD runs on a **cycle** driven by the US equity-session calendar
(`calendar_us.py`, backed by `exchange_calendars`). Each trading day is split
into phases — `PRE`, `OPEN` (front side), `MIDDAY`, `POST` (after hours);
`OVERNIGHT` is a closed state that never ignites. The clock decides the phase and
is what makes "as early as the evidence allows" precise rather than a guess.

A cycle:

1. **Universe** — `sources/finviz.py` pulls the whole sub-$10 US universe
   (Elite screener, full column set — the column-id mapping is verified in
   `docs/VERIFIED.md` G-17). Finviz is also the only extended-hours price source
   on this deployment.
2. **Reconcile** — `normalize.py` turns every raw row into one canonical record
   per symbol with a reason-coded price basis (`REGULAR` vs `EXTENDED`), a data
   grade (`A`–`F`), and staleness/conflict flags. Conflicting or stale quotes are
   dropped, never averaged silently.
3. **Features** — `features.py` derives the five scoring inputs (tape, momentum,
   catalyst, gap, context) plus a 15-minute dollar-volume / acceleration history
   fed in by the `tracker.py` PathTracker.
4. **Gates** — `gates.py` runs hard, fail-closed gates before any scoring:
   `G-DATA` (grade/stale/halt), `G-PHASE` (market state), `G-PRICE` (≤ $10, no
   floor), `G-HALT`, `G-TRADES`. Three gates that need a feed this deployment
   does not have — `G-SPREAD` (bid/ask book), `G-LIQ` (15-min bar feed),
   `G-EXIT` (slippage estimate) — are **off by default** and report
   `NOT_ENFORCED` (they re-arm if you add a book/bar feed; see `docs/VERIFIED.md`
   G-18). A name that fails any enforced gate is not scored and cannot alert.
5. **Score & rank** — `scoring.py` computes a 0–100 Stage-1 score from five
   weighted components, ranks candidates, and takes the `BOARD_SIZE` (5) top
   names. Rank 1 becomes ROTD only if its score clears `ROTD_MIN_SCORE` (70).
   The score is deliberately uncalibrated — it is an ordering, not a probability.
6. **State machine** — `state_machine.py` runs each board name through a ladder
   `WATCH → QUALIFIED → BOARD → ARMED → IGNITION`, spending the day's alert
   budget on the best-confirmed name. Transitions are reversible and every step
   stores a reason.
7. **Catalyst** — `catalyst.py` runs the methodology_v3 auditor (`auditor.py`)
   against a news/filings bundle, asynchronously. The model classifies and
   audits only; it can never exceed the rules-only verdict, and on timeout or no
   model it fails closed to rules-only.
8. **Explain & record** — `notifier.py` renders the board, ROTD pick, ignition,
   and the daily MISS REPORT. Every score, gate and suppression carries reason
   codes. `store.py` persists snapshots, candidates, alerts, outcomes,
   runner lists and the MISS REPORT to SQLite so any day can be replayed.

### Shadow clock (Phase 6)

`shadow.py` is a calendar-bound, 10-trading-day gate. Each cycle calls
`shadow.note_cycle`; the clock advances only on real NYSE sessions. Until it
completes, `rotd run` refuses to push (`exit 3`) and logs the reason. This is
the default safety gate — ROTD proves itself silent-when-wrong before it can
speak.

## Configuration

Every threshold is a named setting in `config.py` with a unit, a starting value
and a one-line rationale — no magic numbers. Show any value with
`rotd config KEY`. Highlights:

| Setting | Default | Meaning |
|---|---|---|
| `MAX_PRICE` | `10.00` USD | Universe ceiling (sub-$10). No price floor. |
| `ALERT_BUDGET_PER_DAY` | `3` | Ignition alerts per day (+1 reserve). |
| `BOARD_SIZE` | `5` | Top names carried to the board. |
| `ROTD_MIN_SCORE` | `70` | Rank-1 must clear this to become ROTD. |
| `SHADOW_DAYS_REQUIRED` | `10` | Trading days before live pushes allowed. |
| `USE_SPREAD_GATE` / `USE_LIQ_GATE` / `USE_EXIT_GATE` | `0` | Gates needing a book/bar feed; off (VERIFIED G-18). |

## Documentation

- [`docs/RECON.md`](docs/RECON.md) — Phase 0 recon of the existing system and the
  proposed ROTD layout.
- [`docs/VERIFIED.md`](docs/VERIFIED.md) — every fact marked VERIFY, its status,
  and the safe default used when it could not be confirmed (the Finviz column-id
  map G-17 and the no-book-feed finding G-18 live here).
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — the architectural decisions,
  including the shadow clock and the methodology_v3 auditor contract.
- [`docs/methodology_v3.md`](docs/methodology_v3.md) — the red-team catalyst
  auditing methodology.

## Source adapters

ROTD owns a fresh, forked source layer in `src/rotd/sources/` (no dependency on
the `penny` package, and `penny-early-warning` is never edited). Current
adapters: **Finviz** (universe, movers, quote rows, news, filings, extended-hours
price), **TwelveData** (regular-session 1-min bars + quote), **Finnhub** (quote +
market status), **Alpha Vantage** (global quote), **Yahoo** (chart), **SEC EDGAR**
(Atom), **FINRA** (RegSHO + consolidated short interest). Telegram delivery uses
`FakeTelegram` by default; live sends are wired but require a bot token.

## Testing & verification

- `rotd selftest` — ~50 offline deterministic self-checks across fixtures,
  reconciliation, gates, scoring, determinism, the shadow clock and the tracker
  (run with `rotd -v selftest` for the checklist). No network.
- `pytest tests/` — unit tests with the network blocked.
- `probes/` — live fact-finding scripts used to verify each source; their
  findings are recorded in `docs/VERIFIED.md`, not guessed.

## Not financial advice

Penny stocks are extremely volatile and easy to manipulate, and every automated
signal can be wrong. Nothing here is financial advice. Use at your own risk.
`G-HALT`, data-quality suppression and the alert budget all exist to keep ROTD
quiet when it should be; they do not make it safe.

## License

MIT. See [`LICENSE`](LICENSE).
