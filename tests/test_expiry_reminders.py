"""Subscription expiry reminder cadence (T-7 / T-3 / T-1 / T-0).

No network: FakeTelegram records every send, and the reminder pass is invoked
directly so the dedup table, the four templates and the inline renewal button
are all pinned without a running daemon.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from rotd.cli import _remind_expiries, send_due_expiry_reminders
from rotd.config import Config
from rotd.notifier import (EXPIRY_REMINDERS, render_expiry_reminder, renew_markup)
from rotd.store import Store
from rotd.telegram import FakeTelegram
from rotd.util import now_utc


def _setup(tmp_path: Path, renew_url: str = "https://example.com/renew"):
    store = Store(tmp_path / "r.db")
    cfg = Config(path=tmp_path / "c.env")
    cfg.set("TELEGRAM_CHAT_ID", "111")
    if renew_url:
        cfg.set("RENEW_URL", renew_url)
    tg = FakeTelegram(cfg)
    return store, cfg, tg


def _expire_in(store: Store, chat_id: str, days: int) -> str:
    """Give ``chat_id`` an expiry exactly ``days`` from today (UTC)."""
    exp = (now_utc().date() + timedelta(days=days)).isoformat()
    store.add_subscriber(chat_id, max(1, days), f"user {chat_id}")
    store.execute("UPDATE subscribers SET expires_at=? WHERE chat_id=?",
                  (exp, chat_id))
    return exp


# --- the four templates ----------------------------------------------------

def test_all_four_templates_render_with_the_expiry_date():
    exp = "2026-11-05"
    t7 = render_expiry_reminder(7, exp)
    assert t7.startswith("⏳ ROTD Subscription Notice")
    assert "expire in 7 days" in t7 and exp in t7

    t3 = render_expiry_reminder(3, exp)
    assert t3.startswith("⚠️ 3 Days Left on ROTD Access")
    assert "Just 3 days remaining" in t3

    t1 = render_expiry_reminder(1, exp)
    assert t1.startswith("🚨 Final Warning: Expires Tomorrow")
    assert "ends tomorrow" in t1

    t0 = render_expiry_reminder(0, exp)
    assert t0.startswith("❌ Subscription Expired")
    assert "live alerts have been paused" in t0

    # {expiry_date} is only meaningful in the T-7 body, but must never leak raw.
    assert "{expiry_date}" not in t7
    assert sorted(EXPIRY_REMINDERS) == [0, 1, 3, 7]


def test_unknown_milestone_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        render_expiry_reminder(5, "2026-11-05")
    with pytest.raises(ValueError):
        renew_markup(5, "https://example.com/renew")


# --- inline renewal button -------------------------------------------------

def test_button_labels_and_url_per_milestone():
    url = "https://example.com/renew"
    labels = {7: "Renew Now", 3: "Extend My Access",
              1: "Renew Immediately", 0: "Reactivate Now"}
    for milestone, label in labels.items():
        markup = renew_markup(milestone, url)
        assert markup == {"inline_keyboard": [[{"text": label, "url": url}]]}


def test_no_button_when_renew_url_is_unset():
    assert renew_markup(7, "") is None
    assert renew_markup(7, "   ") is None


# --- cadence + dedup -------------------------------------------------------

def test_sends_each_milestone_once(tmp_path: Path):
    store, cfg, tg = _setup(tmp_path)
    for days in (7, 3, 1, 0):
        _expire_in(store, f"10{days}", days)

    sent = send_due_expiry_reminders(cfg, store, tg)

    assert sent == {7: 1, 3: 1, 1: 1, 0: 1}
    assert len(tg.sent) == 4
    # One message per subscriber, each carrying its own renewal button.
    by_chat = {m["chat"]: m for m in tg.sent}
    assert by_chat["107"]["text"].startswith("⏳")
    assert by_chat["103"]["text"].startswith("⚠️")
    assert by_chat["101"]["text"].startswith("🚨")
    assert by_chat["100"]["text"].startswith("❌")
    for milestone, chat in ((7, "107"), (3, "103"), (1, "101"), (0, "100")):
        markup = by_chat[chat]["markup"]
        assert markup["inline_keyboard"][0][0]["url"] == "https://example.com/renew"
        assert markup["inline_keyboard"][0][0]["text"] == EXPIRY_REMINDERS[milestone][0]

    # Second pass the same day: nothing is due again.
    assert send_due_expiry_reminders(cfg, store, tg) == {}
    assert len(tg.sent) == 4
    store.close()


def test_milestones_only_fire_on_their_exact_day(tmp_path: Path):
    store, cfg, tg = _setup(tmp_path)
    for days in (6, 5, 4, 2):
        _expire_in(store, f"20{days}", days)

    assert send_due_expiry_reminders(cfg, store, tg) == {}
    assert tg.sent == []
    store.close()


def test_a_renewed_subscription_reminds_again_on_the_new_expiry(tmp_path: Path):
    store, cfg, tg = _setup(tmp_path)
    old = _expire_in(store, "222", 3)
    assert send_due_expiry_reminders(cfg, store, tg) == {3: 1}
    assert store.expiry_reminder_sent("222", 3, old)

    # Renew with /addsub (expiry = today + 30), then walk the calendar forward:
    # the new expiry has its own milestones, so the T-7 fires again.
    store.add_subscriber("222", 30, "renewed")
    new = store.get_subscriber("222")["expires_at"]
    assert new != old
    store.execute("UPDATE subscribers SET expires_at=? WHERE chat_id='222'",
                  ((now_utc().date() + timedelta(days=7)).isoformat(),))
    new7 = store.get_subscriber("222")["expires_at"]

    assert send_due_expiry_reminders(cfg, store, tg) == {7: 1}
    assert store.expiry_reminder_sent("222", 7, new7)
    store.close()


def test_pre_registered_handle_rows_are_never_reminded(tmp_path: Path):
    store, cfg, tg = _setup(tmp_path)
    store.add_subscriber("@futureuser", 7, "@futureuser")
    store.execute("UPDATE subscribers SET expires_at=? WHERE chat_id='@futureuser'",
                  ((now_utc().date() + timedelta(days=7)).isoformat(),))

    assert send_due_expiry_reminders(cfg, store, tg) == {}
    assert tg.sent == []
    store.close()


def test_malformed_expiry_is_skipped_not_guessed(tmp_path: Path):
    store, cfg, tg = _setup(tmp_path)
    store.add_subscriber("333", 7, "broken")
    store.execute("UPDATE subscribers SET expires_at='not-a-date' WHERE chat_id='333'")

    assert send_due_expiry_reminders(cfg, store, tg) == {}
    assert tg.sent == []
    store.close()


def test_a_failed_send_is_retried_and_never_marked(tmp_path: Path):
    store, cfg, tg = _setup(tmp_path)
    exp = _expire_in(store, "222", 1)

    class _Blocked(FakeTelegram):
        def send(self, *a, **k):
            raise RuntimeError("bot was blocked by the user")

    blocked = _Blocked(cfg)
    assert send_due_expiry_reminders(cfg, store, blocked) == {}
    # Nothing recorded, because nothing was delivered...
    assert not store.expiry_reminder_sent("222", 1, exp)

    # ...so the same milestone goes out on the next pass once the chat works.
    assert send_due_expiry_reminders(cfg, store, tg) == {1: 1}
    assert store.expiry_reminder_sent("222", 1, exp)
    store.close()


def test_reminders_are_per_user_not_broadcast(tmp_path: Path):
    store, cfg, tg = _setup(tmp_path)
    _expire_in(store, "222", 7)
    _expire_in(store, "333", 7)

    send_due_expiry_reminders(cfg, store, tg)

    chats = sorted(m["chat"] for m in tg.sent)
    # One DM per subscriber — the owner (111) is not in the expiry list and
    # must not receive anyone else's reminder.
    assert chats == ["222", "333"]
    store.close()


# --- daemon hook -----------------------------------------------------------

def test_daemon_hook_is_interval_gated_and_toggleable(tmp_path: Path):
    store, cfg, tg = _setup(tmp_path)
    _expire_in(store, "222", 0)

    _remind_expiries.last = 0.0
    _remind_expiries(cfg, store, tg)
    assert len(tg.sent) == 1
    assert _remind_expiries.last > 0

    # Inside the interval nothing happens, even with a fresh due milestone.
    _expire_in(store, "333", 0)
    _remind_expiries(cfg, store, tg)
    assert len(tg.sent) == 1

    # Off switch: forced-due pass is skipped entirely.
    cfg.set("EXPIRY_REMINDERS", "0")
    _expire_in(store, "444", 0)
    _remind_expiries.last = 0.0
    _remind_expiries(cfg, store, tg)
    assert len(tg.sent) == 1
    store.close()


def test_no_reminders_without_a_renew_url_still_sends_text(tmp_path: Path):
    store, cfg, tg = _setup(tmp_path, renew_url="")
    _expire_in(store, "222", 3)

    assert send_due_expiry_reminders(cfg, store, tg) == {3: 1}
    assert tg.sent[0]["markup"] is None
    assert "Just 3 days remaining" in tg.sent[0]["text"]
    store.close()


def test_reminder_picks_up_an_expiry_set_days_later(tmp_path: Path):
    """The daily pass has no memory of "already checked": a subscription that
    reaches its T-7 tomorrow is reminded tomorrow, not today."""
    store, cfg, tg = _setup(tmp_path)
    _expire_in(store, "222", 8)
    assert send_due_expiry_reminders(cfg, store, tg) == {}

    tomorrow = now_utc().date() + timedelta(days=1)
    # Simulate the next day by asking the store directly with an explicit date.
    due = store.due_expiry_reminders(today=tomorrow)
    assert len(due) == 1 and due[0]["days_left"] == 7
    store.close()
