"""Verify the Finviz universe -> snapshot path now yields sub-$10 records.

Regression guard for the id-misalignment bug: the universe must produce tickers,
sub-$10 prices, and a non-zero record count.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.cli import build_clients                              # noqa: E402
from rotd.config import Config                                  # noqa: E402
from rotd.normalize import snapshot_from_finviz_rows            # noqa: E402
from rotd.util import UTC                                        # noqa: E402


def main() -> int:
    cfg = Config(path=Path("config.env"))
    fv = build_clients(cfg)["finviz"]
    raw = fv.universe()
    rows = [fv.parse_row(r) for r in raw]
    sub = [r for r in rows if r.get("symbol") and r.get("price") is not None
           and r["price"] <= 10]
    print(f"universe rows {len(rows)}; sub-$10 with ticker+price: {len(sub)}")

    now = datetime(2026, 10, 13, 23, 32, tzinfo=UTC)
    snap = snapshot_from_finviz_rows(sub, cfg, "POST", date(2026, 10, 13),
                                     cycle_ts=now)
    priced = sum(1 for r in snap.records.values() if r.price is not None)
    print(f"snapshot records {len(snap.records)}; priced {priced}; "
          f"grade {snap.data_grade}")
    for k in list(snap.records)[:3]:
        r = snap.records[k]
        print(f"  {k}: px={r.price} after={r.after_close} "
              f"basis={r.price_basis} reasons={r.reasons}")
    return 0 if snap.records else 1


if __name__ == "__main__":
    raise SystemExit(main())
