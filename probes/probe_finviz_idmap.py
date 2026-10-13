"""Probe: map FINVIZ column ids -> header names, empirically.

Fact we need: the exact `c=` id for every field `parse_row` reads. We do not
guess: request one id at a time is too slow, so we request the *full* id range in
one call and read the header back in order, then cross-check a few ids singly.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.cli import build_clients                    # noqa: E402
from rotd.config import Config                        # noqa: E402


def header_for(cfg, c: str) -> list[str]:
    fv = build_clients(cfg)["finviz"]
    text = fv._export("screener", c=c, v="152", s="")
    return next(csv.reader(io.StringIO(text.lstrip("\ufeff"))))


def main() -> int:
    cfg = Config(path=Path("config.env"))

    # Request ids 0..100 in one call. Finviz keeps the requested order, so the
    # header we get back is a direct id->name table.
    ids = list(range(0, 100))
    c = ",".join(str(i) for i in ids)
    hdr = header_for(cfg, c)
    print(f"requested {len(ids)} ids; header back has {len(hdr)} columns")
    for i, name in enumerate(hdr):
        print(f"  id {i:2d}  {name}")

    # Cross-check three ids singly against their position in the table above.
    print()
    for cid in (1, 65, 68):
        h = header_for(cfg, str(cid))
        print(f"  single c={cid:2d} -> {h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
