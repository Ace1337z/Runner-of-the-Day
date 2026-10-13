# ROTD — Phase 0 Recon

**Date:** 2026-02-15
**Scope:** Read the existing `penny-early-warning` system and record what ROTD can
reuse, how it is built, and the final repo layout. **No code was changed in the
existing system.** All facts below were read from the checkout at
`C:\Users\OK\OneDrive\Documents\Default Project\penny-early-warning`
(branch `main`, HEAD `ce63351`), plus a small number of local probe
runs (Python/import checks) recorded in `docs/VERIFIED.md`.

> This file is recon output only. It proposes; Phase 1 implements. Every
> `VERIFY` item from the build prompt is tracked in `docs/VERIFIED.md` with a
> status.

---

## 1. What the existing system is

A single-process Python 3.10+ server that watches every US stock priced under
$10 during the pre-market, regular and post-market sessions (04:00–20:00 ET)
and sends Telegram alerts when a stock starts building a move on rising volume.
It is a screener/decision-support tool. It places no orders.

- Entry point: console script `penny = penny.cli:main` (`pyproject.toml`).
- Package root: `src/penny/` (setuptools `packages.find where = ["src"]`).
- Config: one env-style file, defaults for every key, live reload, masked
  secrets (`src/penny/config.py`).
- Storage: SQLite (`state.db`, `journal.db`), schema + migrations in
  `src/penny/store.py`.
- Telegram: thin HTTP client + `FakeTelegram` test double
  (`src/penny/telegram.py`).
- Tests: `pytest`, 34 test modules, `testpaths = ["tests"]`, `pythonpath = ["src"]`.
- Deploy: `deploy/penny.service` (systemd), `install.sh`, `update.sh`.
- Docs: `README.md`, `docs/RUNBOOK.md`, `docs/FINDINGS.md`.

The old system's known weaknesses (from the README and the build prompt):
it alerts too often, and its data is inconsistent across sources. ROTD is a
separate system that fixes these structurally (rank+budget, one reconciled
snapshot, daily self-measurement).

---

## 2. Source adapters and their interfaces

All live in `src/penny/sources/`. They share plumbing from `base.py`:

- `SourceError` — recoverable source failure; callers fail soft.
- `RateLimiter(rate, per_seconds)` — thread-safe N-calls-per-window limiter.
- `TTLCache` — monotonic-clock key/value cache with `get(key, ttl)` / `set` / `clear`.
- `parse_csv(text)` — BOM/blank-line tolerant CSV → `list[dict[str, str]]`.
- `looks_like_html(text)` — used to detect a bad Finviz token.
- `fnum(value)` — tolerant number parsing (strips `% $ ,` and `K/M/B/T`).
- `pct_to_fraction(value)` — `1.23 → 0.0123`.
- `BaseClient` — timeouts, retries, secret scrubbing, request counting, optional
  limiter; `request/get_text/get_json/post_json`, `health()`, `error_text()`.
  `session()` sets `User-Agent: penny-early-warning/1.0`.

### 2.1 Adapter inventory

| Module | `name` | Constructor | Public methods (signature) | Transport |
|---|---|---|---|---|
| `finviz.py` | `finviz` | `(cfg, secrets=None)` | `fetch(link, *, ttl=0.0, **params) -> str`; `fetch_csv(link, *, ttl=0.0, **params) -> list[dict]`; `screener(...)`; `universe(ttl=300.0)`; `movers(session=None, top_n=200, ...)`; `snapshot(symbols, session=None, ...)`; `normalize_row(row, session) -> dict|None`; `verify(ticker)`; `stock_history(ticker, period="d")`; `groups(group="sector", view="152")`; `latest_filings(ticker)`; `news(view="1")`; `ticker_news(ticker, ttl=300.0)`; `insiders(ticker, tc="7")`; `economic_calendar(date_from)`; `health()` | REST/CSV, token via `FINVIZ_TOKEN` |
| `twelvedata.py` | `twelvedata` | `(cfg, secrets=None)` | `quote(symbol) -> dict|None`; `bars(symbol, interval="1min", outputsize=300, ...)`; `configured()`; `health()` | REST JSON, key via `TWELVEDATA_KEY` |
| `alpaca.py` | `alpaca` | `(cfg, secrets=None)` | `bars(symbol, start: datetime, end: datetime, ...)`; `news(symbols, hours=72, limit=50)`; `configured()`; `health()` | REST JSON, `ALPACA_KEY`/`ALPACA_SECRET`, feed `ALPACA_FEED=iex` |
| `finnhub.py` | `finnhub` | `(cfg, secrets=None)` | `company_news(symbol, days=3)`; `configured()`; `health()` | REST JSON, `FINNHUB_KEY` |
| `sec.py` | `sec` | `(cfg, secrets=None)` | `cik_for(ticker)`; `filings(ticker, days=5) -> list[dict]`; `filing_text(filing, limit=40000)`; `health()` | EDGAR JSON/text, `SEC_USER_AGENT` |
| `finra.py` | `finra` | `(cfg, secrets=None)` | `daily(day=None, lookback=7) -> dict[str, dict]`; `short_ratio(symbol) -> dict|None`; `health()` | FINRA short-interest files |
| `yahoo.py` | `yahoo` | `(cfg, secrets=None)` | `bars(symbol, days=1, interval="1m")`; `index_levels(symbols=("^GSPC","^IXIC","^VIX"))`; `google_news(symbol, limit=8)`; `health()` | Yahoo chart JSON / Google News RSS |

