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
from rotd.normalize import Canonical
from rotd.notifier import Notifier, render_ignition
from rotd.runtime import Runtime
from rotd.scoring import ComponentScore, Scored
from rotd.state_machine import R_BACKSIDE_OF_HOD, State, StateMachine, Transition
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


# --- item 9 follow-up: a stale IEX book must not become a spread ----------

def test_apply_quotes_drops_stale_and_keeps_fresh(cfg: Config):
    """A quote older than QUOTE_MAX_AGE_S_* is skipped (DATA-03).

    The free IEX feed leaves the last book of the day up for hours; applying it
    would set a 10%-wide spread on a liquid name and break G-SPREAD/G-EXIT.
    """
    now = T0 + timedelta(hours=1)
    stale_ts = now - timedelta(hours=29)
    fresh_ts = now - timedelta(seconds=2)
    quotes = {
        "OLD": {"bid": 3.2876, "ask": 3.6343, "ts": stale_ts},
        "NEW": {"bid": 3.43, "ask": 3.44, "ts": fresh_ts},
    }

    class FakeAlpaca:
        def latest_quotes(self, syms):  # noqa: ANN001
            return {s: quotes[s] for s in syms if s in quotes}

    cfg._values["ALPACA_KEY"] = "k"
    rt = Runtime.__new__(Runtime)
    rt.cfg = cfg
    rt.clients = {"alpaca": FakeAlpaca()}
    snap = type("S", (), {"mode": "OPEN", "records": {
        "OLD": Canonical(symbol="OLD", price=3.43, fetched_at=now),
        "NEW": Canonical(symbol="NEW", price=3.43, fetched_at=now)}})()

    rt._apply_quotes(snap, ["OLD", "NEW"], now)

    assert snap.records["OLD"].bid is None and snap.records["OLD"].spread_pct is None
    assert snap.records["NEW"].bid == 3.43
    assert snap.records["NEW"].spread_pct is not None


def test_alpaca_bars_sends_a_start_and_health_uses_quotes(cfg: Config):
    """`bars` without a start returns `bars: null`; `health` must probe quotes."""
    from rotd.sources.alpaca import AlpacaClient

    cfg._values["ALPACA_KEY"] = "k"
    cfg._values["ALPACA_SECRET"] = "s"
    calls: list[dict] = []

    class Resp:
        status_code = 200

        def __init__(self, body):  # noqa: ANN001
            self._body = body

        def json(self):
            return self._body

    def fake_request(method, url, **kw):  # noqa: ANN001
        calls.append({"url": url, "params": kw.get("params") or {}})
        if "/bars" in url:
            return Resp({"bars": [{"t": "2026-10-16T19:59:00Z", "o": 1, "h": 2,
                                   "l": 1, "c": 2, "v": 100}]})
        return Resp({"quotes": {"AAPL": {"bp": 1.0, "ap": 1.01,
                                         "t": "2026-10-16T19:59:00Z"}}})

    al = AlpacaClient(cfg)
    al._request = fake_request  # type: ignore[method-assign]

    bars = al.bars("AAPL", limit=2)
    assert bars and bars[0]["close"] == 2.0
    assert "start" in calls[0]["params"]

    ok, msg = al.health()
    assert ok and msg == "ok"
    assert "/quotes/latest" in calls[-1]["url"]


# --- multi-user broadcast + tiered access ---------------------------------

