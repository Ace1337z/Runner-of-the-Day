# ROTD Companion — TradingView Indicator Guide

The chart companion for ROTD. When Telegram sends **`🚨 ROTD IGNITION — $XYZ`**,
open the chart and this tells you three things:

| | |
|---|---|
| **Where do I enter?** | Aqua ▲ **ENTRY** arrow |
| **Where does my stop go?** | Orange **"Stop in use"** line |
| **Where do I take profit?** | Lime ▼ **TP** triangles |

Source: [`ROTD_Companion.pine`](ROTD_Companion.pine) · **Won't compile?** → [Troubleshooting](TROUBLESHOOTING.md)

---

## The 5 things that matter

If you read nothing else, read these.

### 1. Green status or no trade

The top-right panel's first cell decides everything:

```
TRADEABLE              (green)  →  you may trade
NO - BACKSIDE OF HOD   (red)    →  >15% off the high. Setup dead.
NO - BELOW VWAP        (red)    →  under the abort line. Scanner would abort.
NO - TOO EXTENDED      (red)    →  >25% above where the run started.
NO - WEAK STRUCTURE    (red)    →  under 4.0/15 structure points.
NO - RED ON DAY        (red)    →  not green vs previous close.
NO - DEAD VOLUME       (red)    →  bar below its 20-bar average volume.
NO - UNDER 9 EMA       (red)    →  momentum has cooled.
WAIT                   (red)    →  right stock, wrong moment.
```

**Anything other than TRADEABLE means no trade.** This single rule prevents most
losing runner trades.

### 2. Enter on the aqua ▲ arrow — and only if you're close

The arrow prints on the **close** of the first 1-minute bar with every gate green.

Check the **Chase** row in the status panel before you buy:

| Value | Action |
|---|---|
| 0–10% | Clean entry |
| 10–25% | Late — size down |
| >25% | Arrow won't fire. **Pass.** |

If you opened the chart late and price has already run — **pass**. Another alert comes tomorrow. Chasing is the #1 way to lose money here.

### 3. Place the stop before you do anything else

Put a real stop order at the **"Stop in use"** price. Not a mental stop.

- **"Risk %"** in the status panel tells you your loss from here.
- Position size = *(money you accept losing)* ÷ *(Risk % / 100)*.

> If Risk % looks wide (e.g. 8–11%), **size down**. Do not tighten the stop below the line — normal noise will eject you.

### 4. Take profit in thirds on the lime ▼ TP

TP is a **ladder**, not one exit:

| Signal | Do this |
|---|---|
| 1st ▼ TP — **2 ATR from entry** | Sell **⅓–½** |
| 2nd ▼ TP — **4 ATR from entry** | Sell **⅓** |
| 3rd ▼ TP — **7 ATR from entry** | Optional — keep a runner |
| Extra ▼ TP — long upper wick | Sell a partial if you still hold |

Targets are measured in **ATR from your entry price**, frozen when the run
starts. That is deliberate: an earlier version used "+16% above VWAP", but
VWAP follows price upward — a fast spike blew through it on bar 2, and a slow
grind let VWAP catch up and **never** reached it. A stock that doubled could
show zero TPs. ATR does not drift, so the same run shape gives the same target
whether it takes 5 bars or 40.

Each level fires **once per run**, so the chart stops repainting the same
signal. Taking the first partial means a reversal can no longer hurt you, while
the rest can still ride the parabolic part.

### 5. Never lower the stop. Ever.

The yellow trail only moves **up**. If you're widening it, you've stopped trading the system.

Exit on either:
- ✕ **EXIT** (red) — trend over
- **STOP** (maroon label) — stop breached, **get out now**

---

## Install (2 minutes)

1. Open any chart → **Pine Editor** (bottom panel)
2. Delete existing code, paste the `.pine` file contents
3. **Save** → name it `ROTD Companion`
4. **Add to chart**
5. **Timeframe: 1-minute** ← important
6. Save it as a chart layout so next time is two clicks

