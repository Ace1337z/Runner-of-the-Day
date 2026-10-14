"""Verify the /setkey Telegram command writes allowed keys and refuses others,
against a temp config file (no network, no DB).""" 

from __future__ import annotations

import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.config import Config
from rotd.telegram import TelegramClient, FakeTelegram, SETTABLE_KEYS, handle_command


def main() -> int:
    d = tempfile.mkdtemp()
    cfg_path = Path(d) / "rotd.env"
    cfg_path.write_text("FINVIZ_TOKEN=\nTWELVEDATA_KEY=\n", encoding="utf-8")
    cfg = Config(path=cfg_path)

    tg = TelegramClient(cfg, secrets=["secretvalue"])
    # pretend chat matches owner
    cfg.append_to_file("TELEGRAM_CHAT_ID", "12345")

    # allowed key
    rep = tg.handle_command("/setkey FINVIZ_TOKEN abc123", "12345", cfg)
    assert rep.startswith("ok:"), rep
    assert cfg.raw("FINVIZ_TOKEN") == "abc123", cfg.raw("FINVIZ_TOKEN")

    # denied key (not in allowlist)
    rep2 = tg.handle_command("/setkey ROTD_MIN_SCORE 99", "12345", cfg)
    assert rep2.startswith("refused:"), rep2

    # wrong chat -> ignored (None)
    rep3 = tg.handle_command("/setkey FINVIZ_TOKEN hack", "999", cfg)
    assert rep3 is None, rep3
    assert cfg.raw("FINVIZ_TOKEN") == "abc123"

    # FakeTelegram records + still applies
    ft = FakeTelegram(cfg)
    rep4 = ft.handle_command("/setkey TWELVEDATA_KEY tdkey", "12345", cfg)
    assert rep4.startswith("ok:"), rep4
    assert "TWELVEDATA_KEY tdkey" in ft.commands[0]
    assert cfg.raw("TWELVEDATA_KEY") == "tdkey"

    # usage
    rep5 = tg.handle_command("/setkey", "12345", cfg)
    assert "usage:" in rep5, rep5

    print("setkey checks OK; settable keys:", len(SETTABLE_KEYS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
