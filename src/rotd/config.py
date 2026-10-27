"""Configuration for ROTD.

Every threshold is a **named setting** with a unit, a starting value and a
one-line rationale. Nothing that gates a decision is a magic number in code: the
settings live in the ``SETTINGS`` registry below and are read through a typed
``Config`` object.

The registry is the single source of truth. ``tests/test_config.py`` fails if any
registry entry is missing a unit or a rationale, or if any setting is referenced
in code but absent from the registry.

Secrets (tokens, keys) live in the same env-style file but are never displayed,
never logged, and never passed on a command line (build-prompt §14.3/§15). The
file is owner-only (mode 600 on POSIX, an ACL on Windows); ``config.env`` is
gitignored.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

ALL_MODES = ("POST", "OVERNIGHT", "PRE", "OPEN", "MIDDAY", "CLOSED")


@dataclass(frozen=True)
class Setting:
    """One named setting: unit + starting value + one-line rationale."""
    key: str
    default: str
    unit: str          # "" when unitless/boolean/text
    rationale: str
    secret: bool = False
    mode: str = ""      # "" = global; else a mode name (POST/PRE/...)


# --------------------------------------------------------------------------
# The settings registry. Add a setting here, never a bare number in code.
# --------------------------------------------------------------------------

_GLOBAL: list[Setting] = [
    # --- identity / paths ---
    Setting("ROTD_HOME", "", "path", "Install home; anchors the DB, config and logs."),

    # --- universe & scope (section 18.1/18.2) ---
    Setting("MAX_PRICE", "10.00", "USD",
            "Price ceiling used ONLY when MAX_MARKET_CAP=0 (legacy sub-$10 mode). There is NO price floor."),
    Setting("MAX_MARKET_CAP", "100000000", "USD",
            "Universe ceiling: only names with market cap <= this. 0 = disabled (falls back to MAX_PRICE). "
            "Allows high-priced micro-caps ($15-$40 shares) with tiny floats into the pool."),
    Setting("INCLUDE_OTC", "0", "bool", "OTC off by default: the data is poorer and rules differ."),
    Setting("INCLUDE_DERIVATIVES", "0", "bool", "Warrants/rights/units excluded by default."),
    Setting("PRICE_BANDS", "0.20,0.50,1,5,10", "USD bounds",
            "Price-band boundaries for metric/MISS breakdowns (§18.1)."),

    # --- data reconciliation (DATA-01..12) ---
    Setting("PRICE_CONFLICT_PCT_REG", "50.0", "%",
            "Two sources >50% apart in regular hours => CONFLICT (DATA-02). Wide on purpose: "
            "micro-caps jump +20-30% in seconds and unverified helper feeds lag; a wide "
            "tolerance keeps vertical moves / feed lag from falsely flagging CONFLICT."),
    Setting("PRICE_CONFLICT_PCT_EXT", "95.0", "%",
            "Two sources more than this far apart in extended hours => CONFLICT (DATA-02). "
            "The gap is measured RELATIVE to the higher price, so the value is bounded below "
            "100%: anything >= 100 disables the gate entirely. 95% (vs 50% in regular hours) "
            "absorbs a stale IEX quote on a pre-market gapper without ever accepting two "
            "sources that disagree by an order of magnitude."),
    Setting("QUOTE_MAX_AGE_S_REG", "20", "s", "A regular-hours quote older than this is STALE (DATA-03)."),
    Setting("QUOTE_MAX_AGE_S_EXT", "60", "s", "An extended-hours quote older than this is STALE (DATA-03)."),
    Setting("BAR_MAX_AGE_S", "90", "s", "A bar series whose last bar is older than this is STALE (DATA-03)."),
    Setting("FLOAT_CONFLICT_FACTOR", "1.5", "x",
            "Two float sources >1.5x apart => FLOAT_CONFLICT (DATA-06)."),
    Setting("FLOAT_MIN_OUTSTANDING_RATIO", "0.5", "x",
            "Market cap / price gives shares outstanding, which can never be smaller than "
            "the float. A float below this fraction of the implied count is a units error "
            "(DATA-06b) and the float is dropped, not guessed. 0 disables the check."),
    Setting("FLOAT_MAX_OUTSTANDING_RATIO", "20.0", "x",
            "A float more than this many times below the implied outstanding count is a "
            "units error (DATA-06b); again the float is dropped rather than rescaled."),
    Setting("SEC_SHARES_MAX_AGE_D", "14", "days",
            "A share count from an SEC filing older than this cannot bound float (DATA-06)."),
    Setting("REVERSE_SPLIT_RECENT_D", "90", "days",
            "A reverse split within this window flags REVERSE_SPLIT_RECENT (DATA-07)."),

    # --- hard live-volume gate (section 2) ---
    # Fail closed on warmup/dead tape: whole-day volume is NOT enough to alert.
    Setting("MIN_VOL_3M_SHARES", "50000", "shares",
            "Minimum shares traded in the last 3 minutes (in-memory rolling delta). "
            "Blocks ARMED/IGNITION on dead tape; None (warmup) also fails closed."),
    Setting("MIN_DVOL_10M_REG", "150000", "USD",
            "Minimum 10-minute rolling dollar volume in OPEN/MIDDAY."),
    Setting("MIN_DVOL_10M_EXT", "75000", "USD",
            "Minimum 10-minute rolling dollar volume in PRE/POST."),

    # --- front-side structure (section 8) ---
    Setting("MAX_DRAWDOWN_FROM_HOD_PCT", "15.0", "%",
            "Block ARMED->IGNITION when price is more than this below the day high "
            "(no backside-of-HOD fades)."),
    Setting("VWAP_IGNITION_TOLERANCE_PCT", "0.5", "%",
            "Ignition tolerates a wick this far BELOW VWAP (a healthy retest of VWAP "
            "support), instead of aborting on a strict price < vwap test."),
    Setting("MIDDAY_MIN_TAPE_RAW", "0.72", "0..1",
            "MIDDAY only: the TAPE component must reach this raw score to ignite, so a "
            "slow 2-hour stair-step grind cannot ride maxed FUEL/STRUCTURE/CONTEXT to an "
            "alert. 0 disables the extra midday floor."),
    Setting("MIN_STRUCTURE_SCORE", "2.0", "points",
            "Minimum weighted STRUCTURE points (of 15) required to reach ARMED. "
            "Rejects high-volume/fuel setups with terrible front-side structure. "
            "2.0 (not 4.0) accommodates beaten-down penny setups whose 52w-low / gap "
            "sub-scores are structurally weak but whose tape is genuinely igniting."),

    # --- gates (section 7) ---
    Setting("EXT_SPREAD_MULT", "1.5", "x", "POST/PRE spreads are looser by 1.5x (G-SPREAD)."),
    Setting("LIQ_15M_POST", "25000", "USD", "15-min dollar volume floor in POST (G-LIQ)."),
    Setting("LIQ_15M_PRE", "25000", "USD", "15-min dollar volume floor in PRE (G-LIQ)."),
    Setting("LIQ_15M_OPEN", "100000", "USD", "15-min dollar volume floor in OPEN (G-LIQ)."),
    Setting("LIQ_15M_MIDDAY", "75000", "USD", "15-min dollar volume floor in MIDDAY (G-LIQ)."),
    Setting("MIN_TRADES_15M", "0", "prints",
            "Minimum prints in 15m for G-TRADES; 0 = not enforce (data unavailable on feed)."),
    Setting("MIN_AVG_TRADE", "0", "USD",
            "Minimum average trade size for G-TRADES; 0 = not enforce (data unavailable)."),
    Setting("EXIT_TEST_USD", "500", "USD", "The test exit size for G-EXIT."),
    Setting("EXIT_MAX_SEC", "60", "s", "A test exit must fill inside this many seconds (G-EXIT)."),
    Setting("EXIT_MAX_SLIP_PCT", "2.0", "%", "A test exit must fill within this slippage (G-EXIT)."),
    Setting("SPREAD_TIERS", "1.00:1.5,0.50:3,0.20:5,0.05:8,0.00:12", "price:max spread %",
            "Max spread by price tier, loosest at the bottom (G-SPREAD)."),
    # Gate enable flags. Each gate below needs a feed this deployment does NOT
    # have (VERIFIED G-18): no source carries a bid/ask book or a 15-min print
    # count, so the gates are OFF by default and report NOT_ENFORCED rather than
    # fail-closed on data the system structurally cannot obtain. Turn one on the
    # moment a feed supplies its input (e.g. Alpaca unblocked for the book).
    Setting("USE_SPREAD_GATE", "1", "bool",
            "G-SPREAD needs a bid/ask book; no source has one (VERIFIED G-18). 0 = not enforced."),
    Setting("USE_LIQ_GATE", "0", "bool",
            "G-LIQ needs 15-min dollar volume; needs a 1-min bar feed (VERIFIED G-18). 0 = not enforced."),
    Setting("USE_EXIT_GATE", "0", "bool",
            "G-EXIT needs a spread/slippage estimate; needs a book (VERIFIED G-18). 0 = not enforced."),

    # --- scoring weights (section 9.1) ---
    Setting("W_TAPE", "30", "points", "Tape-ignition weight (Stage-1 score)."),
    Setting("W_CATALYST", "5", "points", "Catalyst weight (Stage-1 score)."),
    Setting("W_FUEL", "20", "points", "Fuel weight (Stage-1 score)."),
    Setting("W_STRUCTURE", "15", "points", "Structure weight (Stage-1 score)."),
    Setting("W_CONTEXT", "10", "points", "Context weight (Stage-1 score)."),

    # --- ranking / board (section 9.2) ---
    Setting("BOARD_SIZE", "5", "names", "BOARD = the top 5 qualified candidates."),
    Setting("ROTD_MIN_SCORE", "60", "points",
            "Rank 1 is only ROTD if its score is at least this (else 'no qualified runner')."),

    # --- state machine / hysteresis (section 11.1) ---
    Setting("QUALIFY_CONSEC", "1", "cycles", "Consecutive fully-gated cycles to reach QUALIFIED."),
    Setting("BOARD_MIN_DWELL_S", "15", "s", "Must stay top-5 this long to hold BOARD (hysteresis)."),
    Setting("BOARD_EXIT_RANK", "8", "rank", "Leaves BOARD only below this rank (hysteresis)."),
    Setting("ARM_MAX_RANK", "3", "rank", "ARMED requires rank <= this."),
    Setting("ARM_MIN_SCORE", "70", "points", "ARMED requires score >= this, re-checked every cycle while ARMED."),
    Setting("IGNITION_THRESHOLD", "70", "points", "ARMED keeps its confirmation only while score >= this; the last gate before IGNITION (the alert trigger). Must stay >= ARM_MIN_SCORE, or a name could arm at one score and fire on a weaker one."),
    Setting("CHASE_CAP_PCT", "18.0", "%", "No ignition if price is >this% above the armed level."),
    Setting("EXT_CONFIRM_BARS", "1", "bars", "POST/PRE need this many confirming extended bars."),
    Setting("REG_CONFIRM_BARS", "1", "bars", "Regular hours need this many confirming bars."),
    Setting("HALT_RESUME_EVAL_MIN", "2", "minutes", "Evaluate a resumption for this long after a halt."),

    # --- alert budget (section 11.2) ---
    Setting("ALERT_BUDGET_PER_DAY", "30", "pushes", "At most this many IGNITION pushes per trading day (A1)."),
    Setting("ALERT_BUDGET_MIN", "1", "pushes", "Floor for /setbudget (a sane minimum; 0 would silence the day)."),
    Setting("ALERT_BUDGET_MAX", "50", "pushes", "Ceiling for /setbudget (a sane cap so a typo cannot set an absurd budget)."),
    Setting("EXCEPTIONAL_SCORE", "90", "points", "A reserve-slot candidate must score at least this (A4)."),
    Setting("ALERT_MIN_GAP_MIN", "15", "minutes", "Minimum spacing between pushes (A3)."),
    Setting("RESERVE_SLOTS", "1", "pushes", "Reserve pushes beyond the budget for exceptional names (A4)."),
    Setting("BOARD_SCHEDULE_ET", "16:30,19:30,04:30,07:30,09:00,09:35", "HH:MM ET",
            "Silent scheduled BOARD messages (A6)."),

    # --- ABORT (section 11.3) ---
    Setting("ABORT_VWAP_MINUTES", "5", "minutes", "A 5-minute close under the alert VWAP aborts."),
    Setting("ABORT_SPREAD_WIDE_S", "300", "s", "A spread that stays wide this long aborts."),

    # --- tracking / grading (section 12, 18.3, 18.4) ---
    Setting("CHECKPOINTS_MIN", "15,60,eod,next_open,next_close", "minutes/labels",
            "Outcome checkpoints after the reference price (§12.2)."),
    Setting("RUNNER_GAIN_PCT", "30", "%", "A runner reaches this before the stop (§18.4)."),
    Setting("RUNNER_STOP_PCT", "12", "%", "The loss level a runner must beat to the gain (§18.4)."),
    Setting("RUNNER_LIST_PCT", "100", "%", "Runner list = +this% vs prior regular close (§12.3)."),
    Setting("RUNNER_LIST_MIN_DVOL", "5000000", "USD", "Main runner-list dollar-volume floor (§18.4)."),
    Setting("RUNNER_LIST_MIN_DVOL_LOW", "1000000", "USD", "Illiquid runner-list floor (§18.4)."),
    Setting("LEAD_MARK_PCT", "30", "%", "Lead time = system first saw it vs crossing this % (§12.3)."),

    # --- catalyst service (section 10) ---
    Setting("CATALYST_TOP_N_EARLY", "10", "rank", "Audit a candidate's catalyst early once it reaches this rank."),
    Setting("CATALYST_CACHE_TTL_H", "24", "hours", "Cache a catalyst audit by (symbol, item id)."),
    Setting("CATALYST_TIMEOUT_S", "15", "s", "LLM call timeout; on timeout the catalyst is UNKNOWN."),
    Setting("AI_TIMEOUT_S", "12", "s", "Request timeout for AI auditor/verdict calls; longer prompts to slow gateways need 30s."),
    Setting("AI_PROMPT_FILE", "methodology_v3.md", "file", "System prompt file under prompts/ (§10)."),
    Setting("AI_TEMPERATURE", "0.0", "float", "Auditor temperature; 0 is required by A6."),
    Setting("AI_MAX_TOKENS", "1200", "tokens", "Auditor response cap (A6: near 1200)."),
    Setting("AI_CAUSE_WINDOW_H", "14", "hours", "A news item is FRESH inside this window (§10 A3); 14h keeps premarket/morning releases live through midday."),
    Setting("MIN_RR", "2.0", "ratio", "Minimum risk:reward; ENTER below this downgrades to ARMED (A4.9)."),
    Setting("CALIBRATION_CLAMP", "0.25", "float", "Max number of confidence grades a calibration may move (A4.10)."),
    Setting("DILUTION_BAND_HIGH", "60", "score", "dilution_score at/above this is band HIGH (§10 6.7)."),
    Setting("DILUTION_BAND_MEDIUM", "35", "score", "dilution_score at/above this is band MEDIUM."),
    Setting("PROMO_AMPLIFY_PCT", "0", "%", "Reserved: promoter-amplification threshold; 0 = disabled."),

    # --- source rate limits / caching (§4.4, doctor 429 fix) ---
    Setting("QUOTE_CACHE_TTL_S", "10", "s", "Reuse a successful per-symbol quote within this window instead of re-fetching."),
    Setting("SOURCE_COOLDOWN_S", "60", "s", "After an HTTP 429 a source is put on cooldown for this long before its next call."),
    Setting("TRADINGVIEW_ENABLED", "1", "bool", "Use the keyless TradingView scanner for batch freshness quotes; falls back to Alpaca/Finnhub when off/unavailable."),
    Setting("TRADINGVIEW_TIMEOUT_S", "10", "s", "HTTP timeout for one batch scanner POST."),
    Setting("TRADINGVIEW_CACHE_TTL_S", "10", "s", "Reuse one batch scanner result for this window (at most one POST per cycle)."),
    Setting("TRADINGVIEW_MAX_DEVIATION_PCT", "150", "%", "Drop a scanner price that deviates from the reconciled Finviz price by more than this (unverified feed stays out of the decision path). 150% matches the expanded pre-market volatility tolerance, so a fast gapper is not discarded as 'deviant' mid-spike."),

    # --- discovery (section 6) ---
    Setting("CARRY_LOOKBACK_D", "90", "days", "Carry-list 'recent runner history' window (§6.4)."),
    Setting("SYMPATHY_ENABLED", "0", "bool", "Sympathy adds off until the sector join is proven (§6.5)."),

    # --- retention / ops (section 13, 15) ---
    Setting("RAW_RETENTION_DAYS", "7", "days", "Delete raw_payloads and non-qualified per-cycle data older than this."),
    Setting("RAW_RETENTION_HOURS", "1", "hours",
            "Delete raw_payloads older than this (2h is enough for replay/debugging)."),
    Setting("CAND_RETENTION_HOURS", "1", "hours",
            "Delete non-board candidates/decisions older than this; board/armed/ignition rows are kept."),
    Setting("DB_PRUNE_INTERVAL_S", "300", "s",
            "Run the retention + incremental-vacuum pass this often (and once at startup)."),
    Setting("CAND_JSON_TOP_N", "50", "rank",
            "Keep full gate/feature JSON only for this many top names; other rows store compact reasons."),
    Setting("MEMORY_TTL_S", "7200", "s",
            "Evict per-symbol in-memory dictionaries after this long out of the active universe."),
    Setting("VOL_HISTORY_RESTORE_MIN", "15", "minutes",
            "On restart, rehydrate the rolling volume history from this recent window."),
    Setting("PRIOR_SESSION_CARRY_MIN", "240", "minutes",
            "At the 16:00 ET trading-day rollover, seed the rolling volume history from "
            "this much of the prior POST/OVERNIGHT tape, so pre-market starts warm instead "
            "of waiting 3-10 minutes for the deque to refill. 0 disables the carry-over."),
    Setting("DISK_WARN_PCT", "10", "%", "Warn once when free disk falls under this."),
    Setting("QUIET_HOURS", "", "HH:MM-HH:MM,mode", "Optional per-mode quiet hours (none by default, §18.5)."),

    # --- runtime ---
    Setting("UNIVERSE_POLL_S", "15", "s", "Universe sweep cadence (mode profile overrides)."),
    Setting("MOVERS_POLL_S", "10", "s", "Mover-screen cadence (mode profile overrides)."),
    Setting("WATCHDOG_STALL_S", "180", "s", "A cycle taking longer than this trips the watchdog."),
    Setting("LISTEN_POLL_S", "5", "s", "Loop cadence of `rotd listen`: one cycle + one Telegram command poll (§14.4)."),
    Setting("TELEGRAM_POLL_TIMEOUT_S", "20", "s", "Long-poll timeout for getUpdates in `rotd listen`."),
    Setting("SHADOW", "0", "bool", "1 = decide and log but never push (rotd shadow). Owner commands still act."),
    Setting("SELFTEST", "0", "bool", "1 = fully offline deterministic mode."),
    Setting("SHADOW_DAYS_REQUIRED", "10", "trading_days", "Shadow clock length before live pushes are allowed (section 17 Phase 6)."),
    Setting("SHADOW_AUTOSTART", "0", "bool", "1 = start the shadow clock automatically on the first shadow cycle."),
    Setting("SHADOW_RUN_WHEN_CLOSED", "1", "bool", "1 = keep running decision cycles outside NYSE sessions (live off-thread so commands stay responsive)."),
    Setting("EXPIRY_REMINDERS", "0", "bool",
            "1 = DM subscribers the T-7/T-3/T-1/T-0 renewal reminders (once per milestone per expiry; set RENEW_URL for the button)."),
    Setting("EXPIRY_REMINDER_INTERVAL_S", "3600", "s",
            "How often the listen daemon checks for due expiry reminders (the cadence itself is date-based, so this only controls latency)."),

    # --- secrets (never displayed / never logged) ---
    Setting("FINVIZ_TOKEN", "", "", "Finviz Elite token.", secret=True),
    Setting("FINVIZ_BASE", "https://elite.finviz.com", "url", "Finviz base URL."),
    Setting("FINNHUB_KEY", "", "", "Finnhub API key.", secret=True),
    Setting("ALPHA_VANTAGE_KEY", "", "", "Alpha Vantage API key.", secret=True),
    Setting("ALPACA_KEY", "", "", "Alpaca API key id.", secret=True),
    Setting("ALPACA_SECRET", "", "", "Alpaca API secret.", secret=True),
    Setting("ALPACA_FEED", "iex", "feed", "Alpaca data feed (iex = single venue)."),
    Setting("SEC_USER_AGENT", "ROTD research", "text", "SEC requires a descriptive User-Agent."),
    Setting("AI_BASE_URL", "", "url", "OpenAI-compatible base URL for the catalyst model."),
    Setting("AI_KEY", "", "", "Catalyst model API key.", secret=True),
    Setting("AI_MODEL", "deepseek-v4.1-flash", "model", "Catalyst model id."),
    Setting("TELEGRAM_TOKEN", "", "", "Telegram bot token.", secret=True),
    Setting("TELEGRAM_CHAT_ID", "", "", "The only chat id allowed to read/command."),
    Setting("RENEW_URL", "", "url",
            "Renewal/payment link attached to every subscription-expiry reminder "
            "(empty = reminders are sent as plain text with no button)."),
]

# Per-mode defaults: liquidity is already mode-keyed above; these carry cadence
# and the alert-type policy so behaviour differs by window from config, not code.
_MODE: list[Setting] = []
for _m in ("POST", "OVERNIGHT", "PRE", "OPEN", "MIDDAY"):
    _MODE.append(Setting(f"{_m}_ALLOW_IGNITION", "1" if _m != "OVERNIGHT" else "0", "bool",
                         f"Whether IGNITION pushes are allowed in {_m} (OVERNIGHT never alerts on quotes).",
                         mode=_m))
    _MODE.append(Setting(f"{_m}_POLL_S", {"POST": "10", "OVERNIGHT": "300", "PRE": "10",
                                          "OPEN": "10", "MIDDAY": "15"}[_m], "s",
                         f"Cycle cadence in {_m}. Candle-1 speed: POST/PRE/OPEN poll "
                         f"every 10s so a fresh ignition is caught on the first candle.", mode=_m))

SETTINGS: list[Setting] = _GLOBAL + _MODE
BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}

SECRET_KEYS = {s.key for s in SETTINGS if s.secret}

# Settings whose change needs a restart.
RESTART_KEYS = {"TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"}


def rationale(key: str) -> str:
    s = BY_KEY.get(key)
    return s.rationale if s else ""


def unit_of(key: str) -> str:
    s = BY_KEY.get(key)
    return s.unit if s else ""


# --------------------------------------------------------------------------


def restrict_permissions(path: Path) -> None:
    """Owner-only access: mode 600 on POSIX, an ACL for the current user on Windows."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                       capture_output=True, timeout=15, check=False)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not restrict %s with icacls: %s", path, exc)


