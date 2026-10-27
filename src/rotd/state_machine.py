"""The candidate state machine and the alert budget (build prompt §11).

States (§11.1), from lowest to highest commitment::

    WATCH -> QUALIFIED -> BOARD -> ARMED -> IGNITION

plus terminal/transient ``ABORT``, ``DEGRADED``, ``RECOVERED``, and
``HALTED_SUSPENDED``. Transitions use hysteresis so a name does not flap:

- QUALIFIED requires ``QUALIFY_CONSEC`` consecutive fully-gated cycles.
- BOARD entry needs the name in the top-``BOARD_SIZE``; exit needs it to fall
  below ``BOARD_EXIT_RANK`` and to have held BOARD at least ``BOARD_MIN_DWELL_S``.
- ARMED needs rank <= ``ARM_MAX_RANK`` and score >= ``ARM_MIN_SCORE``.
- IGNITION needs a confirmed bar cross and price within ``CHASE_CAP_PCT`` of the
  armed level (no chasing an extended move). **Fail closed:** while a name sits in
  ARMED waiting for confirmation, rank and score are re-validated every cycle; if
  either slips it steps back down (to BOARD, or QUALIFIED off the board) and its
  confirmation counter resets, so it can never ignite on stale strength.

Alert budget (§11.2): at most ``ALERT_BUDGET_PER_DAY`` IGNITION pushes per trading
day, plus ``RESERVE_SLOTS`` for a candidate scoring at/above
``EXCEPTIONAL_SCORE``. Two ignitions must be at least ``ALERT_MIN_GAP_MIN`` apart
(A3). BOARD/ABORT/DEGRADED/RECOVERED never count against the budget.

A4 (no double-alert): a name already ignited today cannot ignite again.
A6: BOARD messages are on a fixed silent schedule.

Every transition is returned as a :class:`Transition` with a machine reason code
so the store can log even the rejects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Optional

from .config import Config
from .gates import R_BACKSIDE_OF_HOD, backside_of_hod, live_volume_reason
from .scoring import Ranking, Scored
from .util import as_utc, now_utc

log = logging.getLogger(__name__)


class State(str, Enum):
    WATCH = "WATCH"
    QUALIFIED = "QUALIFIED"
    BOARD = "BOARD"
    ARMED = "ARMED"
    IGNITION = "IGNITION"
    ABORT = "ABORT"
    DEGRADED = "DEGRADED"
    RECOVERED = "RECOVERED"
    HALTED_SUSPENDED = "HALTED_SUSPENDED"


# Reason codes for every transition (explainability rule).
R_QUALIFY = "QUALIFY_CONSEC_MET"
R_QUALIFY_WAIT = "QUALIFY_CONSEC_PENDING"
R_BOARD_ENTER = "BOARD_ENTER"
R_BOARD_HOLD = "BOARD_HOLD_DWELL"
R_BOARD_EXIT = "BOARD_EXIT_RANK"
R_ARM = "ARM_RANK_AND_SCORE"
R_ARM_CHASE = "ARM_CHASE_CAP"
R_ARM_BELOW_VWAP = "IGNITION_BELOW_VWAP_TOLERANCE"
R_IGNITE_RED_DAY = "ABORT_RED_ON_DAY"
R_ARM_RANK = "ARM_RANK_TOO_LOW"
R_ARM_SCORE = "ARM_SCORE_TOO_LOW"
R_ARM_STRUCT = "ARM_STRUCTURE_TOO_LOW"
R_ARM_STEP_RANK = "ARM_STEP_DOWN_RANK"
R_ARM_STEP_SCORE = "ARM_STEP_DOWN_SCORE"
R_ARM_STEP_STRUCT = "ARM_STEP_DOWN_STRUCTURE"
R_ARM_STEP_VOL = "ARM_STEP_DOWN_VOL"
R_IGNITE = "IGNITION_CONFIRMED"
R_IGNITE_BUDGET = "IGNITION_BUDGET_EXHAUSTED"
R_IGNITE_DUP = "IGNITION_ALREADY_TODAY"
R_IGNITE_GAP = "IGNITION_MIN_GAP"
R_IGNITE_PHASE = "IGNITION_PHASE_CLOSED"
R_IGNITE_MIDDAY_TAPE = "IGNITION_MIDDAY_TAPE_TOO_LOW"
R_ABORT_VWAP = "ABORT_BELOW_VWAP"
R_ABORT_SPREAD = "ABORT_SPREAD_WIDE"
R_ABORT_DATA = "ABORT_DATA_QUALITY"
R_DEGRADE = "DEGRADED_DATA"
R_RECOVER = "RECOVERED_DATA"
R_SUSPEND = "HALTED_SUSPENDED"
R_DROP = "DROPPED_OUT_OF_GATES"


@dataclass
class Transition:
    symbol: str
    prev_state: State
    new_state: State
    reason: str
    detail: str = ""
    spend: bool = False          # does this transition consume an ignition slot?

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "from": self.prev_state.value,
                "to": self.new_state.value, "reason": self.reason,
                "detail": self.detail, "spend": self.spend}


@dataclass
class Budget:
    """Per-day ignition budget with a reserve for exceptional names (A4)."""
    day: date
    limit: int
    reserve: int
    spent: list[dict[str, Any]] = field(default_factory=list)
    reserve_spent: int = 0
    ignited_symbols: set[str] = field(default_factory=set)
    last_ignition_at: Optional[datetime] = None

    @property
    def used(self) -> int:
        return len(self.spent)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def can_spend(self, score: float, cfg: Config, symbol: str,
                  now: Optional[datetime] = None) -> tuple[bool, str]:
        sym = symbol.upper()
        if sym in self.ignited_symbols:
            return False, R_IGNITE_DUP
        now = now or now_utc()
        gap = cfg.float("ALERT_MIN_GAP_MIN", 15)
        if self.last_ignition_at and (now - self.last_ignition_at) < timedelta(minutes=gap):
            return False, R_IGNITE_GAP
        if self.remaining > 0:
            return True, "BUDGET"
        exceptional = cfg.float("EXCEPTIONAL_SCORE", 90)
        if score >= exceptional and self.reserve_spent < self.reserve:
            return True, "RESERVE"
        return False, R_IGNITE_BUDGET

    def spend(self, symbol: str, slot: str, score: float,
              now: Optional[datetime] = None) -> None:
        now = now or now_utc()
        sym = symbol.upper()
        self.ignited_symbols.add(sym)
        self.spent.append({"symbol": sym, "slot": slot, "score": score, "at": now.isoformat()})
        if slot == "RESERVE":
            self.reserve_spent += 1
        self.last_ignition_at = now

    def slot_label(self) -> str:
        if self.remaining > 0:
            return f"{self.used + 1}/{self.limit}"
        return "RESERVE"

    def reset(self) -> None:
        """Clear today's spend (Telegram ``/resetbudget``)."""
        self.spent.clear()
        self.reserve_spent = 0
        self.ignited_symbols.clear()
        self.last_ignition_at = None

    def relabel(self, limit: int, reserve: int) -> None:
        """Apply a live ``/setbudget`` change without losing today's spend."""
        self.limit = max(0, limit)
        self.reserve = max(0, reserve)


