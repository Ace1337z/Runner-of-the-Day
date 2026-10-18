"""Verify the expanded Telegram command set replies correctly.

Uses FakeTelegram (no network). Seeds a tiny store so the data commands return
real replies. No live API calls; safe to run offline.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.config import Config
from rotd.telegram import FakeTelegram
from rotd.store import Store

CHAT = "285995945"


def main() -> int:
    d = tempfile.mkdtemp()
    cfg_path = Path(d) / "rotd.env"
    cfg_path.write_text(
        "TELEGRAM_CHAT_ID=285995945\n"
        "SHADOW=1\n"
        "ALERT_BUDGET_PER_DAY=3\n"
        "FINVIZ_TOKEN=\nFINNHUB_KEY=\n",
        encoding="utf-8",
    )
    cfg = Config(path=cfg_path)
    store = Store(Path(d) / "rotd.db")
    day = date.today()
    store.save_snapshot(day, "OPEN", __import__("rotd.util", fromlist=["now_utc"]).now_utc(),
                        "B", {"half_day": False}, "hash1", 10)
    store.save_candidate(day, "OPEN", __import__("rotd.util", fromlist=["now_utc"]).now_utc(),
                         "ABCD", 82.0, 1, True, {}, {})
    store.save_candidate(day, "OPEN", __import__("rotd.util", fromlist=["now_utc"]).now_utc(),
                         "ZXYZ", 70.0, 2, True, {}, {})
    store.save_alert(day, "OPEN", __import__("rotd.util", fromlist=["now_utc"]).now_utc(),
                     "ABCD", "IGNITION", "R1", 82.0, 1, 1.5, None, None, "TEST", "h", True)
    store.save_runner(day, "ABCD", 1.0, 1.5, 120.0, 6_000_000, False, True,
                      __import__("rotd.util", fromlist=["now_utc"]).now_utc())
    store.set_meta("shadow_start", day.isoformat())
    store.set_meta("shadow_last_seen", day.isoformat())
    store.set_meta("shadow_cycles", "5")
    store.set_meta("shadow_alerts", "2")

    tg = FakeTelegram(cfg)
    ctx = {"store": store, "cfg": cfg}

    def ask(text: str) -> str:
        # inject the update then run handle_command directly (offline, deterministic)
        return tg.handle_command(text, CHAT, cfg, ctx=ctx)

    commands = ["/start", "/help", "/keys", "/status", "/shadow", "/board",
                "/runners", "/alerts", "/miss", "/log", "/doctor", "/bogus"]
    results = {}
    for c in commands:
        r = ask(c)
        assert r is not None, f"{c} returned None (ignored) — but owner chat should get a reply"
        results[c] = r
        print(f"--- {c} ---\n{r}\n")

    # setkey still works and is applied
    r = tg.handle_command("/setkey FINVIZ_TOKEN abc123", CHAT, cfg, ctx=ctx)
    assert r.startswith("ok:"), r
    assert cfg.raw("FINVIZ_TOKEN") == "abc123"
    # denied key still refused
    r = tg.handle_command("/setkey ROTD_MIN_SCORE 99", CHAT, cfg, ctx=ctx)
    assert r.startswith("refused:"), r
    # wrong chat ignored
    r = tg.handle_command("/setkey FINVIZ_TOKEN x", "999", cfg, ctx=ctx)
    assert r is None, r

    store.close()
    print("command-set checks OK; commands exercised:", len(commands))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
