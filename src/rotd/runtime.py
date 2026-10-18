"""The runtime: one cycle that produces one snapshot and one set of decisions.

This is the only place that fetches. A cycle:

1. determines the mode and trading day (``calendar_us``);
2. fetches the universe (Finviz) and, for the names it tracks, a fresher
   quote (keyless TradingView scanner, then Alpaca/Finnhub) — all raw fetches
   happen here, once;
3. reconciles them into **one** snapshot (``normalize``);
4. computes features, runs gates, scores and ranks (``features``/``gates``/``scoring``);
5. advances the state machine and spends the alert budget (``state_machine``);
6. renders and sends (or shadow-logs) messages (``notifier``);
7. writes the snapshot, candidates, decisions and alerts (``store``).

Nothing downstream of step 3 fetches. The cycle is deterministic given its stored
inputs (AC1/S5), so ``rotd replay`` can re-run it byte-for-byte.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
from .util import as_utc, fmt_ts, now_utc, sha256_hex

log = logging.getLogger(__name__)


def _compact_gates(rep: GateReport) -> dict[str, Any]:
    """Compact gate record for non-board rows (item 6): failures only, no detail.

    The full per-gate JSON for 3,000 symbols per tick is what grew the DB to
    93 GB. Failing gates are the only ones that carry information for a name that
    did not pass, and their detail strings are the bulk of the payload.
    """
    return {r.gate: {"pass": r.passed, "reason": r.reason}
            for r in rep.results if not r.passed} or {"pass": True}


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
        self._vol_history: dict[str, deque] = {}   # (now, cumulative volume) per symbol
        self._catalyst_items: dict[str, list[dict[str, Any]]] = {}
        self._first_seen: dict[str, datetime] = {}
        self._last_touch: dict[str, datetime] = {}
        self._sec_tickers: Optional[dict[str, dict[str, Any]]] = None
        self._universe: set[str] = set()
        self._data_grades: list[str] = []
        self._fresh_cursor = 0  # rotates the Finnhub freshness quote across the pool

    def _ensure_day(self, day: date) -> None:
        if self._day != day:
            self._day = day
            self.sm = StateMachine(self.cfg, day)
            self._history.clear()
            self._vol_history.clear()
            self._catalyst_items.clear()
            self._first_seen.clear()
            self._last_touch.clear()
            self._data_grades = []
            self._warm_boot(day)

    # -- warm boot (item 7) -------------------------------------------------
    def _warm_boot(self, day: date) -> None:
        """Rehydrate the per-day state a restart would otherwise lose.

        - ``_vol_history`` from the last ``VOL_HISTORY_RESTORE_MIN`` of snapshots
          (each snapshot carries the tracked pool's cumulative volumes), so the
          hard live-volume gate is not blind for its first 3 minutes after a
          restart;
        - BOARD/ARMED states from today's decisions;
        - the alert budget already spent today from ``alerts``.
        """
        if self.store is None or self.sm is None:
            return
        mins = self.cfg.int("VOL_HISTORY_RESTORE_MIN", 15)
        cutoff = (now_utc() - timedelta(minutes=mins)).isoformat()
        try:
            rows = self.store.query(
                "SELECT cycle_ts, quality_json FROM snapshots "
                "WHERE trading_day=? AND cycle_ts>=? ORDER BY cycle_ts",
                (day.isoformat(), cutoff))
        except Exception as exc:  # noqa: BLE001 - a corrupt row must not block a cycle
            log.warning("warm boot: snapshot history unavailable: %s", exc)
            rows = []
        for row in rows:
            try:
                q = json.loads(row["quality_json"] or "{}")
            except ValueError:
                continue
            ts = datetime.fromisoformat(str(row["cycle_ts"]).replace("Z", "+00:00"))
            for sym, vol in (q.get("volumes") or {}).items():
                if vol is None:
                    continue
                self._vol_history.setdefault(sym, deque(maxlen=60)).append(
                    (as_utc(ts), float(vol), q.get("prices", {}).get(sym)))
        # Derive the deltas/VWAP the live cycle would have computed, so the hard
        # volume gate is armed on the very first cycle after a restart.
        for sym, dq in self._vol_history.items():
            if not dq:
                continue
            hist = self._history.setdefault(sym, {})
            last_ts = dq[-1][0]
            v3 = self._delta_volume(dq, last_ts, 180.0)
            v10 = self._delta_volume(dq, last_ts, 600.0)
            if v3 is not None:
                hist["vol_3m"] = v3
            if v10 is not None and dq[-1][2]:
                hist["dollar_volume_10m"] = v10 * dq[-1][2]
            vwap = self._rolling_vwap(dq)
            if vwap is not None:
                hist["vwap"] = vwap
        try:
            self._restore_states(day)
            self._restore_budget(day)
        except Exception as exc:  # noqa: BLE001
            log.warning("warm boot: state restore failed: %s", exc)

    def _restore_states(self, day: date) -> None:
        # The *latest* decision per symbol wins: a name that went BOARD -> ARMED
        # -> ABORT must not be restored into ARMED just because it was armed once.
        rows = self.store.query(
            "SELECT symbol, new_state, cycle_ts FROM decisions d "
            "WHERE trading_day=? AND id = ("
            "  SELECT MAX(id) FROM decisions x "
            "  WHERE x.trading_day=d.trading_day AND x.symbol=d.symbol) "
            "AND new_state IN ('BOARD','ARMED')", (day.isoformat(),))
        for r in rows:
            st = self.sm._st(r["symbol"])
            st.state = State(r["new_state"])
            st.last_seen = as_utc(datetime.fromisoformat(
                str(r["cycle_ts"]).replace("Z", "+00:00")))
            if st.state == State.ARMED:
                # The armed level is not persisted; a restored ARMED name simply
                # re-arms (level + fresh confirmation) on the next qualifying cycle.
                st.confirm_bars = 0

    def _restore_budget(self, day: date) -> None:
        rows = self.store.alerts_for_day(day, kind="IGNITION")
        b = self.sm.budget
        for r in rows:
            sym = r["symbol"]
            slot = r["budget_slot"] or ""
            b.ignited_symbols.add(sym)
            b.spent.append({"symbol": sym, "slot": slot, "score": r["score"],
                            "at": r["cycle_ts"]})
            if slot == "RESERVE":
                b.reserve_spent += 1
            b.last_ignition_at = as_utc(
                datetime.fromisoformat(str(r["cycle_ts"]).replace("Z", "+00:00")))

    # -- fetch (the only network in a cycle) --------------------------------
    def _fetch_universe(self, mode: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        fv = self.clients.get("finviz")
        if fv is None:
            return [], {"finviz": {"ok": False, "error": "no client"}}
        try:
            raw = fv.universe()
            rows = [fv.parse_row(r) for r in raw]
            # Scope: market-cap ceiling (MAX_MARKET_CAP) when set, else the legacy
            # price ceiling (MAX_PRICE). A name without a price is never usable.
            rows = [r for r in rows if r.get("price") is not None]
            max_mcap = self.cfg.float("MAX_MARKET_CAP", 0.0)
            if max_mcap > 0:
                rows = [r for r in rows if r.get("market_cap") is not None
                        and r["market_cap"] <= max_mcap]
            else:
                cap = self.cfg.float("MAX_PRICE", 10.0)
                rows = [r for r in rows if r["price"] <= cap]
            return rows, {"finviz": {"ok": True, "n": len(rows)}}
        except Exception as exc:  # noqa: BLE001
            log.warning("universe fetch failed: %s", exc)
            return [], {"finviz": {"ok": False, "error": str(exc)[:120]}}

    def _fresh_points(self, symbols: list[str], now: datetime,
                      reference: Optional[dict[str, float]] = None,
                      mode: str = "") -> dict[str, list[PricePoint]]:
        """Fresher quotes for tracked symbols, if a feed is available.

        1. One keyless TradingView batch POST covers the whole pool (no
           free-tier throttling), regular session only, and every price is kept
           only while it stays within ``TRADINGVIEW_MAX_DEVIATION_PCT`` of the
           reconciled Finviz price — an unverified feed must not enter the
           decision path on its own.
        2. Alpaca batched quotes give a real bid/ask (the spread the Finviz
           export does not carry) for up to 40 symbols in one round trip.
        3. Finnhub quote as the last-resort freshness price, paced by its own
           client rate limit (one fresh call per cycle).
        Any throttle/429/cooldown falls back silently to the Finviz price.
        """
        tv = self.clients.get("tradingview")
        ref = reference or {}
        out: dict[str, list[PricePoint]] = {}
        pool = symbols[:self.cfg.int("BOARD_SIZE", 5) * 4]
        if not pool:
            return out

        if tv is not None and self.cfg.bool("TRADINGVIEW_ENABLED", True):
            try:
                batch = tv.fetch_batch_metrics(pool)
            except Exception as exc:  # noqa: BLE001 - fail soft to Alpaca/Finnhub
                log.debug("tradingview batch unavailable: %s", exc)
                batch = {}
            if batch:
                # Scanner "close" is regular-session truth; extended hours stay
                # with Finviz (the only verified extended-hours source).
                if mode in ("OPEN", "MIDDAY"):
                    cap = float(self.cfg.float("TRADINGVIEW_MAX_DEVIATION_PCT", 20))
                    for sym, m in batch.items():
                        px = m.get("close")
                        if px is None:
                            continue
                        base = ref.get(sym)
                        if base and abs(px - base) / base * 100.0 > cap:
                            log.debug("tradingview: dropping %s (%.3f vs finviz %.3f)",
                                      sym, px, base)
                            continue
                        out[sym] = [PricePoint(source="tradingview", price=px, ts=now,
                                               scope="UNKNOWN", volume=m.get("volume"))]
                if out:
                    return out  # one batch covers the pool

        al = self.clients.get("alpaca")
        if al is not None and self.cfg.raw("ALPACA_KEY") and mode in ("OPEN", "MIDDAY"):
            try:
                quotes = al.latest_quotes(pool)
            except Exception as exc:  # noqa: BLE001 - BLOCKED key fails soft
                log.debug("alpaca quotes unavailable: %s", exc)
                quotes = {}
            for sym, q in quotes.items():
                bid, ask = q.get("bid"), q.get("ask")
                if not bid or not ask:
                    continue
                out[sym] = [PricePoint(source="alpaca", price=(bid + ask) / 2.0,
                                       ts=q.get("ts") or now, scope="SINGLE_VENUE",
                                       bid=bid, ask=ask)]
            if out:
                return out

        fh = self.clients.get("finnhub")
        if fh is None or not self.cfg.raw("FINNHUB_KEY"):
            return out
        start = self._fresh_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._fresh_cursor += 1
        for sym in ordered:
            before = fh.api_calls
            try:
                q = fh.quote(sym)  # cache hit is free; a fresh fetch is paced
            except SourceError as exc:
                msg = str(exc)
                if "throttled" in msg or "429" in msg or "cooldown" in msg:
                    break  # budget spent this cycle; the rotation resumes next cycle
                continue  # a missing quote for one symbol is fine
            except Exception:  # noqa: BLE001
                continue
            if not q.get("price"):
                continue
            out[sym] = [PricePoint(source="finnhub", price=q.get("price"),
                                   ts=q.get("ts") or now,
                                   scope="CONSOLIDATED", volume=q.get("volume"),
                                   prev_close=q.get("prev_close"))]
            # One fresh (uncached) fetch per cycle is enough to respect the budget.
            if fh.api_calls > before:
                break
        return out


    # -- batched quotes (item 9) --------------------------------------------
    def _apply_quotes(self, snap: Snapshot, tracked: list[str], now: datetime) -> None:
        """Fill bid/ask/spread for the tracked pool from one Alpaca batch call.

        Finviz has no book, so ``spread_pct`` (and therefore G-SPREAD and the
        spread penalty) has been MISSING. One ``/v2/stocks/quotes/latest`` call
        covers up to 40 symbols. Fail soft: a blocked/rejected key leaves the
        records exactly as reconciled.

        A quote older than the mode's ``QUOTE_MAX_AGE_S_*`` is skipped (DATA-03):
        the IEX book outside regular hours can be hours old and 10% wide, and a
        stale spread is worse than no spread.
        """
        al = self.clients.get("alpaca")
        if al is None or not self.cfg.raw("ALPACA_KEY"):
            return
        try:
            quotes = al.latest_quotes(tracked)
        except Exception as exc:  # noqa: BLE001 - optional feed, never a cycle error
            log.debug("alpaca quotes unavailable: %s", exc)
            return
        max_age = (self.cfg.float("QUOTE_MAX_AGE_S_REG", 20)
                   if snap.mode in ("OPEN", "MIDDAY")
                   else self.cfg.float("QUOTE_MAX_AGE_S_EXT", 60))
        for sym, q in quotes.items():
            rec = snap.records.get(sym)
            bid, ask = q.get("bid"), q.get("ask")
            if rec is None or not bid or not ask or ask < bid:
                continue
            ts = q.get("ts")
            if ts is not None and (now - as_utc(ts)).total_seconds() > max_age:
                log.debug("alpaca: dropping stale quote %s (age %.0fs > %.0fs)",
                          sym, (now - as_utc(ts)).total_seconds(), max_age)
                continue
            rec.bid, rec.ask = bid, ask
            mid = (bid + ask) / 2.0
            if mid > 0:
                rec.spread_pct = (ask - bid) / mid * 100.0

    # -- short-term volume velocity (Upgrade 1) --------------------------------
    @staticmethod
    def _delta_volume(dq: deque, now: datetime, seconds: float) -> Optional[float]:
        """Cumulative volume now minus the sample ~``seconds`` ago (None if too young)."""
        if not dq:
            return None
        cutoff = now - timedelta(seconds=seconds)
        base: Optional[float] = None
        for sample in dq:  # deque is ordered oldest->newest; (ts, cumulative volume[, price])
            ts, vol = sample[0], sample[1]
            if ts <= cutoff:
                base = vol
            else:
                break
        if base is None:
            return None  # no sample old enough yet
        cur = dq[-1][1]
        return max(0.0, cur - base)

    def _update_vol_history(self, snap: Snapshot, now: datetime) -> None:
        """Roll per-symbol cumulative volume and derive 3m/10m deltas + session VWAP.

        Samples are ``(ts, cumulative_volume, price)``; the deltas feed the hard
        live-volume gate and the rolling session VWAP feeds ``vwap_dist`` and the
        ABORT-below-VWAP check. Both are per-session facts rebuilt from this deque,
        never from whole-day aggregates.
        """
        for sym, rec in snap.records.items():
            if rec.volume is None:
                continue
            dq = self._vol_history.setdefault(sym, deque(maxlen=60))
            dq.append((now, rec.volume, rec.price))
            hist = self._history.setdefault(sym, {})
            vol_3m = self._delta_volume(dq, now, 180.0)
            vol_10m = self._delta_volume(dq, now, 600.0)
            if vol_3m is not None:
                hist["vol_3m"] = vol_3m
                rec.vol_3m = vol_3m
            if vol_10m is not None and rec.price:
                hist["dollar_volume_10m"] = vol_10m * rec.price
                rec.dollar_volume_10m = vol_10m * rec.price
            vwap = self._rolling_vwap(dq)
            if vwap is not None:
                hist["vwap"] = vwap
                rec.vwap = vwap

    @staticmethod
    def _rolling_vwap(dq: deque) -> Optional[float]:
        """Session VWAP from cumulative-volume deltas (None until 2 samples)."""
        pv = 0.0
        vol = 0.0
        prev_vol: Optional[float] = None
        for sample in dq:
            if len(sample) < 3 or sample[2] is None:
                continue
            _, cum, price = sample
            if prev_vol is not None:
                dv = cum - prev_vol
                if dv > 0:
                    pv += dv * price
                    vol += dv
            prev_vol = cum
        return (pv / vol) if vol > 0 else None

    def _live_volume(self, syms) -> dict[str, dict[str, Optional[float]]]:
        """Per-symbol live tape for the hard volume gate."""
        out: dict[str, dict[str, Optional[float]]] = {}
        for sym in syms:
            h = self._history.get(sym) or {}
            out[sym] = {"vol_3m": h.get("vol_3m"),
                        "dvol_10m": h.get("dollar_volume_10m")}
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
        reference = {(r.get("symbol") or "").upper(): r.get("price") for r in rows
                     if r.get("price")}
        fresh = self._fresh_points(tracked, now, reference=reference, mode=mode)

        snap = snapshot_from_finviz_rows(rows, self.cfg, mode, day,
                                         extra_points=fresh, source_health=health,
                                         cycle_ts=now)
        snap.quality["half_day"] = _is_half(mode, day)
        self._universe = set(snap.records)
        self._data_grades.append(snap.data_grade)
        self._apply_quotes(snap, tracked, now)

        # Short-term volume velocity (Upgrade 1): roll the cumulative volume per
        # symbol and derive 3m/10m deltas so the scorer can reward a stock that is
        # trading RIGHT NOW, not one that spiked at the open and went dead.
        self._update_vol_history(snap, now)

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
            gates_by[sym] = rep
            features_by[sym] = fs
            if rep.passed:
                sc = score_candidate(rec, fs, rep, self.cfg, mode)
                scored.append(sc)
                scored_by[sym] = sc
            else:
                gate_failures[sym] = rep.summary()

        rk = rank(scored, self.cfg)
        # Persist candidates only after scoring/ranking so the DB reflects the
        # actual score and rank for passed candidates. Gated-out candidates remain
        # recorded with NULL score/rank and their first failing gate reason.
        # Item 6: full gate/feature JSON only for the top ``CAND_JSON_TOP_N`` names;
        # everything else stores a compact reason, because per-tick JSON for 3,000
        # symbols is what grew the DB to 93 GB.
        top_n = self.cfg.int("CAND_JSON_TOP_N", 50)
        for sym, rep in gates_by.items():
            sc = scored_by.get(sym)
            ranked = sc is not None and sc.rank is not None and sc.rank <= top_n
            keep_full = bool(rep.passed or ranked)
            self.store.save_candidate(
                day, mode, now, sym,
                round(sc.score, 2) if sc else None,
                sc.rank if sc else None,
                rep.passed,
                rep.as_dict() if keep_full else _compact_gates(rep),
                features_by[sym].as_dict() if keep_full else {},
                "" if rep.passed else (rep.first_failure.reason if rep.first_failure else ""))

        # methodology_v3 audit (§10): off the decision path, never gates an alert.
        audit = self._audit_candidates(day, mode, now, rk, snap, scored_by,
                                       gates_by, features_by, async_mode=True)

        # state machine
        prices = {s: snap.records[s].price for s in snap.records
                  if snap.records[s].price is not None}
        live = self._live_volume(snap.records)
        transitions = self.sm.step(rk, snap.records, mode, now=now, prices=prices,
                                   data_grade=snap.data_grade, live=live)

        sent = self._dispatch(day, mode, now, rk, snap, transitions)

        # shadow clock (§17 Phase 6): count the session and log (never push) alerts
        n_ign = sum(1 for s in sent if s.get("kind") == "IGNITION")
        shadow.note_cycle(self.store, day, alerts=n_ign)

        # tracking/grading (§12): feed this cycle's price into every live tracker so
        # its checkpoints (+30% before -12%) resolve as the day unfolds.
        self._observe_trackers(snap, now)

        # persist snapshot + decisions. The snapshot carries the tracked pool's
        # cumulative volumes and prices so a restart can rehydrate the rolling
        # volume history (item 7); the pool is bounded, so this stays small.
        snap.quality["volumes"] = {sym: rec.volume for sym, rec in snap.records.items()
                                   if sym in self._vol_history and rec.volume is not None}
        snap.quality["prices"] = {sym: rec.price for sym, rec in snap.records.items()
                                  if sym in self._vol_history and rec.price is not None}
        self.store.save_snapshot(day, mode, now, snap.data_grade, snap.quality,
                                 snap.payload_hash, len(snap.records))
        # Item 6: log only transitions that carry information (a state change or a
        # BOARD/ARMED/IGNITION row); per-symbol per-tick holds are what bloated the DB.
        keep = (State.BOARD, State.ARMED, State.IGNITION)
        for t in transitions:
            if t.prev_state == t.new_state and t.new_state not in keep:
                continue
            self.store.log_decision(day, mode, now, t.symbol, t.prev_state.value,
                                    t.new_state.value, t.reason, t.detail)
        # update history for the next cycle
        for s in scored:
            rec = snap.records.get(s.symbol)
            if rec and rec.price is not None:
                self._history.setdefault(s.symbol, {})["prev_price"] = rec.price
                self._first_seen.setdefault(s.symbol, now)
        self._evict_stale(now)

        return CycleResult(snapshot=snap, ranking=rk, transitions=transitions,
                           gate_failures=gate_failures, sent=sent, audit=audit)

    # -- bounded memory (item 10) -------------------------------------------
    def _evict_stale(self, now: datetime) -> None:
        """Drop per-symbol state for names unseen in the active universe > MEMORY_TTL_S.

        The universe is ~3,000 names; without eviction the feature/volume/catalyst
        dictionaries grow for the life of the daemon.
        """
        ttl = self.cfg.float("MEMORY_TTL_S", 7200)
        if ttl <= 0:
            return
        cutoff = now - timedelta(seconds=ttl)
        tracked = set(self._history) | set(self._vol_history) | set(self._catalyst_items)
        for sym in tracked:
            if sym in self._universe:
                continue
            last = self._last_touch.get(sym)
            # A symbol never seen in the universe has no touch record: its history
            # came from warm boot and is bounded by the restore window, so keep it
            # until the TTL measured from boot.
            if last is not None and last < cutoff:
                self._history.pop(sym, None)
                self._vol_history.pop(sym, None)
                self._catalyst_items.pop(sym, None)
                self._first_seen.pop(sym, None)
                self._last_touch.pop(sym, None)
        for sym in self._universe:
            self._last_touch[sym] = now

    def _catalyst_score(self, symbol: str) -> Optional[float]:
        if not self.catalyst:
            return None
        # No model configured (or rules-only fallback): there is no catalyst
        # verdict, so the score must be MISSING (None), never a dummy number.
        # A present dummy would stop scoring.py's active-weight normalization and
        # silently cap every otherwise-complete score (Bug Fix 2).
        if not self.catalyst.configured():
            return None
        items = getattr(self, "_catalyst_items", {}).get(symbol, [])
        # No fresh news/filing inside AI_CAUSE_WINDOW_H: nothing to audit, so the
        # catalyst component is MISSING (None) and the other four scale to 0..100.
        if not items:
            return None
        return self.catalyst.best_score(symbol, items)

    def _fetch_catalyst_items(self, symbols: list[str], now: datetime) -> None:
        """Fetch fresh news/headlines + SEC filings for the audit candidates.

        Populates ``self._catalyst_items[symbol]`` with items inside
        ``AI_CAUSE_WINDOW_H`` (Feature Wiring 4). Runs once per day per symbol
        (the per-day cache is cleared by ``_ensure_day``) so the Finviz rate
        budget is respected. Fail soft: any source error leaves the symbol with
        no items — the auditor sees an empty bundle, never a crash.
        """
        fv = self.clients.get("finviz")
        sec = self.clients.get("sec")
        if fv is None and sec is None:
            return
        window_h = self.cfg.float("AI_CAUSE_WINDOW_H", 4)
        for sym in symbols:
            if sym in self._catalyst_items:
                continue  # already fetched this trading day
            items: list[dict[str, Any]] = []
            if fv is not None:
                try:
                    for row in fv.news(sym):
                        item = self._news_item(row, now, window_h)
                        if item:
                            items.append(item)
                except Exception as exc:  # noqa: BLE001 - a feed miss is not a crash
                    log.debug("catalyst news fetch failed for %s: %s", sym, exc)
                try:
                    for row in fv.latest_filings(sym):
                        item = self._filing_item(row, now, window_h)
                        if item:
                            items.append(item)
                except Exception as exc:  # noqa: BLE001
                    log.debug("catalyst filings fetch failed for %s: %s", sym, exc)
            # SEC EDGAR as the filings fallback: Finviz's latest-filings export is
            # often empty for micro caps, and a missing 8-K is a missing catalyst.
            if not any(i.get("kind") == "filing" for i in items):
                items += self._sec_filings(sec, sym, now, window_h)
            self._catalyst_items[sym] = items

    def _sec_filings(self, sec, sym: str, now: datetime,
                     window_h: float) -> list[dict[str, Any]]:
        """Recent EDGAR filings for one symbol (CIK map cached once per process)."""
        if sec is None:
            return []
        try:
            if self._sec_tickers is None:
                self._sec_tickers = sec.company_tickers()
            cik = (self._sec_tickers.get(sym.upper()) or {}).get("cik")
            if not cik:
                return []
            rows = sec.submissions(int(cik)).get("filings") or []
        except Exception as exc:  # noqa: BLE001 - SEC is a best-effort fallback
            log.debug("sec filings fetch failed for %s: %s", sym, exc)
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            form = (row.get("form") or "").strip()
            age_min = _row_age_min(row.get("updated"), now)
            if age_min is None or age_min > window_h * 60.0:
                continue
            out.append({"kind": "filing", "id": (row.get("link") or form).strip(),
                        "form": form, "date": (row.get("updated") or "")[:10],
                        "items": None, "summary": (row.get("title") or "").strip()})
            if len(out) >= 5:
                break
        return out

    @staticmethod
    def _news_item(row: dict[str, Any], now: datetime, window_h: float) -> Optional[dict[str, Any]]:
        """One Finviz news row -> a BundleInputs news item (fresh only)."""
        title = (row.get("Title") or "").strip()
        if not title:
            return None
        age_min = _row_age_min(row.get("Date"), now)
        if age_min is not None and age_min > window_h * 60.0:
            return None  # outside the cause window: not a fresh catalyst
        item_id = (row.get("Url") or "").strip() or sha256_hex(title.encode("utf-8"))[:16]
        return {"kind": "news", "id": item_id, "outlet": (row.get("Source") or "").strip(),
                "age_text": _age_text(age_min), "age_class": "FRESH" if age_min is not None else "OLD",
                "headline": title, "text": title}

    @staticmethod
    def _filing_item(row: dict[str, Any], now: datetime, window_h: float) -> Optional[dict[str, Any]]:
        """One Finviz latest-filings row -> a BundleInputs filings item (fresh only).

        The latest-filings export columns are: Filing Date, Report Date, Form,
        Description, Filing (SEC index URL), Document.
        """
        form = (row.get("Form") or row.get("Form Type") or "").strip()
        title = (row.get("Description") or row.get("Title") or "").strip()
        if not form and not title:
            return None
        age_min = _row_age_min(row.get("Filing Date") or row.get("Date"), now)
        if age_min is not None and age_min > window_h * 60.0:
            return None
        item_id = (row.get("Filing") or row.get("URL") or "").strip() \
            or sha256_hex(f"{form}:{title}".encode("utf-8"))[:16]
        return {"kind": "filing", "id": item_id, "form": form,
                "date": (row.get("Filing Date") or row.get("Date") or "").strip(),
                "items": None, "summary": title}

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
        # Wire the fetched news/filings (Feature Wiring 4) into the fact bundle so
        # the methodology_v3 auditor receives real text when a model is configured.
        items = getattr(self, "_catalyst_items", {}).get(rec.symbol, [])
        news_items = [i for i in items if i.get("kind") == "news"]
        filing_items = [i for i in items if i.get("kind") == "filing"]
        return BundleInputs(
            symbol=rec.symbol, company=rec.company, exchange="",
            session=mode, time_et=fmt_ts(now),
            price=rec.price, pct_vs_close=rec.change_pct,
            rise_15m_pct=None, volx_15m=fs.value("rvol"),
            dollar_vol_15m=fs.value("dollar_volume_15m"), day_volume=rec.volume,
            rotation_ratio=fs.value("float_rotation"), vwap_state=vwap_state,
            bid=rec.bid, ask=rec.ask, spread_pct=rec.spread_pct,
            float_shares=rec.shares_float, short_pct_float=rec.short_float,
            fuel_rating=_rating(fs.value("squeeze_flag"), rec.short_float),
            phase=phase, gate_passed=gate_passed,
            gate_blockers=[r.gate for r in rep.results if not r.passed],
            rules_verdict="ARMED" if gate_passed else "REJECT",
            levels=[],
            news={"status": "OK", "items": news_items},
            filings={"status": "OK", "items": filing_items},
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
        # Fetch fresh news + filings for the audit candidates (once per day per
        # symbol; the per-day cache is cleared by _ensure_day). Only when a model
        # is configured — without one the auditor is rules-only and needs no text.
        if self.catalyst.configured():
            self._fetch_catalyst_items(audit_syms, now)
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
                rows = [r for r in rows if r.get("price") is not None]
                max_mcap = self.cfg.float("MAX_MARKET_CAP", 0.0)
                if max_mcap > 0:
                    rows = [r for r in rows if r.get("market_cap") is not None
                            and r["market_cap"] <= max_mcap]
                else:
                    cap = self.cfg.float("MAX_PRICE", 10.0)
                    rows = [r for r in rows if r["price"] <= cap]
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


def _row_age_min(date_str: Optional[str], now: datetime) -> Optional[float]:
    """Minutes since a news/filing row's date string (None when unparseable).

    Handles ISO-8601 (SEC/Finviz export) and the quote-page format
    ``Jul-15-26 08:30AM`` (ET, 2-digit year) the per-ticker news feed uses.
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return max(0.0, (now - as_utc(dt)).total_seconds() / 60.0)
    except ValueError:
        pass
    m = re.match(
        r"([A-Za-z]{3})-(\d{1,2})-(\d{2})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])", s)
    if not m:
        # Finviz latest-filings date: M/D/YYYY (no time).
        m2 = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", s)
        if m2:
            from .util import ET
            try:
                mm2, dd2, yy2 = (int(x) for x in m2.groups())
                dt2 = datetime(yy2, mm2, dd2, 0, 0, tzinfo=ET)
            except (ValueError, TypeError):
                return None
            return max(0.0, (now - dt2).total_seconds() / 60.0)
        return None
    months = {name: i + 1 for i, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}
    mon, day, yr, hh, mm, ampm = m.groups()
    month = months.get(mon.title())
    if month is None:
        return None
    try:
        hour = int(hh) % 12 + (12 if ampm.lower() == "pm" else 0)
        from .util import ET
        dt = datetime(2000 + int(yr), month, int(day), hour, int(mm), tzinfo=ET)
    except (ValueError, TypeError):
        return None
    return max(0.0, (now - dt).total_seconds() / 60.0)


def _age_text(age_min: Optional[float]) -> str:
    if age_min is None:
        return "unknown age"
    if age_min < 60:
        return f"{int(age_min)}m ago"
    return f"{age_min / 60.0:.1f}h ago"


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
