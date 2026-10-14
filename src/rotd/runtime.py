"""The runtime: one cycle that produces one snapshot and one set of decisions.

This is the only place that fetches. A cycle:

1. determines the mode and trading day (``calendar_us``);
2. fetches the universe + quotes (Finviz) and, for the names it tracks, a fresher
   quote (TwelveData) — all raw fetches happen here, once;
3. reconciles them into **one** snapshot (``normalize``);
4. computes features, runs gates, scores and ranks (``features``/``gates``/``scoring``);
5. advances the state machine and spends the alert budget (``state_machine``);
6. renders and sends (or shadow-logs) messages (``notifier``);
7. writes the snapshot, candidates, decisions and alerts (``store``).

Nothing downstream of step 3 fetches. The cycle is deterministic given its stored
inputs (AC1/S5), so ``rotd replay`` can re-run it byte-for-byte.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from .auditor import BundleInputs
from .catalyst import CatalystService
from .config import Config
from .features import FeatureSet, compute_features
from .gates import GateReport, run_gates
from .normalize import (Canonical, PricePoint, Snapshot, snapshot_from_finviz_rows)
from .notifier import Notifier
from .scoring import Ranking, Scored, rank, score_candidate
from . import shadow
from .state_machine import StateMachine, State, Transition
from .store import Store
from .sources.base import SourceError
from .tracker import PathTracker, Runner, build_runner_list
from .util import as_utc, fmt_ts, now_utc

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    snapshot: Snapshot
    ranking: Ranking
    transitions: list[Transition] = field(default_factory=list)
    gate_failures: dict[str, str] = field(default_factory=dict)
    sent: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"snapshot": self.snapshot.as_dict(), "ranking": self.ranking.as_dict(),
                "n_transitions": len(self.transitions),
                "n_gate_failures": len(self.gate_failures),
                "n_sent": len(self.sent),
                "n_audited": len(self.audit.get("audited", []))}


class Runtime:
    """Holds the per-day state and runs cycles."""

    def __init__(self, cfg: Config, store: Store, clients: dict[str, Any],
                 notifier: Notifier, catalyst: Optional[CatalystService] = None):
        self.cfg = cfg
        self.store = store
        self.clients = clients
        self.notifier = notifier
        self.catalyst = catalyst or CatalystService(cfg, store)
        self.sm: Optional[StateMachine] = None
        self.trackers: dict[int, PathTracker] = {}
        self._day: Optional[date] = None
        self._history: dict[str, dict[str, Any]] = {}
        self._first_seen: dict[str, datetime] = {}
        self._universe: set[str] = set()
        self._data_grades: list[str] = []
        self._fresh_cursor = 0  # rotates the TwelveData freshness quote across the board

    def _ensure_day(self, day: date) -> None:
        if self._day != day:
            self._day = day
            self.sm = StateMachine(self.cfg, day)
            self._history.clear()
            self._first_seen.clear()
            self._data_grades = []

    # -- fetch (the only network in a cycle) --------------------------------
    def _fetch_universe(self, mode: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        fv = self.clients.get("finviz")
        if fv is None:
            return [], {"finviz": {"ok": False, "error": "no client"}}
        try:
            raw = fv.universe()
            rows = [fv.parse_row(r) for r in raw]
            # Keep only sub-$10 names (G-PRICE ceiling) with a price.
            cap = self.cfg.float("MAX_PRICE", 10.0)
            rows = [r for r in rows if r.get("price") is not None and r["price"] <= cap]
            return rows, {"finviz": {"ok": True, "n": len(rows)}}
        except Exception as exc:  # noqa: BLE001
            log.warning("universe fetch failed: %s", exc)
            return [], {"finviz": {"ok": False, "error": str(exc)[:120]}}

    def _fresh_points(self, symbols: list[str], now: datetime) -> dict[str, list[PricePoint]]:
        """A fresher quote for tracked symbols, if a feed is available.

        Rate-aware: at most the cached-or-one-new quote per cycle, rotating through the
        board so every name is refreshed over time. Failures (429, cooldown, local
        throttle) fall back silently to the Finviz price — logged once per cooldown.
        """
        td = self.clients.get("twelvedata")
        if td is None or not self.cfg.raw("TWELVEDATA_KEY"):
            return {}
        out: dict[str, list[PricePoint]] = {}
        pool = symbols[:self.cfg.int("BOARD_SIZE", 5) * 4]
        if not pool:
            return out
        start = self._fresh_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._fresh_cursor += 1
        for sym in ordered:
            before = td.api_calls
            try:
                q = td.quote(sym)  # cache hit is free; a fresh fetch is paced
            except SourceError as exc:
                msg = str(exc)
                if "throttled" in msg or "429" in msg or "cooldown" in msg:
                    break  # budget spent this cycle; the rotation resumes next cycle
                continue  # a missing quote for one symbol is fine
            except Exception:  # noqa: BLE001
                continue
            out[sym] = [PricePoint(source="twelvedata", price=q.get("price"),
                                   ts=q.get("ts") or now,
                                   scope="UNKNOWN", volume=q.get("volume"),
                                   prev_close=q.get("prev_close"))]
            # One fresh (uncached) fetch per cycle is enough to respect the budget.
            if td.api_calls > before:
                break
        return out


    # -- the cycle ----------------------------------------------------------
    def cycle(self, mode: str, day: date, now: Optional[datetime] = None,
              async_mode: bool = True) -> CycleResult:
        now = as_utc(now or now_utc())
        self._ensure_day(day)

        rows, health = self._fetch_universe(mode)
        # Names worth a fresh quote: the previous board plus any with big moves.
        movers = sorted(rows, key=lambda r: abs(r.get("change_pct") or 0), reverse=True)
        tracked = [r["symbol"] for r in movers[:40]]
        fresh = self._fresh_points(tracked, now)

        snap = snapshot_from_finviz_rows(rows, self.cfg, mode, day,
                                         extra_points=fresh, source_health=health,
                                         cycle_ts=now)
        snap.quality["half_day"] = _is_half(mode, day)
        self._universe = set(snap.records)
        self._data_grades.append(snap.data_grade)

        # features -> gates -> score -> rank
        scored: list[Scored] = []
        gate_failures: dict[str, str] = {}
        scored_by: dict[str, Scored] = {}
        gates_by: dict[str, GateReport] = {}
        features_by: dict[str, FeatureSet] = {}
        from .calendar_us import phase_age_minutes
        age = phase_age_minutes(now)
        for sym, rec in snap.records.items():
            if rec.price is None:
                continue
            fs = compute_features(rec, self.cfg, snap,
                                  catalyst_score=self._catalyst_score(sym),
                                  history=self._history.get(sym),
                                  mode_age_min=age)
            rep = run_gates(rec, fs, self.cfg, mode, snap)
            self.store.save_candidate(day, mode, now, sym,
                                      None, None, rep.passed, rep.as_dict(),
                                      fs.as_dict(),
                                      "" if rep.passed else (rep.first_failure.reason
                                                             if rep.first_failure else ""))
            gates_by[sym] = rep
            features_by[sym] = fs
            if rep.passed:
                sc = score_candidate(rec, fs, rep, self.cfg, mode)
                scored.append(sc)
                scored_by[sym] = sc
            else:
                gate_failures[sym] = rep.summary()

        rk = rank(scored, self.cfg)

        # methodology_v3 audit (§10): off the decision path, never gates an alert.
        audit = self._audit_candidates(day, mode, now, rk, snap, scored_by,
                                       gates_by, features_by, async_mode=True)

        # state machine
        prices = {s: snap.records[s].price for s in snap.records
                  if snap.records[s].price is not None}
        transitions = self.sm.step(rk, snap.records, mode, now=now, prices=prices,
                                   data_grade=snap.data_grade)

        sent = self._dispatch(day, mode, now, rk, snap, transitions)

        # shadow clock (§17 Phase 6): count the session and log (never push) alerts
        n_ign = sum(1 for s in sent if s.get("kind") == "IGNITION")
        shadow.note_cycle(self.store, day, alerts=n_ign)

        # tracking/grading (§12): feed this cycle's price into every live tracker so
        # its checkpoints (+30% before -12%) resolve as the day unfolds.
        self._observe_trackers(snap, now)

        # persist snapshot + decisions
        self.store.save_snapshot(day, mode, now, snap.data_grade, snap.quality,
                                 snap.payload_hash, len(snap.records))
        for t in transitions:
            self.store.log_decision(day, mode, now, t.symbol, t.prev_state.value,
                                    t.new_state.value, t.reason, t.detail)
        # update history for the next cycle
        for s in scored:
            rec = snap.records.get(s.symbol)
            if rec and rec.price is not None:
                self._history.setdefault(s.symbol, {})["prev_price"] = rec.price
                self._first_seen.setdefault(s.symbol, now)

        return CycleResult(snapshot=snap, ranking=rk, transitions=transitions,
                           gate_failures=gate_failures, sent=sent, audit=audit)

    def _catalyst_score(self, symbol: str) -> Optional[float]:
        if not self.catalyst:
            return None
        items = getattr(self, "_catalyst_items", {}).get(symbol, [])
        return self.catalyst.best_score(symbol, items)

    # -- tracking / grading (§12) -------------------------------------------
    def _observe_trackers(self, snap: Snapshot, now: datetime) -> None:
        """Feed a fresh price to every live tracker and persist resolved checkpoints."""
        if not self.trackers:
            return
        for tr in list(self.trackers.values()):
            rec = snap.records.get(tr.symbol)
            price = rec.price if rec else None
            if price is None:
                continue
            before = (tr.hit_gain_at, tr.hit_stop_at)
            tr.observe(price, now, self.cfg)
            # record the moment the +gain / -stop trip resolves the grade
            if tr.hit_gain_at and tr.hit_gain_at != before[0]:
                tr.mark("GAIN_HIT", price, now)
            if tr.hit_stop_at and tr.hit_stop_at != before[1]:
                tr.mark("STOP_HIT", price, now)
            tr.persist(self.store)

    # -- methodology_v3 audit (§10) -----------------------------------------
    def build_bundle_inputs(self, rec: Canonical, sc: Scored, rep: GateReport,
                            fs: FeatureSet, mode: str, now: datetime,
                            gate_passed: bool = True) -> BundleInputs:
        """Translate ROTD's internal decision objects into the §4 fact bundle.

        Code owns every number; the model only reads. Fields ROTD cannot yet
        compute are passed ``None`` so the auditor sees an explicit ``null``.
        """
        rank_no = sc.rank if sc.rank is not None else 999
        phase = _phase_from_mode(mode)
        vwap = fs.value("vwap_dist")
        vwap_state = None
        if vwap is not None:
            vwap_state = "ABOVE" if vwap > 0.5 else ("BELOW" if vwap < -0.5 else "AT")
        return BundleInputs(
            symbol=rec.symbol, company=rec.company, exchange="",
            session=mode, time_et=fmt_ts(now),
            price=rec.price, pct_vs_close=rec.change_pct,
            rise_15m_pct=None, volx_15m=fs.value("rvol"),
            dollar_vol_15m=fs.value("dollar_volume_15m"), day_volume=rec.volume,
            rotation_ratio=None, vwap_state=vwap_state,
            bid=rec.bid, ask=rec.ask, spread_pct=rec.spread_pct,
            float_shares=rec.shares_float, short_pct_float=rec.short_float,
            fuel_rating=_rating(fs.value("squeeze_flag"), rec.short_float),
            phase=phase, gate_passed=gate_passed,
            gate_blockers=[r.gate for r in rep.results if not r.passed],
            rules_verdict="ARMED" if gate_passed else "REJECT",
            levels=[], news={"status": "OK", "items": []},
            filings={"status": "OK", "items": []},
            context={"market": None, "sector_hot": None, "analogs": None,
                     "multi_timeframe": None, "rank": rank_no},
        )

    def _audit_candidates(self, day: date, mode: str, now: datetime, rk: Ranking,
                          snap: Snapshot, scored_by: dict[str, Scored],
                          gates_by: dict[str, GateReport],
                          features_by: dict[str, FeatureSet],
                          async_mode: bool) -> dict[str, Any]:
        """Audit the board-scale candidates (methodology_v3 §10).

        Off the decision path: in live mode the call runs on a background thread
        so a cycle never blocks. Without a model the result is the rules-only
        block, and nothing here changes a verdict (the state machine owns it).
        """
        top_n = self.cfg.int("CATALYST_TOP_N_EARLY", 10)
        audit_syms = [s.symbol for s in rk.day_ranked[:top_n]]
        out: dict[str, Any] = {"audited": [], "verdicts": {}}
        for sym in audit_syms:
            rec = snap.records.get(sym)
            sc = scored_by.get(sym)
            rep = gates_by.get(sym)
            fs = features_by.get(sym)
            if not (rec and sc and rep and fs):
                continue
            inp = self.build_bundle_inputs(rec, sc, rep, fs, mode, now,
                                           gate_passed=rep.passed)
            if async_mode:
                self.catalyst.audit_report_async(inp)
            else:
                res = self.catalyst.audit_report(inp)
                out["verdicts"][sym] = res.report.verdict
            out["audited"].append(sym)
        return out

    def _dispatch(self, day: date, mode: str, now: datetime, rk: Ranking,
                  snap: Snapshot, transitions: list[Transition]) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []
        for t in transitions:
            if t.new_state == State.IGNITION and t.spend:
                scored = next((s for s in rk.day_ranked if s.symbol == t.symbol), None)
                if not scored:
                    continue
                rec = snap.records.get(t.symbol)
                slot = self.sm.budget.slot_label() if self.sm else "-"
                flags = [t.reason]
                msg = self.notifier.ignition(t.symbol, scored, rec, slot, mode, flags, now)
                rec_id = self.store.save_alert(
                    day, mode, now, t.symbol, "IGNITION", slot, scored.score, scored.rank,
                    rec.price if rec else None, self._history.get(t.symbol, {}).get("vwap"),
                    rec.spread_pct if rec else None, t.reason,
                    str(hash(msg or ""))[:16], sent=bool(msg))
                if rec and rec.price:
                    self.trackers[rec_id] = PathTracker(
                        alert_id=rec_id, symbol=t.symbol, trading_day=day,
                        ref_price=rec.price, ref_ts=now)
                sent.append({"symbol": t.symbol, "kind": "IGNITION", "id": rec_id})
            elif t.new_state == State.ABORT:
                rec = snap.records.get(t.symbol)
                self.notifier.abort(t.symbol, t, rec, now)
                sent.append({"symbol": t.symbol, "kind": "ABORT"})
        return sent

    # -- end of day ---------------------------------------------------------
    def finalize_day(self, day: date) -> dict[str, Any]:
        """Build the runner list and the MISS REPORT for a completed day."""
        fv = self.clients.get("finviz")
        records: list[Canonical] = []
        if fv is not None:
            try:
                rows = [fv.parse_row(r) for r in fv.universe()]
                cap = self.cfg.float("MAX_PRICE", 10.0)
                rows = [r for r in rows if r.get("price") is not None and r["price"] <= cap]
                snap = snapshot_from_finviz_rows(rows, self.cfg, "POST", day)
                records = list(snap.records.values())
            except Exception as exc:  # noqa: BLE001
                log.warning("runner-list fetch failed: %s", exc)
        runners = build_runner_list(records, self.cfg, first_seen=self._first_seen,
                                    in_universe=self._universe)
        for r in runners:
            self.store.save_runner(day, r.symbol, r.prior_close, r.day_high, r.gain_pct,
                                   r.dollar_vol, r.illiquid, r.in_universe, r.first_seen)
        alerts = self.store.alerts_for_day(day, kind="IGNITION")
        from .tracker import build_miss_report
        report = build_miss_report(self.store, day, runners, alerts, self._data_grades,
                                   in_universe=self._universe, cfg=self.cfg)
        self.store.save_miss_report(day, report)
        self.notifier.miss_report(report)
        return report


def _is_half(mode: str, day: date) -> bool:
    try:
        from .util import is_half_day
        return is_half_day(day)
    except Exception:  # noqa: BLE001
        return False


def _phase_from_mode(mode: str) -> str:
    """Map the session mode to the methodology_v3 phase label."""
    return {"PRE": "PRE", "OPEN": "FRONT_SIDE", "MIDDAY": "MIDDAY",
            "POST": "POST", "OVERNIGHT": "CLOSED"}.get(mode, "UNKNOWN")


def _rating(squeeze: Optional[float], short_float: Optional[float]) -> Optional[str]:
    """STRONG/MODERATE/WEAK from the squeeze signal + short float."""
    sf = short_float if short_float is not None else 0.0
    strong = (squeeze or 0.0) >= 1.0 or sf >= 20.0
    if strong:
        return "STRONG"
    if sf >= 8.0:
        return "MODERATE"
    return "WEAK"