@dataclass
class SymbolState:
    symbol: str
    state: State = State.WATCH
    consecutive_qualified: int = 0
    board_since: Optional[datetime] = None
    armed_level: Optional[float] = None
    armed_at: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    degraded_from: Optional[State] = None
    confirm_bars: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state.value, "consec": self.consecutive_qualified,
                "board_since": self.board_since.isoformat() if self.board_since else None,
                "armed_level": self.armed_level}


class StateMachine:
    """Holds per-symbol state and applies one cycle of transitions."""

    def __init__(self, cfg: Config, day: date):
        self.cfg = cfg
        self.day = day
        self.states: dict[str, SymbolState] = {}
        self.budget = Budget(day=day, limit=cfg.int("ALERT_BUDGET_PER_DAY", 5),
                             reserve=cfg.int("RESERVE_SLOTS", 1))

    def _st(self, symbol: str) -> SymbolState:
        s = self.states.get(symbol)
        if s is None:
            s = SymbolState(symbol=symbol)
            self.states[symbol] = s
        return s

    def step(self, ranking: Ranking, records: dict[str, Any], mode: str,
             now: Optional[datetime] = None,
             prices: Optional[dict[str, float]] = None,
             vwaps: Optional[dict[str, float]] = None,
             data_grade: str = "A",
             live: Optional[dict[str, dict[str, Optional[float]]]] = None,
             ) -> list[Transition]:
        """Advance every symbol one cycle and return the transitions.

        ``live`` carries the short-term tape per symbol
        (``{"vol_3m": shares, "dvol_10m": USD}``). It is required for ARMED:
        a name with no live-volume history fails closed (``NO_VOL_HISTORY``),
        because whole-day volume can never stand in for tape trading now.
        """
        now = as_utc(now or now_utc())
        prices = prices or {}
        vwaps = vwaps or {}
        live = live or {}

        def _live(sym: str) -> tuple[Optional[float], Optional[float]]:
            d = live.get(sym) or {}
            return d.get("vol_3m"), d.get("dvol_10m")

        def _vol_reason(sym: str) -> Optional[str]:
            v3, dv10 = _live(sym)
            px = prices.get(sym)
            if px is None:
                px = getattr(records.get(sym), "price", None)
            return live_volume_reason(v3, dv10, self.cfg, mode, px)
        out: list[Transition] = []

        board_syms = {s.symbol for s in ranking.board}
        rank_of = {s.symbol: s.rank for s in ranking.day_ranked}
        score_of = {s.symbol: s.score for s in ranking.day_ranked}
        # Weighted STRUCTURE component points (raw * W_STRUCTURE=15). Used by the
        # MIN_STRUCTURE_SCORE gate so heavy volume/fuel can never carry a dying
        # double-top into ARMED.
        struct_of = {s.symbol: (s.components.get("STRUCTURE").points if s.components.get("STRUCTURE") else 0.0)
                     for s in ranking.day_ranked}
        # Raw (0..1) TAPE per symbol, for the extra MIDDAY velocity floor.
        tape_raw_of = {s.symbol: (s.components.get("TAPE").raw if s.components.get("TAPE") else None)
                       for s in ranking.day_ranked}
        qualified_syms = {s.symbol for s in ranking.day_ranked}

        # Extra symbols seen this cycle that are not ranked (still advance state).
        seen = set(qualified_syms) | set(records.keys())

        for sym in seen:
            st = self._st(sym)
            prev = st.state
            st.last_seen = now
            rec = records.get(sym)

            # --- data-quality driven degradation ---
            if rec is not None and getattr(rec, "price", None) is None:
                if prev not in (State.HALTED_SUSPENDED,):
                    out.append(self._move(st, State.HALTED_SUSPENDED, R_SUSPEND,
                                          "no fresh quote"))
                continue
            if data_grade in ("D", "F") and prev == State.IGNITION:
                st.degraded_from = State.IGNITION
                out.append(self._move(st, State.DEGRADED, R_DEGRADE, f"grade {data_grade}"))
                continue
            if prev == State.DEGRADED and data_grade in ("A", "B", "C"):
                back = st.degraded_from or State.WATCH
                out.append(self._move(st, State.RECOVERED, R_RECOVER, f"back to {back.value}"))
                st.state = back
                prev = back

            if sym not in qualified_syms:
                # Fell out of the gates: drop out of the upper states.
                if prev in (State.QUALIFIED, State.BOARD, State.ARMED):
                    out.append(self._move(st, State.WATCH, R_DROP, "failed gates"))
                    st.consecutive_qualified = 0
                    st.confirm_bars = 0
                continue

            # --- WATCH -> QUALIFIED (consecutive fully-gated cycles) ---
            if prev == State.WATCH:
                st.consecutive_qualified += 1
                need = self.cfg.int("QUALIFY_CONSEC", 2)
                if st.consecutive_qualified >= need:
                    out.append(self._move(st, State.QUALIFIED, R_QUALIFY,
                                          f"{st.consecutive_qualified}>={need}"))
                else:
                    out.append(Transition(sym, prev, State.WATCH, R_QUALIFY_WAIT,
                                          f"{st.consecutive_qualified}/{need}"))
                prev = st.state
                if prev != State.QUALIFIED:
                    continue

            # --- QUALIFIED <-> BOARD (hysteresis) ---
            if prev == State.QUALIFIED:
                if sym in board_syms:
                    out.append(self._move(st, State.BOARD, R_BOARD_ENTER,
                                          f"rank {rank_of.get(sym)}"))
                    st.board_since = now
                prev = st.state

            if prev == State.BOARD:
                exit_rank = self.cfg.int("BOARD_EXIT_RANK", 8)
                dwell = self.cfg.float("BOARD_MIN_DWELL_S", 60)
                held = (now - st.board_since).total_seconds() if st.board_since else 1e9
                if sym not in board_syms and (rank_of.get(sym, 999) > exit_rank) and held >= dwell:
                    out.append(self._move(st, State.QUALIFIED, R_BOARD_EXIT,
                                          f"rank {rank_of.get(sym)}"))
                else:
                    out.append(Transition(sym, prev, State.BOARD, R_BOARD_HOLD,
                                          f"rank {rank_of.get(sym)}"))
                prev = st.state
                if prev != State.BOARD:
                    continue

            # --- BOARD -> ARMED ---
            check_arm = prev in (State.QUALIFIED, State.BOARD)
            if check_arm:
                rank = rank_of.get(sym, 999)
                score = score_of.get(sym, 0.0)
                if rank > self.cfg.int("ARM_MAX_RANK", 3):
                    out.append(Transition(sym, prev, prev, R_ARM_RANK, f"rank {rank}"))
                    continue
                if score < self.cfg.float("ARM_MIN_SCORE", 68):
                    out.append(Transition(sym, prev, prev, R_ARM_SCORE, f"score {score:.1f}"))
                    continue
                # Minimum structure gate (section 8): a high-volume/fuel name with a
                # dying/rolling-over chart must not arm. struct_of is the weighted
                # STRUCTURE points (0..15); MIN_STRUCTURE_SCORE (default 4.0) is the floor.
                if struct_of.get(sym, 0.0) < self.cfg.float("MIN_STRUCTURE_SCORE", 4.0):
                    out.append(Transition(sym, prev, prev, R_ARM_STRUCT,
                                          f"structure {struct_of.get(sym, 0.0):.1f}/15"))
                    continue
                # HARD live-volume gate (section 2): a dead chart cannot arm.
                vol_reason = _vol_reason(sym)
                if vol_reason:
                    v3, dv10 = _live(sym)
                    out.append(Transition(sym, prev, prev, vol_reason,
                                          f"vol_3m {v3} dvol_10m {dv10}"))
                    continue
                price = prices.get(sym)
                if price is None and rec is not None:
                    price = getattr(rec, "price", None)
                st.armed_level = price
                st.armed_at = now
                st.confirm_bars = 0  # fresh confirmation window on every re-arm
                out.append(self._move(st, State.ARMED, R_ARM,
                                      f"rank {rank}, score {score:.1f}"))
                prev = st.state

            # --- ARMED -> IGNITION ---
            if prev == State.ARMED:
                # FAIL CLOSED: re-validate the arm criteria every cycle while the
                # name waits for confirmation. ARMED is a promise ("this is a top
                # name right now"); if rank or score slips during the confirmation
                # window, the promise is gone and we must step down instead of
                # blindly confirming on stale strength. Checked BEFORE the chase
                # cap and the confirm-bar counter so a demoted name never accrues
                # confirmation toward an ignition it no longer deserves.
                rank = rank_of.get(sym, 999)
                score = score_of.get(sym, 0.0)
                if rank > self.cfg.int("ARM_MAX_RANK", 3):
                    st.confirm_bars = 0
                    back = State.BOARD if sym in board_syms else State.QUALIFIED
                    out.append(self._move(st, back, R_ARM_STEP_RANK, f"rank {rank}"))
                    prev = st.state
                    continue
                # The last gate before IGNITION: even a *ranked* name must stay at
                # or above the ignition score while confirming, or it steps back.
                if score < self.cfg.float("IGNITION_THRESHOLD", 70):
                    st.confirm_bars = 0
                    back = State.BOARD if sym in board_syms else State.QUALIFIED
                    out.append(self._move(st, back, R_ARM_STEP_SCORE, f"score {score:.1f}"))
                    prev = st.state
                    continue
                # Structure must hold through the confirmation window too: a name
                # whose chart rolls over mid-confirmation loses its front-side
                # promise exactly like a slipping rank does.
                if struct_of.get(sym, 0.0) < self.cfg.float("MIN_STRUCTURE_SCORE", 4.0):
                    st.confirm_bars = 0
                    back = State.BOARD if sym in board_syms else State.QUALIFIED
                    out.append(self._move(st, back, R_ARM_STEP_STRUCT,
                                          f"structure {struct_of.get(sym, 0.0):.1f}/15"))
                    prev = st.state
                    continue
                # Same hard live-volume gate on every ARMED cycle: tape that goes
                # quiet mid-confirmation is a demotion, not a slow ignition.
                vol_reason = _vol_reason(sym)
                if vol_reason:
                    st.confirm_bars = 0
                    back = State.BOARD if sym in board_syms else State.QUALIFIED
                    v3, dv10 = _live(sym)
                    out.append(self._move(st, back, R_ARM_STEP_VOL,
                                          f"{vol_reason} vol_3m {v3} dvol_10m {dv10}"))
                    prev = st.state
                    continue
                price = prices.get(sym) or (getattr(rec, "price", None) if rec else None)
                # Backside of the high: never ignite a name already rolling over.
                if rec is not None and backside_of_hod(price, getattr(rec, "day_high", None),
                                                       self.cfg):
                    out.append(Transition(sym, prev, State.ARMED, R_BACKSIDE_OF_HOD,
                                          f"{price} vs hod {getattr(rec, 'day_high', None)}"))
                    continue
                # Must be GREEN ON THE DAY. A dead-cat bounce or sub-VWAP chop is
                # not a runner even when the score momentarily qualifies.
                prev_close = getattr(rec, "prev_close", None) if rec is not None else None
                if price is not None and prev_close is not None and price <= prev_close:
                    st.confirm_bars = 0
                    out.append(Transition(sym, prev, State.ARMED, R_IGNITE_RED_DAY,
                                          f"{price:.4f} <= prev close {prev_close:.4f}"))
                    continue
                # VWAP retest tolerance: a healthy breakout often wicks back to
                # VWAP support. 0.5% below VWAP is tolerated; deeper is not.
                vwap_val = vwaps.get(sym)
                if vwap_val and price is not None:
                    vwap_tolerance = vwap_val * (1.0 - self.cfg.float("VWAP_IGNITION_TOLERANCE_PCT", 0.5) / 100.0)
                    if price < vwap_tolerance:
                        st.confirm_bars = 0
                        out.append(Transition(sym, prev, State.ARMED, R_ARM_BELOW_VWAP,
                                              f"{price:.4f} below vwap tolerance {vwap_tolerance:.4f}"))
                        continue
                armed = st.armed_level
                # Chase cap: never ignite far above the armed level.
                if price and armed and armed > 0:
                    chase = (price - armed) / armed * 100.0
                    if chase > self.cfg.float("CHASE_CAP_PCT", 4.0):
                        out.append(Transition(sym, prev, State.ARMED, R_ARM_CHASE,
                                              f"+{chase:.1f}% above armed"))
                        continue
                # Confirmation bars (extended needs more than regular).
                need = (self.cfg.int("EXT_CONFIRM_BARS", 1) if mode in ("PRE", "POST", "OVERNIGHT")
                        else self.cfg.int("REG_CONFIRM_BARS", 1))
                st.confirm_bars += 1
                if st.confirm_bars < need:
                    out.append(Transition(sym, prev, State.ARMED, "IGNITION_PENDING",
                                          f"confirm {st.confirm_bars}/{need}"))
                    continue
                # Phase must allow ignition.
                if not self.cfg.bool(f"{mode}_ALLOW_IGNITION", mode not in ("OVERNIGHT", "CLOSED")):
                    out.append(Transition(sym, prev, State.ARMED, R_IGNITE_PHASE, mode))
                    continue
                # MIDDAY velocity floor: keep midday enabled for genuine breaking-news
                # rockets, but refuse a slow stair-step grind that only qualifies
                # because FUEL/STRUCTURE/CONTEXT are maxed.
                if mode == "MIDDAY":
                    mid_floor = self.cfg.float("MIDDAY_MIN_TAPE_RAW", 0.72)
                    tape_raw = tape_raw_of.get(sym)
                    if mid_floor > 0 and (tape_raw is None or tape_raw < mid_floor):
                        out.append(Transition(sym, prev, State.ARMED, R_IGNITE_MIDDAY_TAPE,
                                              f"tape {tape_raw if tape_raw is not None else 'n/a'}"
                                              f" < {mid_floor:.2f} in MIDDAY"))
                        continue
                # Budget.
                ok, why = self.budget.can_spend(score, self.cfg, sym, now)
                if not ok:
                    out.append(Transition(sym, prev, State.ARMED, why, f"budget {self.budget.used}/{self.budget.limit}"))
                    continue
                slot = self.budget.slot_label()
                self.budget.spend(sym, "RESERVE" if slot == "RESERVE" else slot, score, now)
                out.append(self._move(st, State.IGNITION, R_IGNITE,
                                      f"slot {slot}, score {score:.1f}", spend=True))

        return out

    # -- abort checks (run continuously on ignited names) -------------------
    def check_aborts(self, prices: dict[str, float], vwaps: dict[str, float],
                     now: Optional[datetime] = None) -> list[Transition]:
        now = as_utc(now or now_utc())
        out: list[Transition] = []
        for sym, st in self.states.items():
            if st.state != State.IGNITION:
                continue
            price = prices.get(sym)
            vwap = vwaps.get(sym)
            if price is not None and vwap and price < vwap:
                out.append(self._move(st, State.ABORT, R_ABORT_VWAP,
                                      f"{price:.4f} < vwap {vwap:.4f}"))
        return out

    @staticmethod
    def _move(st: SymbolState, new: State, reason: str, detail: str = "",
              spend: bool = False) -> Transition:
        prev = st.state
        st.state = new
        return Transition(st.symbol, prev, new, reason, detail, spend)
