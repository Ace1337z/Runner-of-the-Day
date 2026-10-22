# ROTD Companion — TradingView Indicator Guide

The chart companion for ROTD. When Telegram sends **`🚨 ROTD IGNITION — $XYZ`**,
open the chart and this tells you three things:

| | |
|---|---|
| **Where do I enter?** | Aqua ▲ **ENTRY** arrow |
| **Where does my stop go?** | Orange **"Stop in use"** line |
| **Where do I take profit?** | Lime ▼ **TP** triangles |

Source: [`ROTD_Companion.pine`](ROTD_Companion.pine)

---

## The 5 things that matter

If you read nothing else, read these.

### 1. Green HUD or no trade

The top-right panel's first cell decides everything:

```
✅ TRADEABLE        →  you may trade
⛔ BACKSIDE OF HOD  →  setup is dead. Skip.
⛔ TOO EXTENDED     →  >25% above armed level. Skip.
⛔ BELOW VWAP       →  scanner aborted this. Skip.
⛔ WEAK STRUCTURE   →  chart is broken. Skip.
⏳ WAIT             →  right stock, wrong moment. Wait.
```

**Any ⛔ means no trade.** This single rule prevents most losing runner trades.

### 2. Enter on the aqua ▲ arrow — and only if you're close

The arrow prints on the **close** of the first 1-minute bar with every gate green.

Check the HUD row **"Chase from arm"** before you buy:

| Value | Action |
|---|---|
| 0–10% | Clean entry |
| 10–25% | Late — size down |
| >25% | Arrow won't fire. **Pass.** |

If you opened the chart late and price has already run — **pass**. Another alert comes tomorrow. Chasing is the #1 way to lose money here.

### 3. Place the stop before you do anything else

Put a real stop order at the **"Stop in use"** price. Not a mental stop.

- **"Risk %"** on the HUD tells you your loss from here.
- Position size = *(money you accept losing)* ÷ *(Risk % / 100)*.

> If Risk % looks wide (e.g. 8–11%), **size down**. Do not tighten the stop below the line — normal noise will eject you.

### 4. Take profit in thirds on the lime ▼ TP

TP is a **ladder**, not one exit:

| Signal | Do this |
|---|---|
| 1st ▼ TP (+16% above VWAP) | Sell **⅓–½** |
| 2nd ▼ TP (long upper wick) | Sell **⅓** |
| 3rd ▼ TP | Optional — keep a runner |

Taking the first partial means a reversal can no longer hurt you, while the rest can still ride a +35% move. **Greed at +16% is the classic runner killer.**

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

### Match settings to your server

Indicator **Settings → group 1** must equal your `config.env`:

| Setting | Default |
|---|---|
| `ARM_MIN_SCORE` | 68 |
| `IGNITION_THRESHOLD` | 70 |
| `ROTD_MIN_SCORE` | 60 |
| `CHASE_CAP_PCT` | 25 |
| `MAX_DRAWDOWN_FROM_HOD_PCT` | 15 |
| `MIN_STRUCTURE_SCORE` | 4.0 |

These already match the live system. **Only change them if you change `config.env`** — otherwise the chart and Telegram will disagree.

Leave **Structure timeframe = 5**. EMAs are read from 5-minute and drawn on your 1-minute chart; 1-minute EMAs are too noisy to define a trend.

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

### HUD (top-right)

| Field | Use it for |
|---|---|
| **Tape score** | 0–100 momentum. Green ≥70, yellow ≥68, red below |
| **Mode** | TREND (loose trail) or CLIMAX (tight trail) |
| **Hard stop / Stop in use** | Where your order goes |
| **Risk %** | Sizing — your loss to the stop |
| **Stretch vs VWAP** | Above 16% = TP territory |
| **Structure pts** | /15. Below 4.0 = not tradeable |
| **Chase from arm** | Anti-FOMO gauge |
| **RVOL** | Volume vs same time on prior days |

---

## Worked example

Telegram: `ROTD IGNITION — $ABCD @ 2.05 | Score 73`

1. **Open $ABCD 1-min.** HUD: ✅ TRADEABLE, score 71, RVOL 4.2x, structure 9.1/15, chase 3%
2. **▲ ENTRY at 2.08.** Buy 1,000 sh = $2,080
3. **Stop in use = 1.92.** Risk = 7.7% = $160. Place the order
4. **+16% → ▼ TP at 2.42.** Sell 400 sh. Now a stop-out is roughly break-even
5. **CLIMAX mode.** Trail tightens. ▼ TP at 2.60 → sell 300
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

1. **No green HUD, no trade.** Non-negotiable.
2. **Stop in before you look at targets.**
3. **Pass on extended entries.** There is always another alert.
4. **Take the first partial** — especially when it feels too early.
5. **Never lower a stop.**
6. **Never average down on a runner.** If it failed, it already gave you its move.
7. **One alert, one position.** Don't stack correlated names.
8. **1-minute chart only.** Higher timeframes break every threshold.

---

## Limitations — read this

- **The score is not the scanner's.** Float, short interest, 3-minute volume,
  10-minute dollar volume and the LLM catalyst verdict are not available on
  TradingView. Expect **±6 points** difference.
- **What matches exactly is the gate logic** — VWAP abort, 15% HOD limit, 25%
  chase cap, structure floor. Those are what keep you out of bad trades.
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
HUD GREEN?     →  NO  →  skip, wait for next alert
        ↓ YES
AQUA ARROW     →  enter at close / next open
        ↓
PLACE STOP     →  "Stop in use" (real order)
        ↓
1st ▼ TP       →  sell 1/3 to 1/2
2nd ▼ TP       →  sell 1/3
        ↓
✕ EXIT / STOP  →  out of the rest
```
