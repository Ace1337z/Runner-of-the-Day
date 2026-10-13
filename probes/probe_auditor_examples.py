"""Auditor validation smoke test against the methodology_v3 examples.

Runs offline. Loads the Example A and Example B input/output pairs from
docs/methodology_v3.md and asserts the validator accepts each output and that
the code-side mechanical verdict never exceeds the rules ceiling (A4)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from rotd.auditor import mechanical_verdict, validate_report
from rotd.config import Config

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "methodology_v3.md"


def _json_blocks(text: str) -> list[dict]:
    out = []
    for body in re.findall(r"```json\n(.*?)\n```", text, flags=re.S):
        try:
            out.append(json.loads(body))
        except ValueError:
            pass
    return out


def main() -> None:
    cfg = Config(path=ROOT / "config.env")
    blocks = _json_blocks(DOC.read_text(encoding="utf-8"))
    bundles = {b["meta"]["ticker"]: b for b in blocks
               if isinstance(b.get("meta"), dict) and "ticker" in b["meta"]}
    reports = {r["ticker"]: r for r in blocks
               if r.get("schema") == "decision_v3" and "ticker" in r}
    print("bundles:", sorted(bundles), "| reports:", sorted(reports))
    ok_all = True
    for ticker, rep_out in reports.items():
        bundle = bundles.get(ticker)
        if not bundle:
            print(f"{ticker}: NO BUNDLE"); ok_all = False; continue
        rep, failures = validate_report(rep_out, bundle, cfg)
        mech, caps = mechanical_verdict(
            rep_out["rubric"], bundle["gate"]["passed"],
            rep_out["supply_state"], rep_out["cause"]["kind"])
        valid = rep is not None
        ok_all = ok_all and valid
        print(f"{ticker}: valid={valid} verdict={rep_out['verdict']} "
              f"code_verdict={mech} caps={caps} failures={failures[:3]}")
    print("ALL VALID" if ok_all else "SOME INVALID")


if __name__ == "__main__":
    main()
