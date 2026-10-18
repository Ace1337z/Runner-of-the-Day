"""SQLite persistence for ROTD.

One file (``rotd.db`` by default). The schema (build prompt §13) stores:

- ``snapshots``   — one reconciled snapshot per cycle (the single source of truth
  the decision path reads), keyed by (trading_day, mode, cycle_ts).
- ``candidates``  — per-cycle per-symbol candidate rows (score, gates, reasons).
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
"""


def _iso(dt: Optional[datetime]) -> str:
    return as_utc(dt or now_utc()).isoformat()


class Store:
    """Thread-safe SQLite wrapper with a small, explicit API."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
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
        self.execute(
            """INSERT OR REPLACE INTO candidates
               (trading_day, mode, cycle_ts, symbol, score, rank, passed_gates,
                gate_json, feature_json, suppression)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (trading_day.isoformat(), mode, _iso(cycle_ts), symbol.upper(),
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
        """Hourly/startup retention + space reclaim (item 6).

        Deletes raw payloads older than ``RAW_RETENTION_HOURS`` and non-board
        candidates/decisions older than ``CAND_RETENTION_HOURS``, then returns
        freed pages to the OS in small chunks. Runs off the decision path.
        """
        day = today or now_utc().date()
        raw_h = cfg.int("RAW_RETENTION_HOURS", 24)
        cand_h = cfg.int("CAND_RETENTION_HOURS", 48)
        removed: dict[str, int] = {}
        raw_cut = (now_utc() - timedelta(hours=raw_h)).isoformat()
        cand_cut = (now_utc() - timedelta(hours=cand_h)).isoformat()
        removed["raw_payloads"] = self.execute(
            "DELETE FROM raw_payloads WHERE cycle_ts < ?", (raw_cut,)).rowcount
        removed["candidates"] = self.execute(
            "DELETE FROM candidates WHERE cycle_ts < ? AND (rank IS NULL OR rank > ?)",
            (cand_cut, cfg.int("CAND_JSON_TOP_N", 50))).rowcount
        removed["decisions"] = self.execute(
            "DELETE FROM decisions WHERE cycle_ts < ? "
            "AND new_state NOT IN ('BOARD','ARMED','IGNITION') "
            "AND prev_state NOT IN ('BOARD','ARMED','IGNITION')", (cand_cut,)).rowcount
        # Snapshots are only needed for the in-process warm-boot window; each row
        # carries the tracked pool's quality blob.
        removed["snapshots"] = self.execute(
            "DELETE FROM snapshots WHERE cycle_ts < ?", (raw_cut,)).rowcount
        removed.update(self.prune(cfg.int("RAW_RETENTION_DAYS", 7), day))
        try:
            self._conn.execute("PRAGMA incremental_vacuum(2000)")
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