def test_subscriber_lifecycle_and_expiry(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    expires = store.add_subscriber("222", 30, "Dave")
    assert expires == now_utc().date() + timedelta(days=30)
    assert store.get_active_subscribers() == ["222"]
    assert store.is_active("222") and not store.is_active("333")

    # Renewal extends the expiry and keeps the label when none is given.
    store.add_subscriber("222", 90)
    assert store.subscriber_expiry("222") == (now_utc().date()
                                              + timedelta(days=90)).isoformat()
    assert store.all_subscribers()[0]["label"] == "Dave"

    # A lapsed row is neither active nor gone.
    store.execute("UPDATE subscribers SET expires_at=? WHERE chat_id=?",
                  ((now_utc().date() - timedelta(days=1)).isoformat(), "222"))
    assert store.get_active_subscribers() == []
    assert store.subscriber_expiry("222") is not None
    assert store.remove_subscriber("222") is True
    assert store.remove_subscriber("222") is False
    store.close()


def test_notifier_broadcasts_to_owner_and_active_subscribers(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    store.add_subscriber("222", 10, "Dave")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    client = FakeTelegram(cfg)
    n = Notifier(cfg, client, store)

    assert n.ignition("ZZZ", Scored(symbol="ZZZ", score=88.0, components={}),
                      Canonical(symbol="ZZZ", price=1.0), "1/5", "OPEN", ["ARM"]) is not None
    assert [m["chat"] for m in client.sent] == ["111", "222"]

    # The owner must not get a duplicate copy if they are also a subscriber.
    store.add_subscriber("111", 10)
    client.sent.clear()
    n.miss_report({"trading_day": "2026-10-16", "runners": []})
    assert [m["chat"] for m in client.sent] == ["111", "222"]
    store.close()


def test_one_blocked_subscriber_does_not_stop_the_others(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    store.add_subscriber("222", 10)
    store.add_subscriber("333", 10)
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")

    class Flaky(FakeTelegram):
        def send(self, text, chat_id=None, disable_preview=True, markup=None, *, silent=False):
            if chat_id == "222":
                raise RuntimeError("403: bot was blocked by the user")
            return super().send(text, chat_id=chat_id, silent=silent)

    client = Flaky(cfg)
    Notifier(cfg, client, store).abort("ZZZ", Transition("ZZZ", State.ARMED,
                                                         State.ABORT, "FADE", "x"),
                                       None)
    assert [m["chat"] for m in client.sent] == ["111", "333"]
    store.close()


def test_tiered_command_access(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    store.add_subscriber("222", 10, "Dave")
    store.add_subscriber("333", 10, "Mike")
    store.execute("UPDATE subscribers SET expires_at=? WHERE chat_id=?",
                  ((now_utc().date() - timedelta(days=3)).isoformat(), "333"))
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    tg = FakeTelegram(cfg)
    ctx = {"store": store, "cfg": cfg}

    # Tier 1: owner keeps every admin command.
    assert tg.handle_command("/setbudget 6", "111", cfg, ctx).startswith("Budget updated")
    # Tier 2: a subscriber may read, never administer.
    assert tg.handle_command("/status", "222", cfg, ctx).startswith("ROTD status")
    assert tg.handle_command("/setkey FINNHUB_KEY x", "222", cfg, ctx) == "Unauthorized."
    assert tg.handle_command("/addsub 999 30", "222", cfg, ctx) == "Unauthorized."
    # Expired: told once, by date; admin commands are still refused, not dated.
    assert tg.handle_command("/setkey FINNHUB_KEY x", "333", cfg, ctx) == "Unauthorized."
    assert tg.handle_command("/status", "333", cfg, ctx) == (
        "⚠️ Your subscription expired on "
        f"{(now_utc().date() - timedelta(days=3)).isoformat()}. Contact admin to renew.")
    # Unknown: silent, no reply at all.
    assert tg.handle_command("/status", "444", cfg, ctx) is None
    assert tg.handle_command("/setkey FINNHUB_KEY x", "444", cfg, ctx) is None
    store.close()


def test_addsub_rmsub_and_subs_replies(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    tg = FakeTelegram(cfg)
    ctx = {"store": store, "cfg": cfg}
    exp = (now_utc().date() + timedelta(days=30)).isoformat()

    assert tg.handle_command("/addsub 222 30 Dave", "111", cfg, ctx) == (
        f"✅ Subscriber 222 (Dave) activated for 30 days (expires {exp}). "
        "Welcome playbook delivered.")
    assert tg.handle_command("/addsub 222 0", "111", cfg, ctx) == \
        "subscriber: days must be >= 1"
    listing = tg.handle_command("/subs", "111", cfg, ctx)
    assert "222 (Dave) — ACTIVE (30 days left, exp" in listing
    assert listing.endswith("Total Active: 1 | Total Registered: 1")
    assert tg.handle_command("/rmsub 222", "111", cfg, ctx) == "🗑️ Subscriber 222 removed."
    assert tg.handle_command("/rmsub 222", "111", cfg, ctx) == "no subscriber with chat id 222"
    store.close()


# --- welcome playbook + /broadcast ----------------------------------------

def test_addsub_delivers_the_welcome_playbook(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    tg = FakeTelegram(cfg)
    exp = (now_utc().date() + timedelta(days=30)).isoformat()

    tg.handle_command("/addsub 222 30 Dave", "111", cfg, {"store": store, "cfg": cfg})
    welcome = [m for m in tg.messages if m["chat"] == "222"]
    assert len(welcome) == 1
    assert "HOW TO TRADE IGNITION ALERTS" in welcome[0]["text"]
    assert exp in welcome[0]["text"]
    assert "30 day(s)" in welcome[0]["text"]
    store.close()


def test_addsub_welcome_failure_is_reported_not_fatal(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")

    class NoDm(FakeTelegram):
        def send(self, text, chat_id=None, disable_preview=True, markup=None, *, silent=False):
            if chat_id == "222":
                raise RuntimeError("400: chat not found")
            return super().send(text, chat_id=chat_id, silent=silent)

    tg = NoDm(cfg)
    exp = (now_utc().date() + timedelta(days=7)).isoformat()
    reply = tg.handle_command("/addsub 222 7", "111", cfg, {"store": store, "cfg": cfg})
    assert reply == (f"✅ Subscriber 222 (no label) activated for 7 days "
                     f"(expires {exp}). Note: Welcome playbook will be delivered "
                     "once the user sends /start to the bot.")
    assert store.is_active("222")  # the subscription still saved
    store.close()


def test_subscriber_start_and_help_return_the_playbook(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    tg = FakeTelegram(cfg)
    ctx = {"store": store, "cfg": cfg}
    store.add_subscriber("222", 10, "Dave")

    for cmd in ("/start", "/help"):
        reply = tg.handle_command(cmd, "222", cfg, ctx)
        assert "HOW TO TRADE IGNITION ALERTS" in reply
        assert "10 day(s)" in reply
        assert store.subscriber_expiry("222") in reply
        assert "/setkey" not in reply          # admin surface stays hidden
    # The owner still gets the admin menu, not the playbook.
    assert "/setkey" in tg.handle_command("/help", "111", cfg, ctx)
    store.close()


def test_broadcast_reaches_owner_and_active_subscribers(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    tg = FakeTelegram(cfg)
    store.add_subscriber("222", 10, "Dave")
    store.add_subscriber("333", 10, "Mike")
    store.execute("UPDATE subscribers SET expires_at=? WHERE chat_id=?",
                  ((now_utc().date() - timedelta(days=1)).isoformat(), "333"))
    ctx = {"store": store, "cfg": cfg}

    assert tg.handle_command("/broadcast ", "111", cfg, ctx) == \
        "⚠️ Usage: /broadcast <your announcement message>"
    tg.messages.clear()
    reply = tg.handle_command("/broadcast half day tomorrow, close at 13:00",
                              "111", cfg, ctx)
    assert reply == "📢 Broadcast sent! Delivered to 2 chat(s) (0 failed)."
    assert [m["chat"] for m in tg.messages] == ["111", "222"]
    assert all(m["text"].startswith("📢 ROTD ANNOUNCEMENT")
               and "close at 13:00" in m["text"] for m in tg.messages)

    # Subscribers are refused, unknowns are ignored.
    assert tg.handle_command("/broadcast hi", "222", cfg, ctx) == "⛔ Admin command only."
    assert tg.handle_command("/broadcast hi", "999", cfg, ctx) is None
    store.close()


def test_broadcast_counts_blocked_chats(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    store.add_subscriber("222", 10)
    store.add_subscriber("333", 10)

    class Flaky(FakeTelegram):
        def send(self, text, chat_id=None, disable_preview=True, markup=None, *, silent=False):
            if chat_id == "222":
                raise RuntimeError("403: bot was blocked by the user")
            return super().send(text, chat_id=chat_id, silent=silent)

    tg = Flaky(cfg)
    reply = tg.handle_command("/broadcast test", "111", cfg, {"store": store, "cfg": cfg})
    assert reply == "📢 Broadcast sent! Delivered to 2 chat(s) (1 failed)."
    assert [m["chat"] for m in tg.messages] == ["111", "333"]
    store.close()


def test_broadcast_sends_while_shadow_is_on(tmp_path: Path):
    """SHADOW suppresses decision pushes, not an explicit owner command."""
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    cfg.set("SHADOW", "1")
    tg = FakeTelegram(cfg)
    store.add_subscriber("222", 10)
    reply = tg.handle_command("/broadcast open at 09:30", "111", cfg,
                              {"store": store, "cfg": cfg})
    assert reply == "📢 Broadcast sent! Delivered to 2 chat(s) (0 failed)."
    assert [m["chat"] for m in tg.messages] == ["111", "222"]
    store.close()


def test_shadowstart_menu_name_is_accepted(tmp_path: Path):
    """Telegram strips the dash from command names; the handler must accept it."""
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    tg = FakeTelegram(cfg)
    ctx = {"store": store, "cfg": cfg}
    for cmd in ("/shadow-start", "/shadowstart"):
        reply = tg.handle_command(cmd, "111", cfg, ctx)
        assert reply.startswith("shadow clock started on "), reply
    store.close()


def test_finalize_never_targets_a_day_that_has_not_traded(tmp_path: Path, monkeypatch):
    """A daemon started on a weekend must not finalize the upcoming session.

    The A1 label has already rolled to Monday while the clock still says
    Saturday; the old "not is_session" test finalized Monday on the spot and then
    skipped its real MISS REPORT on Monday evening.
    """
    from types import SimpleNamespace
    from rotd import cli as cli_mod

    store = Store(tmp_path / "r.db")
    calls: list = []

    class RT:
        def __init__(self):
            self.store = store

        def finalize_day(self, day):
            calls.append(day)
            self.store.save_miss_report(day, {"runners": []})
            return {"runners": []}

    monkeypatch.setattr(cli_mod, "session_info",
                        lambda: SimpleNamespace(trading_day=date(2026, 10, 19),
                                                is_session=False))
    last = [date(2026, 10, 19)]          # weekend start, label already rolled
    cli_mod._finalize_if_closed(RT(), last)
    assert calls == [], calls

    # Once the label rolls for real, the completed day is finalized exactly once.
    store.save_snapshot(date(2026, 10, 16), "OPEN", now_utc(), "B",
                        {"half_day": False}, "h", 10)
    last = [date(2026, 10, 16)]
    cli_mod._finalize_if_closed(RT(), last)
    assert calls == [date(2026, 10, 16)]
    cli_mod._finalize_if_closed(RT(), last)
    assert calls == [date(2026, 10, 16)]
    store.close()


def test_finalize_skips_a_day_with_no_data(tmp_path: Path, monkeypatch):
    """Never fabricate a MISS REPORT for a label the daemon never observed."""
    from types import SimpleNamespace
    from rotd import cli as cli_mod

    store = Store(tmp_path / "r.db")
    calls: list = []

    class RT:
        def __init__(self):
            self.store = store

        def finalize_day(self, day):
            calls.append(day)
            return {"runners": []}

    monkeypatch.setattr(cli_mod, "session_info",
                        lambda: SimpleNamespace(trading_day=date(2026, 10, 19),
                                                is_session=True))
    cli_mod._finalize_if_closed(RT(), [date(2026, 10, 16)])
    assert calls == []
    store.close()


# --- fresh-install alignment: busy timeout + gateway parser edges ----------

def test_store_uses_a_busy_timeout(tmp_path: Path):
    """Startup pruning on the daemon must not fail a concurrent CLI writer."""
    store = Store(tmp_path / "r.db")
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    store.close()


def test_model_text_survives_free_gateway_shapes():
    # fenced JSON with prose around it, as Token Harbor :free models emit
    fenced = 'Here is the report:\n```json\n{"a":1}\n```\n'
    assert model_text(fenced) == '{"a":1}'
    # reasoning_content chunks are not the answer
    body = ('data: {"choices":[{"delta":{"reasoning_content":"hm"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"{\\"a\\":1}"}}]}\n\n'
            'data: [DONE]\n')
    assert model_text(body, "text/event-stream") == '{"a":1}'
    # an unclosed <think> (cut stream) is dropped, not parsed as JSON
    assert model_text("<think>still thinking") == ""
    # content-parts shape
    parts = '{"choices":[{"message":{"content":[{"type":"text","text":"ok"}]}}]}'
    assert model_text(parts) == "ok"
