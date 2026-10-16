"""Telegram delivery — owned fork.

ROTD builds against the :class:`FakeTelegram` test double in tests and shadow
mode (user decision). Live sends are wired but unused until a token is supplied.
Secrets are never placed in a message or a log line.

Commands are accepted only from the configured chat id (§14.2).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable, Optional

from .util import scrub, fmt_ts, now_utc
from .calendar_us import session_info

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 3900


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    """Real Telegram Bot API client; commands only from the configured chat."""

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        self.cfg = cfg
        self._secrets = list(secrets or [])
        self._session = None
        self._last_edit: dict[int, str] = {}

    @property
    def token(self) -> str:
        return str(self.cfg.raw("TELEGRAM_TOKEN") or "")

    @property
    def chat_id(self) -> str:
        return str(self.cfg.raw("TELEGRAM_CHAT_ID") or "")

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return API.format(token=self.token, method=method)

    def _call(self, method: str, payload: dict, timeout: float = 20.0) -> Any:
        if not self.token:
            raise TelegramError("telegram: TELEGRAM_TOKEN not set")
        import requests
        if self._session is None:
            self._session = requests.Session()
        for attempt in range(3):
            try:
                resp = self._session.post(self._url(method), json=payload, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    raise TelegramError(scrub(str(exc), self._secrets)) from exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After") or 3))
                continue
            try:
                data = resp.json()
            except ValueError as exc:
                raise TelegramError("telegram: invalid JSON response") from exc
            if not data.get("ok"):
                desc = scrub(str(data.get("description") or "unknown error"), self._secrets)
                raise TelegramError(f"telegram: {desc}")
            return data.get("result")
        raise TelegramError("telegram: send failed after retries")

    def send(self, text: str, chat_id: Optional[str] = None, disable_preview: bool = True,
             markup: Optional[dict] = None, *, silent: bool = False) -> Optional[int]:
        chat = chat_id or self.chat_id
        if not chat:
            raise TelegramError("telegram: TELEGRAM_CHAT_ID not set")
        msg_id = None
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat, "text": chunk,
                "disable_web_page_preview": disable_preview,
                "disable_notification": silent,
            }
            if markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = markup
            result = self._call("sendMessage", payload)
            if isinstance(result, dict):
                msg_id = result.get("message_id", msg_id)
        return msg_id

    def edit(self, msg_id: int, text: str, chat_id: Optional[str] = None,
             markup: Optional[dict] = None) -> bool:
        chat = chat_id or self.chat_id
        if not chat or not msg_id:
            return False
        body = "\n".join(split_message(text))
        if self._last_edit.get(msg_id) == body:
            return True
        payload: dict[str, Any] = {"chat_id": chat, "message_id": msg_id,
                                   "text": body[:4096], "disable_web_page_preview": True}
        if markup is not None:
            payload["reply_markup"] = markup
        try:
            self._call("editMessageText", payload)
            self._last_edit[msg_id] = body
            return True
        except TelegramError as exc:
            if "message is not modified" in str(exc).lower():
                return True
            log.warning("telegram edit failed: %s", exc)
            return False

    def answer_callback(self, callback_id: str, text: str = "",
                        show_alert: bool = False) -> bool:
        if not callback_id:
            return False
        try:
            self._call("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": text[:200] or None,
                        "show_alert": show_alert})
            return True
        except TelegramError:
            return False

    def send_chat_action(self, action: str = "typing",
                         chat_id: Optional[str] = None) -> bool:
        chat = chat_id or self.chat_id
        if not chat:
            return False
        try:
            self._call("sendChatAction", {"chat_id": chat, "action": action})
            return True
        except TelegramError:
            return False

    def set_commands(self, commands: Iterable[tuple[str, str]]) -> bool:
        try:
            payload = _menu_payload(commands)
            if not payload:
                return False
            self._call("setMyCommands", {"commands": payload})
            return True
        except TelegramError:
            return False

    def get_commands(self) -> list[tuple[str, str]]:
        """The menu Telegram currently advertises for this bot (or [])."""
        try:
            result = self._call("getMyCommands", {})
            return [(row.get("command", ""), row.get("description", ""))
                    for row in (result or [])]
        except Exception:  # noqa: BLE001
            return []

    def get_updates(self, offset: int = 0, timeout: int = 25,
                    allowed_updates: Optional[list[str]] = None) -> list[dict]:
        if not self.token:
            return []
        try:
            result = self._call("getUpdates",
                                {"offset": offset, "timeout": timeout,
                                 "allowed_updates": allowed_updates or ["message", "callback_query"]},
                                timeout=timeout + 10)
        except Exception:  # noqa: BLE001 - any Telegram/network error must not crash
            # the listener loop; return no updates and let it retry next tick.
            return []
        return result if isinstance(result, list) else []

    def health(self) -> tuple[bool, str]:
        if not self.token:
            return False, "TELEGRAM_TOKEN not set (FakeTelegram in use)"
        try:
            result = self._call("getMe", {})
            username = (result or {}).get("username", "?")
            return True, f"bot @{username} reachable"
        except Exception as exc:  # noqa: BLE001
            return False, scrub(str(exc), self._secrets)

    def detect_chat_id(self, attempts: int = 30, timeout: int = 2) -> Optional[str]:
        """Installer helper: read the chat id once the user messages the bot.

        Polls ``getUpdates`` for up to ``attempts`` rounds; returns the first
        chat id seen, or ``None`` if nothing arrives. Mirrors the penny installer
        flow so ``TELEGRAM_CHAT_ID`` can be auto-filled without manual entry.
        """
        if not self.token:
            return None
        offset = 0
        for _ in range(attempts):
            for upd in self.get_updates(offset=offset, timeout=timeout):
                offset = max(offset, int(upd.get("update_id", 0)) + 1)
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = (msg.get("chat") or {}).get("id")
                if chat is not None:
                    return str(chat)
        return None

    def handle_command(self, text: str, sender_chat: str, cfg: "Config",
                       ctx: Optional[dict] = None) -> Optional[str]:
        """See :func:`handle_command` at module level; bound here for convenience."""
        return handle_command(self, text, sender_chat, cfg, ctx)


# Keys a user may set from Telegram after install (everything else stays at its
# safe default in config.env). /setkey is the installer's "add keys later" path.
SETTABLE_KEYS = (
    "FINVIZ_TOKEN", "TWELVEDATA_KEY", "FINNHUB_KEY", "ALPHA_VANTAGE_KEY",
    "ALPACA_KEY", "ALPACA_SECRET", "SEC_USER_AGENT",
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
)

# Every command the bot understands. Owner-chat only. `setkey`/`start`/`help`
# work without a DB; the data commands take an optional context with the store.
COMMANDS = (
    ("/start", "wake the bot and show the command menu"),
    ("/help", "list every command"),
    ("/setkey KEY VALUE", "set an API key after install (secrets saved, not echoed)"),
    ("/setbudget N", "set the daily IGNITION alert budget (1..10, applied immediately)"),
    ("/keys", "show which API keys are set and which are missing"),
    ("/status", "now, session mode, shadow clock and alert budget used"),
    ("/board", "the latest ranked board (top candidates)"),
    ("/runners", "today's runner list (big movers)"),
    ("/miss", "today's MISS REPORT"),
    ("/alerts", "today's ignition alerts"),
    ("/shadow", "the 10-trading-day shadow clock"),
    ("/log", "recent event log"),
    ("/doctor", "live checker: config, sources, calendar, clock"),
)


def _usage() -> str:
    return ("ROTD commands:\n"
            + "\n".join(f"  {c[0]:<20} {c[1]}" for c in COMMANDS))


def menu_commands() -> list[tuple[str, str]]:
    """The slash menu Telegram should show, sanitized to Bot API rules.

    Telegram requires a single-word, lowercase command name (<=32 chars, no
    spaces) and a description <=256 chars. One invalid entry makes the whole
    ``setMyCommands`` call fail silently, which is exactly how a sibling bot
    sharing the same token can leave its own menu in place. Entries like
    ``/setkey KEY VALUE`` are therefore published as ``setkey`` with the usage
    folded into the description.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_name, raw_desc in COMMANDS:
        parts = raw_name.lstrip("/").split()
        name = parts[0].split("@")[0].lower() if parts else ""
        name = re.sub(r"[^a-z0-9_]", "", name)[:32]
        if not name or name in seen:
            continue
        seen.add(name)
        extra = " ".join(parts[1:]).strip()
        # Fold a usage argument (e.g. "KEY VALUE") into the description, once.
        desc = raw_desc
        if extra and extra.lower() not in desc.lower():
            desc = f"{desc} — {extra}"
        out.append((name, re.sub(r"\s+", " ", desc).strip()[:256] or "ROTD command"))
    return out


