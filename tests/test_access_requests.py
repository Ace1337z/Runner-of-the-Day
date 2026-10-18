"""Access-request flow: seen users, /request, 1-click approve, /checksubs.

No network: FakeTelegram records sends, and the callback handler is called
directly with the update shapes Telegram produces.
"""

from __future__ import annotations

from pathlib import Path

from datetime import timedelta

from rotd.cli import _handle_callback, _listen_reply
from rotd.config import Config
from rotd.notifier import render_subscriber_welcome
from rotd.store import Store
from rotd.telegram import FakeTelegram
from rotd.util import now_utc


def _setup(tmp_path: Path):
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    tg = FakeTelegram(cfg)
    ctx = {"store": store, "cfg": cfg}
    return store, cfg, tg, ctx


def _update(text: str, chat: str, username: str = "", first: str = "", uid: int = 1):
    return {"update_id": uid, "message": {
        "chat": {"id": int(chat)}, "text": text,
        "from": {"id": int(chat), "username": username, "first_name": first}}}


def _callback(data: str, sender: str, target_msg: int = 7, cid: str = "cb1"):
    return {"id": cid, "data": data, "from": {"id": int(sender)},
            "message": {"message_id": target_msg, "chat": {"id": int(sender)}}}


# --- seen users + resolution ----------------------------------------------

def test_seen_users_and_username_resolution(tmp_path: Path):
    store, _, _, _ = _setup(tmp_path)
    store.record_seen_user("222", "@EdoGen", "Edo")
    seen = store.get_seen_user("222")
    assert seen["username"] == "edogen" and seen["first_name"] == "Edo"
    assert seen["status"] == "SEEN"

    # A later update without a username must not erase the handle.
    store.record_seen_user("222", "", "")
    assert store.get_seen_user("222")["username"] == "edogen"

    assert store.resolve_target_to_chat_id("@EdoGen") == ("222", "@edogen")
    assert store.resolve_target_to_chat_id("222") == ("222", "@edogen")
    assert store.resolve_target_to_chat_id("-100123") == ("-100123", "")
    assert store.resolve_target_to_chat_id("@nobody") is None
    store.close()


def test_request_status_and_checksubs_rows(tmp_path: Path):
    store, _, _, _ = _setup(tmp_path)
    assert store.get_request_status("222") == "SEEN"
    store.set_request_status("222", "pending")
    assert store.get_request_status("222") == "PENDING"

    store.add_subscriber("222", 30, "@edogen")
    store.add_subscriber("333", 1, "Old")
    store.execute("UPDATE subscribers SET expires_at='2020-01-01' WHERE chat_id='333'")
    rows = {r["chat_id"]: r for r in store.get_all_subscribers()}
    assert rows["222"]["is_active"] and rows["222"]["days_left"] == 30
    assert not rows["333"]["is_active"] and rows["333"]["days_left"] < 0
    store.close()


# --- /start + /request -----------------------------------------------------

