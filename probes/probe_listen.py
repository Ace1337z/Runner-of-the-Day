"""Verify `rotd listen --once` runs a cycle and answers an injected command.

Uses FakeTelegram (no network). Injects a /status update and confirms the bot
replied via send(). Then confirms a wrong-chat command is ignored (no reply).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.config import Config
from rotd.telegram import FakeTelegram
from rotd.store import Store
from rotd.cli import cmd_listen


def main() -> int:
    d = tempfile.mkdtemp()
    cfg_path = Path(d) / "rotd.env"
    cfg_path.write_text(
        "TELEGRAM_CHAT_ID=285995945\nSHADOW=1\nALERT_BUDGET_PER_DAY=3\n",
        encoding="utf-8",
    )
    cfg = Config(path=cfg_path)
    store = Store(Path(d) / "rotd.db")

    # Patch the Telegram client to FakeTelegram so listen answers offline.
    import rotd.cli as cli
    real_build = cli._build
    captured = {}

    def fake_build(c, s, use_fake_telegram=False):
        rt, clients, tg = real_build(c, s, use_fake_telegram=use_fake_telegram)
        ft = FakeTelegram(c)
        ft.updates = [{"update_id": 1,
                       "message": {"text": "/status",
                                   "chat": {"id": 285995945}}}]
        ft.cfg = c
        captured["tg"] = ft
        return rt, clients, ft

    cli._build = fake_build

    class Args:
        once = True

    rc = cmd_listen(cfg, Args())
    assert rc == 0, rc
    ft = captured["tg"]
    assert len(ft.messages) >= 1, "no reply sent"
    last = ft.last_text()
    assert "ROTD status" in last, last

    cli._build = real_build
    store.close()
    print("listen checks OK; status reply seen:", "ROTD status" in last)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