**That's it - there is nothing to type in, and no server settings to copy.**
The indicator deliberately does not use score thresholds - see
[why there is no score gate](#why-there-is-no-score-gate).

### The only 3 settings you might touch

| Setting | Default | Change it only if... |
|---|---|---|
| `CHASE_CAP_PCT` | 25 | you changed it in `config.env` |
| `MAX_DRAWDOWN_FROM_HOD_PCT` | 15 | you changed it in `config.env` |
| `MIN_STRUCTURE_SCORE` | 4.0 | you changed it in `config.env` |

These are the only gates the chart can reproduce **exactly** - they are pure
price geometry, so they cannot drift from the scanner. Leave them alone unless
you actually changed your server.

Leave **Structure timeframe = 5**. EMAs are read from 5-minute and drawn on your
1-minute chart; 1-minute EMAs are too noisy to define a trend.

---

## Why there is no score gate

The scanner gates on score: **ARM 68 / IGNITION 70 / ROTD 60**. The chart does
**not** - and that is deliberate.

Those scores are built partly from data TradingView cannot see:

- float and short interest
- 3-minute volume and 10-minute dollar volume
- the LLM catalyst verdict

Typing 68/70/60 into the indicator would make it *imitate a verdict it cannot
compute* - and you would have to retype them every time you tuned `config.env`.
Worse, it would quietly disagree with the server while looking authoritative.

**So the division of labour is:**

| Question | Who answers it |
|---|---|
| Is this a real setup worth alerting? | **The scanner** (Telegram) |
| Does it still look valid *right now*, in price terms? | **This chart** |

The chart's job is to catch a setup **dying between the alert and your entry** -
and every check it uses for that is a fact you can see:

| Gate | Exact? |
|---|---|
| Close below VWAP | Yes |
| More than 15% below high of day | Yes |
| More than 25% above armed level | Yes |
| Structure at least 4.0 / 15 | Yes - all 4 inputs are chart-visible |
| Above the 9 EMA + normal volume | Yes - observable facts |

Nothing above needs typing, and nothing can drift from the server.

---

## The chart

### Lines

| Line | Colour | Meaning |
|---|---|---|
| 9 EMA | 🟢 Green | Immediate momentum |
| 21 EMA | 🔴 Red | The swing trend |
| **VWAP** | 🟣 **Purple** | **The abort line — most important line on the chart** |
| Ratchet trail | 🟡 Yellow | Your moving stop (only while a run is live) |
| Hard stop | 🔴 Red thin | Absolute floor = VWAP − buffer |
| Stop in use | 🟠 Orange thin | The stop you actually place |

### Background

| Colour | Meaning |
|---|---|
| 🟢 Teal | Tradeable run is live |
| 🔴 Dark red | Backside of HOD — stand down |
| 🔴 Red band | Climax zone (+2σ/+3σ) — maximum extension risk |

### Markers

| Marker | Meaning |
|---|---|
| ▲ aqua **ENTRY** | Valid entry |
| ▼ lime **TP** | Take partial profit |
| ✕ red **EXIT** | Trend over |
| **STOP** maroon | Stop breached — exit now |
| ◆ orange **VWAP** | Lost VWAP — early warning |

### Status panel (top-right)

| Field | Use it for |
|---|---|
| **Status** | The verdict. `TRADEABLE`, or exactly which gate is failing |
| **Price** | Current price |
| **Stop in use** | Where your order goes |
| **Risk %** | Sizing — your loss to the stop. Shows `STOP HIT - EXIT` once breached |
| **Mode** | TREND (loose trail) or CLIMAX (tight trail) |
| **Next TP** | The next ATR target price. `all taken` once all three fire |
| **Structure** | /15. Below 4.0 = not tradeable |
| **Chase** | % above where the run started. Red past 25% |
| **RVOL** | Volume vs same time on prior days |

---

## Worked example

Telegram: `ROTD IGNITION — $ABCD @ 2.05`

1. **Open $ABCD 1-min.** Status: **TRADEABLE** · RVOL 4.2x · structure 9.1/15 · chase 3%
2. **▲ ENTRY at 2.08.** Buy 1,000 sh = $2,080
3. **Stop in use = 1.92.** Risk = 7.7% = $160. Place the order
4. **▼ TP1 at 2.16 (2 ATR).** Sell 400 sh. Now a stop-out is roughly break-even
5. **CLIMAX mode.** Trail tightens. ▼ TP2 at 2.32 → sell 300
6. **Price rolls to 2.44, breaks trail → ✕ EXIT.** Sell last 300

Averaged ≈ **+18%** on a move that peaked at +31%, with no path to a loss.
Not perfect — but uncatchable as a loser.

---

## Alerts

**Alert** (clock icon) → Condition: **ROTD Companion** → pick `ROTD ENTRY`,
`ROTD TAKE PROFIT`, or `ROTD EXIT` → set **Once Per Bar Close**.

> **Never** use "Once Per Bar" or "Any alert() call" — those fire mid-bar and
> create phantom signals that vanish.

---

## Rules that keep you profitable

1. **No green status, no trade.** Non-negotiable.
2. **Stop in before you look at targets.**
3. **Pass on extended entries.** There is always another alert.
4. **Take the first partial** — especially when it feels too early.
5. **Never lower a stop.**
6. **Never average down on a runner.** If it failed, it already gave you its move.
7. **One alert, one position.** Don't stack correlated names.
8. **1-minute chart only.** Higher timeframes break every threshold.

---

## Limitations — read this

- **The chart does not gate on score at all**, so there is nothing to retype and
  nothing that can drift. Score thresholds live on the server only.
- **The tape score shown is not the scanner's.** Float, short interest, 3-minute
  volume, 10-minute dollar volume and the LLM catalyst verdict are not available
  on TradingView. Expect **±6 points** — read it as a mood indicator, not a verdict.
- **Every gate on the chart matches the scanner exactly** — VWAP abort, 15% HOD
  limit, 25% chase cap, structure floor, 9 EMA + volume. There is no approximate
  gate left, because the approximate one (score) was removed.
- **RVOL needs history.** Shows `building…` until 2 prior sessions exist. Normal.
- **The trail is deliberately wide (11%).** On a quiet name that's a large stop.
  Always check Risk % and size accordingly.
- **This is decision support, not advice.** Your scanner prints this on every
  alert and it applies here too. Runners reverse hard; no indicator changes that.
- **Not compiled inside TradingView.** Verified with a custom Pine v6 grammar
  linter, exact parity tests against `rotd/scoring.py`, and risk/RVOL
  simulations. If the editor still objects, open an issue with the error code
  and the highlighted line.

---

## Quick card

```
ALERT ARRIVES  →  open 1-min chart
        ↓
TRADEABLE?     →  NO  →  skip, wait for next alert
        ↓ YES
AQUA ARROW     →  enter at close / next open
        ↓
PLACE STOP     →  "Stop in use" (real order)
        ↓
1st ▼ TP       →  sell 1/3 to 1/2   (2 ATR)
2nd ▼ TP       →  sell 1/3           (4 ATR)
        ↓
✕ EXIT / STOP  →  out of the rest
```
