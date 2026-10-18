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
from .notifier import (WELCOME_PROMPT, render_access_request,
                       render_subscriber_welcome, render_subscribers_list,
                       request_markup)

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
    "FINVIZ_TOKEN", "FINNHUB_KEY", "ALPHA_VANTAGE_KEY",
    "ALPACA_KEY", "ALPACA_SECRET", "AI_KEY", "AI_BASE_URL", "AI_MODEL",
)

# Every command the bot understands. Owner-only unless in READONLY_COMMANDS.
COMMANDS = (
    ("/start", "wake the bot and show the command menu"),
    ("/help", "list every command"),
    ("/request", "request access from the administrator"),
    ("/setkey KEY VALUE", "set an API key after install (secrets saved, not echoed)"),
    ("/setbudget N", "set the daily IGNITION alert budget (1..10, applied immediately)"),
    ("/resetbudget", "clear today's spent IGNITION slots (budget back to 0/N)"),
    ("/keys", "show which API keys are set and which are missing"),
    ("/addsub CHAT_ID DAYS [LABEL]", "grant a read-only subscription (renews if known)"),
    ("/rmsub CHAT_ID", "revoke a subscription immediately"),
    ("/checksubs", "list every subscriber with status and days left"),
    ("/subs", "alias of /checksubs"),
    ("/broadcast MESSAGE", "send an announcement to the owner and all subscribers"),
    ("/shadow-start [YYYY-MM-DD]", "start the 10-trading-day shadow clock"),
    ("/setup", "how to re-run the installer (host shell only)"),
    ("/status", "now, session mode, shadow clock and alert budget used"),
    ("/board", "the latest ranked board (top candidates)"),
    ("/runners", "today's runner list (big movers)"),
    ("/miss", "today's MISS REPORT"),
    ("/alerts", "today's ignition alerts"),
    ("/shadow", "the 10-trading-day shadow clock"),
    ("/log", "recent event log"),
    ("/doctor", "live checker: config, sources, calendar, clock"),
)

# Two-tier access. Anything not listed here is admin-only, so a new command is
# locked to the owner by default (fail closed).
READONLY_COMMANDS = frozenset({"status", "board", "runners", "miss", "alerts"})
PUBLIC_COMMANDS = frozenset({"start", "help", "request"})
EXPIRED_REPLY = "⚠️ Your subscription expired on {date}. Contact admin to renew."

REQUEST_SENT_REPLY = ("✅ Your access request has been sent to the administrator! "
                      "You will receive your welcome guide here as soon as it is approved.")
REQUEST_PENDING_REPLY = "⏳ Your access request is already pending admin approval."


def _usage(role: str = "admin") -> str:
    if role == "admin":
        rows = COMMANDS
    else:
        allowed = READONLY_COMMANDS | PUBLIC_COMMANDS
        rows = tuple(c for c in COMMANDS
                     if c[0].lstrip("/").split()[0] in allowed)
    return ("ROTD commands:\n"
            + "\n".join(f"  {c[0]:<20} {c[1]}" for c in rows))


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


def used_today(ctx: Optional[dict], cfg: "Config") -> int:
    """IGNITION alerts already spent today (live state first, DB as fallback)."""
    rt = (ctx or {}).get("runtime")
    sm = getattr(rt, "sm", None) if rt is not None else None
    if sm is not None:
        return sm.budget.used
    store = (ctx or {}).get("store")
    if store is not None:
        try:
            return store.count_alerts(session_info().trading_day, "IGNITION")
        except Exception:  # noqa: BLE001 - a reply must never fail on a DB hiccup
            return 0
    return 0


def _sender_role(self, sender_chat: str, cfg: "Config",
                 ctx: Optional[dict]) -> tuple[str, Optional[str]]:
    """Resolve a sender to ``(role, expired_on)``.

    ``owner``      — ``TELEGRAM_CHAT_ID``: every command.
    ``subscriber`` — an active row in ``subscribers``: read-only commands.
    ``expired``    — a known chat whose subscription lapsed.
    ``unknown``    — anything else: no reply at all (fail closed).
    """
    owner = str(cfg.raw("TELEGRAM_CHAT_ID") or self.chat_id or "").strip()
    sender = str(sender_chat).strip()
    if not sender:
        return "unknown", None
    if owner and sender == owner:
        return "owner", None
    store = (ctx or {}).get("store")
    if store is not None:
        try:
            if store.is_active(sender):
                return "subscriber", None
            expiry = store.subscriber_expiry(sender)
            if expiry:
                return "expired", expiry
        except Exception as exc:  # noqa: BLE001 - a DB hiccup must deny, not allow
            log.warning("subscriber lookup failed: %s", exc)
    # No owner configured yet (fresh install): allow the wake-up commands only,
    # so the first person to message the bot can read its chat id and finish setup.
    if not owner:
        return "bootstrap", None
    return "unknown", None