`FinvizClient` has special token handling: `_token()` re-reads the live config
each call; a 401/403 or an HTML body raises `FinvizTokenRejected` and pauses
Finviz steps until the token changes (no restart needed). This is the pattern
ROTD should mirror for its own token-rejection health.

### 2.2 Which adapters ROTD reuses (via thin wrappers)

Per the build prompt §5.1 and DATA-01/04 notes:

| Need | Reused adapter | Wrap as |
|---|---|---|
| Universe, movers, quote rows, news, filings | `finviz.FinvizClient` | `rotd.sources.finviz_feed` |
| Fresh price/volume/1-minute candles | `twelvedata.TwelveDataClient` | `rotd.sources.twelvedata_feed` |
| Candle fallback | `alpaca`, `finnhub` | `rotd.sources.alpaca_feed`, `rotd.sources.finnhub_feed` |
| Filings text | `sec.SecClient` | `rotd.sources.edgar_feed` |
| Short data | `finra.FinraClient` | `rotd.sources.finra_feed` |
| Last-resort bars / index regime | `yahoo.YahooClient` | `rotd.sources.yahoo_feed` |

**Decision (user-approved):** ROTD is a *fresh repo*. The needed adapters are
**copied into `src/rotd/sources/` as an owned fork** (retaining their module
structure, `BaseClient` and helpers) rather than importing the `penny` package.
ROTD does not depend on `penny` at install or run time. This keeps ROTD's
"do not edit the existing system" rule trivially true and keeps the two systems
independently deployable.

Wrappers add, on top of the copied adapters:
- raw-payload capture (store untouched text/JSON + `fetched_at`) — the copied
  clients return parsed rows only, so the wrapper must capture the raw body;
- canonical-record normalization (see §5 of this doc);
- a uniform `health()` aggregation into ROTD's `source_health` table.

### 2.3 Reusing without editing

Because the copy is a fork, the adapters may be lightened (e.g. drop the
`penny`-specific storage hooks) but their **HTTP behaviour, parsing and
`FinvizClient` token discipline are kept byte-for-byte** wherever possible so
that the existing, tested parsing continues to hold. The original tests'
fixtures for CSV/JSON shapes are the reference for ROTD's own adapter tests.

---

## 3. Config system

`src/penny/config.py`:

- `DEFAULTS: dict[str, str]` — every key has a default. Groups are declared in
  `GROUPS` for `summary()`/`save()` ordering.
- `SECRET_KEYS` — never displayed, scrubbed from logs.
- `RESTART_KEYS` — structural keys that need a restart when changed.
- Live reload via file mtime; `raw(key)`, typed getters (`int`, `float`,
  `bool`, `str`), `display(key)` (masked), `set(key, value)`, `summary()`.
- `restrict_permissions(path)` — chmod 600 on POSIX, `icacls` ACL on Windows.
- Derived paths: `home`, `data_path(*parts)`, `state_db`, `journal_db`,
  `bars_dir`.

**ROTD approach:** a clone of this pattern in `src/rotd/config.py`, with:
- a **separate** config file (default `~/.rotd/rotd.env`, overridable via
  `ROTD_HOME`), not `penny.env`;
- **per-mode profiles** (§3 of the build prompt): `POST`, `OVERNIGHT`, `PRE`,
  `OPEN`, `MIDDAY`, `CLOSED`, each a set of named settings (cadence, liquidity
  gates, weight profile, allowed alert types) living in config, not code;
- **every threshold is a named setting with a unit, a starting value and a
  one-line rationale** (build-prompt ground rule). Enforced by a settings
  registry that carries `(name, unit, default, rationale)`, and a test that
  fails if a registry entry is missing a rationale;
- secrets via hidden input or a mode-600 file, never in argv, scrubbed from logs.

---

## 4. Database approach

