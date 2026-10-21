"""SQLite persistence for ROTD.

One file (``rotd.db`` by default). The schema (build prompt §13) stores:

- ``snapshots``   — one reconciled snapshot per cycle (the single source of truth
  the decision path reads), keyed by (trading_day, mode, cycle_ts).
- ``candidates``  — latest state per symbol (score, gates, reasons), one row per
  (trading_day, mode, symbol); only the top ``CAND_JSON_TOP_N`` ranked names are
  written, which keeps the file small.
- ``decisions``   — the state-machine transition logged for every symbol/cycle,
  *including rejects*, with reason codes (explainability rule).
- ``alerts``      — every push actually sent (or shadow-logged), with its budget
  slot, reference price and reasons.
- ``outcomes``    — checkpoints and the eventual grade for every alert.
- ``catalysts``   — async catalyst verdicts, cached by (symbol, item_id).
- ``runner_list`` — the day's sub-$10 +100% runners (MISS REPORT input).
- ``miss_report`` — one row per trading day: the assembled MISS REPORT.
- ``events``      — free-form operational events (health, errors, config reload).
- ``raw_payloads``— optional raw JSON blobs for replay, pruned by retention.

Writes are append-only in the decision path; retention (§13) deletes raw payloads
and non-qualified per-cycle rows after ``RAW_RETENTION_DAYS``.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .util import UTC, as_utc, now_utc, stable_json

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# The subscription-expiry reminder cadence: days-left values that trigger a DM.
# Order matters for the message text only; the set is used for membership.
REMINDER_MILESTONES: tuple[int, ...] = (7, 3, 1, 0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_day   TEXT NOT NULL,
    mode          TEXT NOT NULL,
    cycle_ts      TEXT NOT NULL,          -- ISO-8601 UTC
    data_grade    TEXT NOT NULL,          -- A..F (DATA-12)
    quality_json  TEXT NOT NULL,          -- per-source facts + reason codes
    payload_hash  TEXT NOT NULL,          -- stable_json hash for replay
    n_symbols     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(trading_day, mode, cycle_ts)
);
-- latest_snapshot(): newest cycle for a day.
CREATE INDEX IF NOT EXISTS idx_snap_day_ts ON snapshots(trading_day, cycle_ts DESC);

CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_day   TEXT NOT NULL,
    mode          TEXT NOT NULL,
    cycle_ts      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    score         REAL,
    rank          INTEGER,
    passed_gates  INTEGER NOT NULL DEFAULT 0,
    gate_json     TEXT NOT NULL DEFAULT '{}',   -- per-gate pass/fail + reason codes
    feature_json  TEXT NOT NULL DEFAULT '{}',   -- feature values with basis tags
    suppression   TEXT NOT NULL DEFAULT '',     -- reason code if suppressed
    UNIQUE(trading_day, mode, cycle_ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_cand_day_sym ON candidates(trading_day, symbol);
-- Panel query shape (/board, /status): latest cycle for a day, then rank order.
CREATE INDEX IF NOT EXISTS idx_cand_day_ts_rank ON candidates(trading_day, cycle_ts DESC, rank);
-- Score-ordered lookups (top-N panel, candidate JSON retention).
CREATE INDEX IF NOT EXISTS idx_cand_ts_score ON candidates(trading_day, cycle_ts DESC, score DESC);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_day   TEXT NOT NULL,
    mode          TEXT NOT NULL,
    cycle_ts      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    prev_state    TEXT NOT NULL,
    new_state     TEXT NOT NULL,
    reason        TEXT NOT NULL,          -- machine reason code
    detail        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dec_day_sym ON decisions(trading_day, symbol);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_day     TEXT NOT NULL,
    mode            TEXT NOT NULL,
    cycle_ts        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    kind            TEXT NOT NULL,        -- IGNITION / BOARD / ABORT
    budget_slot     TEXT NOT NULL DEFAULT '',  -- e.g. 1/3 or reserve
    score           REAL,
    rank            INTEGER,
    ref_price       REAL,
    vwap            REAL,
    spread_pct      REAL,
    reason          TEXT NOT NULL,
    message_hash    TEXT NOT NULL,
    sent            INTEGER NOT NULL DEFAULT 0,  -- 0 in shadow
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_day ON alerts(trading_day);
CREATE INDEX IF NOT EXISTS idx_alert_sym ON alerts(trading_day, symbol);

CREATE TABLE IF NOT EXISTS outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id      INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    trading_day   TEXT NOT NULL,
    checkpoint    TEXT NOT NULL,          -- 15m / 60m / eod / next_open / next_close
    ts            TEXT NOT NULL,
    price         REAL,
    ret_pct       REAL,
    max_gain_pct  REAL,
    max_dd_pct    REAL,
    hit_gain_first INTEGER,               -- 1 runner, 0 stopped first, NULL pending
    detail        TEXT NOT NULL DEFAULT '',
    UNIQUE(alert_id, checkpoint)
);

CREATE TABLE IF NOT EXISTS catalysts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    item_id       TEXT NOT NULL,          -- news/filing id
    kind          TEXT NOT NULL,          -- NEWS / FILING
    verdict       TEXT NOT NULL,          -- MAJOR / MINOR / NONE / UNKNOWN
    confidence    REAL,
    headline      TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    decided_at    TEXT NOT NULL,
    UNIQUE(symbol, item_id)
);

CREATE TABLE IF NOT EXISTS runner_list (
    trading_day   TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    prior_close   REAL,
    day_high      REAL,
    gain_pct      REAL,
    dollar_vol    REAL,
    illiquid      INTEGER NOT NULL DEFAULT 0,
    in_universe   INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT,
    detail        TEXT NOT NULL DEFAULT '',
    UNIQUE(trading_day, symbol)
);

CREATE TABLE IF NOT EXISTS miss_report (
    trading_day   TEXT NOT NULL PRIMARY KEY,
    generated_at  TEXT NOT NULL,
    body_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    level         TEXT NOT NULL,
    source        TEXT NOT NULL,
    message       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS raw_payloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_day   TEXT NOT NULL,
    cycle_ts      TEXT NOT NULL,
    source        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    body          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_day ON raw_payloads(trading_day);

-- Read-only Telegram subscribers. The owner is TELEGRAM_CHAT_ID and is never
-- stored here; this table only holds the time-bound guest list.
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id       TEXT PRIMARY KEY,
    expires_at    TEXT NOT NULL,          -- ISO date 'YYYY-MM-DD' (UTC)
    label         TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);

-- Every chat that ever messaged the bot. Telegram will not let the bot open a
-- chat by @username, so the numeric id is captured here first and /request
-- turns a row into a PENDING access request.
CREATE TABLE IF NOT EXISTS seen_users (
    chat_id     TEXT PRIMARY KEY,
    username    TEXT DEFAULT '',          -- lowercase, no '@'
    first_name  TEXT DEFAULT '',
    status      TEXT DEFAULT 'SEEN',      -- SEEN, PENDING, APPROVED, REJECTED
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_username ON seen_users(username);

-- One row per (chat, milestone) reminder already delivered, so the T-7/T-3/T-1/T-0
-- cadence can run every day (and on every restart) without ever sending the same
-- milestone twice. A renewal resets nothing: the next subscription cycle has new
-- expiry dates and therefore new milestones.
CREATE TABLE IF NOT EXISTS expiry_reminders_sent (
    chat_id       TEXT NOT NULL,
    milestone     INTEGER NOT NULL,       -- 7, 3, 1, 0 (days left when sent)
    expires_at    TEXT NOT NULL,          -- the expiry this reminder belonged to
    sent_at       TEXT NOT NULL,
    PRIMARY KEY (chat_id, milestone, expires_at)
);
CREATE INDEX IF NOT EXISTS idx_expiry_reminders_chat ON expiry_reminders_sent(chat_id);
"""


