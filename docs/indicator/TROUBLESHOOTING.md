# If it still says "cannot compile"

A generic "cannot compile" with no line number is hard to diagnose remotely.
This page lets you find the failing line yourself in about 2 minutes.

The script is built in numbered **sections**. Commenting sections out from the
bottom upward tells you exactly which one breaks.

## How to bisect

1. Open the script in Pine Editor.
2. Select a whole section (from its `// ====` banner to the next banner) and
   delete it, or put `//` at the start of each line.
3. Save. If it compiles → the deleted section was the problem.
   If it still fails → restore it and delete a lower section instead.

Work **bottom-up**: sections 13 → 12 → 11 → 10. Sections 1–9 are the engine;
if only 10–13 are removed the script still compiles.

| Section | What it is | Safe to delete? |
|---|---|---|
| 1 | Inputs | no — everything uses them |
| 2 | Core levels (VWAP, EMAs, day high/low) | no |
| 3 | Tape score | yes |
| 4 | Structure score | yes |
| 5 | Run regime | no |
| 6 | Risk model | no |
| 7 | Take-profit engine | yes |
| 8 | Exit logic | yes |
| 9 | Trade gates | no |
| 10 | Plots | **yes — start here** |
| 11 | Signals (plotshape) | yes |
| 12 | HUD (table) | yes |
| 13 | Alerts | yes |

## Most likely culprits, in order

These are the constructs that differ between Pine versions and account for
almost every generic failure:

1. **`table.new` / `table.cell`** (section 12) — if the table signature differs
   on your account, delete section 12 first. You lose the HUD but keep every
   signal.
2. **`str.format`** (section 12 and 13) — replace with plain `+` concatenation
   if it objects.
3. **`request.security` count** (section 2) — if your plan's limit bites,
   delete the three `dPrev*` lines and replace `dayHigh`/`dayLow`/`prevClose`
   with `high` / `low` / `close[1]`. The HOD gate then only sees today's bar,
   which is less accurate but still works on a 1-minute chart.
4. **`array<array<...>>`** — not used any more (removed in an earlier fix), so
   this should no longer be a cause.

## Quick smoke test

Paste this first. If even this fails, the problem is the account/plan, not the
script:

```pine
//@version=6
indicator("smoke", overlay = true)
plot(close, "close")
```

If that compiles, paste the full script again and bisect as above.

## What to send me if it still fails

- The **exact** message text, including any `CE#####` code
- Which section you had already deleted when it started/failed
- Your TradingView plan (Basic / Essential / Plus / Premium / Expert / Ultimate)

With the section number I can fix it directly instead of guessing.
