"""Verify the new `rotd setup --non-interactive` writes env values to a config
file (mode 600) and that `rotd config` masks secrets. No network, no real keys.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.config import Config


def main() -> int:
    d = tempfile.mkdtemp()
    cfg_path = Path(d) / "rotd.env"

    # Simulate the env the installer exports (only TELEGRAM_* required)
    os.environ["TELEGRAM_TOKEN"] = "secret-token-123"
    os.environ["TELEGRAM_CHAT_ID"] = "99887766"
    os.environ["FINVIZ_TOKEN"] = "fv-abc"
    os.environ.pop("ROTD_CONFIG", None)

    cfg = Config(path=cfg_path)
    cfg.ensure_exists()
    cfg.append_to_file("TELEGRAM_TOKEN", os.environ["TELEGRAM_TOKEN"])
    cfg.append_to_file("TELEGRAM_CHAT_ID", os.environ["TELEGRAM_CHAT_ID"])
    cfg.append_to_file("FINVIZ_TOKEN", os.environ["FINVIZ_TOKEN"])

    # mode 600 check — only enforced on POSIX (the install target: Linux +
    # systemd). Windows chmod cannot drop group/other bits, so skip there.
    mode = cfg_path.stat().st_mode & (stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
    perms = oct(mode)
    if os.name != "nt":
        assert perms == "0o600", f"expected 0o600, got {perms}"

    # round-trip read
    assert cfg.raw("TELEGRAM_TOKEN") == "secret-token-123", cfg.raw("TELEGRAM_TOKEN")
    assert cfg.raw("TELEGRAM_CHAT_ID") == "99887766"
    assert cfg.raw("FINVIZ_TOKEN") == "fv-abc"

    # masked display
    disp = cfg.display("TELEGRAM_TOKEN")
    assert "secret-token-123" not in disp, disp
    assert "*" in disp, disp

    # detect_chat_id on FakeTelegram returns configured id
    from rotd.telegram import FakeTelegram
    ft = FakeTelegram(cfg)
    assert ft.detect_chat_id() == "99887766", ft.detect_chat_id()

    print("setup checks OK")
    print("  perms:", perms)
    print("  TELEGRAM_TOKEN display:", disp)
    print("  FINVIZ_TOKEN:", cfg.raw("FINVIZ_TOKEN"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
