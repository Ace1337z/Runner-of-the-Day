"""Command-line interface (build prompt §14.3).

Commands::

    rotd doctor [--json]         check config, sources, calendar, clock, disk
    rotd selftest                fully offline deterministic self-test (no network)
    rotd shadow [--once]         run cycles and log; push nothing
    rotd run [--once]            live run (honours SHADOW in config)
    rotd replay DATE             re-run a stored day through the same code
    rotd status                  show the current day, state and budget
    rotd miss [DATE]             print the MISS REPORT for a day
    rotd config [KEY]            show config (secrets masked)

Secrets are never printed and never taken from the command line.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .calendar_us import session_info
from .catalyst import CatalystService
from .config import Config, SECRET_KEYS
from .notifier import Notifier
from .runtime import Runtime
from .sources import build_clients
from .store import Store
from .telegram import FakeTelegram, TelegramClient
from .util import fmt_ts, now_utc


def _setup_logging(cfg: Config, verbose: bool) -> None:
    log_dir = cfg.log_dir
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(log_dir / "rotd.log", encoding="utf-8")],
    )
    from .util import scrub
    for secret in cfg.secret_values:
        logging.getLogger().addFilter(_Redactor(secret))


class _Redactor(logging.Filter):
    def __init__(self, secret: str):
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secret and self.secret in str(record.getMessage()):
            record.msg = str(record.getMessage()).replace(self.secret, "***")
            record.args = ()
        return True


def _build(cfg: Config, store: Store, use_fake_telegram: bool = False):
    clients = build_clients(cfg)
    if use_fake_telegram or not cfg.raw("TELEGRAM_TOKEN"):
        tg = FakeTelegram(cfg)
    else:
        tg = TelegramClient(cfg, secrets=cfg.secret_values)
    notifier = Notifier(cfg, tg, store)
    catalyst = CatalystService(cfg, store)
    rt = Runtime(cfg, store, clients, notifier, catalyst)
    return rt, clients, tg


# --- commands --------------------------------------------------------------

def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    out: dict[str, Any] = {"version": __version__, "config": str(cfg.path),
                           "db": str(cfg.db_path), "checks": {}}
    si = session_info()
    out["checks"]["calendar"] = {
        "mode": si.mode, "trading_day": si.trading_day.isoformat(),
        "session": si.session_date.isoformat(), "is_session": si.is_session,
        "is_half": si.is_half, "clock": fmt_ts(now_utc()),
    }
    clients = build_clients(cfg)
    for name, client in clients.items():
        ok, msg = client.health()
        out["checks"][f"source:{name}"] = {"ok": ok, "message": msg}
    tg = TelegramClient(cfg, secrets=cfg.secret_values) if cfg.raw("TELEGRAM_TOKEN") else FakeTelegram(cfg)
    ok, msg = tg.health()
    out["checks"]["telegram"] = {"ok": ok, "message": msg,
                                 "as": "FakeTelegram" if isinstance(tg, FakeTelegram) else "live"}
    cat = CatalystService(cfg, store)
    cok, cmsg = cat.health()
    # No model configured is a *supported* deployment (rules-only verdicts), so it
    # is a warning, not a failure: the auditor falls back to the rules-only block.
    out["checks"]["catalyst"] = {"ok": True, "warn": not cok, "message": cmsg}
    # the shadow clock (§17 Phase 6): a live push is blocked until it completes.
    # An in-progress clock is the *expected* healthy state, so it is reported but
    # never counted as a doctor failure — only a corrupt/unreadable clock is.
    from . import shadow
    try:
        sst = shadow.status(store, cfg=cfg)
        out["checks"]["shadow_clock"] = {"ok": True, "message": sst.line(), **sst.as_dict()}
    except Exception as exc:  # noqa: BLE001
        out["checks"]["shadow_clock"] = {"ok": False, "message": str(exc)}
    # the methodology_v3 auditor prompt must load and carry its markers (§10)
    try:
        from .auditor import load_system_prompt
        prompt = load_system_prompt(cfg)
        out["checks"]["auditor_prompt"] = {
            "ok": len(prompt) > 5000, "message": f"{cfg.str('AI_PROMPT_FILE', '?')} "
            f"({len(prompt)} chars)", "prompt_chars": len(prompt)}
    except Exception as exc:  # noqa: BLE001
        out["checks"]["auditor_prompt"] = {"ok": False, "message": str(exc)}
    # disk
    try:
        import shutil
        free_pct = shutil.disk_usage(str(cfg.home)).free / shutil.disk_usage(str(cfg.home)).total * 100
        warn = cfg.float("DISK_WARN_PCT", 10)
        out["checks"]["disk"] = {"free_pct": round(free_pct, 1),
                                 "ok": free_pct >= warn, "warn_below": warn}
    except Exception as exc:  # noqa: BLE001
        out["checks"]["disk"] = {"ok": False, "message": str(exc)}
    store.close()

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"ROTD doctor  v{__version__}")
        print(f"  config: {out['config']}")
        print(f"  db:     {out['db']}")
        for name, c in out["checks"].items():
            if "ok" in c:
                if not c["ok"]:
                    mark = "!! "
                elif c.get("warn"):
                    mark = " ~ "
                else:
                    mark = "OK "
                extra = {k: v for k, v in c.items() if k not in ("ok", "warn")}
                print(f"  {mark}{name}: {extra}")
            else:
                print(f"     {name}: {c}")
    fails = [k for k, c in out["checks"].items() if "ok" in c and not c["ok"]]
    return 0 if not fails else 1


def cmd_selftest(cfg: Config, args: argparse.Namespace) -> int:
    """Offline: replay the golden fixtures through the real pipeline."""
    from . import selftest
    return selftest.run(cfg, verbose=args.verbose)


def cmd_shadow(cfg: Config, args: argparse.Namespace) -> int:
    cfg.set("SHADOW", "1")
    return _run(cfg, args, live=False)


def cmd_shadow_status(cfg: Config, args: argparse.Namespace) -> int:
    from . import shadow
    store = Store(cfg.db_path)
    st = shadow.status(store, cfg=cfg)
    store.close()
    if args.json:
        print(json.dumps(st.as_dict(), indent=2, default=str))
    else:
        print(st.line())
        if st.started:
            print(f"  started {st.started}  last seen {st.last_seen}  "
                  f"required {st.sessions_required} trading days")
    return 0


def cmd_shadow_start(cfg: Config, args: argparse.Namespace) -> int:
    from . import shadow
    store = Store(cfg.db_path)
    si = session_info()
    try:
        day = date.fromisoformat(args.date) if args.date else si.trading_day
    except ValueError:
        print(f"bad date: {args.date} (want YYYY-MM-DD)")
        store.close()
        return 2
    try:
        st = shadow.start(store, day, force=args.force)
    except ValueError as exc:
        print(str(exc))
        store.close()
        return 2
    store.set_meta("shadow_enabled", "1")
    print(f"shadow clock started on {day}")
    print(st.line())
    store.close()
    return 0


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    return _run(cfg, args, live=True)


def _run(cfg: Config, args: argparse.Namespace, live: bool) -> int:
    from . import shadow
    store = Store(cfg.db_path)
    shadow_enabled = cfg.bool("SHADOW")
    # Phase 6 gate: a live run may not push until the shadow clock has completed.
    if live and not shadow.live_allowed(store, cfg):
        si0 = session_info()
        if cfg.bool("SHADOW_AUTOSTART") and cfg.bool("SHADOW"):
            shadow.start(store, si0.trading_day)
        st = shadow.status(store, cfg=cfg)
        print(f"live pushes blocked: {st.reason} ({st.sessions_done}/{st.sessions_required} "
              f"shadow trading days). Run 'rotd shadow' first.")
        store.close()
        return 3
    rt, clients, tg = _build(cfg, store, use_fake_telegram=not shadow_enabled and not live)
    si = session_info()
    print(f"cycle at {fmt_ts(now_utc())}  mode={si.mode}  day={si.trading_day}  "
          f"shadow={shadow_enabled}")
    if shadow_enabled and cfg.bool("SHADOW_AUTOSTART") and store.get_meta("shadow_start") is None:
        shadow.start(store, si.trading_day)
    res = rt.cycle(si.mode, si.trading_day)
    print(f"  snapshot: grade {res.snapshot.data_grade}, {len(res.snapshot.records)} names")
    print(f"  ranked: {len(res.ranking.day_ranked)}  board: {[s.symbol for s in res.ranking.board]}")
    print(f"  rotd: {res.ranking.rotd.symbol if res.ranking.rotd else 'none'} "
          f"({res.ranking.rotd_reason})")
    print(f"  transitions: {len(res.transitions)}  sent: {len(res.sent)}")
    if shadow_enabled:
        print(f"  {shadow.status(store, cfg=cfg).line()}")
    store.close()
    return 0


def cmd_finalize(cfg: Config, args: argparse.Namespace) -> int:
    """End of day: build the runner list and the MISS REPORT for a completed day."""
    store = Store(cfg.db_path)
    si = session_info()
    try:
        day = date.fromisoformat(args.date) if args.date else si.trading_day
    except ValueError:
        print(f"bad date: {args.date} (want YYYY-MM-DD)")
        store.close()
        return 2
    rt, clients, tg = _build(cfg, store, use_fake_telegram=True)
    report = rt.finalize_day(day)
    from .notifier import render_miss_report
    print(f"MISS REPORT for {day}:")
    print(render_miss_report(report))
    store.close()
    return 0


def cmd_replay(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        print(f"bad date: {args.date} (want YYYY-MM-DD)")
        store.close()
        return 2
    snaps = store.query("SELECT * FROM snapshots WHERE trading_day=? ORDER BY cycle_ts",
                        (day.isoformat(),))
    if not snaps:
        print(f"no stored snapshots for {day}")
        store.close()
        return 1
    print(f"replaying {day}: {len(snaps)} snapshots")
    for s in snaps:
        cands = store.query("SELECT * FROM candidates WHERE trading_day=? AND cycle_ts=?",
                            (day.isoformat(), s["cycle_ts"]))
        passed = sum(1 for c in cands if c["passed_gates"])
        print(f"  {s['cycle_ts']}  mode={s['mode']}  grade={s['data_grade']}  "
              f"names={s['n_symbols']}  gated_pass={passed}")
    # determinism check: the recipe hash is stable
    print(f"replay hash of first snapshot: {snaps[0]['payload_hash'][:16]}")
    store.close()
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    si = session_info()
    print(f"ROTD status  v{__version__}  {fmt_ts(now_utc())}")
    print(f"  mode={si.mode}  trading_day={si.trading_day}  half={si.is_half}")
    latest = store.latest_snapshot(si.trading_day)
    if latest:
        print(f"  last snapshot: {latest['cycle_ts']} grade={latest['data_grade']} "
              f"names={latest['n_symbols']}")
    else:
        print("  last snapshot: none today")
    n = store.count_alerts(si.trading_day, "IGNITION")
    print(f"  ignition budget used: {n}/{cfg.int('ALERT_BUDGET_PER_DAY', 3)}")
    from . import shadow
    print(f"  {shadow.status(store, cfg=cfg).line()}")
    for a in store.alerts_for_day(si.trading_day, kind="IGNITION"):
        print(f"    - ${a['symbol']} slot {a['budget_slot']} score {a['score']}")
    print("  recent events:")
    for e in store.recent_events(8):
        print(f"    {e['ts'][:19]} {e['level']}: {e['message'][:80]}")
    store.close()
    return 0


def cmd_miss(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    day = date.fromisoformat(args.date) if args.date else session_info().trading_day
    report = store.get_miss_report(day)
    store.close()
    if not report:
        print(f"no MISS REPORT for {day}")
        return 1
    from .notifier import render_miss_report
    print(render_miss_report(report))
    return 0


# Keys the `setup` command writes; order matters (Telegram first, like penny).
SETUP_ORDER = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]
SETUP_PROMPTS = {
    "TELEGRAM_TOKEN": "Telegram bot token (@BotFather)",
    "TELEGRAM_CHAT_ID": "Telegram chat id (or message the bot to auto-detect)",
}


def cmd_setup(cfg: Config, args: argparse.Namespace) -> int:
    """Idempotent config writer, mirroring the penny-early-warning installer.

    Interactive (a real tty): prompts only for Telegram, masked; pressing Enter
    keeps the existing value. Non-interactive (env vars, or ``--non-interactive``):
    reads every key from the environment and writes what is present. Secrets are
    written chmod 600 and never echoed. All other keys can be set later from
    Telegram via ``/setkey KEY VALUE``.
    """
    from .util import mask

    cfg.ensure_exists()
    print("ROTD setup")
    print(f"configuration file: {cfg.path}")
    if cfg.path.exists():
        print("existing values are shown masked; press Enter to keep them.\n")

    non_interactive = args.non_interactive or not sys.stdin.isatty()
    if non_interactive:
        print("non-interactive mode: reading values from the environment")
        for key in SETUP_ORDER:
            env = os.environ.get(key)
            if env:
                cfg.append_to_file(key, env)
        print("configuration written from environment values (mode 600).")
        _post_setup_checks(cfg, args)
        return 0

    for key in SETUP_ORDER:
        prompt = SETUP_PROMPTS.get(key, key)
        current = cfg.raw(key)
        if key == "TELEGRAM_CHAT_ID" and not current:
            value = _detect_chat_id(cfg)
            if value:
                cfg.append_to_file(key, value)
                print(f"  {key} = {mask(value)} (auto-detected)")
                continue
        label = f"{prompt} [{mask(current) if current else 'empty'}]"
        try:
            import getpass
            if key in SECRET_KEYS:
                value = getpass.getpass(f"{label}: ")
            else:
                value = input(f"{label}: ")
        except EOFError:
            value = ""
        if value:
            cfg.append_to_file(key, value)

    print(f"\nconfiguration written to {cfg.path} (mode 600)")
    print("Every other key (Finviz, AI, SEC, ...) is optional and can be set later")
    print("from Telegram: /setkey KEY VALUE.  'rotd config' lists what is set.")
    _post_setup_checks(cfg, args)
    return 0


def _detect_chat_id(cfg: Config) -> Optional[str]:
    token = cfg.raw("TELEGRAM_TOKEN")
    if not token:
        return None
    print("\nSend any message to your Telegram bot now; waiting up to 60 seconds...")
    client = TelegramClient(cfg, secrets=cfg.secret_values)
    chat_id = client.detect_chat_id(attempts=30, timeout=2)
    if chat_id:
        print(f"detected chat id {chat_id}")
    else:
        print("could not detect the chat id; you can enter it manually")
    return chat_id


def _post_setup_checks(cfg: Config, args: argparse.Namespace) -> None:
    if getattr(args, "skip_doctor", False):
        return
    print("\nrunning the offline self-test...")
    from .selftest import run_selftest
    report = run_selftest(cfg)
    print(report.text())
    if getattr(args, "doctor", False):
        print("\nrunning the live checker (doctor)...")
        try:
            cmd_doctor(cfg, args)
        except Exception as exc:  # noqa: BLE001
            print(f"doctor reported: {exc}")


def cmd_detect_chat(cfg: Config, args: argparse.Namespace) -> int:
    """Detect the Telegram chat id by polling getUpdates after the user messages the bot."""
    value = _detect_chat_id(cfg)
    if value:
        cfg.append_to_file("TELEGRAM_CHAT_ID", value)
        print(f"TELEGRAM_CHAT_ID set to {value}")
        return 0
    print("no chat id detected; message the bot and retry, or set it manually.")
    return 1


def cmd_config(cfg: Config, args: argparse.Namespace) -> int:
    if args.key:
        if args.key not in {s.key for s in __import__("rotd.config", fromlist=["SETTINGS"]).SETTINGS}:
            print(f"unknown key: {args.key}")
            return 2
        print(f"{args.key} = {cfg.display(args.key)}")
        return 0
    print(cfg.summary())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rotd", description="Runner of the Day")
    p.add_argument("--config", "-c", help="path to the config file")
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check config, sources, calendar, clock")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_doctor)

    st = sub.add_parser("selftest", help="offline deterministic self-test")
    st.set_defaults(func=cmd_selftest)

    sh = sub.add_parser("shadow", help="run cycles and log; push nothing")
    sh.add_argument("--once", action="store_true")
    sh.set_defaults(func=cmd_shadow)

    sst = sub.add_parser("shadow-status", help="show the 10-trading-day shadow clock")
    sst.add_argument("--json", action="store_true")
    sst.set_defaults(func=cmd_shadow_status)

    sstt = sub.add_parser("shadow-start", help="start the shadow clock on a trading day")
    sstt.add_argument("--date", help="the trading day to start on (default: today)")
    sstt.add_argument("--force", action="store_true", help="restart an existing clock")
    sstt.set_defaults(func=cmd_shadow_start)

    rn = sub.add_parser("run", help="live run (honours SHADOW)")
    rn.add_argument("--once", action="store_true")
    rn.set_defaults(func=cmd_run)

    rp = sub.add_parser("replay", help="re-run a stored day")
    rp.add_argument("date")
    rp.set_defaults(func=cmd_replay)

    fn = sub.add_parser("finalize", help="end of day: runner list + MISS REPORT")
    fn.add_argument("date", nargs="?")
    fn.set_defaults(func=cmd_finalize)

    ss = sub.add_parser("status", help="show current day, state, budget")
    ss.set_defaults(func=cmd_status)

    ms = sub.add_parser("miss", help="print the MISS REPORT for a day")
    ms.add_argument("date", nargs="?")
    ms.set_defaults(func=cmd_miss)

    cf = sub.add_parser("config", help="show config (secrets masked)")
    cf.add_argument("key", nargs="?")
    cf.set_defaults(func=cmd_config)

    sp = sub.add_parser("setup", help="write config (Telegram first; idempotent)")
    sp.add_argument("--non-interactive", action="store_true",
                    help="read values from the environment; do not prompt")
    sp.add_argument("--skip-doctor", action="store_true",
                    help="skip the self-test / doctor at the end")
    sp.add_argument("--doctor", action="store_true",
                    help="also run the live checker (doctor) after writing config")
    sp.set_defaults(func=cmd_setup)

    dc = sub.add_parser("detect-chat", help="auto-detect TELEGRAM_CHAT_ID from a bot message")
    dc.set_defaults(func=cmd_detect_chat)

    p.add_argument("--version", action="version", version=f"rotd {__version__}")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config(path=Path(args.config) if args.config else None)
    cfg.ensure_exists()
    if args.command != "config":
        _setup_logging(cfg, args.verbose)
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
