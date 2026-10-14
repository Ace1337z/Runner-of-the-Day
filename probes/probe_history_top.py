"""Inspect the stored 2026-10-13 cycles and what ROTD would have said about
the day's biggest sub-$10 movers, for a backtest-vs-reality check."""

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

    print("== stored snapshots (2026-10-13) ==")
    for r in db.execute(
        "SELECT trading_day,mode,cycle_ts,data_grade,n_symbols FROM snapshots "
        "WHERE trading_day='2026-10-13' ORDER BY cycle_ts"
    ):
        print(f"  {r['mode']:8s} {str(r['cycle_ts'])[:19]} grade={r['data_grade']} "
              f"symbols={r['n_symbols']}")

    # top gainers that closed 2026-10-13 (finviz ta_topgainers)
    cfg = Config(path=Path("config.env"))
    fv = FinvizClient(cfg)
    rows = fv.screener(signal="top_gainers")
    sub = [fv.parse_row(r) for r in rows]
    sub = [r for r in sub if r.get("symbol") and (r.get("price") or 0) <= 10]
    sub.sort(key=lambda r: (r.get("change_pct") or 0), reverse=True)
    print(f"\n== Finviz top_gainers, sub-$10 ({len(sub)} names) ==")
    for r in sub[:10]:
        print(f"  {r['symbol']:6s} px={r['price']} chg={r['change_pct']}% "
              f"susp={r.get('suspended')} grd={r.get('data_grade')}")

    # How many of those were in the 23:43 POST candidate set, and their gate result
    cand = {row["symbol"]: row for row in db.execute(
        "SELECT symbol,score,passed_gates,gate_json FROM candidates "
        "WHERE trading_day='2026-10-13' AND mode='POST' AND cycle_ts LIKE '%23:43%'")}
    print(f"\n== overlap of top sub-$10 gainers vs ROTD 23:43 POST candidates ==")
    for r in sub[:10]:
        sym = r["symbol"]
        if sym in cand:
            row = cand[sym]
            g = json.loads(row["gate_json"])
            failed = [k for k, v in g.items() if not v.get("passed")]
            print(f"  {sym:6s} IN candidates  score={row['score']} "
                  f"passed_gates={row['passed_gates']} failed={failed or '-'}")
        else:
            print(f"  {sym:6s} NOT in universe/candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
