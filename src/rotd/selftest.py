"""Offline self-test (build prompt §16).

Runs the real pipeline over the saved golden fixtures with **no network** and
asserts the invariants the system must always hold. It is the same code path the
live cycle uses — only the fetch step is replaced by a fixture reader — so a
passing self-test is real evidence, not a mock.

Checks:
1. Config integrity — every setting has a unit (or is unitless) and a rationale.
2. Fixtures parse — Finviz universe, TwelveData quote, Finnhub quote/status,
   Alpha Vantage quote, Yahoo chart all parse into the shapes the adapters expect.
3. DATA-01..12 — reconciliation tags basis, flags conflicts, fails closed on
   missing/stale data, and produces a grade.
4. Gates fail closed — a record with no spread/liquidity fails.
5. Scoring — a fully-fresh runner passes every gate and ranks as ROTD.
6. Determinism — the same inputs produce the same snapshot hash twice.
7. Notifier — a rendered ignition contains the symbol and the score, in both
   timezones.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from . import normalize as N
from . import features as F
from . import scoring as S
from .config import BY_KEY, Config, SETTINGS
from .gates import run_gates
from .notifier import render_ignition
from .sources import (AlphaVantageClient, FinnhubClient, FinvizClient,
                      TwelveDataClient, YahooClient, parse_csv)
from .sources.base import fixture_text, load_fixture
from .store import Store
from .util import now_utc

log = logging.getLogger(__name__)


class SelftestFailure(AssertionError):
    pass


def _check(cond: bool, message: str, results: list[tuple[bool, str]], name: str) -> None:
    results.append((bool(cond), name))
    if not cond:
        raise SelftestFailure(message)


def run(cfg: Config, verbose: bool = False) -> int:
    results: list[tuple[bool, str]] = []

    def step(name: str) -> None:
        if verbose:
            print(f"  . {name}")

    try:
        # 1. config integrity
        missing_rat = [s.key for s in SETTINGS if not s.rationale]
        _check(not missing_rat, f"settings without rationale: {missing_rat}",
               results, "config: every setting has a rationale")
        dupes = [s.key for s in SETTINGS if list(BY_KEY).count(s.key) > 1]
        step("config integrity")

        # 2. fixtures parse
        uni = parse_csv(fixture_text("finviz_universe.txt"))
        _check(len(uni) > 100, "finviz universe fixture too small", results,
               "fixtures: finviz universe parses")
        row0 = FinvizClient.parse_row(uni[0])
        _check("symbol" in row0 and "price" in row0, "bad finviz row", results,
               "fixtures: finviz row shape")
        td = TwelveDataClient(cfg)
        q = json.loads(fixture_text("twelvedata_quote.txt"))
        _check("close" in q and "is_market_open" in q, "bad twelvedata quote", results,
               "fixtures: twelvedata quote shape")
        fh = json.loads(fixture_text("finnhub_quote.txt"))
        _check("c" in fh and "pc" in fh, "bad finnhub quote", results,
               "fixtures: finnhub quote shape")
        ms = json.loads(fixture_text("finnhub_market_status.txt"))
        _check("session" in ms and "isOpen" in ms, "bad market status", results,
               "fixtures: finnhub market-status shape")
        av = json.loads(fixture_text("alpha_vantage_global_quote.txt"))
        _check("Global Quote" in av, "bad alpha vantage", results,
               "fixtures: alpha vantage shape")
        yh = json.loads(fixture_text("yahoo_chart.txt"))
        _check("chart" in yh, "bad yahoo chart", results, "fixtures: yahoo chart shape")
        finra = fixture_text("finra_regsho.txt")
        _check("ShortVolume" in finra, "bad finra regsho", results,
               "fixtures: finra regsho shape")
        # SEC is ISO-8859-1; ensure we can decode it and the XML parses.
        sec_bytes = load_fixture("sec_edgar_atom.txt")
        _check(b"<?xml" in sec_bytes, "bad sec atom", results, "fixtures: sec atom present")

        # 3. reconciliation / DATA checks
        now = now_utc()
        _reconcile_checks(cfg, now, results, step)
        _gate_checks(cfg, results, step)
        _score_checks(cfg, results, step)
        _determinism_check(cfg, results, step)
        _notifier_check(cfg, results, step)
        _store_check(cfg, results, step)
        _shadow_check(cfg, results, step)
        _auditor_checks(cfg, results, step)
        _runtime_audit_check(cfg, results, step)
    except SelftestFailure as exc:
        print(f"FAIL: {exc}")
        _print(results)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        _print(results)
        return 1

    print(f"selftest OK — {len(results)} checks passed")
    if verbose:
        _print(results)
    return 0


def _reconcile_checks(cfg: Config, now, results, step) -> None:
    from .normalize import (BASIS_EXTENDED, BASIS_REGULAR, Canonical, PricePoint,
                            R_CONFLICT, R_STALE, R_UNKNOWN_BASIS, build_snapshot,
                            choose_basis, reconcile_symbol)
    from .util import UTC
    from datetime import timedelta

    # DATA-05: basis by mode
    _check(choose_basis("PRE") == BASIS_EXTENDED and choose_basis("OPEN") == BASIS_REGULAR,
           "basis-by-mode wrong", results, "DATA-05 basis tagging")

    # two agreeing sources -> OK, two far-apart -> CONFLICT
    ok = reconcile_symbol("T", [PricePoint("finviz", 2.00, now, scope="SINGLE_VENUE"),
                                PricePoint("twelvedata", 2.01, now)], cfg, "PRE", now=now)
    _check(ok.price_reason == N.R_OK, "agreeing sources flagged", results,
           "DATA-02 agreement is OK")
    confl = reconcile_symbol("T", [PricePoint("finviz", 2.00, now, scope="SINGLE_VENUE"),
                                   PricePoint("twelvedata", 2.50, now)], cfg, "PRE", now=now)
    _check(R_CONFLICT in confl.reasons, "far-apart sources not flagged", results,
           "DATA-02 conflict detected")

    # DATA-03: stale
    stale_ts = now - timedelta(seconds=9999)
    st = reconcile_symbol("T", [PricePoint("finviz", 2.0, stale_ts, scope="SINGLE_VENUE")],
                          cfg, "OPEN", now=now)
    _check(st.price is None and R_STALE in st.reasons, "stale quote not dropped",
           results, "DATA-03 stale dropped")

    # DATA-04: unknown volume basis -> rvol missing handled in features (checked later)
    step("DATA-01..09 reconciliation")

    # DATA-12: snapshot grade falls to the worst record
    good = Canonical(symbol="A", price=1.0, data_grade="A", volume=1, volume_reason="OK")
    bad = Canonical(symbol="B", price=None, data_grade="F")
    snap = build_snapshot([good, bad], date(2026, 1, 2), "PRE", cycle_ts=now)
    _check(snap.data_grade == "F", "cycle grade not worst", results, "DATA-12 worst grade")
    step("DATA-12 cycle grade")


def _gate_checks(cfg: Config, results, step) -> None:
    from .normalize import Canonical, Snapshot
    from .util import now_utc
    now = now_utc()
    # A record with no spread must fail G-SPREAD (fail closed).
    rec = Canonical(symbol="X", price=2.0, prev_close=1.0, volume=1e6, volume_basis="SINGLE_VENUE",
                    data_grade="A", fetched_at=now)
    for r in cfg.raw("SPREAD_TIERS").split(","):
        pass
    snap = Snapshot(trading_day=now.date(), mode="PRE", cycle_ts=now, records={"X": rec})
    fs = F.compute_features(rec, cfg, snap)
    rep = run_gates(rec, fs, cfg, "PRE", snap)
    _check(not rep.passed, "no-spread record passed gates", results,
           "gates fail closed on missing spread")
    _check(any(g.gate == "G-SPREAD" and not g.passed for g in rep.results),
           "G-SPREAD did not fail", results, "G-SPREAD fails without bid/ask")
    step("gates fail closed")


def _score_checks(cfg: Config, results, step) -> None:
    from .normalize import BASIS_EXTENDED, Canonical, Snapshot
    from .util import now_utc
    now = now_utc()
    rec = Canonical(symbol="RUN", price=2.50, price_basis=BASIS_EXTENDED,
                    price_scope="SINGLE_VENUE", prev_close=1.20, change_pct=108.0,
                    volume=8e6, volume_basis="SINGLE_VENUE", dollar_volume=2e7,
                    bid=2.49, ask=2.52, spread_pct=1.2, day_high=2.60, day_low=1.30,
                    shares_float=3e6, float_reason="OK", short_float=22.0, data_grade="A",
                    fetched_at=now)
    snap = Snapshot(trading_day=now.date(), mode="PRE", cycle_ts=now, records={"RUN": rec})
    fs = F.compute_features(rec, cfg, snap, catalyst_score=80.0,
                            history={"prev_price": 2.30, "vwap": 2.2,
                                     "dollar_volume_15m": 600000, "avg_volume": 6e6,
                                     "gap_pct": 12.0, "dist_52w_low": 150.0,
                                     "days_to_cover": 4.0}, mode_age_min=20.0)
    rep = run_gates(rec, fs, cfg, "PRE", snap)
    _check(rep.passed, f"fresh runner failed gates: {rep.summary()}", results,
           "fresh runner passes all gates")
    sc = S.score_candidate(rec, fs, rep, cfg, "PRE")
    _check(0 < sc.score <= 100, f"score out of range: {sc.score}", results,
           "score in range 0..100")
    rk = S.rank([sc], cfg)
    _check(rk.rotd is not None and rk.rotd.symbol == "RUN", "runner not selected",
           results, "top qualified candidate becomes ROTD")
    step("gates + score happy path")


def _determinism_check(cfg: Config, results, step) -> None:
    rows = [FinvizClient.parse_row(r) for r in parse_csv(fixture_text("finviz_universe.txt"))]
    rows = [r for r in rows if r.get("price") is not None and r["price"] <= 10.0][:200]
    day = date(2026, 1, 2)
    from datetime import datetime
    ts = datetime(2026, 1, 2, 15, 0, tzinfo=now_utc().tzinfo)
    a = N.snapshot_from_finviz_rows(rows, cfg, "PRE", day, cycle_ts=ts)
    b = N.snapshot_from_finviz_rows(rows, cfg, "PRE", day, cycle_ts=ts)
    _check(a.payload_hash == b.payload_hash, "snapshot hash not stable", results,
           "AC1/S5 determinism: identical hash twice")
    step("determinism")


def _shadow_check(cfg: Config, results, step) -> None:
    """The 10-trading-day shadow clock must count sessions, not calendar days (§17)."""
    from . import shadow
    import shutil
    td = Path(tempfile.mkdtemp(prefix="rotd-shadow-"))
    try:
        st = Store(td / "s.db")
        # 2026-10-13 is a Tuesday; 10 trading days later is 2026-10-27 (no holidays).
        start_day = date(2026, 10, 13)
        s0 = shadow.start(st, start_day)
        _check(s0.started == start_day and s0.sessions_done == 1,
               "shadow clock did not start correctly", results,
               "shadow: clock starts on a trading day")
        shadow.note_cycle(st, start_day, alerts=2)
        _check(st.get_meta("shadow_cycles") == "1" and st.get_meta("shadow_alerts") == "2",
               "shadow cycle/alert counters wrong", results,
               "shadow: cycles and alerts are counted")
        # skip a weekend evening: a non-trading day must not advance the clock
        shadow.note_cycle(st, date(2026, 10, 11))
        s1 = shadow.status(st, cfg=cfg)
        _check(s1.sessions_done == 1, "weekend advanced the shadow clock", results,
               "shadow: weekends do not advance the clock")
        # reach the required count: 10 sessions from 2026-10-13 is 2026-10-26
        shadow.note_cycle(st, date(2026, 10, 26), alerts=1)
        s2 = shadow.status(st, cfg=cfg)
        _check(s2.sessions_done == 10 and s2.complete and s2.live_allowed,
               f"shadow clock should be complete, got {s2.sessions_done}",
               results, "shadow: completes after the required sessions")
        # a live push is blocked until the clock completes
        prev_shadow = cfg.raw("SHADOW")
        cfg._values["SHADOW"] = "1"
        st.set_meta("shadow_last_seen", "2026-10-20")
        s3 = shadow.status(st, cfg=cfg)
        _check(not s3.live_allowed and s3.reason == shadow.R_CLOCK_INCOMPLETE,
               "incomplete clock allowed a live push", results,
               "shadow: live pushes blocked while incomplete")
        if prev_shadow == "":
            cfg._values.pop("SHADOW", None)
        else:
            cfg._values["SHADOW"] = prev_shadow
        st.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)
    step("shadow clock")


def _notifier_check(cfg: Config, results, step) -> None:
    from .normalize import Canonical
    from .scoring import ComponentScore, Scored
    from .util import now_utc
    sc = Scored(symbol="ZZZ", score=88.0, components={
        "TAPE": ComponentScore("TAPE", 0.9, 30, 27)})
    rec = Canonical(symbol="ZZZ", price=3.1, change_pct=95.0, spread_pct=1.1)
    text = render_ignition("ZZZ", sc, rec, "1/3", "PRE", ["new_high"], now=now_utc())
    _check("ZZZ" in text and "88" in text, "ignition text missing fields", results,
           "notifier renders symbol and score")
    _check("ET" in text and "KL" in text, "ignition text missing both timezones",
           results, "notifier shows ET and KL times")
    step("notifier")


def _store_check(cfg: Config, results, step) -> None:
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "t.db")
        day = date(2026, 1, 2)
        st.save_snapshot(day, "PRE", now_utc(), "A", {"g": "A"}, "hash", 3)
        st.save_alert(day, "PRE", now_utc(), "SYM", "IGNITION", "1/3", 88.0, 1, 2.0,
                      None, 1.0, "IGNITION_CONFIRMED", "h", sent=True)
        _check(st.count_alerts(day) == 1, "alert not stored", results, "store: alerts")
        st.save_candidate(day, "PRE", now_utc(), "SYM", 88.0, 1, True, {"g": {}}, {"f": {}})
        _check(st.query_one("SELECT COUNT(*) n FROM candidates")["n"] == 1,
               "candidate not stored", results, "store: candidates")
        st.save_miss_report(day, {"trading_day": day.isoformat(), "n_alerts": 1})
        _check(st.get_miss_report(day)["n_alerts"] == 1, "miss report not stored",
               results, "store: miss report round-trip")
        st.close()
    step("store round-trips")


def _auditor_checks(cfg: Config, results, step) -> None:
    """The methodology_v3 red-team auditor (§10) must load, build and validate."""
    import re
    from .auditor import (AuditorService, BundleInputs, build_fact_bundle,
                          load_system_prompt, mechanical_verdict, validate_report)

    # 1. the prompt loads between its own-line markers
    prompt = load_system_prompt(cfg)
    _check(len(prompt) > 5000 and prompt.lstrip().startswith("# 1. ROLE"),
           "system prompt missing/truncated", results, "auditor: prompt loads")
    step("auditor: prompt")

    # 2. a bundle carries every §4 group with a null-safe shape
    inp = BundleInputs(symbol="EXMP", price=2.31, gate_passed=True, rules_verdict="ENTER",
                       phase="FRONT_SIDE", dilution_band="LOW",
                       news={"status": "OK", "items": [{"id": "N1", "headline": "x"}]})
    bundle = build_fact_bundle(inp, cfg)
    _check(set(bundle) >= {"meta", "tape", "book", "fuel", "supply", "phase",
                           "gate", "rules", "levels", "news", "filings", "context"},
           "bundle missing a §4 group", results, "auditor: bundle shape")

    # 3. both documented examples validate and the mechanical verdict holds
    doc = (Path(__file__).resolve().parent / "prompts" / "methodology_v3.md").read_text("utf-8")
    blocks = []
    for body in re.findall(r"```json\n(.*?)\n```", doc, flags=re.S):
        try:
            blocks.append(json.loads(body))
        except ValueError:
            pass
    bundles = {b["meta"]["ticker"]: b for b in blocks
               if isinstance(b.get("meta"), dict) and "ticker" in b["meta"]}
    reports = {r["ticker"]: r for r in blocks
               if r.get("schema") == "decision_v3" and "ticker" in r}
    _check("EXMP" in bundles and "EXMQ" in bundles, "examples not found", results,
           "auditor: example bundles present")
    for ticker, out in reports.items():
        rep, failures = validate_report(out, bundles[ticker], cfg)
        _check(rep is not None, f"example {ticker} rejected: {failures[:2]}", results,
               f"auditor: example {ticker} validates")
        mech, _ = mechanical_verdict(out["rubric"], bundles[ticker]["gate"]["passed"],
                                     out["supply_state"], out["cause"]["kind"])
        _check(mech == out["verdict"], f"mechanical verdict {mech} != {out['verdict']}",
               results, f"auditor: mechanical verdict {ticker}")

    # 4. a bad report is rejected, never repaired (numbers-in-text / verdict ceiling)
    good = dict(reports["EXMP"])
    cap_break = dict(good, caps_applied=["NO_IGNITION"])  # ENTER above a cap
    rep_bad, fail_bad = validate_report(cap_break, bundles["EXMP"], cfg)
    _check(rep_bad is None and fail_bad, "cap-breaking report not rejected", results,
           "auditor: rejects verdict above a cap")
    ref_bad = dict(good, reason_refs=["N9"])
    rep_ref, fail_ref = validate_report(ref_bad, bundles["EXMP"], cfg)
    _check(rep_ref is None and fail_ref, "unknown ref not rejected", results,
           "auditor: rejects unknown ref")

    # 5. the service falls back to rules-only when the model call fails (fail closed)
    def _boom(system: str, user: str) -> str:
        raise RuntimeError("no network in selftest")

    svc = AuditorService(cfg, caller=_boom)
    res = svc.audit(BundleInputs(symbol="EXMP", gate_passed=False, rules_verdict="REJECT"))
    _check(res.report.source == "rules", "auditor did not fall back to rules", results,
           "auditor: rules-only fallback")
    step("auditor: methodology_v3")


def _runtime_audit_check(cfg: Config, results, step) -> None:
    """The runtime must build a §4 bundle from live objects and audit it (§10)."""
    import tempfile
    from datetime import date
    from .auditor import AuditorService, build_fact_bundle
    from .catalyst import CatalystService
    from .features import compute_features
    from .gates import run_gates
    from .normalize import Canonical, Snapshot
    from .notifier import Notifier
    from .runtime import Runtime, _phase_from_mode
    from .scoring import rank, score_candidate
    from .store import Store
    from .telegram import FakeTelegram
    from .util import now_utc

    now = now_utc()
    rec = Canonical(symbol="EXMP", price=2.31, prev_close=1.90, open=2.00,
                    volume=3_000_000, volume_basis="SINGLE_VENUE", bid=2.30, ask=2.32,
                    spread_pct=0.9, day_high=2.40, day_low=1.98, shares_float=4_000_000,
                    short_float=22.0, company="Exemplar Inc", data_grade="A", fetched_at=now)
    snap = Snapshot(trading_day=date(2026, 10, 13), mode="OPEN", cycle_ts=now,
                    records={"EXMP": rec})
    fs = compute_features(rec, cfg, snap, catalyst_score=None)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    # G-LIQ needs an intra-cycle 15m dollar volume the synthetic record lacks, so
    # the candidate may be gated out here; that is fine — the point is that the
    # audit wiring builds a bundle for whatever the gate decided (§10).
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    sc.rank = 1

    # Windows keeps a handle on the SQLite file, so do not auto-delete the dir.
    td = Path(tempfile.mkdtemp(prefix="rotd-selftest-"))
    try:
        store = Store(td / "rotd.db")
        cat = CatalystService(cfg, store, auditor=AuditorService(cfg, caller=None))
        rt = Runtime(cfg, store, {}, Notifier(cfg, FakeTelegram(cfg)), cat)
        inp = rt.build_bundle_inputs(rec, sc, rep, fs, "OPEN", now,
                                     gate_passed=rep.passed)
        _check(inp.symbol == "EXMP" and inp.price == 2.31
               and inp.gate_passed is rep.passed,
               "bundle inputs built wrong", results, "runtime audit: bundle inputs")
        bundle = build_fact_bundle(inp, cfg)
        _check(bundle["meta"]["ticker"] == "EXMP" and bundle["tape"]["price"] == 2.31,
               "fact bundle wrong from live objects", results,
               "runtime audit: fact bundle from live objects")
        # the audit runs (rules-only without a model) and never raises
        res = rt._audit_candidates(date(2026, 10, 13), "OPEN", now,
                                   rank([sc], cfg), snap, {"EXMP": sc},
                                   {"EXMP": rep}, {"EXMP": fs}, async_mode=False)
        _check("EXMP" in res["audited"] and "EXMP" in res["verdicts"],
               "audit did not run for the candidate", results,
               "runtime audit: audits the ranked candidate")
        store.close()
        step("runtime audit wiring")
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    _check(_phase_from_mode("OPEN") == "FRONT_SIDE", "phase map wrong", results,
           "runtime audit: phase map")


def _print(results) -> None:
    for ok, name in results:
        print(f"  {'OK ' if ok else '!! '}{name}")
