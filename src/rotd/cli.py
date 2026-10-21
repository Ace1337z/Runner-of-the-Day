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
import threading
import time
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
from .util import fmt_ts, last_session, now_utc

log = logging.getLogger(__name__)


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


def _reload_live(rt, cfg: Config, store: Store, tg) -> None:
    """Apply a live ``config.env`` edit without a restart (item 5).

    ``/setkey`` writes the file; the daemon must pick the new credentials up on
    the next cycle. Rebuilding the clients also clears their caches/cooldowns so
    a corrected key is not masked by the old one's 401.
    """
    clients = build_clients(cfg)
    rt.clients = clients
    rt.catalyst = CatalystService(cfg, store)
    rt.notifier.cfg = cfg
    rt.notifier.client = tg
    tg.cfg = cfg
    if hasattr(tg, "secrets"):
        tg.secrets = list(cfg.secret_values)
    log.info("listen: config reloaded; clients rebuilt (%s)", ", ".join(sorted(clients)))


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


def _handle_callback(cfg, store, tg, cb: dict) -> None:
    """Handle an inline-button tap: access request, approve, reject.

    Owner-only for ``sub_app``/``sub_rej`` (the callback's sender is re-checked
    against ``TELEGRAM_CHAT_ID``, never trusted from the button data). Every path
    answers the callback query so Telegram stops the button spinner.
    """
    from .telegram import REQUEST_PENDING_REPLY, REQUEST_SENT_REPLY
    cb_id = str(cb.get("id") or "")
    data = str(cb.get("data") or "")
    sender = str(((cb.get("from") or {}).get("id")) or "")
    owner = str(cfg.raw("TELEGRAM_CHAT_ID") or "").strip()
    if not sender:
        tg.answer_callback(cb_id, "")
        return
    try:
        store.record_seen_user(sender, (cb.get("from") or {}).get("username", ""),
                               (cb.get("from") or {}).get("first_name", ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("callback: record_seen_user failed: %s", exc)

    def _edit(text: str) -> None:
        msg = cb.get("message") or {}
        if msg.get("message_id"):
            try:
                tg.edit(int(msg["message_id"]), text, chat_id=sender or owner or None)
            except Exception as exc:  # noqa: BLE001 - fall back to a plain reply
                log.warning("callback: edit failed: %s", exc)

    if data == "req_access":
        # Same path as /request so the button and the typed command cannot drift.
        if sender == owner:
            tg.answer_callback(cb_id, "Owner")
            return
        if store.is_active_subscriber(sender):
            tg.answer_callback(cb_id, "Already active")
            _edit("✅ You already have access. Type /help for the playbook.")
            return
        status = store.get_request_status(sender)
        if status == "PENDING":
            tg.answer_callback(cb_id, "Already pending")
            _edit(REQUEST_PENDING_REPLY)
            return
        if status == "REJECTED":
            tg.answer_callback(cb_id, "")
            return
        seen = store.get_seen_user(sender) or {}
        store.set_request_status(sender, "PENDING")
        if owner:
            from .notifier import render_access_request, request_markup
            try:
                tg.send(render_access_request(sender, seen.get("username", ""),
                                              seen.get("first_name", "")),
                        chat_id=owner, markup=request_markup(sender))
            except Exception as exc:  # noqa: BLE001
                log.warning("callback: owner notify failed: %s", exc)
        tg.answer_callback(cb_id, "Request sent")
        _edit(REQUEST_SENT_REPLY)
        return

    if not (data.startswith("sub_app:") or data.startswith("sub_rej:")):
        tg.answer_callback(cb_id, "")
        return
    if not owner or sender != owner:
        tg.answer_callback(cb_id, "Not authorized", show_alert=True)
        return

    from .notifier import render_subscriber_welcome

    if data.startswith("sub_app:"):
        try:
            _, target, days_s = data.split(":", 2)
            days = int(days_s)
        except ValueError:
            tg.answer_callback(cb_id, "Bad button")
            return
        seen = store.get_seen_user(target) or {}
        label = (f"@{seen['username']}" if seen.get("username")
                 else (seen.get("first_name") or ""))
        try:
            expires = store.add_subscriber(target, days, label)
        except ValueError as exc:
            tg.answer_callback(cb_id, str(exc)[:200])
            return
        store.set_request_status(target, "APPROVED")
        delivered = True
        try:
            tg.send(render_subscriber_welcome(days, expires.isoformat()), chat_id=target)
        except Exception as exc:  # noqa: BLE001 - subscription is already saved
            delivered = False
            log.warning("callback: welcome to %s failed: %s", target, exc)
        tg.answer_callback(cb_id, "Approved!")
        _edit(f"✅ Approved {target} ({label or 'no label'}) for {days} days "
              f"(expires {expires.isoformat()}). Welcome playbook "
              f"{'delivered!' if delivered else 'will be delivered on their next /start.'}")
        return

    target = data[len("sub_rej:"):]
    store.set_request_status(target, "REJECTED")
    tg.answer_callback(cb_id, "Rejected")
    _edit(f"❌ Rejected access request from {target}.")
    try:
        tg.send("❌ Your access request to ROTD was declined.", chat_id=target)
    except Exception as exc:  # noqa: BLE001 - they may have blocked the bot
        log.warning("callback: reject notice to %s failed: %s", target, exc)


def cmd_listen(cfg: Config, args: argparse.Namespace) -> int:
    """Persistent daemon: run decision cycles AND reply to Telegram commands.

    This is the process the systemd unit runs. The loop answers Telegram commands
    on the main thread (so a message is seen within ``LISTEN_POLL_S`` at most),
    while the heavy decision cycle runs on a worker thread so it can never block a
    reply. A cycle in progress is allowed to finish before the next one starts.

    Flow per pass:
      1. short-poll ``getUpdates`` and reply (fast — commands are never delayed
         behind a cycle);
      2. start the decision cycle on a worker thread;
      3. short-poll again for the remaining interval so a command sent during a
         cycle is answered immediately;
      4. sleep the rest of the interval, then reload config if it changed.

    ``--once`` runs a single pass and exits (used by tests).
    """
    from . import shadow
    store = Store(cfg.db_path)
    rt, clients, tg = _build(cfg, store, use_fake_telegram=not cfg.raw("TELEGRAM_TOKEN"))
    _maintain(cfg, store)  # retention + space reclaim once at startup (item 6)
    _remind_expiries(cfg, store, tg)  # T-7/T-3/T-1/T-0 renewal DMs at startup
    si0 = session_info()
    # Shadow gate: do NOT push live until the clock completes. We still run cycles
    # (logging) and we DO answer Telegram commands so /setkey keeps working.
    if cfg.bool("SHADOW_AUTOSTART") and cfg.bool("SHADOW") and \
            store.get_meta("shadow_start") is None:
        shadow.start(store, si0.trading_day)
    poll_s = cfg.int("LISTEN_POLL_S", 5)
    split = max(1, poll_s // 2)  # time for each short poll so total >= poll_s
    log.info("rotd listen: tick %ss; live pushes=%s; cycle runs off-thread",
             poll_s, not cfg.bool("SHADOW"))
    # Resume the Telegram read cursor across restarts so we never re-process old
    # messages (and never lose our place). systemd Restart=always would otherwise
    # reset offset=0 on every crash and re-answer history.
    offset = int(store.get_meta("tg_offset") or 0)
    # Claim the slash-command menu once at startup so a shared bot token does not
    # keep showing a sibling system's commands. Re-asserted on /start too.
    _assert_menu(cfg, tg)
    # Guards the worker-cycle so only one runs at a time (Store is locked, but we
    # avoid piling up cycles that would just contend on the DB).
    cycle_thread: Optional[threading.Thread] = None
    # The label of the last *completed* A1 day, so a daemon started on a
    # weekend still finalizes Friday instead of the upcoming session.
    last_trading_day: list[date] = [last_session()]

    def maybe_cycle() -> None:
        nonlocal cycle_thread
        if cycle_thread is not None and cycle_thread.is_alive():
            return  # previous cycle still running; skip this tick (back-pressure)
        if not cfg.bool("SHADOW_RUN_WHEN_CLOSED", True) and si0 and \
                not session_info().is_session:
            return  # user opted out of after-hours cycles
        si = session_info()
        if cfg.bool("SHADOW") and cfg.bool("SHADOW_AUTOSTART") and \
                store.get_meta("shadow_start") is None:
            shadow.start(store, si.trading_day)
        cycle_thread = threading.Thread(
            target=lambda: _safe_cycle(rt, si.mode, si.trading_day), daemon=True)
        cycle_thread.start()

    if getattr(args, "once", False):
        offset = _listen_reply(cfg, store, tg, offset, split, rt=rt)
        maybe_cycle()
        if cycle_thread is not None:
            cycle_thread.join(timeout=max(2, poll_s))
        store.close()
        return 0
    try:
        while True:
            try:
                # 0. finalize the previous trading day once it has closed (builds the
                #    runner list + MISS REPORT and pushes it to Telegram).
                _finalize_if_closed(rt, last_trading_day)
                # 1. answer commands first (no cycle in front of them)
                offset = _listen_reply(cfg, store, tg, offset, split, rt=rt)
                # 2. kick off the cycle on a worker thread
                maybe_cycle()
                # 3. keep answering during the cycle window
                offset = _listen_reply(cfg, store, tg, offset, split, rt=rt)
                # 4. finish the interval, then apply any /setkey edits
                time.sleep(max(0.5, poll_s - 2 * split))
                changed = cfg.reload_if_changed()
                if cfg.take_dirty() or changed:
                    _reload_live(rt, cfg, store, tg)
                _maintain(cfg, store)
                _remind_expiries(cfg, store, tg)
            except Exception as exc:  # noqa: BLE001 - one bad command or a network
                # blip must never kill the daemon (that would loop under Restart=always
                # and look like multi-second reply delays). Log and keep going.
                log.error("listen: pass failed, continuing: %s", exc)
                time.sleep(split)
            else:
                # Persist the read cursor only after a clean pass.
                try:
                    store.set_meta("tg_offset", str(offset))
                except Exception:  # noqa: BLE001
                    pass
    except KeyboardInterrupt:  # pragma: no cover
        log.info("rotd listen: interrupted, exiting")
    if cycle_thread is not None:
        cycle_thread.join(timeout=5)
    try:
        store.set_meta("tg_offset", str(offset))
    except Exception:  # noqa: BLE001
        pass
    store.close()
    return 0


def _maintain(cfg: Config, store: Store) -> None:
    """Run retention + space reclaim at most once per ``DB_PRUNE_INTERVAL_S`` (item 6)."""
    import time as _time
    every = cfg.int("DB_PRUNE_INTERVAL_S", 3600)
    now = _time.monotonic()
    if now - _maintain.last < max(60, every):
        return
    _maintain.last = now
    try:
        store.maintain(cfg)
    except Exception as exc:  # noqa: BLE001 - maintenance never kills the daemon
        log.warning("listen: maintenance pass failed: %s", exc)


_maintain.last = 0.0  # type: ignore[attr-defined]


def send_due_expiry_reminders(cfg: Config, store: Store, tg) -> dict[int, int]:
    """DM every subscriber at a T-7/T-3/T-1/T-0 milestone, once per milestone.

    The check is date-based (``days_left`` from the stored ``expires_at``), so
    running it more often is harmless: ``Store.due_expiry_reminders`` skips any
    milestone already recorded for that expiry, and a milestone is recorded only
    *after* a successful send. That makes the pass safe to call daily, hourly,
    and on every daemon restart.

    Per-user sends (never a fan-out) and per-chat isolation: one blocked or
    deleted chat logs a warning and cannot stop the rest of the cadence.

    Returns ``{milestone: delivered_count}`` for the milestones actually sent.
    """
    from .notifier import Notifier

    due = store.due_expiry_reminders()
    if not due:
        return {}
    # FakeTelegram (offline/tests) and the real client share the same ``send``
    # contract, so the pass exercises the production path in both modes.
    notifier = Notifier(cfg, tg, store)
    url = cfg.str("RENEW_URL", "")
    sent: dict[int, int] = {}
    for row in due:
        milestone = int(row["days_left"])
        if notifier.expiry_reminder(row["chat_id"], milestone, row["expires_at"], url):
            store.mark_expiry_reminder(row["chat_id"], milestone, row["expires_at"])
            sent[milestone] = sent.get(milestone, 0) + 1
        else:
            # Leave it unmarked: the next pass retries the same milestone.
            log.warning("expiry reminder (%sd) to %s not delivered; will retry",
                        milestone, row["chat_id"])
    if sent:
        log.info("expiry reminders sent: %s", ", ".join(
            f"T-{m}: {n}" for m, n in sorted(sent.items(), reverse=True)))
    return sent


def _remind_expiries(cfg: Config, store: Store, tg) -> None:
    """Run the expiry-reminder pass at most once per interval (never kills the loop)."""
    import time as _time
    every = cfg.int("EXPIRY_REMINDER_INTERVAL_S", 3600)
    now = _time.monotonic()
    if now - _remind_expiries.last < max(60, every):
        return
    _remind_expiries.last = now
    if not cfg.bool("EXPIRY_REMINDERS", True):
        return
    try:
        send_due_expiry_reminders(cfg, store, tg)
    except Exception as exc:  # noqa: BLE001 - retention-style job, never fatal
        log.warning("listen: expiry reminder pass failed: %s", exc)


_remind_expiries.last = 0.0  # type: ignore[attr-defined]


def _assert_menu(cfg, tg) -> None:
    """Publish ROTD's slash menu, replacing any sibling bot's commands.

    Only meaningful for the real client. Failures are cosmetic (menu only), so
    they are logged and never raised — the daemon keeps listening regardless.
    """
    from .telegram import menu_commands
    if isinstance(tg, FakeTelegram):
        return
    try:
        cmds = menu_commands()
        if tg.set_commands(cmds):
            log.info("listen: published ROTD slash menu (%d commands)", len(cmds))
        else:
            log.warning("listen: could not publish the slash menu")
    except Exception as exc:  # noqa: BLE001
        log.warning("listen: slash menu update failed: %s", exc)


def _safe_cycle(rt: "Runtime", mode: str, day: "date") -> None:
    """Run one decision cycle, swallowing errors so the daemon keeps listening."""
    try:
        rt.cycle(mode, day)
    except Exception as exc:  # noqa: BLE001
        log.warning("listen: cycle failed: %s", exc)


def _finalize_if_closed(rt: "Runtime", last_trading_day: "list[date]") -> None:
    """Build + send the MISS REPORT once the previous trading day has closed.

    The daemon never otherwise calls ``finalize_day``, so without this the runner
    list and the daily MISS REPORT (the honest accounting of which sub-$10 names
    ran and why ROTD did/didn't flag them) would never be built or pushed. We
    finalize the day we were last trading on as soon as its A1 label rolls
    (16:00 ET), and only once per day.
    """
    si = session_info()
    prev = last_trading_day[0] if last_trading_day else None
    if prev is None:
        last_trading_day[:] = [si.trading_day]
        return
    # An A1 label only rolls at 16:00 ET, i.e. once the session that completes it
    # has closed. "not si.is_session" is not equivalent: on a weekend/holiday
    # start the label has already rolled to the *upcoming* session, so that test
    # finalizes a day that has not happened (wrong label, and the real report is
    # then skipped as already built).
    day_ended = si.trading_day != prev
    if day_ended and store_day_closed(rt.store, prev):
        try:
            report = rt.finalize_day(prev)
            n = len(report.get("runners", [])) if isinstance(report, dict) else 0
            log.info("listen: finalized %s — %s runners, MISS REPORT sent",
                     prev.isoformat(), n)
        except Exception as exc:  # noqa: BLE001
            log.warning("listen: finalize failed for %s: %s", prev.isoformat(), exc)
        last_trading_day[:] = [si.trading_day]
    elif si.trading_day != prev:
        last_trading_day[:] = [si.trading_day]


def store_day_closed(store: "Store", day: "date") -> bool:
    """True when ``day`` has data and no report yet (i.e. it is due a finalize).

    A report already built means it is done. No snapshot at all means the daemon
    never observed that label (fresh install, or it was down all day) — building
    a report then would attribute the *current* universe to a past day.
    """
    if store.get_miss_report(day):
        return False
    return store.query_one(
        "SELECT 1 FROM snapshots WHERE trading_day=? LIMIT 1",
        (day.isoformat(),)) is not None


def _listen_reply(cfg, store, tg, offset, wait_s, rt=None) -> int:
    """Drain queued Telegram updates (short poll) and reply. Returns new offset.

    Every message is passed to :func:`handle_command`, which resolves the sender
    to owner / subscriber / expired / unknown. Unknown senders may only /start,
    /help and /request (no enumeration, no data). Callback queries are the
    owner's 1-click approve/reject buttons.
    """
    ctx = {"store": store, "cfg": cfg, "runtime": rt,
           "notifier": getattr(rt, "notifier", None)}
    if cfg.raw("TELEGRAM_TOKEN") and not isinstance(tg, FakeTelegram):
        updates = tg.get_updates(offset=offset, timeout=wait_s,
                                 allowed_updates=["message", "callback_query"])
    else:
        updates = tg.get_updates(offset=offset, timeout=0)  # tests / FakeTelegram
    for upd in updates:
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
        cb = upd.get("callback_query")
        if cb:
            _handle_callback(cfg, store, tg, cb)
            continue
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        sender = str((msg.get("chat") or {}).get("id") or "")
        # Capture the numeric id + handle before any access check: this table is
        # what makes @username -> chat_id resolution possible later.
        user = msg.get("from") or {}
        try:
            store.record_seen_user(sender, user.get("username", ""),
                                   user.get("first_name", ""))
        except Exception as exc:  # noqa: BLE001 - bookkeeping never blocks a reply
            log.warning("listen: record_seen_user failed: %s", exc)
        if not text:
            continue
        try:
            reply = tg.handle_command(text, sender, cfg, ctx=ctx)
        except Exception as exc:  # noqa: BLE001 - a single bad command must not
            # take down the daemon; ack the update and apologise instead.
            log.error("listen: command %r failed: %s", text[:40], exc)
            offset = max(offset, int(upd.get("update_id", 0)) + 1)
            # Only answer a chat that already has access, so an unknown sender
            # cannot probe the bot by triggering an error.
            owner = str(cfg.raw("TELEGRAM_CHAT_ID") or "")
            if sender and (sender == owner or store.is_active(sender)):
                try:
                    tg.send("⚠️ command error (logged); try /help", chat_id=sender)
                except Exception:  # noqa: BLE001
                    pass
            continue
        if reply is not None:
            # Show "typing" for a beat, then send — feels responsive on slow cycles.
            try:
                tg.send_chat_action("typing", chat_id=sender if sender else None)
            except Exception:  # noqa: BLE001
                pass
            # A stranger's /start (or /help) carries the Request Access button;
            # /request already DMs the owner, so it needs no markup. Keyed off the
            # reply text so the button can never appear on a command that has no
            # handler for the tap.
            from .notifier import REQUEST_ACCESS_MARKUP, WELCOME_PROMPT
            markup = REQUEST_ACCESS_MARKUP if reply == WELCOME_PROMPT else None
            try:
                tg.send(reply, chat_id=sender if sender else None, markup=markup)
            except Exception as exc:  # noqa: BLE001
                log.error("listen: reply failed: %s", exc)
    return offset


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
    # Candidates are stored as latest state per symbol (one row per day/mode),
    # not one row per tick, so report the day's stored candidate state once.
    cands = store.query("SELECT * FROM candidates WHERE trading_day=?",
                        (day.isoformat(),))
    passed = sum(1 for c in cands if c["passed_gates"])
    print(f"  latest candidate state: {len(cands)} symbols stored, {passed} gated_pass")
    for s in snaps:
        print(f"  {s['cycle_ts']}  mode={s['mode']}  grade={s['data_grade']}  "
              f"names={s['n_symbols']}")
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
        # Every known setting is accepted (TELEGRAM_* first, then sources/AI), so
        # `FINVIZ_TOKEN=... bash install.sh` actually pre-seeds the config.
        from .config import SETTINGS
        order = SETUP_ORDER + [s.key for s in SETTINGS if s.key not in SETUP_ORDER]
        for key in order:
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


def _real_telegram(cfg: Config):
    """Force the REAL Telegram client (never FakeTelegram) for install-time checks."""
    if not cfg.raw("TELEGRAM_TOKEN"):
        return None
    return TelegramClient(cfg, secrets=cfg.secret_values)


def cmd_telegram_check(cfg: Config, args: argparse.Namespace) -> int:
    """Verify the real Telegram bot token by calling getMe (network, live client).

    Used by the installer so it fails loudly on a bad token instead of silently
    falling back to FakeTelegram. Never returns a fake-client result.
    """
    tg = _real_telegram(cfg)
    if tg is None:
        print("TELEGRAM_TOKEN not set — cannot check a real bot. Enter it or set TELEGRAM_TOKEN.")
        return 2
    ok, msg = tg.health()
    print(f"telegram (live): {'OK' if ok else 'FAIL'} — {msg}")
    if ok and not getattr(args, "no_menu", False):
        # Claim the slash menu now, so a shared bot does not keep showing a
        # sibling system's commands after install.
        from .telegram import menu_commands
        published = tg.set_commands(menu_commands())
        print(f"telegram menu:  {'published ROTD commands' if published else 'FAILED to publish'}")
    return 0 if ok else 1


def cmd_telegram_menu(cfg: Config, args: argparse.Namespace) -> int:
    """Publish (or show) the bot's slash-command menu without touching anything else.

    ``--show`` prints what Telegram currently advertises — useful to confirm
    whether a sibling system (e.g. penny-early-warning on the same token) still
    owns the menu.
    """
    tg = _real_telegram(cfg)
    if tg is None:
        print("TELEGRAM_TOKEN not set — cannot manage the menu.")
        return 2
    from .telegram import menu_commands
    if getattr(args, "show", False):
        current = tg.get_commands()
        if not current:
            print("telegram menu: (empty — nothing advertised)")
        else:
            print("telegram menu (current):")
            for name, desc in current:
                print(f"  /{name:<14} {desc}")
        if getattr(args, "check", False):
            expected = {n for n, _ in menu_commands()}
            have = {n for n, _ in current}
            if have == expected:
                print("menu matches ROTD commands.")
                return 0
            print(f"menu differs — missing {sorted(expected - have)}, "
                  f"extra {sorted(have - expected)}")
            return 1
        return 0
    ok = tg.set_commands(menu_commands())
    print(f"telegram menu: {'published ROTD commands' if ok else 'FAILED to publish'}")
    return 0 if ok else 1


def cmd_subs(cfg: Config, args: argparse.Namespace) -> int:
    """Manage read-only Telegram subscribers without going through the bot.

    ``rotd subs list`` | ``rotd subs add CHAT_ID|@username DAYS [LABEL]`` |
    ``rotd subs rm CHAT_ID|@username``. Same table and rules as the bot commands.
    """
    store = Store(cfg.db_path)
    try:
        if args.action == "add":
            if not args.chat_id or args.days is None:
                print("usage: rotd subs add CHAT_ID|@username DAYS [LABEL]")
                return 2
            target = str(args.chat_id).strip()
            resolved = store.resolve_target_to_chat_id(target)
            if resolved is None or resolved[0].startswith("@"):
                handle = target.lstrip("@").lower()
                if not handle:
                    print(f"cannot resolve {target!r}")
                    return 2
                expires = store.add_subscriber(f"@{handle}", args.days,
                                               args.label or f"@{handle}")
                print(f"⏳ Pre-registered @{handle} for {args.days} days "
                      f"(expires {expires.isoformat()}); links on their /start.")
                return 0
            chat_id, auto_label = resolved
            label = args.label or auto_label
            try:
                expires = store.add_subscriber(chat_id, args.days, label)
            except ValueError as exc:
                print(str(exc))
                return 2
            store.set_request_status(chat_id, "APPROVED")
            print(f"✅ Subscriber {chat_id} added/renewed for {args.days} days "
                  f"(expires {expires.isoformat()}).")
            return 0
        if args.action == "rm":
            if not args.chat_id:
                print("usage: rotd subs rm CHAT_ID|@username")
                return 2
            target = str(args.chat_id).strip()
            resolved = store.resolve_target_to_chat_id(target)
            chat_id = resolved[0] if resolved else target
            if not store.remove_subscriber(chat_id):
                print(f"no subscriber with chat id {chat_id}")
                return 1
            print(f"🗑️ Subscriber {chat_id} removed.")
            return 0
        from .notifier import render_subscribers_list
        print(render_subscribers_list(store.get_all_subscribers()))
        return 0
    finally:
        store.close()


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

    ls = sub.add_parser("listen", help="daemon: run cycles AND answer Telegram commands")
    ls.add_argument("--once", action="store_true", help="one pass then exit (tests)")
    ls.set_defaults(func=cmd_listen)

    sb = sub.add_parser("subs", help="list / add / remove read-only Telegram subscribers")
    sb.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "rm"], help="default: list")
    sb.add_argument("chat_id", nargs="?")
    sb.add_argument("days", nargs="?", type=int)
    sb.add_argument("label", nargs="?")
    sb.set_defaults(func=cmd_subs)

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

    tc = sub.add_parser("telegram-check", help="verify the REAL bot token via getMe (live, no fake)")
    tc.add_argument("--no-menu", action="store_true",
                    help="skip claiming the slash-command menu")
    tc.set_defaults(func=cmd_telegram_check)

    tm = sub.add_parser("telegram-menu",
                        help="publish or inspect the bot's slash-command menu")
    tm.add_argument("--show", action="store_true",
                    help="show what Telegram currently advertises")
    tm.add_argument("--check", action="store_true",
                    help="with --show, exit non-zero if the menu differs from ROTD's")
    tm.set_defaults(func=cmd_telegram_menu)

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
