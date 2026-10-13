"""Probe: do Finviz column ids line up with the named header?

If the live custom view is missing `Ticker`, the fix is to add the identity
column ids to the request. Verify by requesting an explicit small id set and
reading back the header + a sample ticker. Facts only; no guessing.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.cli import build_clients                    # noqa: E402
from rotd.config import Config                        # noqa: E402


def export(cfg, c: str) -> str:
    fv = build_clients(cfg)["finviz"]
    return fv._export("screener", c=c, v="152", s="")


def main() -> int:
    cfg = Config(path=Path("config.env"))

    # A. identity-only request: id 0 (No.), 1 (Ticker), 2 (Company)
    text = export(cfg, "0,1,2")
    hdr = next(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    row1 = next(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    print(f"A. c='0,1,2' -> {len(hdr)} cols: {hdr}")
    print(f"   first row: {row1}")

    # B. identity + the currently-required set
    req = "67,68,69,71,72,81,86,87,88,26,31,65,66,90,91,92,94"
    text2 = export(cfg, f"0,1,2,3,4,{req}")
    hdr2 = next(csv.reader(io.StringIO(text2.lstrip("\ufeff"))))
    print(f"B. identity+required -> {len(hdr2)} cols: {hdr2}")
    parsed = [r for r in csv.DictReader(io.StringIO(text2.lstrip("\ufeff")))]
    tickers = [r.get("Ticker") for r in parsed if r.get("Ticker")]
    print(f"   rows {len(parsed)}; with Ticker {len(tickers)}; sample {tickers[:5]}")

    # C. plain default (no c=) for reference
    text3 = export(cfg, "")
    hdr3 = next(csv.reader(io.StringIO(text3.lstrip("\ufeff"))))
    print(f"C. c='' -> {len(hdr3)} cols: {hdr3[:14]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