def _menu_payload(commands) -> list[dict]:
    """Sanitize (name, description) pairs into Telegram ``setMyCommands`` rows."""
    payload = []
    for raw_name, raw_desc in list(commands)[:100]:
        parts = str(raw_name).lstrip("/").split()
        name = parts[0].split("@")[0].lower() if parts else ""
        name = re.sub(r"[^a-z0-9_]", "", name)[:32]
        if not name:
            continue
        desc = re.sub(r"\s+", " ", str(raw_desc)).strip()[:256] or "ROTD command"
        payload.append({"command": name, "description": desc})
    return payload


def handle_command(self, text: str, sender_chat: str, cfg: "Config",
                   ctx: Optional[dict] = None) -> Optional[str]:
    """Process one inbound Telegram command from the configured owner chat.

    Returns a human-readable reply string, or ``None`` if the message was not a
    command for this bot. Commands are accepted only from ``TELEGRAM_CHAT_ID``
    (the installer writes it); other chats are ignored silently. ``ctx`` is an
    optional dict ``{"store": Store, "cfg": Config}`` that lets data commands read
    live state. ``setkey``/``start``/``help`` work without it.
    """
    if not text or not text.startswith("/"):
        return None
    if self.chat_id and str(sender_chat) != self.chat_id:
        # Not the owner chat: ignore silently (don't leak a "denied" message).
        return None
    parts = text[1:].split()
    name = parts[0].lower().split("@")[0]
    arg = " ".join(parts[1:]).strip()

    if name in ("start", "help"):
        # Self-heal the slash menu: the bot token is shared with a sibling system,
        # so whichever process last called setMyCommands owns the menu. Re-assert
        # ROTD's menu on every /start so penny's entries lose the fight for good.
        try:
            self.set_commands(menu_commands())
        except Exception:  # noqa: BLE001 - menu is cosmetic; never block the reply
            pass
        return ("ROTD — Runner of the Day. Watching the sub-$10 universe.\n"
                + _usage()
                + "\n\nSet a key any time, e.g. /setkey FINVIZ_TOKEN <token>")

    if name == "setkey":
        if len(parts) < 3:
            return ("usage: /setkey KEY VALUE  — settable keys: "
                    + ", ".join(SETTABLE_KEYS))
        key, value = parts[1].upper(), " ".join(parts[2:])
        if key not in SETTABLE_KEYS:
            return f"refused: {key} is not a settable key"
        try:
            cfg.append_to_file(key, value)
            from .config import restrict_permissions
            try:
                restrict_permissions(cfg.path)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            return f"error saving {key}: {scrub(str(exc), self._secrets)}"
        return f"ok: {key} updated (applied on next cycle)"

    if name == "setbudget":
        if len(parts) < 2:
            return f"usage: /setbudget N  — daily IGNITION budget {cfg.int('ALERT_BUDGET_MIN', 1)}..{cfg.int('ALERT_BUDGET_MAX', 10)}"
        try:
            n = int(parts[1])
        except ValueError:
            return f"invalid budget: {parts[1]!r}"
        lo = cfg.int("ALERT_BUDGET_MIN", 1)
        hi = cfg.int("ALERT_BUDGET_MAX", 10)
        if not (lo <= n <= hi):
            return f"budget must be {lo}..{hi}"
        try:
            cfg.append_to_file("ALERT_BUDGET_PER_DAY", str(n))
        except Exception as exc:  # noqa: BLE001
            return f"error saving budget: {scrub(str(exc), self._secrets)}"
        # Apply to the live per-day budget immediately (no restart). The next
        # day's StateMachine picks the persisted value up from config anyway.
        rt = (ctx or {}).get("runtime")
        if rt is not None and getattr(rt, "sm", None) is not None:
            rt.sm.budget.limit = n
        return f"ok: daily alert budget set to {n} (applied immediately)"

    if name == "keys":
        lines = ["ROTD API keys:"]
        for k in SETTABLE_KEYS:
            v = cfg.raw(k)
            lines.append(f"  {k:<18} {'set' if v else 'missing'}")
        return "\n".join(lines)

    if name == "status":
        from .shadow import status as shadow_status
        si = session_info()
        st = shadow_status(ctx["store"], cfg=cfg) if ctx else shadow_status(None, cfg=cfg)
        budget = cfg.int("ALERT_BUDGET_PER_DAY", 3)
        used = 0
        if ctx and ctx.get("store"):
            used = ctx["store"].count_alerts(si.trading_day, "IGNITION")
        blk = " (BLOCKED: shadow incomplete)" if not st.live_allowed else ""
        return (f"ROTD status  {fmt_ts(now_utc())}\n"
                f"  mode={si.mode}  trading_day={si.trading_day}  half={si.is_half}\n"
                f"  {st.line()}\n"
                f"  ignition budget: {used}/{budget}{blk}\n"
                f"  shadow mode: {'on' if cfg.bool('SHADOW') else 'off'}")

    if name == "shadow":
        from .shadow import status as shadow_status
        st = shadow_status(None, cfg=cfg) if not ctx else \
            shadow_status(ctx["store"], cfg=cfg)
        return f"Shadow clock\n  {st.line()}"

    if name == "board":
        if not ctx or not ctx.get("store"):
            return "no data yet (run `rotd listen` first)."
        from datetime import date as _d
        day = session_info().trading_day
        snap = ctx["store"].latest_snapshot(day)
        if not snap:
            return f"no snapshot for {day} yet."
        from .notifier import render_board, _when
        # Rebuild a tiny board from the latest candidates that passed gates.
        rows = ctx["store"].query(
            "SELECT symbol, score, rank FROM candidates WHERE trading_day=? "
            "ORDER BY cycle_ts DESC, rank ASC LIMIT ?",
            (day.isoformat(), cfg.int("BOARD_SIZE", 5)))
        scored = []
        for r in rows:
            if r["score"] is None:
                continue
            class _S:  # minimal stand-in for Scored used by render_board
                def __init__(s, sym, sc, rk):
                    s.symbol = sym; s.score = float(sc); s.rank = int(rk) if rk else None
            scored.append(_S(r["symbol"], r["score"], r["rank"]))
        if not scored:
            return (f"📋 ROTD BOARD — none\n{_when(None)}  |  "
                    f"mode {session_info().mode}  |  data {snap['data_grade']}\n"
                    f"(no qualified candidates this cycle)")
        return render_board(scored, session_info().mode, snap["data_grade"], {}, now_utc())

    if name == "runners":
        if not ctx or not ctx.get("store"):
            return "no data yet (run `rotd listen` first)."
        from datetime import date as _d
        day = session_info().trading_day
        rows = ctx["store"].runners_for_day(day)
        if not rows:
            return f"no runners logged for {day} yet."
        lines = [f"🏁 ROTD runners — {day}", ""]
        for r in rows[:15]:
            lines.append(f"  ${r['symbol']:<6} +{r['gain_pct']:.0f}%  "
                         f"dv ${r['dollar_vol']/1e6:.1f}M  illiq={'Y' if r['illiquid'] else 'N'}")
        return "\n".join(lines)

    if name == "miss":
        if not ctx or not ctx.get("store"):
            return "no data yet (run `rotd listen` first)."
        from datetime import date as _d
        day = session_info().trading_day
        report = ctx["store"].get_miss_report(day)
        if not report:
            return f"no MISS REPORT for {day} yet."
        from .notifier import render_miss_report
        return render_miss_report(report)

    if name == "alerts":
        if not ctx or not ctx.get("store"):
            return "no data yet (run `rotd listen` first)."
        from datetime import date as _d
        day = session_info().trading_day
        rows = ctx["store"].alerts_for_day(day)
        if not rows:
            return f"no alerts for {day} yet."
        lines = [f"🔔 ROTD alerts — {day}", ""]
        for a in rows:
            lines.append(f"  ${a['symbol']:<6} {a['kind']:<8} score {a['score']} "
                         f"rank {a['rank']} slot {a['budget_slot']}")
        return "\n".join(lines)

    if name == "log":
        if not ctx or not ctx.get("store"):
            return "no data yet (run `rotd listen` first)."
        rows = ctx["store"].recent_events(10)
        if not rows:
            return "no events yet."
        lines = ["ROTD recent events:"]
        for e in rows:
            lines.append(f"  {e['ts'][:19]} {e['level']:<5} {e['source']}: "
                         f"{str(e['message'])[:80]}")
        return "\n".join(lines)

    if name == "doctor":
        return ("run `rotd doctor` for the full live check, or watch the service "
                "logs with `journalctl -u rotd -f`. Quick status: "
                + ("shadow ON" if cfg.bool("SHADOW") else "shadow OFF"))

    # Unknown command from the owner chat: nudge toward /help.
    return f"unknown command: /{name}\n" + _usage()


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split long messages on line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class FakeTelegram:
    """Records messages for offline simulation and tests."""

    def __init__(self, cfg=None):
        self.cfg = cfg
        self.messages: list[dict] = []
        self.edits: list[dict] = []
        self.sent: list[dict] = []
        self.commands: list[str] = []
        self.callbacks: list[dict] = []
        self.updates: list[dict] = []
        self.actions: list[str] = []
        self.menu: list[tuple[str, str]] = []
        self._next_id = 1000

    def send(self, text: str, chat_id: Optional[str] = None, disable_preview: bool = True,
             markup: Optional[dict] = None, *, silent: bool = False) -> int:
        self._next_id += 1
        msg = {"id": self._next_id, "text": text, "markup": markup, "silent": silent}
        self.messages.append(msg)
        self.sent.append(msg)
        return self._next_id

    def edit(self, msg_id: int, text: str, chat_id: Optional[str] = None,
             markup: Optional[dict] = None) -> bool:
        self.edits.append({"id": msg_id, "text": text, "markup": markup})
        for m in self.messages:
            if m["id"] == msg_id:
                m["text"] = text
                m["markup"] = markup
        return True

    def delete(self, msg_id: int, chat_id: Optional[str] = None) -> bool:
        self.messages = [m for m in self.messages if m["id"] != msg_id]
        return True

    def answer_callback(self, callback_id: str, text: str = "",
                        show_alert: bool = False) -> bool:
        self.callbacks.append({"id": callback_id, "text": text, "alert": show_alert})
        return True

    def send_chat_action(self, action: str = "typing",
                         chat_id: Optional[str] = None) -> bool:
        self.actions.append(action)
        return True

    def set_commands(self, commands) -> bool:
        self.menu = list(commands)
        return True

    def get_commands(self) -> list[tuple[str, str]]:
        return list(self.menu)

    def get_updates(self, offset: int = 0, timeout: int = 25,
                    allowed_updates: Optional[list[str]] = None) -> list[dict]:
        pending = [u for u in self.updates if int(u.get("update_id", 0)) >= offset]
        self.updates = [u for u in self.updates if int(u.get("update_id", 0)) < offset]
        return pending

    def health(self) -> tuple[bool, str]:
        return True, "fake telegram (no network)"

    @property
    def chat_id(self) -> str:
        return str(self.cfg.raw("TELEGRAM_CHAT_ID") if self.cfg else "")

    def handle_command(self, text: str, sender_chat: str, cfg: "Config",
                       ctx: Optional[dict] = None) -> Optional[str]:
        # Record the command so tests can assert on it; no network side effects.
        self.commands.append(text)
        return handle_command(self, text, sender_chat, cfg, ctx)

    def detect_chat_id(self, attempts: int = 30, timeout: int = 2) -> Optional[str]:
        # No network: return whatever was configured, else None.
        return str(self.cfg.raw("TELEGRAM_CHAT_ID") if self.cfg else "") or None

    def text_of(self, msg_id: int) -> str:
        for m in self.messages:
            if m["id"] == msg_id:
                return m["text"]
        return ""

    def last_text(self) -> str:
        return self.messages[-1]["text"] if self.messages else ""