`src/penny/store.py`:

- SQLite, `SCHEMA_VERSION`, `STATE_SCHEMA` DDL string, `_ensure_columns()`
  auto-migration, typed helpers, a `@contextmanager` for connections.
- Two databases: `state.db` (alerts, watch, tokens, cycles, daily, ai_calls,
  predictions, kv, leaderboard_history, …) and `journal.db`.

**ROTD approach** (build prompt §13): a **separate** SQLite file in **WAL
mode**, one DB per install (`~/.rotd/rotd.db`). Tables (append-only unless
noted): `raw_payloads`, `quotes`, `bars`, `news`, `filings`, `reference`,
`snapshots`, `features`, `gate_results`, `scores`, `catalyst_audits`,
`candidates` (mutable — current state), `state_transitions`, `alerts`,
`suppressions`, `outcomes`, `runner_list`, `miss_report`, `source_health`,
`config_history`. Every table gets an index on `(symbol, time)`.
Retention + `rotd backup` per §13; disk-growth is **measured in Phase 1**, not
assumed.

Reused ideas: the DDL-string + `_ensure_columns()` migration idiom, WAL, and the
append-only log tables. Not reused: the penny schema itself (ROTD's decision
model is different and must not share tables).

---

## 5. Telegram layer

`src/penny/telegram.py`:

- `TelegramClient(cfg, secrets=None)`: `send(text, chat_id=None,
  disable_preview=True, ...)`, `edit(msg_id, ...)`, `delete(msg_id, ...)`,
  `answer_callback(...)`, `send_chat_action(...)`, `set_commands(...)`,
  `send_document(path, caption="")`, `get_updates(offset, timeout)`,
  `detect_chat_id(attempts, timeout)`, `health()`, `configured()`.
- `split_message(text, limit=MAX_MESSAGE)` — chunks long messages.
- `FakeTelegram` — records sends/edits in memory; `text_of(msg_id)`; used by
  every offline test. ROTD will reuse this test double.

**ROTD approach:** copy the client as `src/rotd/telegram.py` (HTML-escaped,
one idea per message per §14.1). Add:
- **idempotent** sends keyed by `(symbol, day, type)` (alert key) — a restart
  must not resend (A7), so the notifier checks the `alerts`/`suppressions` log
  before sending;
- rate limiting and a message-type distinction — IGNITION pings with sound;
  BOARD/ABORT/DEGRADED/RECOVERED are silent (§14.1);
- command handlers `/top /board /check /why /miss /eod /stats /budget /status
  /set /help`, restricted to the configured chat id (§14.2).

---

## 6. Test setup

- `pytest` (dev extra), `testpaths=["tests"]`, `pythonpath=["src"]`.
- `tests/conftest.py` — shared fixtures (config in a tmp home, fake gateway,
  fake Telegram, fake clock). **No network in unit tests** (build prompt §15)
  is already the house style; ROTD keeps it: every adapter test runs against a
  saved fixture, never the wire.
- 34 test modules today; the build prompt requires unit tests for **every**
  gate, feature, sub-score breakpoint and state transition, **property tests**
  (monotonicity, hysteresis-never-flickers, budget-never-exceeded), golden
  fixtures from the Phase 0/1 probes, and replay tests on ≥ `REPLAY_MIN_DAYS`
  (10) recorded days.
- ROTD reuses: the tmp-home config fixture shape, `FakeTelegram`, and the
  "fixtures, not the wire" rule.

---

## 7. Other reusable parts

- `src/penny/util.py` — `now_et`/`now_utc`/`to_et`, `session_for(dt)`,
  `session_end(dt)`, `is_trading_day(d)`, `us_market_holidays(year)`,
  `money/compact/pct/num`, `clamp/safe_div/percentile/median`, `scrub`,
  `mask`, `sha256_hex`, `http_request`, `parse_json_lenient`. The time/market
  helpers are a starting point but ROTD must use a **real exchange-calendar
  library** (§3, VERIFY-1) rather than the hard-coded holiday list.
- Domain logic worth reading (not copying wholesale): `scoring.py`,
  `strategies/`, `technicals.py`, `fib.py`, `context/`, `journal.py`. The
  build prompt explicitly says to reuse the *logic* of `methodology_v3` for
  supply definitions (§9.3) and the *idea* of the six ABORT triggers (§11.3).
  ROTD reimplements these inside `src/rotd/`; it does not import them.

---

## 8. Proposed final repo layout

