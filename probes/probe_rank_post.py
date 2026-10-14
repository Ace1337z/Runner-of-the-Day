"""Prove the full decision path ranks and picks a ROTD in a tradeable mode.

Runs the live universe through reconcile -> features -> gates -> score -> rank
with the current (not-enforced) gates, in POST mode, and prints the top of the
board + reasons. This is the live equivalent of the offline selftest happy path.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.cli import build_clients                            # noqa: E402
from rotd.config import Config                                # noqa: E402
from rotd.features import compute_features                    # noqa: E402
from rotd.gates import run_gates                              # noqa: E402
from rotd.normalize import snapshot_from_finviz_rows          # noqa: E402
from rotd.scoring import rank, score_candidate                # noqa: E402
from rotd.util import UTC                                      # noqa: E402


def main() -> int:
    cfg = Config(path=Path("config.env"))
    fv = build_clients(cfg)["finviz"]
    raw = fv.universe()
    rows = [fv.parse_row(r) for r in raw]
    sub = [r for r in rows if r.get("symbol") and r.get("price") is not None
           and r["price"] <= 10]

    now = datetime(2026, 10, 13, 19, 43, tzinfo=UTC)   # POST window
    snap = snapshot_from_finviz_rows(sub, cfg, "POST", date(2026, 10, 13),
                                     cycle_ts=now)
    scored = []
    for sym, rec in snap.records.items():
        if rec.price is None:
            continue
        fs = compute_features(rec, cfg, snap)
        rep = run_gates(rec, fs, cfg, "POST", snap)
        if rep.passed:
            sc = score_candidate(rec, fs, rep, cfg, "POST")
            scored.append(sc)
    print(f"records={len(snap.records)} passed_gates={len(scored)}")
    rk = rank(scored, cfg)
    print(f"ranked={len(rk.day_ranked)} board={[s.symbol for s in rk.board]}")
    print(f"ROTD={rk.rotd.symbol if rk.rotd else None} ({rk.rotd_reason})")
    for s in rk.board[:8]:
        comps = s.components or {}
        print(f"  {s.symbol:6s} score={s.score:5.1f} rank={s.rank} "
              f"tape={comps.get('tape')} mom={comps.get('momentum')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
