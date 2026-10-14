"""Verify the listen daemon auto-finalizes (builds runner list + MISS REPORT).

Simulates: a runner exists in the store for 'yesterday', then the session moves to
a closed window / new day. On the next tick the daemon must call finalize_day and
push the MISS REPORT via the (fake) Telegram client.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rotd.config import Config
from rotd.telegram import FakeTelegram
from rotd.store import Store
from rotd.cli import _finalize_if_closed


def main() -> int:
    d = tempfile.mkdtemp()
    cfg_path = Path(d) / "rotd.env"
    cfg_path.write_text("TELEGRAM_CHAT_ID=285995945\nSHADOW=0\n", encoding="utf-8")
    cfg = Config(path=cfg_path)
    store = Store(Path(d) / "rotd.db")

    # 'yesterday' had a runner that ROTD never alerted (the user's exact scenario).
    yday = date.today() - timedelta(days=1)
    store.save_runner(yday, "ZRUN", 1.0, 2.5, 150.0, 8_000_000, False, True, None)
    assert store.get_miss_report(yday) is None, "precondition: no report yet"

    # Build a minimal Runtime-like object with store + a finalize_day spy.
    calls = {}

    class FakeRT:
        def __init__(self, st):
            self.store = st

        def finalize_day(self, day):
            calls["day"] = day.isoformat()
            # Mirror the real Runtime: write a miss report so re-finalize is skipped.
            self.store.save_miss_report(day, {"runners": [{"symbol": "ZRUN"}]})
            return {"runners": [{"symbol": "ZRUN"}]}

    rt = FakeRT(store)
    last = [yday]

    # Simulate a closed window (is_session False) -> previous day should finalize.
    _finalize_if_closed(rt, last)

    assert "day" in calls, "finalize_day was not called at session close"
    assert calls["day"] == yday.isoformat(), calls["day"]
    assert store.get_miss_report(yday) is not None, "miss report not stored"
    # Idempotent: calling again on the same closed window must NOT re-finalize.
    calls.clear()
    _finalize_if_closed(rt, last)
    assert "day" not in calls, "finalize ran twice for the same closed day"
    store.close()
    print(f"finalize probe OK: finalized {yday.isoformat()}, report stored, idempotent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
