"""Fact: does ANY configured source carry bid/ask? (gates fail closed without it).

G-SPREAD and G-EXIT need a bid/ask spread; nothing ranks if it is absent. Scan
every source fixture + the Finviz full view for a quote/book column so the
answer is evidence, not assumption.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "src" / "rotd" / "sources" / "fixtures"

BOOK_KEYS = ("bid", "ask", "spread", "bidsize", "asksize", "bp", "ap",
             "bidprice", "askprice", "bidsize", "asksize")


def scan_json(path: Path) -> list[str]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    keys: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                keys.add(str(k).lower())
                walk(v)
        elif isinstance(o, list):
            for v in o[:3]:
                walk(v)

    walk(d)
    return sorted(k for k in keys if any(b in k for b in BOOK_KEYS))


def scan_csv_header(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return []
    first = text.splitlines()[:1]
    if not first:
        return []
    hdr = next(csv.reader(io.StringIO(first[0])))
    return [h for h in hdr if any(b in h.lower() for b in BOOK_KEYS)]


def main() -> int:
    print(f"scanning {FIX}")
    for p in sorted(FIX.iterdir()):
        if not p.is_file():
            continue
        if p.suffix in (".txt",):
            hits = scan_csv_header(p)
        elif p.suffix == ".json":
            hits = scan_json(p)
        else:
            hits = []
        print(f"  {p.name:40s} book-like keys: {hits or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