class Config:
    """One env-style file, live reload, typed getters, masked secrets."""

    def __init__(self, path: Optional[Path] = None, overrides: Optional[dict[str, str]] = None):
        env_path = os.environ.get("ROTD_CONFIG")
        self.path = Path(path or env_path or (Path.home() / ".rotd" / "rotd.env"))
        self._values: dict[str, str] = {}
        self._mtime: float = -1.0
        self._dirty = False   # a key changed in-process; clients must be rebuilt
        self._overrides = dict(overrides or {})
        self._load()

    # -- loading ------------------------------------------------------------
    def _stat_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return -1.0

    def _load(self) -> None:
        vals: dict[str, str] = {s.key: s.default for s in SETTINGS}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
        # Environment overrides (used by tests and one-off runs).
        for k, v in os.environ.items():
            if k in BY_KEY:
                vals[k] = v
        vals.update(self._overrides)
        self._values = vals
        self._mtime = self._stat_mtime()

    def reload_if_changed(self) -> bool:
        if self._stat_mtime() != self._mtime:
            self._load()
            return True
        return False

    # -- typed getters ------------------------------------------------------
    def raw(self, key: str, default: str = "") -> str:
        return self._values.get(key, BY_KEY[key].default if key in BY_KEY else default)

    def str(self, key: str, default: str = "") -> str:
        v = self.raw(key, default)
        return v if v != "" else default

    def int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.raw(key)))
        except (TypeError, ValueError):
            return default

    def float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.raw(key))
        except (TypeError, ValueError):
            return default

    def bool(self, key: str, default: bool = False) -> bool:
        v = (self.raw(key) or "").strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False if v != "" else default
        return default

    def list(self, key: str, sep: str = ",") -> list[str]:
        raw = self.raw(key)
        return [p.strip() for p in raw.split(sep) if p.strip()]

    def float_list(self, key: str) -> list[float]:
        out = []
        for p in self.list(key):
            try:
                out.append(float(p))
            except ValueError:
                pass
        return out

    # -- display ------------------------------------------------------------
    def display(self, key: str) -> str:
        from .util import mask
        if key in SECRET_KEYS:
            return mask(self.raw(key))
        return self.raw(key)

    def summary(self) -> str:
        lines = []
        for s in SETTINGS:
            val = self.display(s.key)
            unit = f" [{s.unit}]" if s.unit else ""
            lines.append(f"  {s.key} = {val}{unit}")
        return "\n".join(lines)

    # -- persistence --------------------------------------------------------
    def set(self, key: str, value: str) -> None:
        self._values[key] = str(value)

    def take_dirty(self) -> bool:
        """True once after a live key change (``/setkey``, ``/setbudget``).

        ``append_to_file`` refreshes ``_mtime`` itself, so ``reload_if_changed``
        cannot see the write; the listen daemon needs this separate flag to know
        that the credentials in memory changed and the source clients must be
        rebuilt.
        """
        was = self._dirty
        self._dirty = False
        return was

    def append_to_file(self, key: str, value: str) -> None:
        """Persist one key to the config file atomically (created if missing, mode 600).

        Write a sibling temp file, fsync, then ``os.replace``: a crash mid-write
        can never leave the daemon reading a truncated config (the listener reloads
        this file live, §14.4).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if self.path.exists():
            existing = self.path.read_text(encoding="utf-8")
        lines = [ln for ln in existing.splitlines()
                 if not ln.strip().startswith(f"{key}=")]
        lines.append(f"{key}={value}")
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            restrict_permissions(tmp)
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        restrict_permissions(self.path)
        self.set(key, value)
        self._dirty = True
        self._mtime = self._stat_mtime()

    def ensure_exists(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.save()
            restrict_permissions(self.path)

    def save(self) -> None:
        lines = ["# ROTD configuration. Secrets never leave this file.",
                 "# Every key is documented in src/rotd/config.py (SETTINGS).", ""]
        for s in SETTINGS:
            unit = f"  # {s.unit}" if s.unit else ""
            lines.append(f"# {s.rationale}{unit}")
            if s.secret:
                lines.append(f"# {s.key}=<secret>")
            else:
                lines.append(f"{s.key}={self.raw(s.key)}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        restrict_permissions(self.path)
        self._mtime = self._stat_mtime()

    # -- derived paths ------------------------------------------------------
    @property
    def home(self) -> Path:
        base = self.raw("ROTD_HOME") or str(self.path.parent)
        p = Path(base)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def data_path(self, *parts: str) -> Path:
        return self.home.joinpath(*parts)

    @property
    def db_path(self) -> Path:
        return self.data_path("rotd.db")

    @property
    def log_dir(self) -> Path:
        p = self.data_path("logs")
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def fixtures_dir(self) -> Path:
        return Path(__file__).resolve().parent / "sources" / "fixtures"

    # -- secrets ------------------------------------------------------------
    @property
    def secret_values(self) -> list[str]:
        return [self.raw(k) for k in SECRET_KEYS if self.raw(k)]
