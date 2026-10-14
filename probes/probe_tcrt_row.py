"""Show what ROTD actually recorded for the day's biggest mover (TCRT, +188% on
2026-10-13) in the stored EOD snapshot, so the backtest is grounded in real data
rather than guessed."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.config import Config
from rotd.sources.finviz import FinvizClient


def main() -> int:
    db = sqlite3.connect("rotd.db")
    db.row_factory = sqlite3.Row

    # 1) What ROTD stored for TCRT in the 2026-10-13 EOD (23:43 POST) snapshot.
    row = db.execute(
        "SELECT * FROM candidates WHERE trading_day='2026-10-13' AND mode='POST' "
        "AND cycle_ts LIKE '%23:43%' AND symbol='TCRT'").fetchone()
    if row is None:
        print("no stored TCRT candidate in the 23:43 cycle")
        return 0
    print("== stored candidate: TCRT (2026-10-13 23:43 POST) ==")
    print("  score:", row["score"], " passed_gates:", row["passed_gates"],
          " suppression:", repr(row["suppression"]))
    g = json.loads(row["gate_json"])
    print("  GATES:")
    for k, v in g.items():
        print(f"    {k:12s} passed={v.get('passed')} reason={v.get('reason')}")
    f = json.loads(row["feature_json"])
    print("  FEATURES:", {k: f.get(k) for k in
          ("change_pct", "gain_from_prev_close", "price", "volume", "data_grade")})

    # 2) What Finviz reports for TCRT right now (the realized close move).
    cfg = Config(path=Path("config.env"))
    fv = FinvizClient(cfg)
    live = fv.screener(signal="top_gainers")
    for r in live:
        p = fv.parse_row(r)
        if p.get("symbol") == "TCRT":
            print("\n== Finviz live TCRT ==")
            print("  price:", p.get("price"), " change_pct:", p.get("change_pct"),
                  " after_close:", p.get("after_close"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
