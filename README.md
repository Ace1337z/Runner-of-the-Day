# ROTD — Runner of the Day

**A separate, stricter system that surfaces the few sub-$10 US stocks most likely
to be that day's explosive runner (+100% or more), as early as the evidence
allows — and stays silent the rest of the time.**

ROTD is decision support only. It places no orders, never shorts, and never
promises a move. It exists because the earlier penny early-warning system
alerts too often and its data is inconsistent. ROTD fixes that structurally:

- **Rank, then budget** — candidates compete; an alert is a scarce resource spent
  on the day's best name, not a reaction to a per-stock threshold.
- **One snapshot of truth** — every decision reads one reconciled, timestamped
  snapshot per cycle. No module fetches its own data in the decision path.
- **Fail closed** — missing, stale or conflicting data lowers the data-quality
  grade and suppresses alerts. It never becomes a pass.
- **Code counts, model reads** — all numbers, scores and ranks come from code.
  The language model only classifies news and filings, asynchronously.
- **Daily self-measurement** — a MISS REPORT explains, for every sub-$10 runner
  the day produced, exactly why ROTD did or did not flag it, and how early.

## Status

Under construction. Built in phases (see `docs/RECON.md` and the build plan).
At most `ALERT_BUDGET_PER_DAY` (3, +1 reserve) ignition alerts per trading day;
zero alerts on a quiet day is a correct outcome.

## Quickstart

```bash
pip install -e .[dev]
rotd doctor          # check config, sources, calendar, clock
rotd selftest        # offline self-test (no network)
rotd shadow          # run every decision, push nothing
rotd replay DATE     # re-run a stored day through the same code
```

## Documentation

- [`docs/RECON.md`](docs/RECON.md) — Phase 0 recon of the existing system and the
  proposed ROTD layout.
- [`docs/VERIFIED.md`](docs/VERIFIED.md) — every fact marked VERIFY, its status,
  and the safe default used when it could not be confirmed.

## Not financial advice

Penny stocks are extremely volatile and easy to manipulate, and every automated
signal can be wrong. Nothing here is financial advice. Use at your own risk.
`G-HALT`, data-quality suppression and the alert budget all exist to keep ROTD
quiet when it should be; they do not make it safe.

## License

MIT. See [`LICENSE`](LICENSE).
