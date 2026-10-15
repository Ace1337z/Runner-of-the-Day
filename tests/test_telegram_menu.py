"""Telegram slash-menu hygiene: sanitzation + self-heal on /start. No network.

The bot token is shared with a sibling system (penny-early-warning), so the menu
must (a) always be valid per Bot API rules, or the whole setMyCommands call fails
and the sibling's menu silently wins, and (b) be re-asserted when the owner wakes
the bot.
"""
from __future__ import annotations

from rotd.config import Config
from rotd.telegram import FakeTelegram, menu_commands, _menu_payload


def test_menu_names_are_valid_single_words():
    for name, desc in menu_commands():
        assert name == name.lower()
        assert " " not in name and "/" not in name
        assert all(c.isalnum() or c == "_" for c in name)
        assert 1 <= len(name) <= 32
        assert 0 < len(desc) <= 256


def test_setkey_usage_does_not_break_the_name():
    names = [n for n, _ in menu_commands()]
    assert "setkey" in names
    assert not any(" " in n for n in names)
    payload = _menu_payload(menu_commands())
    assert all(" " not in row["command"] for row in payload)
    # the usage hint survives in the description, not the name
    setkey = next(r for r in payload if r["command"] == "setkey")
    assert "KEY VALUE" in setkey["description"]


def test_menu_payload_skips_blank_names():
    payload = _menu_payload([("/  ", "x"), ("/keys", "ok")])
    assert [r["command"] for r in payload] == ["keys"]


def test_start_republishes_the_menu(tmp_path):
    path = tmp_path / "rotd.env"
    path.write_text("TELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    cfg = Config(path=path)
    tg = FakeTelegram(cfg)
    # Simulate a sibling bot having clobbered the menu.
    tg.set_commands([("penny", "a sibling bot menu")])
    reply = tg.handle_command("/start", "42", cfg)
    assert reply and "ROTD" in reply
    names = {n for n, _ in tg.menu}
    assert "penny" not in names
    assert {"start", "keys", "status", "board"} <= names