def _iso(dt: Optional[datetime]) -> str:
    return as_utc(dt or now_utc()).isoformat()


class Store:
    """Thread-safe SQLite wrapper with a small, explicit API."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 30s busy timeout: the startup prune runs on the listen daemon while a
        # CLI command (rotd board/status) opens the same DB. Without it the
        # second writer fails instantly with "database is locked".
        self._conn = sqlite3.connect(str(self.path), timeout=30.0,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL: the listen daemon writes on a worker thread while Telegram reads on
        # the main thread — WAL keeps readers from blocking the writer. NORMAL is
        # durable enough here (a crash loses at most the last commit, not the DB).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Freed pages go back to the file in small chunks instead of a full
        # VACUUM: the live DB is far too large to rewrite (needs ~2x free space).
        self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self._conn.execute("PRAGMA journal_size_limit=67108864")
        self._conn.executescript(SCHEMA)
        self._conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                           (str(SCHEMA_VERSION),))
        self._conn.commit()
        self._maybe_enable_incremental_vacuum()
        # Snapshots are only needed for the in-process warm boot window (item 7);
        # every row is a full per-source quality blob.
        self.execute("DELETE FROM snapshots WHERE cycle_ts < ?",
                     ((now_utc() - timedelta(hours=24)).isoformat(),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _maybe_enable_incremental_vacuum(self) -> None:
        """Turn on ``auto_vacuum=INCREMENTAL`` for a legacy DB, safely.

        The pragma only takes effect on a new file or after a full ``VACUUM``.
        A full VACUUM needs ~2x the DB size in free space; on the 93 GB live file
        that is impossible, so it is skipped (deletes still reclaim space via
        ``incremental_vacuum`` after a fresh file is created by the operator).
        """
        try:
            mode = self.query_one("PRAGMA auto_vacuum")
            if mode is not None and int(mode[0]) == 2:
                return
            size = self.path.stat().st_size if self.path.exists() else 0
            free = shutil.disk_usage(str(self.path.parent)).free
            if size == 0 or size * 2.2 < free:
                self._conn.execute("VACUUM")
                self._conn.commit()
            else:
                log.warning("store: auto_vacuum not enabled (db %d bytes, free %d); "
                            "incremental_vacuum will be a no-op until a fresh DB exists",
                            size, free)
        except Exception as exc:  # noqa: BLE001 - maintenance must never block startup
            log.warning("store: could not check auto_vacuum: %s", exc)

    # -- low-level ----------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- snapshots ----------------------------------------------------------
    def save_snapshot(self, trading_day: date, mode: str, cycle_ts: datetime,
                      data_grade: str, quality: dict[str, Any],
                      payload_hash: str, n_symbols: int) -> int:
        cur = self.execute(
            """INSERT OR REPLACE INTO snapshots
               (trading_day, mode, cycle_ts, data_grade, quality_json,
                payload_hash, n_symbols)
               VALUES (?,?,?,?,?,?,?)""",
            (trading_day.isoformat(), mode, _iso(cycle_ts), data_grade,
             stable_json(quality), payload_hash, n_symbols))
        return cur.lastrowid or 0

    def latest_snapshot(self, trading_day: date) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT * FROM snapshots WHERE trading_day=? ORDER BY cycle_ts DESC LIMIT 1",
            (trading_day.isoformat(),))

    # -- candidates ---------------------------------------------------------
    def save_candidate(self, trading_day: date, mode: str, cycle_ts: datetime,
                       symbol: str, score: Optional[float], rank: Optional[int],
                       passed_gates: bool, gates: dict[str, Any],
                       features: dict[str, Any], suppression: str = "") -> None:
        # Overwrite per-symbol state: a symbol gets one row per (day, mode) holding
        # its *latest* cycle state. Only the top ``CAND_JSON_TOP_N`` ranked names
        # are ever persisted by the runtime (the caller), so this cannot grow
        # unbounded even on 5-second ticks. Keeps the legacy replay path working
        # (latest cycle per symbol is queryable by cycle_ts).
        sym = symbol.upper()
        self.execute(
            "DELETE FROM candidates WHERE trading_day=? AND mode=? AND symbol=?",
            (trading_day.isoformat(), mode, sym))
        self.execute(
            """INSERT INTO candidates
               (trading_day, mode, cycle_ts, symbol, score, rank, passed_gates,
                gate_json, feature_json, suppression)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (trading_day.isoformat(), mode, _iso(cycle_ts), sym,
             score, rank, 1 if passed_gates else 0, stable_json(gates),
             stable_json(features), suppression))

    # -- decisions ----------------------------------------------------------
    def log_decision(self, trading_day: date, mode: str, cycle_ts: datetime,
                     symbol: str, prev_state: str, new_state: str,
                     reason: str, detail: str = "") -> None:
        self.execute(
            """INSERT INTO decisions
               (trading_day, mode, cycle_ts, symbol, prev_state, new_state,
                reason, detail, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (trading_day.isoformat(), mode, _iso(cycle_ts), symbol.upper(),
             prev_state, new_state, reason, detail, _iso(now_utc())))

    # -- alerts -------------------------------------------------------------
    def save_alert(self, trading_day: date, mode: str, cycle_ts: datetime,
                   symbol: str, kind: str, budget_slot: str, score: Optional[float],
                   rank: Optional[int], ref_price: Optional[float],
                   vwap: Optional[float], spread_pct: Optional[float],
                   reason: str, message_hash: str, sent: bool) -> int:
        cur = self.execute(
            """INSERT INTO alerts
               (trading_day, mode, cycle_ts, symbol, kind, budget_slot, score,
                rank, ref_price, vwap, spread_pct, reason, message_hash, sent,
                created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trading_day.isoformat(), mode, _iso(cycle_ts), symbol.upper(), kind,
             budget_slot, score, rank, ref_price, vwap, spread_pct, reason,
             message_hash, 1 if sent else 0, _iso(now_utc())))
        return cur.lastrowid or 0

    def alerts_for_day(self, trading_day: date, kind: Optional[str] = None) -> list[sqlite3.Row]:
        if kind:
            return self.query("SELECT * FROM alerts WHERE trading_day=? AND kind=? "
                              "ORDER BY cycle_ts", (trading_day.isoformat(), kind))
        return self.query("SELECT * FROM alerts WHERE trading_day=? ORDER BY cycle_ts",
                          (trading_day.isoformat(),))

    def count_alerts(self, trading_day: date, kind: str = "IGNITION") -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM alerts WHERE trading_day=? AND kind=?",
                             (trading_day.isoformat(), kind))
        return int(row["n"]) if row else 0

    def reset_budget(self, trading_day: date, kind: str = "IGNITION") -> int:
        """Clear today's spent slots (Telegram ``/resetbudget``). Returns rows removed."""
        cur = self.execute("DELETE FROM alerts WHERE trading_day=? AND kind=?",
                           (trading_day.isoformat(), kind))
        return cur.rowcount

    # -- outcomes -----------------------------------------------------------
    def save_outcome(self, alert_id: int, symbol: str, trading_day: date,
                     checkpoint: str, ts: datetime, price: Optional[float],
                     ret_pct: Optional[float], max_gain_pct: Optional[float],
                     max_dd_pct: Optional[float], hit_gain_first: Optional[bool],
                     detail: str = "") -> None:
        self.execute(
            """INSERT OR REPLACE INTO outcomes
               (alert_id, symbol, trading_day, checkpoint, ts, price, ret_pct,
                max_gain_pct, max_dd_pct, hit_gain_first, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (alert_id, symbol.upper(), trading_day.isoformat(), checkpoint, _iso(ts),
             price, ret_pct, max_gain_pct, max_dd_pct,
             None if hit_gain_first is None else (1 if hit_gain_first else 0), detail))

    # -- catalysts ----------------------------------------------------------
    def save_catalyst(self, symbol: str, item_id: str, kind: str, verdict: str,
                      confidence: Optional[float], headline: str, summary: str,
                      model: str) -> None:
        self.execute(
            """INSERT OR REPLACE INTO catalysts
               (symbol, item_id, kind, verdict, confidence, headline, summary,
                model, decided_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (symbol.upper(), item_id, kind, verdict, confidence, headline, summary,
             model, _iso(now_utc())))

    def get_catalyst(self, symbol: str, item_id: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM catalysts WHERE symbol=? AND item_id=?",
                              (symbol.upper(), item_id))

    # -- runner list / miss report -----------------------------------------
    def save_runner(self, trading_day: date, symbol: str, prior_close: Optional[float],
                    day_high: Optional[float], gain_pct: Optional[float],
                    dollar_vol: Optional[float], illiquid: bool, in_universe: bool,
                    first_seen: Optional[datetime], detail: str = "") -> None:
        self.execute(
            """INSERT OR REPLACE INTO runner_list
               (trading_day, symbol, prior_close, day_high, gain_pct, dollar_vol,
                illiquid, in_universe, first_seen, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (trading_day.isoformat(), symbol.upper(), prior_close, day_high,
             gain_pct, dollar_vol, 1 if illiquid else 0, 1 if in_universe else 0,
             _iso(first_seen) if first_seen else None, detail))

    def runners_for_day(self, trading_day: date) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM runner_list WHERE trading_day=? "
                          "ORDER BY gain_pct DESC", (trading_day.isoformat(),))

    def save_miss_report(self, trading_day: date, body: dict[str, Any]) -> None:
        self.execute(
            "INSERT OR REPLACE INTO miss_report(trading_day, generated_at, body_json) "
            "VALUES (?,?,?)",
            (trading_day.isoformat(), _iso(now_utc()), stable_json(body)))

    def get_miss_report(self, trading_day: date) -> Optional[dict[str, Any]]:
        row = self.query_one("SELECT body_json FROM miss_report WHERE trading_day=?",
                             (trading_day.isoformat(),))
        return json.loads(row["body_json"]) if row else None

    # -- events / raw -------------------------------------------------------
    def log_event(self, level: str, source: str, message: str) -> None:
        self.execute("INSERT INTO events(ts, level, source, message) VALUES (?,?,?,?)",
                     (_iso(now_utc()), level, source, message))

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def save_raw(self, trading_day: date, cycle_ts: datetime, source: str,
                 kind: str, body: Any) -> None:
        self.execute("INSERT INTO raw_payloads(trading_day, cycle_ts, source, kind, body) "
                     "VALUES (?,?,?,?,?)",
                     (trading_day.isoformat(), _iso(cycle_ts), source, kind,
                      body if isinstance(body, str) else stable_json(body)))

    # -- subscribers --------------------------------------------------------
    def add_subscriber(self, chat_id: str, days: int, label: str = "") -> date:
        """Insert or renew a read-only subscriber. Returns the expiry date.

        ``days`` counts from today (UTC), matching the ``date('now')`` comparison
        used by :meth:`get_active_subscribers`.
        """
        cid = str(chat_id).strip()
        if not cid:
            raise ValueError("subscriber: empty chat_id")
        days = int(days)
        if days < 1:
            raise ValueError("subscriber: days must be >= 1")
        expires = now_utc().date() + timedelta(days=days)
        self.execute(
            """INSERT INTO subscribers(chat_id, expires_at, label, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   expires_at=excluded.expires_at,
                   label=CASE WHEN excluded.label != '' THEN excluded.label
                              ELSE subscribers.label END""",
            (cid, expires.isoformat(), label.strip(), _iso(now_utc())))
        return expires

    def remove_subscriber(self, chat_id: str) -> bool:
        cur = self.execute("DELETE FROM subscribers WHERE chat_id=?", (str(chat_id).strip(),))
        return cur.rowcount > 0

    def get_active_subscribers(self) -> list[str]:
        """Chat ids whose subscription has not lapsed (UTC calendar date)."""
        rows = self.query(
            "SELECT chat_id FROM subscribers WHERE date('now') <= expires_at "
            "ORDER BY created_at")
        return [str(r["chat_id"]) for r in rows]

    def is_active(self, chat_id: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM subscribers WHERE chat_id=? AND date('now') <= expires_at",
            (str(chat_id).strip(),))
        return row is not None

    def subscriber_expiry(self, chat_id: str) -> Optional[str]:
        """The stored expiry for a known chat (active or lapsed), else ``None``."""
        row = self.query_one("SELECT expires_at FROM subscribers WHERE chat_id=?",
                             (str(chat_id).strip(),))
        return str(row["expires_at"]) if row else None

    def get_subscriber(self, chat_id: str) -> Optional[sqlite3.Row]:
        """The whole row for a known chat (active or lapsed), else ``None``."""
        return self.query_one("SELECT * FROM subscribers WHERE chat_id=?",
                              (str(chat_id).strip(),))

    def all_subscribers(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM subscribers ORDER BY expires_at DESC")

    def get_all_subscribers(self) -> list[dict[str, Any]]:
        """Every subscriber row with ``days_left`` and ``is_active`` computed.

        ``days_left`` is a plain calendar difference (today -> expiry); a lapsed
        row is negative, not clamped, so the caller can say "expired 3 days ago".
        """
        today = now_utc().date()
        out: list[dict[str, Any]] = []
        for r in self.all_subscribers():
            exp = str(r["expires_at"])
            try:
                days_left = (date.fromisoformat(exp) - today).days
            except ValueError:
                days_left = -1
            out.append({"chat_id": str(r["chat_id"]), "expires_at": exp,
                        "label": str(r["label"] or ""), "days_left": days_left,
                        "is_active": days_left >= 0})
        return out

    # -- expiry reminders ----------------------------------------------------
    def due_expiry_reminders(self, today: Optional[date] = None) -> list[dict[str, Any]]:
        """Subscribers whose expiry matches a reminder milestone, with days left.

        Returns one row per (chat, milestone) that is *due now*: a subscriber
        with exactly ``7``, ``3``, ``1`` or ``0`` days left. A milestone that
        was already sent for this expiry is excluded, so the daily pass is
        idempotent and never repeats itself. Rows whose milestone is not due
        today are omitted entirely (the cadence only fires on the exact day).

        ``days_left`` is a UTC calendar difference, matching ``expires_at`` and
        the same convention as :meth:`get_all_subscribers`.
        """
        day = today or now_utc().date()
        out: list[dict[str, Any]] = []
        for r in self.all_subscribers():
            cid = str(r["chat_id"])
            # A pre-registered "@handle" row has no numeric id yet; the Bot API
            # cannot address it, so it can never receive a reminder.
            if not cid or cid.startswith("@"):
                continue
            exp = str(r["expires_at"])
            try:
                days_left = (date.fromisoformat(exp) - day).days
            except ValueError:
                continue  # malformed expiry: never guess a milestone
            if days_left not in REMINDER_MILESTONES:
                continue
            sent = self.query_one(
                "SELECT 1 FROM expiry_reminders_sent "
                "WHERE chat_id=? AND milestone=? AND expires_at=?",
                (cid, days_left, exp))
            if sent is not None:
                continue  # already reminded for this milestone on this expiry
            out.append({"chat_id": cid, "expires_at": exp, "days_left": days_left,
                        "label": str(r["label"] or "")})
        return out

    def mark_expiry_reminder(self, chat_id: str, milestone: int,
                             expires_at: str) -> None:
        """Record that ``milestone`` was delivered for one subscriber's expiry.

        Idempotent: re-marking the same triple is a no-op, which keeps a
        retry-after-crash from ever double-sending.
        """
        cid = str(chat_id).strip()
        if not cid:
            raise ValueError("expiry reminder: empty chat_id")
        self.execute(
            """INSERT OR IGNORE INTO expiry_reminders_sent
               (chat_id, milestone, expires_at, sent_at) VALUES (?,?,?,?)""",
            (cid, int(milestone), str(expires_at), _iso(now_utc())))

    def expiry_reminder_sent(self, chat_id: str, milestone: int,
                             expires_at: str) -> bool:
        """True when this exact milestone was already delivered."""
        return self.query_one(
            "SELECT 1 FROM expiry_reminders_sent "
            "WHERE chat_id=? AND milestone=? AND expires_at=?",
            (str(chat_id).strip(), int(milestone), str(expires_at))) is not None

    def last_expiry_reminder(self, chat_id: str,
                             expires_at: Optional[str] = None) -> Optional[dict[str, Any]]:
        """The most recent reminder row for a chat (optionally one expiry)."""
        if expires_at is None:
            row = self.query_one(
                "SELECT * FROM expiry_reminders_sent WHERE chat_id=? "
                "ORDER BY sent_at DESC LIMIT 1", (str(chat_id).strip(),))
        else:
            row = self.query_one(
                "SELECT * FROM expiry_reminders_sent WHERE chat_id=? AND expires_at=? "
                "ORDER BY sent_at DESC LIMIT 1",
                (str(chat_id).strip(), str(expires_at)))
        if row is None:
            return None
        return {"chat_id": str(row["chat_id"]), "milestone": int(row["milestone"]),
                "expires_at": str(row["expires_at"]), "sent_at": str(row["sent_at"])}

    # -- seen users / access requests ---------------------------------------
    def record_seen_user(self, chat_id: str, username: str = "",
                         first_name: str = "") -> None:
        """Upsert a chat that messaged or tapped a button (id + @username).

        An empty ``username``/``first_name`` on a later update never erases a
        value already known: users who hide their username would otherwise
        wipe the only handle the owner has.
        """
        cid = str(chat_id).strip()
        if not cid:
            return
        user = str(username or "").strip().lstrip("@").lower()
        name = str(first_name or "").strip()
        self.execute(
            """INSERT INTO seen_users(chat_id, username, first_name, status, updated_at)
               VALUES (?,?,?,'SEEN',?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   username=CASE WHEN excluded.username != '' THEN excluded.username
                                 ELSE seen_users.username END,
                   first_name=CASE WHEN excluded.first_name != '' THEN excluded.first_name
                                   ELSE seen_users.first_name END,
                   updated_at=excluded.updated_at""",
            (cid, user, name, _iso(now_utc())))

    def get_seen_user(self, chat_id: str) -> Optional[dict[str, Any]]:
        row = self.query_one("SELECT * FROM seen_users WHERE chat_id=?",
                             (str(chat_id).strip(),))
        return dict(row) if row else None

    def set_request_status(self, chat_id: str, status: str) -> None:
        """Set SEEN/PENDING/APPROVED/REJECTED for a chat (inserted if unknown)."""
        cid = str(chat_id).strip()
        if not cid:
            return
        self.execute(
            """INSERT INTO seen_users(chat_id, username, first_name, status, updated_at)
               VALUES (?, '', '', ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET status=excluded.status,
                                                 updated_at=excluded.updated_at""",
            (cid, str(status).upper(), _iso(now_utc())))

    def get_request_status(self, chat_id: str) -> str:
        row = self.query_one("SELECT status FROM seen_users WHERE chat_id=?",
                             (str(chat_id).strip(),))
        return str(row["status"]) if row else "SEEN"

    def resolve_target_to_chat_id(self, target: str) -> Optional[tuple[str, str]]:
        """Resolve a numeric id or ``@username`` to ``(chat_id, label)``.

        Numeric targets resolve to themselves (label = known @username, if any)
        so the owner can always add an id even before the user says anything.
        A username must have messaged the bot at least once: Telegram cannot
        open a chat from a handle alone, so an unknown handle returns ``None``
        and the caller stores a pending ``@handle`` row instead.
        """
        t = str(target or "").strip()
        if not t:
            return None
        numeric = t[1:] if t.startswith("-") else t
        if numeric.isdigit():
            seen = self.get_seen_user(t)
            if seen and seen.get("username"):
                return t, f"@{seen['username']}"
            return t, (seen.get("first_name") or "") if seen else ""
        handle = t.lstrip("@").lower()
        if not handle:
            return None
        seen = self.query_one(
            "SELECT chat_id, username FROM seen_users "
            "WHERE LOWER(username)=? ORDER BY updated_at DESC LIMIT 1", (handle,))
        if seen:
            return str(seen["chat_id"]), f"@{seen['username'] or handle}"
        row = self.query_one(
            "SELECT chat_id FROM subscribers WHERE LOWER(label)=? "
            "OR LOWER(chat_id)=? LIMIT 1",
            (f"@{handle}", f"@{handle}"))
        if row:
            return str(row["chat_id"]), f"@{handle}"
        return None

    def find_pending_username(self, username: str) -> Optional[str]:
        """The placeholder ``@handle`` subscriber row for a username, if any."""
        handle = str(username or "").strip().lstrip("@").lower()
        if not handle:
            return None
        row = self.query_one(
            "SELECT chat_id FROM subscribers WHERE LOWER(chat_id)=? LIMIT 1",
            (f"@{handle}",))
        return str(row["chat_id"]) if row else None

    def link_pending_username(self, username: str, chat_id: str) -> Optional[date]:
        """Migrate a pending ``@handle`` row to a numeric chat id (on /start).

        Returns the expiry date when a row was moved, else ``None``.
        """
        pending = self.find_pending_username(username)
        if not pending:
            return None
        expires = self.subscriber_expiry(pending)
        if not expires:
            return None
        try:
            exp = date.fromisoformat(expires)
        except ValueError:
            return None
        handle = f"@{str(username).strip().lstrip('@').lower()}"
        # A numeric row may already exist (owner added the id directly). Never
        # shorten it: keep whichever expiry is later.
        existing = self.subscriber_expiry(chat_id)
        if existing:
            try:
                exp = max(exp, date.fromisoformat(existing))
            except ValueError:
                pass
            expires = exp.isoformat()
        self.execute("DELETE FROM subscribers WHERE chat_id=?", (pending,))
        # Direct insert keeps the exact expiry the owner granted.
        self.execute(
            """INSERT INTO subscribers(chat_id, expires_at, label, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET expires_at=excluded.expires_at,
                                                 label=excluded.label""",
            (str(chat_id).strip(), expires, handle, _iso(now_utc())))
        return exp

    def is_active_subscriber(self, chat_id: str) -> bool:
        """Alias of :meth:`is_active`, used by the request/approval flow."""
        return self.is_active(chat_id)

    # -- retained maintenance ----------------------------------------------
    def prune(self, raw_retention_days: int, today: Optional[date] = None) -> dict[str, int]:
        """Delete raw payloads and per-cycle candidate rows past retention.

        Only ``raw_payloads`` and ``candidates`` are pruned here; ``maintain``
        also trims the hot tables (24h raw, 48h non-board candidates/decisions).
        ``snapshots``, ``alerts``, ``outcomes`` and ``runner_list`` are the
        grading/MISS-REPORT record and are kept (they are compact).
        """
        cutoff = ((today or now_utc().date()) - timedelta(days=raw_retention_days)).isoformat()
        removed: dict[str, int] = {}
        for table in ("raw_payloads", "candidates"):
            cur = self.execute(f"DELETE FROM {table} WHERE trading_day < ?", (cutoff,))
            removed[table] = cur.rowcount
        return removed

    def maintain(self, cfg: Any, today: Optional[date] = None) -> dict[str, int]:
        """Periodic retention + space reclaim (item 6).

        Deletes raw payloads older than ``RAW_RETENTION_HOURS`` and
        candidates/decisions older than ``CAND_RETENTION_HOURS``, then returns
        freed pages to the OS and truncates the WAL. Runs off the decision path;
        ``DB_PRUNE_INTERVAL_S`` (default 300s) controls the cadence.
        """
        day = today or now_utc().date()
        raw_h = cfg.int("RAW_RETENTION_HOURS", 2)
        cand_h = cfg.int("CAND_RETENTION_HOURS", 2)
        removed: dict[str, int] = {}
        raw_cut = (now_utc() - timedelta(hours=raw_h)).isoformat()
        cand_cut = (now_utc() - timedelta(hours=cand_h)).isoformat()
        removed["raw_payloads"] = self.execute(
            "DELETE FROM raw_payloads WHERE cycle_ts < ?", (raw_cut,)).rowcount
        removed["candidates"] = self.execute(
            "DELETE FROM candidates WHERE cycle_ts < ?", (cand_cut,)).rowcount
        removed["decisions"] = self.execute(
            "DELETE FROM decisions WHERE cycle_ts < ? "
            "AND new_state NOT IN ('BOARD','ARMED','IGNITION') "
            "AND prev_state NOT IN ('BOARD','ARMED','IGNITION')", (cand_cut,)).rowcount
        # Snapshots are only needed for the in-process warm-boot window; each row
        # carries the tracked pool's quality blob.
        removed["snapshots"] = self.execute(
            "DELETE FROM snapshots WHERE cycle_ts < ?", (raw_cut,)).rowcount
        removed.update(self.prune(cfg.int("RAW_RETENTION_DAYS", 7), day))
        # Aggressive reclaim: candidates are overwritten per symbol, so the table
        # is tiny; 2h retention is enough for replay/debugging. Free the pages
        # every pass so rotd.db stays small (was: 72 GB in a day).
        try:
            self._conn.execute("PRAGMA incremental_vacuum(10000)")
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning("store: vacuum/checkpoint failed: %s", exc)
        if any(removed.values()):
            log.info("store: maintain removed %s", removed)
        return removed

    # -- meta ---------------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))
