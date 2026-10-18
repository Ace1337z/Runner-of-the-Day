"""Regression tests for the ten permanent upgrades (no network).

Pins the behaviours that would silently rot: SSE/`<think>` parsing, the POST
price line, retention + space reclaim, warm boot, the backside-of-HOD block,
and the Telegram budget commands.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from rotd.auditor import model_text, strip_reasoning
from rotd.catalyst import CatalystService
from rotd.config import Config
from rotd.notifier import Notifier, render_ignition
from rotd.normalize import Canonical
from rotd.runtime import Runtime
from rotd.scoring import ComponentScore, Scored
from rotd.state_machine import R_BACKSIDE_OF_HOD, State, StateMachine
from rotd.store import Store
from rotd.telegram import FakeTelegram, SETTABLE_KEYS, handle_command
from rotd.util import UTC, now_utc

DAY = date(2026, 10, 13)
T0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


# --- item 4: the gateway streams SSE even when asked not to ----------------

def test_model_text_reads_sse_and_ignores_reasoning():
    body = (
        'data: {"choices":[{"delta":{"reasoning":"thinking..."}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"{\\"a\\":"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"1}"}}]}\n\n'
        'data: [DONE]\n'
    )
    assert model_text(body, "text/event-stream") == '{"a":1}'
    assert model_text('{"choices":[{"message":{"content":"<think>no</think>{\\"b\\":2}"}}]}') \
        == '{"b":2}'
    assert strip_reasoning("```json\n{\"c\":3}\n```").startswith("```json")


# --- item 3: POST price line is the after-hours move -----------------------

def test_ignition_post_price_line_shows_ah_move():
    sc = Scored(symbol="ZZZ", score=88.0,
                components={"TAPE": ComponentScore("TAPE", 0.9, 30, 27)})
    rec = Canonical(symbol="ZZZ", price=0.29, change_pct=20.8, spread_pct=1.1,
                    ah_change_pct=2.5, vol_3m=120_000, dollar_volume_10m=34_800)
    text = render_ignition("ZZZ", sc, rec, "1/5", "POST", ["ARM"], now=now_utc())
    assert "Price $0.2900 (+2.5% AH | +20.8% vs prev close)" in text
    assert "3m Vol" in text and "10m Dollar-Vol" in text


# --- item 6: retention + reclaim ------------------------------------------

def test_maintain_trims_hot_tables(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    old = now_utc() - timedelta(days=3)
    store.save_candidate(DAY, "OPEN", old, "AAA", 50.0, 900, False, {}, {})
    store.log_decision(DAY, "OPEN", old, "AAA", "QUALIFIED", "WATCH", "X", "")
    store.save_alert(DAY, "OPEN", old, "AAA", "IGNITION", "1/5", 80.0, 1, 2.0,
                     None, 1.0, "ARM", "h", 1)
    removed = store.maintain(Config(path=tmp_path / "c.env"))
    assert removed["decisions"] == 1
    assert store.count_alerts(DAY, "IGNITION") == 1, "alerts are the grading record"


def test_reset_budget_clears_today_only(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    for _ in range(2):
        store.save_alert(DAY, "OPEN", T0, "AAA", "IGNITION", "1/5", 80.0, 1, 2.0,
                         None, 1.0, "ARM", "h", 1)
    assert store.reset_budget(DAY) == 2
    assert store.count_alerts(DAY, "IGNITION") == 0


# --- item 7: warm boot ----------------------------------------------------

def test_warm_boot_restores_volumes_states_and_budget(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    now = now_utc().replace(microsecond=0)
    day = now.date()
    for i in range(3):
        store.save_snapshot(day, "OPEN", now - timedelta(minutes=10 - i * 4), "A",
                            {"volumes": {"EXMP": 1_000_000 + i * 10_000},
                             "prices": {"EXMP": 2.30}}, "h", 1)
    store.log_decision(day, "OPEN", now - timedelta(minutes=5), "EXMP",
                       "BOARD", "ARMED", "ARM", "")
    store.save_alert(day, "OPEN", now - timedelta(minutes=5), "EXMP", "IGNITION",
                     "1/5", 88.0, 1, 2.30, None, 0.9, "ARM", "h", 1)

    cfg = Config(path=tmp_path / "c.env")
    rt = Runtime(cfg, store, {}, Notifier(cfg, FakeTelegram(cfg), store),
                 CatalystService(cfg, store))
    rt._ensure_day(day)

    assert len(rt._vol_history["EXMP"]) == 3
    assert rt._history["EXMP"]["vol_3m"] == 10_000.0
    assert rt._history["EXMP"]["vwap"] == 2.30
    assert rt.sm.states["EXMP"].state == State.ARMED
    assert rt.sm.budget.used == 1


# --- item 8: backside of HOD blocks the ignition --------------------------

def test_backside_of_hod_blocks_ignition(cfg: Config):
    from rotd.features import compute_features
    from rotd.gates import run_gates
    from rotd.scoring import rank, score_candidate

    cfg._values["QUALIFY_CONSEC"] = "1"
    cfg._values["ARM_MIN_SCORE"] = "0"
    cfg._values["ARM_MAX_RANK"] = "5"
    cfg._values["REG_CONFIRM_BARS"] = "50"  # park in ARMED
    rec = Canonical(symbol="EXMP", price=2.31, prev_close=1.90, open=2.00,
                    volume=3_000_000, volume_basis="SINGLE_VENUE", bid=2.30,
                    ask=2.32, spread_pct=0.9, day_high=2.40, day_low=1.98,
                    shares_float=4_000_000, short_float=22.0,
                    market_cap=9_240_000, company="Exemplar Inc", data_grade="A",
                    fetched_at=T0)
    snap = type("S", (), {"records": {"EXMP": rec}, "data_grade": "A",
                          "quality": {}, "trading_day": DAY, "mode": "OPEN"})()
    fs = compute_features(rec, cfg, snap)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    rk = rank([sc], cfg)
    sm = StateMachine(cfg, DAY)
    live = {"EXMP": {"vol_3m": 1_000_000.0, "dvol_10m": 10_000_000.0}}
    for i in range(4):
        sm.step(rk, snap.records, "OPEN", now=T0 + timedelta(seconds=30 * i),
                prices={"EXMP": 2.31}, live=live)
    # Collapse the price to 20% below the high: front-side only.
    rec.price = 2.40 * (1 - 0.20)
    fs = compute_features(rec, cfg, snap)
    rep = run_gates(rec, fs, cfg, "OPEN", snap)
    sc = score_candidate(rec, fs, rep, cfg, "OPEN")
    rk = rank([sc], cfg)
    out = sm.step(rk, snap.records, "OPEN", now=T0 + timedelta(seconds=200),
                  prices={"EXMP": rec.price}, live=live)
    assert any(t.reason == R_BACKSIDE_OF_HOD for t in out), \
        [(t.symbol, t.reason) for t in out]


# --- item 5: Telegram keys and budget -------------------------------------

def test_settable_keys_are_the_eight_allowed():
    assert set(SETTABLE_KEYS) == {
        "FINVIZ_TOKEN", "FINNHUB_KEY", "ALPHA_VANTAGE_KEY", "ALPACA_KEY",
        "ALPACA_SECRET", "AI_KEY", "AI_BASE_URL", "AI_MODEL"}


def test_setbudget_replies_with_used_count(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "1")
    tg = FakeTelegram(cfg)
    reply = handle_command(tg, "/setbudget 10", "1", cfg)
    assert reply == "Budget updated: 0/10 used today"
    assert cfg.raw("ALERT_BUDGET_PER_DAY") == "10"


def test_setkey_writes_atomically_and_marks_dirty(tmp_path: Path):
    path = tmp_path / "c.env"
    cfg = Config(path=path)
    cfg.set("TELEGRAM_CHAT_ID", "1")
    tg = FakeTelegram(cfg)
    reply = handle_command(tg, "/setkey FINNHUB_KEY abc123", "1", cfg)
    assert reply and "FINNHUB_KEY" in reply
    assert path.read_text(encoding="utf-8").strip().endswith("FINNHUB_KEY=abc123")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert cfg.take_dirty() is True
    assert cfg.take_dirty() is False


# --- gateway compatibility -------------------------------------------------

def test_prompt_is_merged_into_user_turn(cfg: Config):
    """The gateway drops role="system"; the prompt must ride in the user turn."""
    sent: dict = {}

    class FakeResp:
        text = '{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'
        headers = {"Content-Type": "application/json"}

        def raise_for_status(self):
            return None

    import requests
    real = requests.post
    requests.post = lambda url, **kw: (sent.update(kw), FakeResp())[1]
    try:
        from rotd.auditor import http_caller
        http_caller(cfg, "SYSTEM_MARKER", "USER_MARKER")
    finally:
        requests.post = real
    msgs = sent["json"]["messages"]
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    assert "SYSTEM_MARKER" in msgs[0]["content"] and "USER_MARKER" in msgs[0]["content"]


def test_every_prompt_cap_is_known_to_the_validator():
    """A prompt §7.2 code the validator rejects silently degrades audits to rules-only."""
    from rotd.auditor import CAP_MAX_VERDICT, prompt_cap_codes
    assert set(prompt_cap_codes()) <= set(CAP_MAX_VERDICT)


def test_setup_noninteractive_writes_every_key_from_the_environment(tmp_path, monkeypatch):
    """`FINVIZ_TOKEN=... bash install.sh` must actually pre-seed the config."""
    import argparse
    from rotd.cli import cmd_setup

    cfg = Config(path=tmp_path / "rotd.env")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("FINVIZ_TOKEN", "fv")
    monkeypatch.setenv("AI_MODEL", "m")
    monkeypatch.setenv("NOT_A_ROTD_SETTING", "ignored")
    args = argparse.Namespace(non_interactive=True, skip_doctor=True)
    assert cmd_setup(cfg, args) == 0
    written = cfg.path.read_text()
    for line in ("TELEGRAM_TOKEN=t", "FINVIZ_TOKEN=fv", "AI_MODEL=m"):
        assert line in written
    assert "NOT_A_ROTD_SETTING" not in written
