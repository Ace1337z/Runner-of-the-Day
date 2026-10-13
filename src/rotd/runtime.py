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

from .catalyst import CatalystService
from .config import Config
from .features import compute_features
from .gates import run_gates
from .normalize import (Canonical, PricePoint, Snapshot, snapshot_from_finviz_rows)
from .notifier import Notifier
from .scoring import Ranking, Scored, rank, score_candidate
from .state_machine import StateMachine, State, Transition
from .store import Store
from .tracker import PathTracker, Runner, build_runner_list
from .util import as_utc, now_utc

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    snapshot: Snapshot
    ranking: Ranking
    transitions: list[Transition] = field(default_factory=list)
    gate_failures: dict[str, str] = field(default_factory=dict)
    sent: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"snapshot": self.snapshot.as_dict(), "ranking": self.ranking.as_dict(),
                "n_transitions": len(self.transitions),
                "n_gate_failures": len(self.gate_failures),
                "n_sent": len(self.sent)}


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
        """A fresher quote for tracked symbols, if a feed is available."""
        td = self.clients.get("twelvedata")
        if td is None or not self.cfg.raw("TWELVEDATA_KEY"):
            return {}
        out: dict[str, list[PricePoint]] = {}
        # Only the board-scale symbols need a fresher quote; keep the call count low.
        for sym in symbols[:self.cfg.int("BOARD_SIZE", 5) * 4]:
            try:
                q = td.quote(sym)
                out[sym] = [PricePoint(source="twelvedata", price=q.get("price"),
                                       ts=q.get("ts") or now,
                                       scope="UNKNOWN", volume=q.get("volume"),
                                       prev_close=q.get("prev_close"))]
            except Exception:  # noqa: BLE001 - a missing fresh quote is fine
                continue
        return out

    # -- the cycle ----------------------------------------------------------
    def cycle(self, mode: str, day: date, now: Optional[datetime] = None) -> CycleResult:
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
            if rep.passed:
                scored.append(score_candidate(rec, fs, rep, self.cfg, mode))
            else:
                gate_failures[sym] = rep.summary()

        rk = rank(scored, self.cfg)

        # state machine
        prices = {s: snap.records[s].price for s in snap.records
                  if snap.records[s].price is not None}
        transitions = self.sm.step(rk, snap.records, mode, now=now, prices=prices,
                                   data_grade=snap.data_grade)

        sent = self._dispatch(day, mode, now, rk, snap, transitions)

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
                           gate_failures=gate_failures, sent=sent)

    def _catalyst_score(self, symbol: str) -> Optional[float]:
        if not self.catalyst:
            return None
        items = getattr(self, "_catalyst_items", {}).get(symbol, [])
        return self.catalyst.best_score(symbol, items)

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
                                   in_universe=self._universe)
        self.store.save_miss_report(day, report)
        self.notifier.miss_report(report)
        return report


def _is_half(mode: str, day: date) -> bool:
    try:
        from .util import is_half_day
        return is_half_day(day)
    except Exception:  # noqa: BLE001
        return False
