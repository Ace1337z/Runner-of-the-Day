"""Diagnose why a POST-mode cycle ranks nothing.

Runs the real pipeline offline against the live universe and reports the gate
outcome distribution, so a structural data gap cannot hide behind 'NO_CANDIDATES'.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.cli import build_clients                            # noqa: E402
from rotd.config import Config                                # noqa: E402
from rotd.features import compute_features                    # noqa: E402
from rotd.gates import run_gates                              # noqa: E402
from rotd.normalize import snapshot_from_finviz_rows          # noqa: E402
from rotd.util import UTC                                      # noqa: E402


def main() -> int:
    cfg = Config(path=Path("config.env"))
    fv = build_clients(cfg)["finviz"]
    raw = fv.universe()
    rows = [fv.parse_row(r) for r in raw]
    sub = [r for r in rows if r.get("symbol") and r.get("price") is not None
           and r["price"] <= 10]

    for mode in ("POST", "OPEN"):
        now = datetime(2026, 10, 13, 19, 43, tzinfo=UTC)
        snap = snapshot_from_finviz_rows(sub, cfg, mode, date(2026, 10, 13),
                                         cycle_ts=now)
        reasons: Counter[str] = Counter()
        passed = 0
        for sym, rec in snap.records.items():
            fs = compute_features(rec, cfg, snap)
            rep = run_gates(rec, fs, cfg, mode, snap)
            if rep.passed:
                passed += 1
            for r in rep.results:
                if not r.passed:
                    reasons[f"{r.gate}={r.reason}"] += 1
        print(f"mode={mode}: records={len(snap.records)} gates_passed={passed}")
        for r, n in reasons.most_common(12):
            print(f"    {n:5d}  {r}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