def test_start_offers_request_button_and_records_seen_user(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    tg.updates = [_update("/start", "222", "edogen", "Edo")]
    _listen_reply(cfg, store, tg, 0, 0)

    assert store.get_seen_user("222")["username"] == "edogen"
    sent = tg.sent[-1]
    assert "private momentum scanner" in sent["text"]
    assert sent["markup"] == {"inline_keyboard": [[
        {"text": "🔑 Request Access", "callback_data": "req_access"}]]}
    store.close()


def test_request_notifies_owner_once_and_is_idempotent(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    store.record_seen_user("222", "edogen", "Edo")
    tg.updates = [_update("/request", "222", "edogen", "Edo")]
    _listen_reply(cfg, store, tg, 0, 0)

    assert store.get_request_status("222") == "PENDING"
    admin = [m for m in tg.sent if m["chat"] == "111" and "Access Request" in m["text"]]
    assert len(admin) == 1
    assert "• Username: @edogen" in admin[0]["text"]
    assert "• Chat ID: 222" in admin[0]["text"]
    rows = admin[0]["markup"]["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "sub_app:222:30"
    assert rows[0][2]["callback_data"] == "sub_app:222:90"
    assert rows[1][0]["callback_data"] == "sub_rej:222"

    # Second request: user is told it is pending; the owner is not spammed.
    tg.updates = [_update("/request", "222", "edogen", "Edo", uid=2)]
    _listen_reply(cfg, store, tg, 0, 0)
    assert "already pending" in tg.sent[-1]["text"]
    assert len([m for m in tg.sent if m["chat"] == "111"
                and "Access Request" in m["text"]]) == 1
    store.close()


def test_rejected_user_is_dropped_silently(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    store.record_seen_user("222", "edogen", "Edo")
    store.set_request_status("222", "REJECTED")
    tg.updates = [_update("/request", "222", "edogen", "Edo")]
    _listen_reply(cfg, store, tg, 0, 0)
    assert not [m for m in tg.sent if m["chat"] == "222"]
    store.close()


# --- 1-click buttons -------------------------------------------------------

def test_owner_one_click_approval_delivers_playbook(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    store.record_seen_user("222", "edogen", "Edo")
    store.set_request_status("222", "PENDING")

    _handle_callback(cfg, store, tg, _callback("sub_app:222:60", "111"))

    assert store.is_active("222")
    assert store.get_request_status("222") == "APPROVED"
    assert store.get_subscriber("222")["label"] == "@edogen"
    welcome = [m for m in tg.sent if m["chat"] == "222"]
    assert welcome and "HOW TO TRADE IGNITION ALERTS" in welcome[0]["text"]
    assert "60 day(s)" in welcome[0]["text"]
    assert tg.callbacks[-1]["text"] == "Approved!"
    assert "✅ Approved 222 (@edogen) for 60 days" in tg.text_of(7)
    store.close()


def test_non_owner_callback_is_refused(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    store.record_seen_user("222", "edogen", "Edo")
    store.set_request_status("222", "PENDING")

    _handle_callback(cfg, store, tg, _callback("sub_app:222:90", "999"))

    assert not store.is_active("222")
    assert tg.callbacks[-1]["text"] == "Not authorized"
    store.close()


def test_owner_reject_notifies_user(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    store.record_seen_user("222", "edogen", "Edo")
    store.set_request_status("222", "PENDING")

    _handle_callback(cfg, store, tg, _callback("sub_rej:222", "111"))

    assert store.get_request_status("222") == "REJECTED"
    assert not store.is_active("222")
    assert any(m["chat"] == "222" and "declined" in m["text"] for m in tg.sent)
    assert "❌ Rejected access request from 222." in tg.text_of(7)
    store.close()


def test_request_access_button_opens_a_request(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    store.record_seen_user("222", "edogen", "Edo")

    _handle_callback(cfg, store, tg, _callback("req_access", "222"))

    assert store.get_request_status("222") == "PENDING"
    admin = [m for m in tg.sent if m["chat"] == "111" and "Access Request" in m["text"]]
    assert len(admin) == 1
    store.close()


# --- manual /addsub, pre-registration, /checksubs ---------------------------

def test_addsub_by_username_resolves_and_welcomes(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    store.record_seen_user("222", "edogen", "Edo")

    reply = tg.handle_command("/addsub @EdoGen 45", "111", cfg, ctx)

    expected = (now_utc().date() + timedelta(days=45)).isoformat()
    assert reply.startswith("✅ Subscriber 222 (@edogen) activated for 45 days")
    assert store.get_subscriber("222")["expires_at"] == expected
    assert store.get_request_status("222") == "APPROVED"
    assert any(m["chat"] == "222" and "HOW TO TRADE" in m["text"] for m in tg.sent)
    store.close()


def test_addsub_unknown_username_preregisters_and_links_on_start(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    reply = tg.handle_command("/addsub @FutureUser 30", "111", cfg, ctx)
    assert reply.startswith("⏳ Pre-registered @FutureUser for 30 days")
    assert store.find_pending_username("@futureuser") == "@futureuser"

    # They open the bot: /start links the numeric id and sends the playbook.
    tg.updates = [_update("/start", "777", "FutureUser", "Fu")]
    _listen_reply(cfg, store, tg, 0, 0)
    assert store.is_active("777")
    assert store.find_pending_username("@futureuser") is None
    assert store.get_subscriber("777")["label"] == "@futureuser"
    assert any(m["chat"] == "777" and "HOW TO TRADE" in m["text"] for m in tg.sent)
    store.close()


def test_checksubs_lists_active_and_expired(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    store.add_subscriber("222", 29, "@edogen")
    store.add_subscriber("333", 5, "John")
    store.execute("UPDATE subscribers SET expires_at='2026-10-01' WHERE chat_id='333'")

    reply = tg.handle_command("/checksubs", "111", cfg, ctx)
    assert "• 222 (@edogen) — ACTIVE (29 days left, exp" in reply
    assert "• 333 (John) — EXPIRED (exp 2026-10-01)" in reply
    assert reply.endswith("Total Active: 1 | Total Registered: 2")
    assert tg.handle_command("/subs", "111", cfg, ctx) == reply

    # Subscribers cannot read the list.
    assert tg.handle_command("/checksubs", "222", cfg, ctx) == "Unauthorized."
    store.close()


def test_checksubs_empty_and_rmsub_by_username(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    assert tg.handle_command("/checksubs", "111", cfg, ctx) == \
        "📭 No subscribers registered yet."
    store.record_seen_user("222", "edogen", "Edo")
    tg.handle_command("/addsub @edogen 30", "111", cfg, ctx)
    assert tg.handle_command("/rmsub @edogen", "111", cfg, ctx) == \
        "🗑️ Subscriber 222 removed."
    store.close()


def test_welcome_renderer_matches_the_playbook():
    text = render_subscriber_welcome(30, "2026-11-18")
    assert "30 day(s)" in text and "2026-11-18" in text
    assert "/board" in text and "/help" in text


def test_linking_an_expired_preregistration_tells_the_user(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    tg.handle_command("/addsub @FutureUser 30", "111", cfg, ctx)
    store.execute("UPDATE subscribers SET expires_at='2020-01-01' "
                  "WHERE chat_id='@futureuser'")

    tg.updates = [_update("/start", "777", "FutureUser", "Fu")]
    _listen_reply(cfg, store, tg, 0, 0)

    assert not store.is_active("777")
    assert "subscription expired on 2020-01-01" in tg.sent[-1]["text"]
    store.close()


def test_pending_username_is_never_a_broadcast_recipient(tmp_path: Path):
    from rotd.notifier import Notifier
    store, cfg, tg, ctx = _setup(tmp_path)
    tg.handle_command("/addsub @FutureUser 30", "111", cfg, ctx)
    notifier = Notifier(cfg, tg, store)
    assert notifier._recipients() == ["111"]
    store.close()


def test_linking_never_shortens_a_direct_numeric_subscription(tmp_path: Path):
    store, _, _, _ = _setup(tmp_path)
    store.record_seen_user("777", "futureuser", "Fu")
    store.add_subscriber("777", 90, "Direct")
    store.add_subscriber("@futureuser", 10, "@futureuser")

    exp = store.link_pending_username("futureuser", "777")

    assert exp is not None and (exp - now_utc().date()).days == 90
    assert store.find_pending_username("futureuser") is None
    assert store.get_subscriber("777")["expires_at"] == exp.isoformat()
    store.close()


def test_active_subscriber_start_clears_a_stale_pending_row(tmp_path: Path):
    store, cfg, tg, ctx = _setup(tmp_path)
    store.record_seen_user("222", "edogen", "Edo")
    store.add_subscriber("222", 30, "@edogen")
    store.add_subscriber("@edogen", 30, "@edogen")

    tg.updates = [_update("/start", "222", "edogen", "Edo")]
    _listen_reply(cfg, store, tg, 0, 0)

    assert store.find_pending_username("edogen") is None
    assert store.get_subscriber("222")["expires_at"]
    assert "HOW TO TRADE IGNITION ALERTS" in tg.sent[-1]["text"]
    store.close()