```
big-whale-penny-tracker/
  pyproject.toml            # name = "big-whale-penny-tracker"; script: rotd = rotd.cli:main
  requirements.txt
  requirements-dev.txt
  README.md
  LICENSE
  install.sh / update.sh      # (later) systemd deploy
  deploy/rotd.service
  config.env.example
  docs/
    RECON.md                  # this file (Phase 0)
    VERIFIED.md               # every VERIFY item + status (Phase 0)
    DECISIONS.md              # section-18 answers, confirmed
    RUNBOOK.md                # (later)
    FINDINGS.md               # (later)
  src/rotd/
    __init__.py
    cli.py                    # rotd run|shadow|replay|probe|doctor|selftest|fit|report|backup
    config.py                 # settings registry + per-mode profiles
    util.py                   # tz/calendar/time/math/log-scrub helpers
    logging_setup.py
    store.py                  # SQLite WAL schema + migrations + retention
    telegram.py              # client + FakeTelegram (forked)
    notify.py                 # notifier: idempotent, rate-limited, silent-vs-sound
    calendar_us.py            # exchange calendar + mode engine (POST/OVERNIGHT/PRE/OPEN/MIDDAY/CLOSED)
    sources/                  # owned fork of the penny adapters + new ones
      __init__.py
      base.py                 # SourceError, RateLimiter, TTLCache, parse_csv, BaseClient
      finviz.py twelvedata.py alpaca.py finnhub.py sec.py finra.py yahoo.py
      halt_feed.py            # new, if VERIFY confirms a free/paid halt feed
      fixtures/               # saved raw responses (golden fixtures)
    ingest.py                 # raw payload capture with fetch time
    normalize.py              # canonical Quote/Bar/News/Filing/Reference records
    reconcile.py              # DATA-01..DATA-12, quality grades A/B/C
    snapshot.py               # immutable, versioned, timestamped snapshot
    features.py               # pure feature functions
    phases.py                 # PRE_BREAKOUT..FORMER_SUPERNOVA
    gates.py                  # G-PRICE..G-PHASE with reason codes
    score.py                  # Stage 1 rules score (Stage 2 later: model.py)
    ranker.py                 # per-mode rank + board + ROTD
    supply.py                 # ACTIVE_DILUTION/HEAVY/OVERHANG/SHELF_ONLY/CLEAN
    catalyst.py               # async LLM audit service + cache + validation
    state_machine.py          # candidate lifecycle with hysteresis
    budget.py                 # alert budgeter (the only push approver)
    tracker.py / grader.py    # outcomes at checkpoints, alerts and non-alerts
    runner_list.py            # daily runner list
    miss_report.py            # MISS REPORT classify + store + send
    metrics.py                # daily/rolling metrics
    reports.py                # board/EOD/weekly rendering
    replay.py                 # run a stored day through the same code
    runtime.py                # single-instance lock, watchdog, clock-skew check
    selftest.py               # offline self-test
    doctor.py                 # rotd doctor
    probes.py                 # rotd probe SOURCE  (writes fixtures)
    prompts/methodology_v3.md # the catalyst classification contract (§10)
  tests/
    conftest.py
    fixtures/                 # golden fixtures
    test_*.py
```

---

## 9. Decisions needed from the user (section 18)

The user has pre-selected the defaults below; ROTD will proceed with them and
only ask if a probe contradicts one. Listed here for the Phase 0 record:

1. **No price floor.** `MAX_PRICE = 10.00` only. Break metrics/MISS REPORT down
   by price band (<0.20, 0.20–0.50, 0.50–1, 1–5, 5–10). Show up to four decimals.
2. `INCLUDE_OTC = 0` (NASDAQ, NYSE, NYSE American only).
3. `ALERT_BUDGET_PER_DAY = 3`, plus one reserve slot.
4. Grading: runner = +30% before −12%. Runner list = +100% vs prior regular
   close, main list ≥ $5,000,000 intraday dollar volume, plus a low-liquidity
   second list (≥ $1,000,000).
5. Display times in `America/New_York` and `Asia/Kuala_Lumpur` (`zoneinfo` for
   both). Quiet hours: none by default, configurable per mode.
6. MISS REPORT gainers source: Finviz Elite daily export cross-checked against
   intraday data that includes extended hours.
7. MIDDAY pushes allowed, `MIDDAY_MIN_SCORE = 85`, `MIDDAY_MIN_ROTATION = 2.0`.

Open questions that need a real answer before Phase 1/2 (see `VERIFIED.md`):
- Which credentials are actually paid-for/available (Finviz Elite token,
  TwelveData key, Alpaca, Finnhub)? The wrappers fail soft, but Phase 1
  recording needs at least the primary feed live.
- Which exchange-calendar library to standardise on
  (`exchange_calendars` and `pandas_market_calendars` are both importable —
  see `VERIFIED.md`); the default is `exchange_calendars`.
