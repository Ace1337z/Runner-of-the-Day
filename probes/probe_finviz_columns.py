"""Probe: what columns does the live Finviz custom view actually return?

ROTD hit a real bug: `universe()` returned rows with no ticker because the
requested custom view omitted the identity columns. This probe records the exact
header the live export returns for the current REQUIRED_COLUMNS set, so the fix
is verified against reality, not guessed.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.cli import build_clients                    # noqa: E402
from rotd.config import Config                        # noqa: E402
from rotd.sources.finviz import merge_columns         # noqa: E402


def main() -> int:
    cfg = Config(path=Path("config.env"))
    fv = build_clients(cfg)["finviz"]

    text = fv._export("screener", c=merge_columns(""), v="152", s="")
    hdr = next(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    print(f"live header has {len(hdr)} columns:")
    for i, h in enumerate(hdr):
        print(f"  {i:3d}  {h}")

    print()
    ids = [int(x) for x in merge_columns("").split(",") if x.strip()]
    print(f"merge_columns('') -> {len(ids)} ids: {ids}")

    rows = fv.screener(columns="")
    parsed = [fv.parse_row(r) for r in rows]
    named = [p for p in parsed if p.get("symbol")]
    print(f"parsed rows {len(parsed)}; with a ticker: {len(named)}")
    if named:
        print(f"  sample: {named[0].get('symbol')} price={named[0].get('price')}")
    return 0 if named else 1


if __name__ == "__main__":
    raise SystemExit(main())
