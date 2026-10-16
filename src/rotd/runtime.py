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
        self._universe: set[str] = set()
        self._data_grades: list[str] = []
        self._fresh_cursor = 0  # rotates the TwelveData freshness quote across the board

    def _ensure_day(self, day: date) -> None:
        if self._day != day:
            self._day = day
            self.sm = StateMachine(self.cfg, day)
            self._history.clear()
            self._vol_history.clear()
            self._catalyst_items.clear()
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

        Preferred: one keyless TradingView batch POST covers the whole pool
        (no free-tier throttling), regular session only, and every price is kept
        only while it stays within ``TRADINGVIEW_MAX_DEVIATION_PCT`` of the
        reconciled Finviz price — an unverified feed must not enter the decision
        path on its own. Fallback: the paced, cached TwelveData quote. Any
        throttle/429/cooldown falls back silently to the Finviz price.
        """
        td = self.clients.get("twelvedata")
        tv = self.clients.get("tradingview")
        ref = reference or {}
        out: dict[str, list[PricePoint]] = {}
        pool = symbols[:self.cfg.int("BOARD_SIZE", 5) * 4]
        if not pool:
            return out

        if tv is not None and self.cfg.bool("TRADINGVIEW_ENABLED", True):
            try:
                batch = tv.fetch_batch_metrics(pool)
            except Exception as exc:  # noqa: BLE001 - fail soft to TwelveData
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
                    return out  # one batch covers the pool; skip TwelveData

        if td is None or not self.cfg.raw("TWELVEDATA_KEY"):
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


    # -- short-term volume velocity (Upgrade 1) --------------------------------
    @staticmethod
    def _delta_volume(dq: deque, now: datetime, seconds: float) -> Optional[float]:
        """Cumulative volume now minus the sample ~``seconds`` ago (None if too young)."""
        if not dq:
            return None
        cutoff = now - timedelta(seconds=seconds)
        base: Optional[tuple[datetime, float]] = None
        for ts, vol in dq:  # deque is ordered oldest->newest
            if ts <= cutoff:
                base = (ts, vol)
            else:
                break
        if base is None:
            return None  # no sample old enough yet
        cur = dq[-1][1]
        return max(0.0, cur - base[1])

    def _update_vol_history(self, snap: Snapshot, now: datetime) -> None:
        """Roll per-symbol cumulative volume and derive 3m/10m deltas into history."""
        for sym, rec in snap.records.items():
            if rec.volume is None:
                continue
            dq = self._vol_history.setdefault(sym, deque(maxlen=60))
            dq.append((now, rec.volume))
            hist = self._history.setdefault(sym, {})
            vol_3m = self._delta_volume(dq, now, 180.0)
            vol_10m = self._delta_volume(dq, now, 600.0)
            if vol_3m is not None:
                hist["vol_3m"] = vol_3m
            if vol_10m is not None and rec.price:
                hist["dollar_volume_10m"] = vol_10m * rec.price


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
        for sym, rep in gates_by.items():
            fs = features_by[sym]
            sc = scored_by.get(sym)
            self.store.save_candidate(
                day, mode, now, sym,
                round(sc.score, 2) if sc else None,
                sc.rank if sc else None,
                rep.passed, rep.as_dict(), fs.as_dict(),
                "" if rep.passed else (rep.first_failure.reason if rep.first_failure else ""))

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
        # No model configured (or rules-only fallback): there is no catalyst
        # verdict, so the score must be MISSING (None), never a dummy number.
        # A present dummy would stop scoring.py's active-weight normalization and
        # silently cap every otherwise-complete score (Bug Fix 2).
        if not self.catalyst.configured():
            return None
        items = getattr(self, "_catalyst_items", {}).get(symbol, [])
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
        if fv is None:
            return
        window_h = self.cfg.float("AI_CAUSE_WINDOW_H", 4)
        for sym in symbols:
            if sym in self._catalyst_items:
                continue  # already fetched this trading day
            items: list[dict[str, Any]] = []
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
            self._catalyst_items[sym] = items

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
            rotation_ratio=None, vwap_state=vwap_state,
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