def _broadcast(self, text: str, cfg: "Config", ctx: Optional[dict]) -> str:
    """Owner announcement to the owner + every active subscriber."""
    rest = text[1:].split(None, 1)
    body = rest[1].strip() if len(rest) > 1 else ""
    if not body:
        return "⚠️ Usage: /broadcast <your announcement message>"
    notifier = (ctx or {}).get("notifier")
    if notifier is None:
        from .notifier import Notifier
        notifier = Notifier(cfg, self, (ctx or {}).get("store"))
    try:
        success, fail = notifier.send_custom_broadcast(body)
    except ValueError as exc:
        return f"⚠️ {exc}"
    return f"📢 Broadcast sent! Delivered to {success} chat(s) ({fail} failed)."


def handle_command(self, text: str, sender_chat: str, cfg: "Config",
                   ctx: Optional[dict] = None) -> Optional[str]:
    """Process one inbound Telegram command.

    Returns a human-readable reply string, or ``None`` if the message was not a
    command for this bot. Access is two-tier: ``TELEGRAM_CHAT_ID`` is the owner
    (every command); active rows in ``subscribers`` get the read-only set. An
    expired subscriber is told once, by date. Any other chat is ignored silently.
    ``ctx`` is an optional dict ``{"store": Store, "cfg": Config}`` that lets data
    commands read live state.
    """
    if not text or not text.startswith("/"):
        return None
    parts = text[1:].split()
    name = parts[0].lower().split("@")[0]
    role, expired_on = _sender_role(self, sender_chat, cfg, ctx)
    if role == "unknown":
        # A stranger may only ask for access or say hello; everything else stays
        # silent so the bot cannot be enumerated.
        if name not in PUBLIC_COMMANDS:
            return None
    if name == "broadcast":
        # Handled before the generic gate so a subscriber gets the explicit
        # refusal the brief asks for; everyone else is ignored silently.
        if role == "owner":
            return _broadcast(self, text, cfg, ctx)
        if role == "subscriber":
            return "⛔ Admin command only."
        return None
    if role == "bootstrap" and name not in PUBLIC_COMMANDS:
        return None
    if role in ("subscriber", "expired") and name not in (READONLY_COMMANDS
                                                          | PUBLIC_COMMANDS):
        # Admin commands are owner-only. Don't hint at what exists.
        return "Unauthorized."
    if role == "expired":
        return EXPIRED_REPLY.format(date=expired_on)

    if name in ("start", "help"):
        # Self-heal the slash menu: the bot token is shared with a sibling system,
        # so whichever process last called setMyCommands owns the menu. Re-assert
        # ROTD's menu on every /start so penny's entries lose the fight for good.
        try:
            self.set_commands(menu_commands())
        except Exception:  # noqa: BLE001 - menu is cosmetic; never block the reply
            pass
        # A pre-registered @username just pressed /start: bind the placeholder row
        # to the real chat id. For a stranger that is also their activation; for a
        # subscriber it just clears the duplicate pending row.
        store = (ctx or {}).get("store")
        linked = None
        if name == "start" and store is not None:
            seen = store.get_seen_user(sender_chat) or {}
            linked = store.link_pending_username(seen.get("username", ""), sender_chat)
            if linked is not None and role == "unknown":
                store.set_request_status(sender_chat, "APPROVED")
                if linked < now_utc().date():
                    return EXPIRED_REPLY.format(date=linked.isoformat())
                days = max(0, (linked - now_utc().date()).days)
                return render_subscriber_welcome(days, linked.isoformat())
        if role == "bootstrap":
            return ("ROTD — Runner of the Day.\n"
                    f"Your chat id is {sender_chat}. Add it as TELEGRAM_CHAT_ID "
                    "(or run `rotd setup`) to finish setup.")
        if role == "subscriber":
            from datetime import date as _d
            sub = ((ctx or {}).get("store") or None)
            row = sub.get_subscriber(sender_chat) if sub is not None else None
            if row is None:
                return "ROTD — Runner of the Day. You have read-only access.\n" \
                       + _usage("subscriber")
            expires = str(row["expires_at"])
            try:
                days_left = max(0, (_d.fromisoformat(expires) - now_utc().date()).days)
            except ValueError:
                days_left = 0
            return render_subscriber_welcome(days_left, expires)
        if role == "unknown":
            # Unregistered visitor (also /help): offer the Request Access button
            # and never the admin command list.
            return WELCOME_PROMPT
        return ("ROTD — Runner of the Day. Watching the sub-$10 universe.\n"
                + _usage()
                + "\n\nSet a key any time, e.g. /setkey FINVIZ_TOKEN <token>")

    if name == "request":
        store = (ctx or {}).get("store")
        if store is None:
            return "requests need the database; start the listener first."
        if role == "owner":
            return "You are the owner; no access request needed."
        if role == "subscriber":
            return "✅ You already have access. Type /help for the playbook."
        owner = str(cfg.raw("TELEGRAM_CHAT_ID") or self.chat_id or "").strip()
        if not owner:
            return "No administrator is configured yet; run `rotd setup` first."
        status = store.get_request_status(sender_chat)
        if status == "PENDING":
            return REQUEST_PENDING_REPLY
        if status == "REJECTED":
            return None  # fail closed: a rejected chat gets no further response
        seen = store.get_seen_user(sender_chat) or {}
        store.set_request_status(sender_chat, "PENDING")
        # Notify the owner directly. This is a user-initiated admin action, so it
        # is sent even in SHADOW (same rule as /broadcast: commands act).
        text = render_access_request(sender_chat, seen.get("username", ""),
                                     seen.get("first_name", ""))
        try:
            self.send(text, chat_id=owner, markup=request_markup(sender_chat))
        except Exception as exc:  # noqa: BLE001 - the user still gets a reply
            log.warning("request: owner notify failed: %s", exc)
        return REQUEST_SENT_REPLY

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

    if name == "addsub":
        if len(parts) < 3:
            return "usage: /addsub CHAT_ID|@username DAYS [LABEL]"
        store = (ctx or {}).get("store")
        if store is None:
            return "subscribers need the database; start the listener first."
        try:
            days = int(parts[2])
        except ValueError:
            return f"invalid days: {parts[2]!r}"
        target = parts[1]
        label = " ".join(parts[3:]).strip()
        resolved = store.resolve_target_to_chat_id(target)
        if resolved is not None and not resolved[0].startswith("@"):
            chat_id, auto_label = resolved
        else:
            # An @username who has never opened the bot (or an existing pending
            # row): Telegram will not let us message them, so park/refresh a
            # placeholder row and link it on their /start.
            handle = target.lstrip("@").lower()
            if not handle:
                return f"cannot resolve {target!r}"
            placeholder = f"@{handle}"
            try:
                expires = store.add_subscriber(placeholder, days, label or placeholder)
            except ValueError as exc:
                return str(exc)
            return (f"⏳ Pre-registered {target} for {days} days "
                    f"(expires {expires.isoformat()}). As soon as they open the bot "
                    "and press /start, their numeric ID will link automatically and "
                    "the welcome playbook will be delivered.")
        final_label = label or auto_label
        try:
            expires = store.add_subscriber(chat_id, days, final_label)
        except ValueError as exc:
            return str(exc)
        store.set_request_status(chat_id, "APPROVED")
        head = (f"✅ Subscriber {chat_id} ({final_label or 'no label'}) activated for "
                f"{days} days (expires {expires.isoformat()}).")
        # Push the playbook immediately so the new subscriber knows the rules
        # before the first IGNITION lands. A chat that has never messaged the bot
        # cannot be opened by the bot; that is a "retry on /start" case, not a
        # failure of the subscription itself.
        from .notifier import Notifier
        notifier = (ctx or {}).get("notifier")
        if notifier is None:
            notifier = Notifier(cfg, self, store)
        try:
            notifier._send_raw(chat_id, render_subscriber_welcome(days, expires.isoformat()))
        except Exception as exc:  # noqa: BLE001 - subscription already saved
            log.warning("addsub: welcome to %s failed: %s", chat_id, exc)
            return (head + " Note: Welcome playbook will be delivered once the "
                    "user sends /start to the bot.")
        return head + " Welcome playbook delivered."

    if name == "rmsub":
        if len(parts) < 2:
            return "usage: /rmsub CHAT_ID|@username"
        store = (ctx or {}).get("store")
        if store is None:
            return "subscribers need the database; start the listener first."
        target = parts[1]
        resolved = store.resolve_target_to_chat_id(target)
        chat_id = resolved[0] if resolved else target
        if not store.remove_subscriber(chat_id):
            return f"no subscriber with chat id {chat_id}"
        return f"🗑️ Subscriber {chat_id} removed."

    if name in ("subs", "checksubs"):
        store = (ctx or {}).get("store")
        if store is None:
            return "subscribers need the database; start the listener first."
        return render_subscribers_list(store.get_all_subscribers())

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
        if rt is not None:
            if getattr(rt, "sm", None) is not None:
                rt.sm.budget.relabel(n, rt.sm.budget.reserve)
            if getattr(rt, "cfg", None) is not None:
                rt.cfg.set("ALERT_BUDGET_PER_DAY", str(n))
        return f"Budget updated: {used_today(ctx, cfg)}/{n} used today"

    if name == "resetbudget":
        rt = (ctx or {}).get("runtime")
        sm = getattr(rt, "sm", None) if rt is not None else None
        day = sm.budget.day if sm is not None else session_info().trading_day
        store = (ctx or {}).get("store")
        if store is not None:
            store.reset_budget(day, "IGNITION")
        if rt is not None and getattr(rt, "sm", None) is not None:
            rt.sm.budget.reset()
        limit = (rt.sm.budget.limit if rt is not None and getattr(rt, "sm", None)
                 else cfg.int("ALERT_BUDGET_PER_DAY", 5))
        return f"Budget updated: 0/{limit} used today"

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
        budget = cfg.int("ALERT_BUDGET_PER_DAY", 5)
        used = used_today(ctx, cfg)
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

    if name in ("shadow-start", "shadowstart"):
        store = (ctx or {}).get("store")
        if store is None:
            return "the shadow clock needs the database; start the listener first."
        from . import shadow as _shadow
        from datetime import date as _d
        tokens = [p for p in parts[1:] if p != "--force"]
        force = "--force" in parts[1:]
        try:
            day = _d.fromisoformat(tokens[0]) if tokens else session_info().trading_day
        except ValueError:
            return f"bad date: {tokens[0]!r} (want YYYY-MM-DD)"
        try:
            st = _shadow.start(store, day, force=force)
        except ValueError as exc:
            return str(exc)
        store.set_meta("shadow_enabled", "1")
        return f"shadow clock started on {day}\n  {st.line()}"

    if name == "setup":
        return ("`/setup` runs on the host, not in Telegram: "
                "`cd /opt/rotd && ./install.sh` (or `rotd setup`). "
                "Use /setkey to change a key from here.")

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

    # Unknown command from the owner chat: nudge toward /help. Subscribers never
    # reach here (anything outside the read-only set is refused above).
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
        self._by_id: dict[int, dict] = {}

    def send(self, text: str, chat_id: Optional[str] = None, disable_preview: bool = True,
             markup: Optional[dict] = None, *, silent: bool = False) -> int:
        self._next_id += 1
        msg = {"id": self._next_id, "text": text, "markup": markup, "silent": silent,
               "chat": str(chat_id or self.chat_id or "")}
        self.messages.append(msg)
        self.sent.append(msg)
        self._by_id[msg["id"]] = msg
        return self._next_id

    def edit(self, msg_id: int, text: str, chat_id: Optional[str] = None,
             markup: Optional[dict] = None) -> bool:
        self.edits.append({"id": msg_id, "text": text, "markup": markup})
        target = self._by_id.get(msg_id)
        if target is None:
            # Buttons edit a message the bot sent before this process started,
            # so it is not in ``messages``; keep it addressable for text_of().
            target = {"id": msg_id, "text": text, "markup": markup, "chat": str(chat_id or "")}
            self._by_id[msg_id] = target
        target["text"] = text
        target["markup"] = markup
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
        if msg_id in self._by_id:
            return self._by_id[msg_id]["text"]
        for m in self.messages:
            if m["id"] == msg_id:
                return m["text"]
        return ""

    def last_text(self) -> str:
        return self.messages[-1]["text"] if self.messages else ""
